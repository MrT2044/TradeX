"""Backtest-Lauf (Spec §19).

Der Backtest benutzt EXAKT denselben Analysepfad wie Replay und spaeter der
Live-Betrieb: `MarketContext.on_base_bar()` und `StrategyPortfolio.on_updates()`
mit den Strategien aus `tradex/strategy/registry.py`.
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

Mehrere Instrumente, EIN Konto
------------------------------
Jedes Symbol bekommt ein eigenes `SymbolBook` mit eigener Analyse und eigenen
Strategien - aber alle teilen sich ein Risikobuch. Das ist der ganze Sinn:
Tagesverlustlimit und Positionsobergrenze gelten fuer das Konto.

Die Bars werden dafuer STRENG CHRONOLOGISCH verschraenkt. Wuerde man Symbol
fuer Symbol durchlaufen, saehe das gemeinsame Risikobuch beim zweiten Symbol
bereits alle Ergebnisse des ersten - ein Jahr Zukunftswissen. Der Einzelfall
(ein Symbol) ist dabei nur der Sonderfall mit einem Buch, kein zweiter
Codepfad.

Warum die Position schon beim Signal ins Risikobuch kommt
---------------------------------------------------------
Zwischen Signal und Fuellung liegt eine Bar. Wuerde erst bei der Fuellung
gebucht, koennte in diesem Fenster ein zweites Signal dieselbe freie Stelle
belegen und `max_open_positions` waere wirkungslos. Wird die Order nie gefuellt
(weil die Daten enden), wird die Buchung zurueckgenommen.

Warum Signale ueber einer Datenluecke verworfen werden
------------------------------------------------------
"Die naechste Bar" ist nicht immer "die naechste Minute". Am 19.06.2023
(Juneteenth) endete der Handel um 11:58 CT und lief erst um 17:00 CT weiter.
Die Signalbar von 11:58 gilt erst als geschlossen, wenn die naechste Bar
eintrifft - also fuenf Stunden spaeter. Ohne Gegenmassnahme wird daraus ein
Einstieg um 17:01 auf Grundlage eines Kursverlaufs vom Vormittag: ein Trade,
den es nie gab. `max_signal_age_bars` verwirft solche Signale.
"""

from __future__ import annotations

import heapq
from collections.abc import Callable
from dataclasses import dataclass, field

from tradex.analysis import reasons as R
from tradex.analysis.context import MarketContext
from tradex.backtest.execution import (
    ExecutionOrder,
    ExecutionUpdate,
    SimulatedExecutor,
    SimulatedTrade,
    TradeExecutor,
    signal_max_age_ns,
)
from tradex.config import Config
from tradex.domain.bars import Bar, BarSeries
from tradex.domain.instruments import Instrument
from tradex.logging_setup import get_logger
from tradex.news.calendar import NewsCalendar
from tradex.risk.ledger import OpenPosition, RiskLedger
from tradex.strategy.portfolio import StrategyPortfolio
from tradex.strategy.registry import build_portfolio, load_news_calendar
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
    """Anzeigename - bei mehreren Instrumenten alle, mit '+' verbunden."""
    symbols: tuple[str, ...]
    instrument: Instrument
    """Das erste Instrument. Nur fuer Anzeigezwecke; Kennzahlen sind
    instrumentunabhaengig, weil in R gerechnet wird."""
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
    stale: int = 0
    """Signale, die ueber einer Datenluecke lagen und deshalb verworfen wurden."""
    news_missing: int = 0
    """Entscheidungen, bei denen der Nachrichtenfilter aktiv war, aber keine
    Termine fuer den Zeitpunkt vorlagen. Der gefaehrlichste Zustand: der Filter
    laeuft, tut aber nichts, und das Ergebnis sieht aus wie eines MIT Filter."""
    rejections: dict[str, int] = field(default_factory=dict)

    @property
    def net_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def is_multi_symbol(self) -> bool:
        return len(self.symbols) > 1


class SymbolBook:
    """Analyse, Strategien und offene Positionen EINES Instruments.

    Haelt bewusst kein eigenes Risikobuch: das gehoert dem Konto und wird von
    aussen hereingereicht.

    Woher die Fuellungen kommen, ist austauschbar (`executor`). Ohne Angabe ist
    es die Simulation aus den Bars - der Weg von Backtest und Papertrading.
    Der Echtbetrieb reicht stattdessen eine Broker-Anbindung herein. Was NICHT
    austauschbar ist: alles darueber. Analyse, Regel, Positionsgroesse und
    Risikopruefung laufen in beiden Faellen durch denselben Code, sonst waere
    die Frage "handelt es so, wie der Backtest sagt?" nicht mehr beantwortbar.
    """

    def __init__(
        self,
        symbol: str,
        instrument: Instrument,
        config: Config,
        ledger: RiskLedger,
        news: NewsCalendar | None = None,
        executor: TradeExecutor | None = None,
    ) -> None:
        self.symbol = symbol.upper()
        self.instrument = instrument
        self.config = config
        self.params = config.backtest
        self.ledger = ledger

        self.context = MarketContext(self.symbol, instrument, config)
        self.strategy = build_portfolio(self.symbol, instrument, config, ledger=ledger, news=news)

        self.trades: list[SimulatedTrade] = []
        self.unfilled = 0
        self.stale = 0
        #: Zusaetzliche Frist ueber die Dauer der Signalbar hinaus - siehe
        #: `signal_max_age_ns`.
        self._grace_ns = (
            self.params.max_signal_age_bars * config.data.base_timeframe.seconds * 1_000_000_000
        )
        self.executor: TradeExecutor = executor or SimulatedExecutor(
            instrument, self.params, ledger, self._grace_ns
        )

    # -------------------------------------------------------------------- Bar
    def on_bar(self, bar: Bar, index: int) -> None:
        """Eine Basis-Bar dieses Symbols verarbeiten."""
        self._advance(bar, index)

        updates = self.context.on_base_bar(bar)
        if not updates:
            return
        for decision in self.strategy.on_updates(updates, self.context):
            if decision.signal is not None:
                self._place(decision, bar, index)

    def finish(self, last_bar: Bar, last_index: int) -> None:
        """Was am Datenende noch laeuft, sauber abschliessen."""
        self._apply(self.executor.finish(last_bar, last_index))

    def pump(self) -> None:
        """Rueckmeldungen der Ausfuehrungsquelle einsammeln, ohne dass eine Bar kam.

        Im Backtest folgenlos - dort entsteht ohne Bar nichts. Im Echtbetrieb
        der Weg, auf dem ein zwischen zwei Bars ausgeloester Stop ankommt.
        """
        self._apply(self.executor.poll())

    # ------------------------------------------------------------- Positionen
    def _advance(self, bar: Bar, index: int) -> None:
        """Offene Orders fuellen und laufende Positionen gegen diese Bar pruefen."""
        self._apply(self.executor.advance(bar, index))

    def _apply(self, update: ExecutionUpdate) -> None:
        """Was die Ausfuehrungsquelle gemeldet hat, ins Konto uebernehmen."""
        self.stale += update.stale
        self.unfilled += update.unfilled
        for trade in update.closed:
            self._book(trade)

    def _place(self, decision: StrategyDecision, bar: Bar, index: int) -> None:
        """Aus einem Signal eine Order fuer die naechste Bar machen."""
        signal: TradeSignal = decision.signal  # type: ignore[assignment]

        # Ist das Signal schon bei der Ordererteilung veraltet, entstand es vor
        # einer Luecke. Es wird gar nicht erst gebucht.
        if bar.ts - signal.entry_ts > signal_max_age_ns(signal, self._grace_ns):
            self.stale += 1
            log.info(
                "signal_stale",
                symbol=self.symbol,
                strategy=signal.strategy,
                setup=signal.setup_id,
                gap_minutes=round((bar.ts - signal.entry_ts) / 60e9),
            )
            return

        session, trading_day, _ = self.context.resolver.resolve(bar.ts)

        # Gebucht wird VOR der Ausfuehrung: zwischen Signal und Fuellung liegt
        # eine Bar, und in diesem Fenster koennte sonst ein zweites Signal
        # dieselbe freie Stelle belegen. Kommt die Order nicht zustande, nimmt
        # die Ausfuehrungsquelle die Buchung zurueck.
        self.ledger.open_position(
            OpenPosition(
                setup_id=signal.trade_id,
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

        self._apply(
            self.executor.place(
                ExecutionOrder(
                    signal=signal,
                    instrument=self.instrument,
                    session=session,
                    trading_day=trading_day,
                    htf_bias=decision.htf_bias,
                    signal_index=index,
                ),
                bar,
                index,
            )
        )

    def _book(self, trade: SimulatedTrade) -> None:
        self.trades.append(trade)
        self.ledger.close_position(
            trade.trade_id, trade.exit_ts, trade.pnl, trade.trading_day
        )


class Backtester:
    """Fuehrt einen Lauf ueber ein oder mehrere Instrumente durch."""

    def __init__(
        self,
        symbol: str | None = None,
        instrument: Instrument | None = None,
        config: Config | None = None,
        instruments: dict[str, Instrument] | None = None,
    ) -> None:
        if config is None:
            raise ValueError("Backtester braucht eine Konfiguration")
        if instruments is None:
            if symbol is None or instrument is None:
                raise ValueError("Entweder (symbol, instrument) oder instruments angeben")
            instruments = {symbol.upper(): instrument}

        self.config = config
        self.params = config.backtest
        # EIN Risikobuch fuer alle Instrumente - das ist der Sinn der Sache.
        self.ledger = RiskLedger()
        # Ebenso EIN Nachrichtenkalender: er einmal je Symbol geladen waere
        # dieselbe Datei mehrfach im Speicher, und bei einem Abruf zwischen
        # zwei Ladevorgaengen saehen die Buecher verschiedene Termine.
        self.news = load_news_calendar(config)
        self.books = {
            name.upper(): SymbolBook(name, item, config, self.ledger, news=self.news)
            for name, item in instruments.items()
        }

    # ------------------------------------------------------------- Bequemlichkeit
    @property
    def symbol(self) -> str:
        return next(iter(self.books))

    @property
    def strategy(self) -> StrategyPortfolio:
        """Das Portfolio des ersten Symbols - fuer den Einzelfall."""
        return next(iter(self.books.values())).strategy

    @property
    def context(self) -> MarketContext:
        return next(iter(self.books.values())).context

    # -------------------------------------------------------------------- Lauf
    def run(
        self, series: BarSeries, progress: ProgressFn | None = None, every: int = 20_000
    ) -> BacktestResult:
        """Einzelnes Instrument - der Sonderfall von `run_many` mit einem Buch."""
        return self.run_many({self.symbol: series}, progress, every)

    def run_many(
        self,
        series_by_symbol: dict[str, BarSeries],
        progress: ProgressFn | None = None,
        every: int = 20_000,
    ) -> BacktestResult:
        """Mehrere Instrumente an einem Konto, streng chronologisch verschraenkt."""
        prepared = {name.upper(): s for name, s in series_by_symbol.items()}
        unknown = set(prepared) - set(self.books)
        if unknown:
            raise ValueError(f"Keine Buecher fuer: {', '.join(sorted(unknown))}")
        if not prepared or all(len(s) == 0 for s in prepared.values()):
            raise ValueError("Backtest ohne Bars")

        total = sum(len(s) for s in prepared.values())
        last_seen: dict[str, tuple[Bar, int]] = {}

        for done, (symbol, index, bar) in enumerate(_merge(prepared), start=1):
            self.books[symbol].on_bar(bar, index)
            last_seen[symbol] = (bar, index)
            if progress and every and done % every == 0:
                progress(done, total)

        for symbol, (bar, index) in last_seen.items():
            self.books[symbol].finish(bar, index)
        if progress:
            progress(total, total)

        return self._collect(prepared, total)

    # ------------------------------------------------------------- Auswertung
    def _collect(self, prepared: dict[str, BarSeries], total: int) -> BacktestResult:
        books = [self.books[name] for name in prepared]
        trades = sorted(
            (t for book in books for t in book.trades), key=lambda t: t.exit_ts
        )
        decisions = sorted(
            (d for book in books for d in book.strategy.decisions), key=lambda d: d.ts
        )

        rejections: dict[str, int] = {}
        news_missing = 0
        for decision in decisions:
            if any(r.code == R.NEWS_NO_DATA for r in decision.reasons):
                news_missing += 1
            if decision.is_trade:
                continue
            code = decision.blocking_reason or "unbekannt"
            rejections[code] = rejections.get(code, 0) + 1

        names = tuple(sorted(prepared))
        stale = sum(book.stale for book in books)
        log.info(
            "backtest_finished",
            symbols="+".join(names),
            bars=total,
            signals=sum(len(book.strategy.signals) for book in books),
            trades=len(trades),
            stale=stale,
            net_pnl=round(sum(t.pnl for t in trades), 2),
        )

        non_empty = [s for s in prepared.values() if len(s)]
        return BacktestResult(
            symbol="+".join(names),
            symbols=names,
            instrument=self.books[names[0]].instrument,
            base_timeframe=self.config.data.base_timeframe.value,
            bars=total,
            first_ts=min(s[0].ts for s in non_empty),
            last_ts=max(s[-1].ts for s in non_empty),
            start_equity=self.config.risk.account_size,
            trades=tuple(trades),
            decisions=tuple(decisions),
            signals=sum(len(book.strategy.signals) for book in books),
            unfilled=sum(book.unfilled for book in books),
            stale=stale,
            news_missing=news_missing,
            rejections=dict(sorted(rejections.items(), key=lambda kv: kv[1], reverse=True)),
        )


def _merge(series_by_symbol: dict[str, BarSeries]):
    """Bars mehrerer Symbole streng chronologisch zusammenfuehren.

    Bei gleichem Timestamp entscheidet der Symbolname - Hauptsache
    reproduzierbar. Ohne feste Regel haenge das Ergebnis an der zufaelligen
    Reihenfolge des Dictionarys, sobald die Grenzen knapp sind.
    """
    streams = [
        (series[0].ts, name, 0, series)
        for name, series in sorted(series_by_symbol.items())
        if len(series)
    ]
    heapq.heapify(streams)
    while streams:
        _, name, index, series = heapq.heappop(streams)
        yield name, index, series[index]
        nxt = index + 1
        if nxt < len(series):
            heapq.heappush(streams, (series[nxt].ts, name, nxt, series))


def run_backtest(
    symbol: str,
    instrument: Instrument,
    config: Config,
    series: BarSeries,
    progress: ProgressFn | None = None,
) -> BacktestResult:
    """Bequemer Einstieg fuer Skripte und API (ein Instrument)."""
    return Backtester(symbol, instrument, config).run(series, progress)


def run_multi_backtest(
    instruments: dict[str, Instrument],
    config: Config,
    series_by_symbol: dict[str, BarSeries],
    progress: ProgressFn | None = None,
) -> BacktestResult:
    """Mehrere Instrumente an einem Konto."""
    return Backtester(config=config, instruments=instruments).run_many(
        series_by_symbol, progress
    )
