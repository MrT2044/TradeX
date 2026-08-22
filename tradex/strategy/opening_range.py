"""Opening Range Breakout - eine schnelle Intraday-Strategie.

WARUM ES DIESE STRATEGIE GIBT
-----------------------------
Die ICT-Pflichtkette ist strukturell niederfrequent: gemessen 0,25
vollstaendige Ketten pro Handelstag. Fuer ein Day-Trading-System braucht es
Regeln, die oefter ausloesen, ohne dass man die Kette dafuer aufweicht.

Diese hier ist bewusst ein LEHRBUCHVERFAHREN und keine Erfindung: die ersten
`range_minutes` einer Session bilden eine Spanne; ein Schlusskurs jenseits
dieser Spanne gilt als Ausbruch. Das Verfahren ist seit den 1980ern
beschrieben, quantitativ eindeutig und hat genau zwei Parameter. Es wurde
NICHT ausgewaehlt, weil es auf diesen Daten gut aussieht - es wurde vor der
ersten Messung festgelegt.

    Sie ist damit eine HYPOTHESE, genau wie die Kette. Ob sie einen Edge hat,
    beantwortet der Backtest - einschliesslich der Moeglichkeit "nein".

Die Regel
---------
    1. Session beginnt -> Spanne der ersten `range_minutes` messen
    2. Spanne muss mindestens `min_range_atr_mult * ATR` gross sein
       (eine Spanne nahe null erzeugt sonst Ausbrueche aus dem Rauschen)
    3. Schlusskurs ueber dem Spannenhoch  -> Long, unter dem Tief -> Short
    4. Stop auf die Gegenseite der Spanne plus Puffer
    5. Ziel als Vielfaches der Spannenbreite
    6. Hoechstens `max_trades_per_session` Versuche je Session

Warum kein HTF-Bias-Filter
--------------------------
Absichtlich nicht. Die Kette handelt ausschliesslich mit dem Bias - wenn beide
Strategien denselben Filter haetten, waeren ihre Ergebnisse stark korreliert
und das Portfolio waere nur eine teurere Fassung der Kette. Ob der Bias hilft,
laesst sich so ausserdem erstmals VERGLEICHEN statt vorauszusetzen.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tradex.analysis import reasons as R
from tradex.analysis.context import MarketContext, TimeframeUpdate
from tradex.config import Config
from tradex.domain.bars import Bar
from tradex.domain.enums import Direction, SessionName, Timeframe
from tradex.domain.instruments import Instrument
from tradex.persistence.models import Reason
from tradex.strategy.base import Strategy, StrategyOutput, TradeProposal
from tradex.strategy.signal import StrategyDecision

OPENING_RANGE_NAME = "opening_range"


@dataclass(slots=True)
class SessionRange:
    """Die Eroeffnungsspanne einer Session im Aufbau."""

    session: str
    trading_day: int
    first_ts: int
    high: float
    low: float
    bars: int = 0
    complete: bool = False
    trades_taken: int = 0
    broken_up: bool = False
    broken_down: bool = False
    history: list[Bar] = field(default_factory=list)

    @property
    def width(self) -> float:
        return self.high - self.low

    def absorb(self, bar: Bar) -> None:
        self.high = max(self.high, bar.high)
        self.low = min(self.low, bar.low)
        self.bars += 1


class OpeningRangeStrategy(Strategy):
    """Ausbruch aus der Eroeffnungsspanne einer Session."""

    name = OPENING_RANGE_NAME

    def __init__(self, symbol: str, instrument: Instrument, config: Config) -> None:
        self.symbol = symbol.upper()
        self.instrument = instrument
        self.config = config
        self.params = config.opening_range
        self.timeframe: Timeframe = config.opening_range.timeframe

        self._ranges: dict[tuple[str, int], SessionRange] = {}
        self._current: SessionRange | None = None
        self._next_id = 1

    def reset(self) -> None:
        self._ranges = {}
        self._current = None
        self._next_id = 1

    # -------------------------------------------------------------------- Lauf
    def on_updates(
        self, updates: list[TimeframeUpdate], context: MarketContext
    ) -> StrategyOutput:
        out = StrategyOutput()
        if not self.params.enabled:
            return out

        for update in updates:
            if update.timeframe is not self.timeframe:
                continue
            self._on_bar(update, context, out)
        return out

    def _on_bar(
        self, update: TimeframeUpdate, context: MarketContext, out: StrategyOutput
    ) -> None:
        bar = update.bar
        session, trading_day, _ = context.resolver.resolve(bar.ts)

        # Ausserhalb der Handelszeit gibt es keine Eroeffnungsspanne.
        if session == SessionName.CLOSED.value:
            self._current = None
            return
        if session not in {s.value for s in self.params.sessions}:
            self._current = None
            return

        key = (session, trading_day)
        current = self._ranges.get(key)
        if current is None:
            current = SessionRange(
                session=session,
                trading_day=trading_day,
                first_ts=bar.ts,
                high=bar.high,
                low=bar.low,
            )
            self._ranges[key] = current
            # Speicher begrenzen: mehr als ein paar Tage braucht niemand.
            if len(self._ranges) > self.params.max_tracked_sessions:
                oldest = min(self._ranges, key=lambda k: self._ranges[k].first_ts)
                del self._ranges[oldest]
        self._current = current

        bars_needed = max(1, self.params.range_minutes // self.timeframe.minutes)
        if not current.complete:
            current.absorb(bar)
            if current.bars >= bars_needed:
                current.complete = True
            return

        proposal = self._try_breakout(current, update, context)
        if proposal is not None:
            if isinstance(proposal, TradeProposal):
                out.proposals.append(proposal)
            else:
                out.decisions.append(proposal)

    # ---------------------------------------------------------------- Ausbruch
    def _try_breakout(
        self, current: SessionRange, update: TimeframeUpdate, context: MarketContext
    ) -> TradeProposal | StrategyDecision | None:
        bar = update.bar
        if current.trades_taken >= self.params.max_trades_per_session:
            return None

        atr = update.atr
        if atr != atr:  # NaN - noch keine belastbare Volatilitaet
            return None

        # Eine Spanne nahe null erzeugt Ausbrueche aus reinem Rauschen.
        if current.width < self.params.min_range_atr_mult * atr:
            return None
        width_ticks = self.instrument.to_ticks(current.width)
        if width_ticks > self.params.max_range_ticks:
            return None

        if bar.close > current.high and not current.broken_up:
            current.broken_up = True
            direction = Direction.BULLISH
        elif bar.close < current.low and not current.broken_down:
            current.broken_down = True
            direction = Direction.BEARISH
        else:
            return None

        current.trades_taken += 1
        setup_id = self._next_id
        self._next_id += 1

        entry = self.instrument.round_to_tick(bar.close)
        buffer_points = self.instrument.to_points(self.params.stop_buffer_ticks)
        if direction is Direction.BULLISH:
            stop = self.instrument.round_to_tick(current.low - buffer_points)
            target = self.instrument.round_to_tick(
                entry + current.width * self.params.target_range_mult
            )
        else:
            stop = self.instrument.round_to_tick(current.high + buffer_points)
            target = self.instrument.round_to_tick(
                entry - current.width * self.params.target_range_mult
            )

        stop_points = abs(entry - stop)
        stop_ticks = self.instrument.to_ticks(stop_points)
        target_points = abs(target - entry)

        session, trading_day, _ = context.resolver.resolve(bar.ts)
        reasons: list[Reason] = [
            Reason(
                R.OPENING_RANGE_BREAKOUT,
                True,
                {
                    "session": current.session,
                    "high": round(current.high, 2),
                    "low": round(current.low, 2),
                    "width_ticks": round(width_ticks, 1),
                    "minutes": self.params.range_minutes,
                },
            )
        ]

        # Stopgrenzen gelten fuer jede Strategie gleich (Spec §11).
        stops = self.config.stops
        if stop_ticks < stops.min_stop_ticks or stop_ticks > stops.max_stop_ticks:
            code = R.STOP_TOO_TIGHT if stop_ticks < stops.min_stop_ticks else R.STOP_TOO_WIDE
            reasons.append(
                Reason(
                    code,
                    False,
                    {
                        "stop_ticks": round(stop_ticks, 1),
                        "min": stops.min_stop_ticks,
                        "max": stops.max_stop_ticks,
                    },
                )
            )
            return self._no_trade(setup_id, direction, bar, update, reasons, context)

        rr = target_points / stop_points if stop_points > 0 else 0.0
        if rr < self.config.risk.min_rr:
            reasons.append(
                Reason(
                    R.TARGET_RR_TOO_LOW,
                    False,
                    {"best_rr": round(rr, 2), "min_rr": self.config.risk.min_rr},
                )
            )
            return self._no_trade(setup_id, direction, bar, update, reasons, context)

        reasons.append(
            Reason(R.STOP_PLACED, True, {"price": stop, "ticks": round(stop_ticks, 1), "anchor": "range"})
        )
        reasons.append(
            Reason(R.TARGET_FALLBACK, True, {"price": target, "rr": round(rr, 2), "pool": None})
        )

        return TradeProposal(
            strategy=self.name,
            setup_id=setup_id,
            symbol=self.symbol,
            direction=direction,
            entry=entry,
            stop=stop,
            target=target,
            stop_ticks=stop_ticks,
            stop_points=stop_points,
            target_points=target_points,
            rr=rr,
            stop_anchor="range",
            target_source="range_multiple",
            ts=bar.ts,
            index=update.index,
            timeframe=self.timeframe.value,
            stage="breakout",
            checklist={"range": True, "breakout": True},
            reasons=tuple(reasons),
            htf_bias=context.bias().bias.value,
            session=session,
            trading_day=trading_day,
            atr=atr,
        )

    def _no_trade(
        self,
        setup_id: int,
        direction: Direction,
        bar: Bar,
        update: TimeframeUpdate,
        reasons: list[Reason],
        context: MarketContext,
    ) -> StrategyDecision:
        reasons.append(Reason(R.DECISION_NO_TRADE, False, {"stage": "breakout", "missing": []}))
        return StrategyDecision(
            ts=bar.ts,
            index=update.index,
            symbol=self.symbol,
            timeframe=self.timeframe.value,
            setup_id=setup_id,
            direction=direction,
            decision="NO_TRADE",
            stage="breakout",
            checklist={"range": True, "breakout": True},
            reasons=tuple(reasons),
            htf_bias=context.bias().bias.value,
            strategy=self.name,
        )

    # ------------------------------------------------------------------ Anzeige
    def active_setups(self) -> list[object]:
        return []

    @property
    def current_range(self) -> SessionRange | None:
        return self._current
