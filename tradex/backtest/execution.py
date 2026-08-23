"""Ausfuehrungssimulation (Spec §19).

Der Backtest fuegt der Analyse GENAU EINE Sache hinzu: was aus einem Signal
geworden waere. Alles, was die Regeln betrifft, kommt unveraendert aus
`MarketContext` und `StrategyEngine` - dieses Modul entscheidet nie, OB
gehandelt wird, sondern nur, zu welchem Kurs ein bereits gefaelltes Signal
gefuellt und beendet worden waere.

Die vier Annahmen, an denen ein Backtest luegen kann
---------------------------------------------------
1. **Einstiegskurs.** Das Signal entsteht am Schluss der Bestaetigungsbar. Zu
   genau diesem Kurs kann man real nicht mehr kaufen. Gefuellt wird deshalb per
   Voreinstellung auf der Eroeffnung der FOLGEBAR - mit Schlupf gegen sich.
2. **Stop und Ziel in derselben Bar.** OHLC-Daten sagen nicht, was zuerst kam.
   Voreinstellung ist `stop_first`, also der schlechtere Fall.
3. **Kurssprung ueber den Stop.** Eroeffnet die Bar bereits jenseits des Stops,
   wird dort gefuellt, nicht am Stopkurs. Alles andere waere Wunschdenken.
4. **Schlupf.** Der Stop ist eine Market-Order und rutscht. Das Ziel ist eine
   Limit-Order: sie fuellt zum Kurs oder gar nicht - deshalb ohne Schlupf.

Jede dieser Annahmen steht in `config/default.yaml` und nicht hier im Code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from tradex.config import BacktestConfig
from tradex.domain.bars import Bar
from tradex.domain.enums import Direction, ExitReason, Timeframe
from tradex.domain.instruments import Instrument
from tradex.risk.ledger import RiskLedger
from tradex.strategy.signal import TradeSignal


@dataclass(frozen=True, slots=True)
class SimulatedTrade:
    """Ein vollstaendig durchsimulierter Trade.

    Enthaelt bewusst BEIDE Seiten: was die Strategie geplant hat (`planned_*`)
    und was die Ausfuehrung daraus gemacht haette. Die Differenz ist der Preis
    der Realitaet - ohne beide Zahlen laesst sich nicht sagen, ob eine schwache
    Statistik an der Regel oder an den Ausfuehrungsannahmen liegt.
    """

    trade_id: int
    """Kontoweit eindeutig. `setup_id` zaehlt je Strategie und kollidiert."""
    setup_id: int
    symbol: str
    strategy: str
    """Welche Strategie den Trade vorgeschlagen hat.

    Die wichtigste Spalte, sobald mehrere Strategien am selben Konto laufen:
    ohne sie liesse sich nicht sagen, welche das Ergebnis getragen hat."""
    direction: Direction
    quantity: int

    # ---- geplant (aus dem Signal)
    planned_entry: float
    stop: float
    target: float
    planned_rr: float
    planned_stop_ticks: float
    stop_anchor: str
    target_source: str
    timeframe: str
    """Zeitebene der Signalbar - siehe `TradeSignal.timeframe`."""
    htf_bias: str
    session: str
    trading_day: int

    # ---- tatsaechlich simuliert
    signal_ts: int
    entry_ts: int
    entry_index: int
    entry_price: float
    exit_ts: int
    exit_index: int
    exit_price: float
    exit_reason: ExitReason
    bars_held: int

    risk_points: float
    """Abstand Einstieg-Stop nach Fuellung - das tatsaechlich eingegangene 1R."""
    risk_amount: float
    gross_pnl: float
    commission: float
    pnl: float
    r_multiple: float
    mae_points: float
    """Maximum Adverse Excursion: wie weit es gegen die Position lief."""
    mfe_points: float
    """Maximum Favourable Excursion: wie weit es fuer die Position lief."""

    @property
    def side(self) -> str:
        return "LONG" if self.direction is Direction.BULLISH else "SHORT"

    @property
    def is_win(self) -> bool:
        return self.pnl > 0

    @property
    def is_loss(self) -> bool:
        return self.pnl < 0

    @property
    def is_resolved(self) -> bool:
        """False, wenn nur das Datenende die Position beendet hat."""
        return self.exit_reason is not ExitReason.END_OF_DATA

    @property
    def mae_r(self) -> float:
        return self.mae_points / self.risk_points if self.risk_points else 0.0

    @property
    def mfe_r(self) -> float:
        return self.mfe_points / self.risk_points if self.risk_points else 0.0


def signal_max_age_ns(signal: TradeSignal, grace_ns: int) -> int:
    """Wie alt ein Signal beim Fuellen hoechstens sein darf.

    Der Timestamp bezeichnet die EROEFFNUNG der Signalbar. Eine 5m-Bar ist
    also schon fuenf Minuten alt, wenn sie ueberhaupt schliesst - in Basis-Bars
    gerechnet waere sie damit immer "veraltet". Die Dauer der Signalbar gehoert
    deshalb dazu, und `grace_ns` (aus `max_signal_age_bars`) ist die Frist
    DANACH.
    """
    return Timeframe.parse(signal.timeframe).seconds * 1_000_000_000 + grace_ns


@dataclass(frozen=True, slots=True)
class ExecutionOrder:
    """Ein bereits gebuchtes Signal auf dem Weg zur Fuellung.

    Enthaelt alles, was eine Ausfuehrung braucht - und nichts, was mit der
    Frage zu tun hat, OB gehandelt wird. Diese Entscheidung ist beim Eintreffen
    hier laengst gefallen: Analyse, Regel, Risikopruefung und Positionsgroesse
    liegen dahinter, die Position steht bereits im Risikobuch.
    """

    signal: TradeSignal
    instrument: Instrument
    session: str
    trading_day: int
    htf_bias: str
    signal_index: int


@dataclass(slots=True)
class ExecutionUpdate:
    """Was eine Ausfuehrungsquelle seit dem letzten Aufruf gemeldet hat."""

    closed: list[SimulatedTrade] = field(default_factory=list)
    """Fertige Trades. Der Aufrufer bucht sie ins Risikobuch."""
    stale: int = 0
    """Orders, die ueber einer Datenluecke lagen und verworfen wurden."""
    unfilled: int = 0
    """Orders, die nie zustande kamen - Datenende oder Ablehnung des Brokers."""

    def merge(self, other: ExecutionUpdate) -> None:
        self.closed.extend(other.closed)
        self.stale += other.stale
        self.unfilled += other.unfilled


class TradeExecutor(Protocol):
    """Woher die Fuellungen kommen.

    Die EINZIGE Stelle, an der sich der simulierte und der echte Betrieb
    unterscheiden duerfen. Alles davor - Analyse, Regel, Positionsgroesse,
    Risikopruefung - ist derselbe Code; genau das macht Spec Paragraph 29
    ("Backtest = Live") pruefbar statt vereinbart.

    Ein Executor entscheidet nie, OB gehandelt wird. Er darf eine Order
    ablehnen (kein Broker, kein Kontrakt, keine Verbindung), aber er darf keine
    erzeugen und keine Regel auslegen.
    """

    @property
    def open_count(self) -> int:
        """Wie viele Positionen dieser Executor gerade fuehrt."""
        ...

    def place(self, order: ExecutionOrder, bar: Bar, index: int) -> ExecutionUpdate:
        """Eine gebuchte Order annehmen. `bar` ist die Signalbar."""
        ...

    def advance(self, bar: Bar, index: int) -> ExecutionUpdate:
        """Eine Bar anwenden: offene Orders fuellen, laufende Positionen pruefen."""
        ...

    def poll(self) -> ExecutionUpdate:
        """Was passiert ist, OHNE dass eine Bar kam.

        Fuer die Simulation immer nichts - dort entsteht ohne Bar nichts, und
        genau das ist die Aussage. Ein echter Broker meldet dagegen jederzeit:
        ein Stop kann zwischen zwei Bars ausgeloest werden, und darauf zu
        warten, bis die naechste Minute schliesst, waere ein erfundener
        Zeitverzug.
        """
        ...

    def finish(self, last_bar: Bar, last_index: int) -> ExecutionUpdate:
        """Abschluss am Datenende."""
        ...


class OpenTrade:
    """Lebenszyklus einer Position: Order -> Fuellung -> Ausstieg.

    Bewusst als Objekt mit Zustand statt als reine Funktion ueber alle Bars:
    der Backtest laeuft Bar fuer Bar vorwaerts und darf zu keinem Zeitpunkt in
    die Zukunft sehen. Eine Funktion, die das ganze Fenster auf einmal bekaeme,
    koennte diese Zusicherung nicht geben.
    """

    __slots__ = (
        "signal",
        "params",
        "instrument",
        "session",
        "trading_day",
        "htf_bias",
        "signal_index",
        "filled",
        "closed",
        "entry_price",
        "entry_ts",
        "entry_index",
        "_high_water",
        "_low_water",
    )

    def __init__(
        self,
        signal: TradeSignal,
        params: BacktestConfig,
        instrument: Instrument,
        session: str,
        trading_day: int,
        signal_index: int,
        htf_bias: str = "",
    ) -> None:
        self.signal = signal
        self.params = params
        self.instrument = instrument
        self.session = session
        self.trading_day = trading_day
        self.htf_bias = htf_bias
        self.signal_index = signal_index

        self.filled = False
        self.closed = False
        self.entry_price = 0.0
        self.entry_ts = 0
        self.entry_index = -1
        self._high_water = float("-inf")
        self._low_water = float("inf")

    # ---------------------------------------------------------------- Merkmale
    @property
    def is_bullish(self) -> bool:
        return self.signal.direction is Direction.BULLISH

    @property
    def setup_id(self) -> int:
        return self.signal.setup_id

    @property
    def trade_id(self) -> int:
        """Schluessel im Risikobuch - kontoweit eindeutig, anders als setup_id."""
        return self.signal.trade_id

    # Schlupf laeuft IMMER gegen die Position - aber die Richtung "gegen" ist
    # beim Einstieg und beim Ausstieg entgegengesetzt: teurer kaufen heisst
    # hoeher, schlechter verkaufen heisst tiefer. Genau diese Verwechslung
    # macht aus einem pessimistischen Simulator einen geschoenten.
    def _slip_entry(self, price: float, ticks: float) -> float:
        """Einstieg verteuern: Long kauft hoeher, Short verkauft tiefer."""
        offset = self.instrument.to_points(ticks)
        return self.instrument.round_to_tick(price + offset if self.is_bullish else price - offset)

    def _slip_exit(self, price: float, ticks: float) -> float:
        """Ausstieg verschlechtern: Long verkauft tiefer, Short kauft hoeher."""
        offset = self.instrument.to_points(ticks)
        return self.instrument.round_to_tick(price - offset if self.is_bullish else price + offset)

    # ---------------------------------------------------------------- Fuellung
    def fill(self, bar: Bar, index: int) -> None:
        """Order fuellen. Danach laeuft die Position.

        Der Aufrufer bestimmt WANN gefuellt wird (Bar nach dem Signal bzw. die
        Signalbar selbst) - dieses Objekt bestimmt nur, zu welchem Kurs.
        """
        if self.filled:
            raise ValueError(f"Setup {self.setup_id} ist bereits gefuellt")
        reference = (
            self.signal.entry if self.params.entry_fill == "signal_close" else bar.open
        )
        self.entry_price = self._slip_entry(reference, self.params.entry_slippage_ticks)
        self.entry_ts = bar.ts
        self.entry_index = index
        self.filled = True
        self._high_water = self.entry_price
        self._low_water = self.entry_price

    # ------------------------------------------------------------------ Ablauf
    def on_bar(self, bar: Bar, index: int) -> SimulatedTrade | None:
        """Eine Bar auf die offene Position anwenden.

        Liefert den fertigen Trade, sobald die Position beendet ist, sonst None.
        """
        if not self.filled:
            raise ValueError(f"Setup {self.setup_id} ist noch nicht gefuellt")
        if self.closed:
            raise ValueError(f"Setup {self.setup_id} ist bereits geschlossen")

        # Auf der Einstiegsbar gilt der Fuellkurs als Bezugspunkt, nicht die
        # Bar-Eroeffnung: was VOR dem Einstieg passierte, betrifft uns nicht.
        first_bar = index == self.entry_index
        reference = self.entry_price if first_bar else bar.open
        high = max(bar.high, reference) if first_bar else bar.high
        low = min(bar.low, reference) if first_bar else bar.low
        self._high_water = max(self._high_water, high)
        self._low_water = min(self._low_water, low)

        stop = self.signal.stop
        target = self.signal.target

        # 1. Kurssprung: die Bar eroeffnet bereits jenseits eines Kurses.
        if (self.is_bullish and reference <= stop) or (not self.is_bullish and reference >= stop):
            price = self._slip_exit(reference, self.params.stop_slippage_ticks)
            return self._close(bar, index, price, ExitReason.STOP)
        if (self.is_bullish and reference >= target) or (not self.is_bullish and reference <= target):
            return self._close(bar, index, reference, ExitReason.TARGET)

        # 2. Innerhalb der Bar.
        stop_hit = low <= stop if self.is_bullish else high >= stop
        target_hit = high >= target if self.is_bullish else low <= target

        if stop_hit and target_hit:
            # OHLC sagt nicht, was zuerst kam - die Annahme steht in der Config.
            if self.params.same_bar_resolution == "stop_first":
                return self._close(
                    bar, index, self._slip_exit(stop, self.params.stop_slippage_ticks), ExitReason.STOP
                )
            return self._close(bar, index, target, ExitReason.TARGET)
        if stop_hit:
            return self._close(
                bar, index, self._slip_exit(stop, self.params.stop_slippage_ticks), ExitReason.STOP
            )
        if target_hit:
            return self._close(bar, index, target, ExitReason.TARGET)

        # 3. Zeitstop. Auch er ist eine Market-Order und rutscht.
        limit = self.params.max_holding_bars
        if limit and index - self.entry_index >= limit:
            price = self._slip_exit(bar.close, self.params.stop_slippage_ticks)
            return self._close(bar, index, price, ExitReason.TIME)
        return None

    def force_close(self, bar: Bar, index: int, reason: ExitReason) -> SimulatedTrade:
        """Position ausserhalb der Regeln beenden - beim Datenende."""
        return self._close(bar, index, bar.close, reason)

    # ---------------------------------------------------------------- Abschluss
    def _close(self, bar: Bar, index: int, price: float, reason: ExitReason) -> SimulatedTrade:
        self.closed = True
        signal = self.signal
        quantity = signal.quantity
        direction = 1.0 if self.is_bullish else -1.0

        points = (price - self.entry_price) * direction
        gross = self.instrument.points_to_currency(points, quantity)
        commission = self.params.commission_per_contract * quantity
        pnl = gross - commission

        risk_points = abs(self.entry_price - signal.stop)
        risk_amount = self.instrument.points_to_currency(risk_points, quantity)

        favourable = (
            self._high_water - self.entry_price
            if self.is_bullish
            else self.entry_price - self._low_water
        )
        adverse = (
            self.entry_price - self._low_water
            if self.is_bullish
            else self._high_water - self.entry_price
        )

        return SimulatedTrade(
            trade_id=signal.trade_id,
            setup_id=signal.setup_id,
            symbol=signal.symbol,
            strategy=signal.strategy,
            direction=signal.direction,
            quantity=quantity,
            planned_entry=signal.entry,
            stop=signal.stop,
            target=signal.target,
            planned_rr=signal.rr,
            planned_stop_ticks=signal.stop_ticks,
            stop_anchor=signal.stop_anchor,
            target_source=signal.target_source,
            timeframe=signal.timeframe,
            htf_bias=self.htf_bias,
            session=self.session,
            trading_day=self.trading_day,
            signal_ts=signal.entry_ts,
            entry_ts=self.entry_ts,
            entry_index=self.entry_index,
            entry_price=self.entry_price,
            exit_ts=bar.ts,
            exit_index=index,
            exit_price=price,
            exit_reason=reason,
            bars_held=index - self.entry_index,
            risk_points=risk_points,
            risk_amount=risk_amount,
            gross_pnl=gross,
            commission=commission,
            pnl=pnl,
            # Das R-Vielfache ist NETTO gerechnet: Gebuehren gehoeren zum
            # Ergebnis. Eine Statistik, die sie ausklammert, beschreibt einen
            # Handel, den es nicht gibt.
            r_multiple=pnl / risk_amount if risk_amount else 0.0,
            mae_points=max(adverse, 0.0),
            mfe_points=max(favourable, 0.0),
        )


class SimulatedExecutor:
    """Fuellungen aus den Bars selbst - der Weg, den dieses Projekt seit Phase 4
    geht.

    Erfuellt `TradeExecutor`. Der Inhalt ist unveraendert der frueher direkt in
    `SymbolBook` stehende Ablauf; er ist hierher gezogen, damit der Echtbetrieb
    eine ANDERE Quelle einsetzen kann, ohne dass daneben ein zweiter
    Ausfuehrungspfad entsteht.
    """

    __slots__ = ("instrument", "params", "ledger", "_grace_ns", "_open", "_pending")

    def __init__(
        self,
        instrument: Instrument,
        params: BacktestConfig,
        ledger: RiskLedger,
        grace_ns: int,
    ) -> None:
        self.instrument = instrument
        self.params = params
        self.ledger = ledger
        self._grace_ns = grace_ns
        self._open: list[OpenTrade] = []
        self._pending: list[OpenTrade] = []

    @property
    def open_count(self) -> int:
        return len(self._open)

    def place(self, order: ExecutionOrder, bar: Bar, index: int) -> ExecutionUpdate:
        trade = OpenTrade(
            signal=order.signal,
            params=self.params,
            instrument=order.instrument,
            session=order.session,
            trading_day=order.trading_day,
            signal_index=order.signal_index,
            htf_bias=order.htf_bias,
        )
        if self.params.entry_fill == "signal_close":
            # Fuellung auf der Signalbar selbst. Ausdruecklich unrealistisch -
            # sie existiert nur, um den Preis der Verzoegerung zu messen.
            trade.fill(bar, index)
            self._sync_ledger_entry(trade)
            self._open.append(trade)
        else:
            self._pending.append(trade)
        return ExecutionUpdate()

    def advance(self, bar: Bar, index: int) -> ExecutionUpdate:
        update = ExecutionUpdate()

        if self._pending:
            for trade in self._pending:
                if bar.ts - trade.signal.entry_ts > signal_max_age_ns(
                    trade.signal, self._grace_ns
                ):
                    # Zwischen Signal und Fuellung liegt eine Luecke - die
                    # Order wird verworfen statt zu einem erfundenen Kurs
                    # gefuellt.
                    self.ledger.cancel_position(trade.trade_id, trade.trading_day)
                    update.stale += 1
                    continue
                trade.fill(bar, index)
                self._sync_ledger_entry(trade)
                self._open.append(trade)
            self._pending = []

        if not self._open:
            return update

        still_open: list[OpenTrade] = []
        for trade in self._open:
            finished = trade.on_bar(bar, index)
            if finished is None:
                still_open.append(trade)
                continue
            update.closed.append(finished)
        self._open = still_open
        return update

    def poll(self) -> ExecutionUpdate:
        """Ohne Bar passiert in der Simulation nichts - das ist keine Luecke,
        sondern die Definition: gefuellt wird ausschliesslich aus Bars."""
        return ExecutionUpdate()

    def finish(self, last_bar: Bar, last_index: int) -> ExecutionUpdate:
        update = ExecutionUpdate()
        for trade in self._open:
            update.closed.append(
                trade.force_close(last_bar, last_index, ExitReason.END_OF_DATA)
            )
        self._open = []

        for trade in self._pending:
            # Nie gefuellt - die Order zaehlt nicht als genommener Trade.
            self.ledger.cancel_position(trade.trade_id, trade.trading_day)
            update.unfilled += 1
        self._pending = []
        return update

    def _sync_ledger_entry(self, trade: OpenTrade) -> None:
        """Gebuchten Einstieg auf den tatsaechlichen Fuellkurs nachziehen."""
        position = self.ledger.position(trade.trade_id)
        if position is not None:
            position.entry_price = trade.entry_price
            position.entry_ts = trade.entry_ts
