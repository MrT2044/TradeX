"""Backtest-Lauf (Spec §19).

Der Backtest benutzt EXAKT denselben Analysepfad wie Replay und spaeter der
Live-Betrieb: `MarketContext.on_base_bar()` und `StrategyEngine.on_updates()`.
Es gibt hier keine zweite, "fuer den Backtest optimierte" Regelauslegung -
genau das macht Spec §29 ("Backtest ≡ Live") ueberpruefbar statt vereinbart.
Ein Test stellt beide Wege gegenueber und verlangt identische Entscheidungen.

Reihenfolge innerhalb einer Bar
-------------------------------
    1. Offene Positionen gegen DIESE Bar aufloesen (erst fuellen, dann pruefen)
    2. Ergebnisse ins Risikobuch schreiben
    3. Die Bar analysieren -> am Bar-SCHLUSS entsteht evtl. ein neues Signal
    4. Das Signal wird zur Order fuer die NAECHSTE Bar

Diese Reihenfolge ist der Grund, warum kein Look-ahead moeglich ist: eine Order
kann fruehestens auf der Bar nach ihrem Signal gefuellt werden, und ein Ausstieg
wird immer auf der Bar bewertet, in der er physisch stattfand.

Warum die Position schon beim Signal ins Risikobuch kommt
---------------------------------------------------------
Zwischen Signal und Fuellung liegt eine Bar. Wuerde erst bei der Fuellung
gebucht, koennte in diesem Fenster ein zweites Signal dieselbe freie Stelle
belegen und `max_open_positions` waere wirkungslos. Wird die Order nie gefuellt
(weil die Daten enden), wird die Buchung zurueckgenommen.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from tradex.analysis.context import MarketContext
from tradex.backtest.execution import OpenTrade, SimulatedTrade
from tradex.config import Config
from tradex.domain.bars import Bar, BarSeries
from tradex.domain.enums import ExitReason
from tradex.domain.instruments import Instrument
from tradex.logging_setup import get_logger
from tradex.risk.ledger import OpenPosition, RiskLedger
from tradex.strategy.engine import StrategyEngine
from tradex.strategy.signal import StrategyDecision, TradeSignal

log = get_logger(__name__)

#: Kennung der Ausfuehrungsannahmen. Aendert sich die Simulation, aendert sich
#: diese Kennung - sonst waeren zwei gespeicherte Laeufe nicht vergleichbar.
BACKTEST_VERSION = "phase4-backtest-v1"

ProgressFn = Callable[[int, int], None]


@dataclass(slots=True)
class BacktestResult:
    """Rohergebnis eines Laufs. Kennzahlen entstehen daraus in `metrics`/`report`."""

    symbol: str
    instrument: Instrument
    base_timeframe: str
    bars: int
    first_ts: int
    last_ts: int
    start_equity: float
    trades: tuple[SimulatedTrade, ...] = ()
    decisions: tuple[StrategyDecision, ...] = ()
    signals: int = 0
    unfilled: int = 0
    """Signale, die nie gefuellt wurden - die Daten endeten dazwischen."""
    rejections: dict[str, int] = field(default_factory=dict)

    @property
    def net_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)


class Backtester:
    """Fuehrt einen Lauf ueber eine Basis-Bar-Serie durch."""

    def __init__(self, symbol: str, instrument: Instrument, config: Config) -> None:
        self.symbol = symbol.upper()
        self.instrument = instrument
        self.config = config
        self.params = config.backtest

        # Ein gemeinsames Risikobuch fuer Strategie und Simulation: nur so
        # wirken Tagesverlustlimit und Trade-Obergrenze im Backtest wirklich.
        self.ledger = RiskLedger()
        self.context = MarketContext(self.symbol, instrument, config)
        self.strategy = StrategyEngine(self.symbol, instrument, config, ledger=self.ledger)

        self._open: list[OpenTrade] = []
        self._pending: list[OpenTrade] = []
        self._trades: list[SimulatedTrade] = []
        self._unfilled = 0

    # -------------------------------------------------------------------- Lauf
    def run(self, series: BarSeries, progress: ProgressFn | None = None, every: int = 20_000) -> BacktestResult:
        if len(series) == 0:
            raise ValueError("Backtest ohne Bars")

        for index, bar in enumerate(series):
            self._advance(bar, index)

            updates = self.context.on_base_bar(bar)
            if updates:
                for decision in self.strategy.on_updates(updates, self.context):
                    if decision.signal is not None:
                        self._place(decision, bar, index)

            if progress and every and index % every == 0:
                progress(index, len(series))

        self._finish(series[-1], len(series) - 1)
        if progress:
            progress(len(series), len(series))

        rejections: dict[str, int] = {}
        for decision in self.strategy.decisions:
            if decision.is_trade:
                continue
            code = decision.blocking_reason or "unbekannt"
            rejections[code] = rejections.get(code, 0) + 1

        log.info(
            "backtest_finished",
            symbol=self.symbol,
            bars=len(series),
            signals=len(self.strategy.signals),
            trades=len(self._trades),
            net_pnl=round(sum(t.pnl for t in self._trades), 2),
        )

        return BacktestResult(
            symbol=self.symbol,
            instrument=self.instrument,
            base_timeframe=self.config.data.base_timeframe.value,
            bars=len(series),
            first_ts=series[0].ts,
            last_ts=series[-1].ts,
            start_equity=self.config.risk.account_size,
            trades=tuple(self._trades),
            decisions=tuple(self.strategy.decisions),
            signals=len(self.strategy.signals),
            unfilled=self._unfilled,
            rejections=dict(sorted(rejections.items(), key=lambda kv: kv[1], reverse=True)),
        )

    # ------------------------------------------------------------- Positionen
    def _advance(self, bar: Bar, index: int) -> None:
        """Offene Orders fuellen und laufende Positionen gegen diese Bar pruefen."""
        if self._pending:
            for trade in self._pending:
                trade.fill(bar, index)
                self._sync_ledger_entry(trade)
            self._open.extend(self._pending)
            self._pending = []

        if not self._open:
            return

        still_open: list[OpenTrade] = []
        for trade in self._open:
            finished = trade.on_bar(bar, index)
            if finished is None:
                still_open.append(trade)
                continue
            self._book(finished)
        self._open = still_open

    def _place(self, decision: StrategyDecision, bar: Bar, index: int) -> None:
        """Aus einem Signal eine Order fuer die naechste Bar machen."""
        signal: TradeSignal = decision.signal  # type: ignore[assignment]
        session, trading_day, _ = self.context.resolver.resolve(bar.ts)
        trade = OpenTrade(
            signal=signal,
            params=self.params,
            instrument=self.instrument,
            session=session,
            trading_day=trading_day,
            signal_index=index,
            htf_bias=decision.htf_bias,
        )

        self.ledger.open_position(
            OpenPosition(
                setup_id=signal.setup_id,
                direction=signal.direction,
                entry_ts=signal.entry_ts,
                entry_price=signal.entry,
                stop=signal.stop,
                target=signal.target,
                quantity=signal.quantity,
                risk_amount=signal.risk_amount,
            ),
            trading_day,
        )

        if self.params.entry_fill == "signal_close":
            # Fuellung auf der Signalbar selbst. Ausdruecklich unrealistisch -
            # sie existiert nur, um den Preis der Verzoegerung zu messen.
            trade.fill(bar, index)
            self._sync_ledger_entry(trade)
            self._open.append(trade)
        else:
            self._pending.append(trade)

    def _sync_ledger_entry(self, trade: OpenTrade) -> None:
        """Gebuchten Einstieg auf den tatsaechlichen Fuellkurs nachziehen."""
        position = self.ledger.position(trade.setup_id)
        if position is not None:
            position.entry_price = trade.entry_price
            position.entry_ts = trade.entry_ts

    def _book(self, trade: SimulatedTrade) -> None:
        self._trades.append(trade)
        self.ledger.close_position(
            trade.setup_id, trade.exit_ts, trade.pnl, trade.trading_day
        )

    def _finish(self, last_bar: Bar, last_index: int) -> None:
        """Was am Datenende noch laeuft, sauber abschliessen."""
        for trade in self._open:
            self._book(trade.force_close(last_bar, last_index, ExitReason.END_OF_DATA))
        self._open = []

        for trade in self._pending:
            # Nie gefuellt - die Order zaehlt nicht als genommener Trade.
            self.ledger.cancel_position(trade.setup_id, trade.trading_day)
            self._unfilled += 1
        self._pending = []


def run_backtest(
    symbol: str,
    instrument: Instrument,
    config: Config,
    series: BarSeries,
    progress: ProgressFn | None = None,
) -> BacktestResult:
    """Bequemer Einstieg fuer Skripte und API."""
    return Backtester(symbol, instrument, config).run(series, progress)
