"""Displacement - der quantifizierte Impuls (Spec §7 Schritt 3, §29).

"Das sieht nach einem starken Move aus" ist keine Regel. Ein Displacement ist
hier ausschliesslich das UND aus messbaren Bedingungen:

    bullish an Bar i:
        close[i] > open[i]                                   Richtung
        range[i] > range_atr_mult * ATR                      Groesse relativ zur Volatilitaet
        body[i] / range[i] > body_ratio_min                  Ueberzeugung statt Dochtgezappel
        close[i] > high[i-1]        (optional, Default an)   echter Ausbruch
        volume[i] > volume_mult * SMA(volume)  (optional)    Beteiligung

bearish spiegelbildlich.

Volumen als Gate ist per Default AUS. Grund: ob Volumen ueberhaupt verfuegbar
ist, haengt an der Datenquelle. Waere es Pflichtbedingung, wuerde die Strategie
je nach Quelle unterschiedlich handeln - ein direkter Verstoss gegen
"Backtest == Live". Das Ergebnis wird stattdessen als `volume_confirmed`
mitgefuehrt und protokolliert, sodass Phase 4 messen kann, ob es einen Edge bringt.

`strength` in [0,1] wird berechnet und geloggt, ist in Phase 2 aber KEIN Filter.
Sie soll erst dann Entscheidungen beeinflussen, wenn der Backtest zeigt, dass
das etwas bringt.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from tradex.config import DisplacementParams
from tradex.domain.bars import BarSeries
from tradex.domain.enums import Direction


@dataclass(frozen=True, slots=True)
class Displacement:
    """Eine als Impuls qualifizierte Bar."""

    index: int
    ts: int
    direction: Direction
    range: float
    body: float
    body_ratio: float
    atr: float
    range_atr_mult: float
    volume: float
    volume_ratio: float
    """volume / SMA(volume); NaN wenn kein Volumen-Referenzwert vorliegt."""
    volume_confirmed: bool
    strength: float

    @property
    def is_bullish(self) -> bool:
        return self.direction is Direction.BULLISH


class DisplacementDetector:
    """Inkrementelle Displacement-Erkennung fuer einen Timeframe."""

    __slots__ = ("params", "found", "_max_tracked")

    def __init__(self, params: DisplacementParams, max_tracked: int = 200) -> None:
        self.params = params
        self.found: list[Displacement] = []
        self._max_tracked = max_tracked

    def update(
        self, series: BarSeries, index: int, atr_value: float, volume_avg: float
    ) -> Displacement | None:
        """Bar `index` pruefen. Liefert das Displacement oder None."""
        params = self.params
        if index < 1 or not np.isfinite(atr_value) or atr_value <= 0:
            return None
        # An der Kontraktnaht springt der Preis um die Basis - das waere ein
        # Scheinimpuls, kein Marktbewegung.
        if series.roll_boundary[index] or series.roll_boundary[index - 1]:
            return None

        open_ = float(series.open[index])
        high = float(series.high[index])
        low = float(series.low[index])
        close = float(series.close[index])
        bar_range = high - low
        if bar_range <= 0:
            return None

        body = abs(close - open_)
        body_ratio = body / bar_range
        range_mult = bar_range / atr_value

        if range_mult <= params.range_atr_mult:
            return None
        if body_ratio <= params.body_ratio_min:
            return None

        if close > open_:
            direction = Direction.BULLISH
        elif close < open_:
            direction = Direction.BEARISH
        else:
            return None

        if params.require_break_prev_extreme:
            if direction is Direction.BULLISH and not close > float(series.high[index - 1]):
                return None
            if direction is Direction.BEARISH and not close < float(series.low[index - 1]):
                return None

        volume = float(series.volume[index])
        has_volume_reference = bool(np.isfinite(volume_avg)) and volume_avg > 0
        volume_ratio = volume / volume_avg if has_volume_reference else float("nan")
        volume_confirmed = has_volume_reference and volume_ratio > params.volume_mult

        if params.volume_is_gate and not volume_confirmed:
            return None

        displacement = Displacement(
            index=index,
            ts=int(series.ts[index]),
            direction=direction,
            range=bar_range,
            body=body,
            body_ratio=body_ratio,
            atr=float(atr_value),
            range_atr_mult=range_mult,
            volume=volume,
            volume_ratio=volume_ratio,
            volume_confirmed=volume_confirmed,
            strength=self._strength(range_mult, body_ratio, volume_ratio, has_volume_reference),
        )
        self.found.append(displacement)
        if len(self.found) > self._max_tracked:
            del self.found[: len(self.found) - self._max_tracked]
        return displacement

    # ----------------------------------------------------------------- Staerke
    def _strength(
        self, range_mult: float, body_ratio: float, volume_ratio: float, has_volume: bool
    ) -> float:
        """Gewichtete Kennzahl in [0, 1].

        Fehlt der Volumen-Referenzwert, faellt sein Gewicht aus dem Nenner. Sonst
        haetten Instrumente ohne Volumen systematisch niedrigere Werte und waeren
        nicht mit anderen vergleichbar.
        """
        weights = self.params.strength_weights
        range_score = min(range_mult / self.params.strength_range_cap_atr_mult, 1.0)
        body_score = min(body_ratio, 1.0)

        total = weights.range + weights.body
        score = weights.range * range_score + weights.body * body_score
        if has_volume:
            volume_score = min(volume_ratio / self.params.strength_volume_cap_mult, 1.0)
            score += weights.volume * volume_score
            total += weights.volume
        return float(score / total) if total > 0 else 0.0

    # ---------------------------------------------------------------- Abfragen
    def last(self, direction: Direction | None = None) -> Displacement | None:
        for item in reversed(self.found):
            if direction is None or item.direction is direction:
                return item
        return None

    def within(
        self, index: int, lookback_bars: int, direction: Direction | None = None
    ) -> Displacement | None:
        """Juengstes Displacement innerhalb der letzten `lookback_bars` Bars.

        Die Strategie braucht nicht "gab es je einen Impuls", sondern "gab es
        gerade eben einen" - deshalb ist das die eigentlich benutzte Abfrage.
        """
        for item in reversed(self.found):
            if index - item.index > lookback_bars:
                return None
            if direction is None or item.direction is direction:
                return item
        return None
