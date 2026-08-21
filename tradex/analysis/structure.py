"""Marktstruktur: Break of Structure (BOS) und Market Structure Shift (MSS).

Definitionen
------------
Referenzlevel ist immer der ZULETZT bestaetigte Swing, der noch nicht gebrochen
wurde. Ein Bruch liegt vor, wenn

    bullish :  close[i] > referenz_high + min_break_ticks
    bearish :  close[i] < referenz_low  - min_break_ticks

(mit `confirm_on: wick` statt close entsprechend high[i] / low[i]).

    BOS = Bruch in Richtung des bestehenden Zustands oder aus einer Range heraus
    MSS = Bruch GEGEN den bestehenden Zustand (auch CHoCH genannt)

Damit ist MSS genau das, was Spec §7 Schritt 6 als Pflichtbedingung fordert:
der erste Strukturbruch entgegen der vorherigen Richtung.

Bewusste Festlegungen
---------------------
1. Close statt Docht (Default). Ein Docht ueber ein Hoch ist ein Sweep, kein
   Strukturbruch - beides zu unterscheiden ist der Kern der Strategie.
2. Ein gebrochener Swing wird verbraucht. Ohne das wuerde jede weitere Bar ueber
   demselben Hoch erneut ein BOS melden.
3. Referenz ist der JUENGSTE bestaetigte Swing, nicht der hoechste. Liegt er bei
   seiner Bestaetigung bereits unter dem aktuellen Kurs, feuert der Bruch sofort.
   Das ist gewollt: im Live-Betrieb weiss man erst dann von dem Swing, und
   Backtest und Live muessen sich identisch verhalten.
4. Hoechstens ein Ereignis pro Bar. Sollten (nur mit `confirm_on: wick`) beide
   Seiten zutreffen, gewinnt der groessere Bruch.
"""

from __future__ import annotations

from dataclasses import dataclass

from tradex.analysis.swings import Swing
from tradex.config import StructureParams
from tradex.domain.bars import BarSeries
from tradex.domain.enums import StructureEventType, StructureState, SwingType


@dataclass(frozen=True, slots=True)
class StructureEvent:
    """Ein Strukturbruch."""

    index: int
    ts: int
    type: StructureEventType
    broken_swing_index: int
    broken_price: float
    break_price: float
    """Kurs, der den Bruch ausgeloest hat (close oder high/low je nach Config)."""
    previous_state: StructureState
    new_state: StructureState

    @property
    def is_mss(self) -> bool:
        return self.type in (StructureEventType.MSS_BULLISH, StructureEventType.MSS_BEARISH)

    @property
    def is_bullish(self) -> bool:
        return self.type in (StructureEventType.BOS_BULLISH, StructureEventType.MSS_BULLISH)


@dataclass(frozen=True, slots=True)
class SwingLabel:
    """Einordnung eines Swings gegenueber seinem Vorgaenger (HH/HL/LH/LL)."""

    swing: Swing
    label: str  # HH | LH | HL | LL


class StructureTracker:
    """Inkrementeller Strukturzustand eines Timeframes."""

    __slots__ = (
        "params",
        "tick_size",
        "state",
        "events",
        "labels",
        "_active_high",
        "_active_low",
        "_prev_high_price",
        "_prev_low_price",
    )

    def __init__(self, params: StructureParams, tick_size: float) -> None:
        self.params = params
        self.tick_size = tick_size
        self.state = StructureState.RANGE
        self.events: list[StructureEvent] = []
        self.labels: list[SwingLabel] = []
        self._active_high: Swing | None = None
        self._active_low: Swing | None = None
        self._prev_high_price: float | None = None
        self._prev_low_price: float | None = None

    # ------------------------------------------------------------------- API
    def register_swings(self, swings: list[Swing]) -> None:
        """Neu bestaetigte Swings uebernehmen. Muss VOR `update` aufgerufen werden."""
        for swing in swings:
            if swing.type is SwingType.HIGH:
                if self._prev_high_price is not None:
                    label = "HH" if swing.price > self._prev_high_price else "LH"
                    self.labels.append(SwingLabel(swing, label))
                self._prev_high_price = swing.price
                self._active_high = swing
            else:
                if self._prev_low_price is not None:
                    label = "HL" if swing.price > self._prev_low_price else "LL"
                    self.labels.append(SwingLabel(swing, label))
                self._prev_low_price = swing.price
                self._active_low = swing
        if len(self.labels) > self.params.max_events:
            del self.labels[: len(self.labels) - self.params.max_events]

    def update(self, series: BarSeries, index: int) -> StructureEvent | None:
        """Bar `index` auf einen Strukturbruch pruefen."""
        buffer = self.params.min_break_ticks * self.tick_size
        use_close = self.params.confirm_on == "close"
        up_price = float(series.close[index] if use_close else series.high[index])
        down_price = float(series.close[index] if use_close else series.low[index])

        bull_margin = (
            up_price - (self._active_high.price + buffer) if self._active_high else float("-inf")
        )
        bear_margin = (
            (self._active_low.price - buffer) - down_price if self._active_low else float("-inf")
        )

        if bull_margin <= 0 and bear_margin <= 0:
            return None
        go_bullish = bull_margin >= bear_margin

        if go_bullish:
            assert self._active_high is not None
            event = self._make_event(
                series, index, self._active_high, up_price, bullish=True
            )
            self._active_high = None
        else:
            assert self._active_low is not None
            event = self._make_event(
                series, index, self._active_low, down_price, bullish=False
            )
            self._active_low = None

        self.events.append(event)
        if len(self.events) > self.params.max_events:
            del self.events[: len(self.events) - self.params.max_events]
        return event

    # --------------------------------------------------------------- Intern
    def _make_event(
        self, series: BarSeries, index: int, swing: Swing, break_price: float, *, bullish: bool
    ) -> StructureEvent:
        previous = self.state
        if bullish:
            is_shift = previous is StructureState.BEARISH
            event_type = (
                StructureEventType.MSS_BULLISH if is_shift else StructureEventType.BOS_BULLISH
            )
            self.state = StructureState.BULLISH
        else:
            is_shift = previous is StructureState.BULLISH
            event_type = (
                StructureEventType.MSS_BEARISH if is_shift else StructureEventType.BOS_BEARISH
            )
            self.state = StructureState.BEARISH
        return StructureEvent(
            index=index,
            ts=int(series.ts[index]),
            type=event_type,
            broken_swing_index=swing.index,
            broken_price=swing.price,
            break_price=break_price,
            previous_state=previous,
            new_state=self.state,
        )

    # -------------------------------------------------------------- Abfragen
    @property
    def last_event(self) -> StructureEvent | None:
        return self.events[-1] if self.events else None

    def last_mss(self) -> StructureEvent | None:
        for event in reversed(self.events):
            if event.is_mss:
                return event
        return None

    def mss_within(self, index: int, lookback_bars: int) -> StructureEvent | None:
        """Juengster MSS innerhalb der letzten `lookback_bars` Bars, sonst None.

        Das ist die Form, in der Spec §7 Schritt 6 die Bestaetigung braucht:
        nicht "gab es irgendwann mal einen MSS", sondern "gerade eben".
        """
        for event in reversed(self.events):
            if not event.is_mss:
                continue
            if index - event.index <= lookback_bars:
                return event
            return None
        return None

    @property
    def active_high(self) -> Swing | None:
        return self._active_high

    @property
    def active_low(self) -> Swing | None:
        return self._active_low

    def recent_labels(self, count: int) -> list[SwingLabel]:
        return self.labels[-count:] if count > 0 else []
