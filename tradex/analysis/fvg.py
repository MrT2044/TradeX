"""Fair Value Gaps (Imbalances).

Definition ueber drei aufeinanderfolgende Bars (i-2, i-1, i)
------------------------------------------------------------
    bullish :  low[i]  > high[i-2]      Zone = [ high[i-2] , low[i]  ]
    bearish :  high[i] < low[i-2]       Zone = [ high[i]   , low[i-2] ]

Die mittlere Bar ist der Impuls; die Zone ist der Preisbereich, den der Markt
uebersprungen hat.

Gueltigkeitsfilter
------------------
Eine Luecke zaehlt nur, wenn sie beides erfuellt:
    Groesse >= min_size_ticks                (absolut, gegen Rauschen)
    Groesse >= min_atr_mult * ATR            (relativ, gegen Regimewechsel)

Der absolute Filter allein wuerde in ruhigen Phasen zu viele Zonen liefern, der
relative allein in extrem ruhigen Phasen Zonen von wenigen Ticks durchlassen.

Ist der ATR noch NaN (Aufwaermphase), wird die Luecke VERWORFEN. Nicht
validierbar heisst nicht gueltig - das ist die sichere Richtung.

Rollgrenzen
-----------
An der Kontraktnaht springt der Preis um die Basis zwischen altem und neuem
Kontrakt. Das sieht aus wie eine riesige Imbalance, ist aber ein Buchungsartefakt.
Fenster mit `roll_boundary` werden deshalb uebersprungen.

Lebenszyklus
------------
    OPEN      Preis war seit Entstehung nie in der Zone
    TOUCHED   Preis hat die Zone beruehrt
    MITIGATED Zone zu >= mitigation_threshold durchhandelt (Default 50 %)
    EXPIRED   max_age_bars ohne Mitigation vergangen
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from tradex.config import FvgParams
from tradex.data.rolls import spans_roll
from tradex.domain.bars import BarSeries
from tradex.domain.enums import Direction, FvgState


@dataclass(slots=True)
class Fvg:
    """Eine Fair-Value-Gap-Zone samt Lebenszyklus."""

    id: int
    direction: Direction
    created_index: int
    """Index der DRITTEN Bar - erst dort ist die Luecke sichtbar."""
    created_ts: int
    bottom: float
    top: float
    size_ticks: float
    atr_at_creation: float
    state: FvgState = FvgState.OPEN
    touched_index: int | None = None
    mitigated_index: int | None = None
    expired_index: int | None = None
    # Zeitstempel des Endes. Das Chart zeichnet erledigte Zonen nur bis hierhin -
    # eine mitigierte Zone bis zum rechten Rand weiterzuziehen wuerde suggerieren,
    # dass sie noch relevant ist, und das Bild mit Altlasten zustellen.
    closed_ts: int | None = None
    max_fill: float = 0.0
    """Groesster bisher erreichter Fuellgrad in [0, 1]."""
    meta: dict[str, float] = field(default_factory=dict)

    @property
    def size(self) -> float:
        return self.top - self.bottom

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2.0

    @property
    def is_active(self) -> bool:
        """Zone ist noch handelsrelevant (Spec §7 Schritt 5: Retracement moeglich)."""
        return self.state in (FvgState.OPEN, FvgState.TOUCHED)

    @property
    def entry_edge(self) -> float:
        """Die Kante, an der der Preis in die Zone eintritt.

        Bullish faellt der Preis von oben herein -> obere Kante.
        Bearish steigt er von unten herein -> untere Kante.
        """
        return self.top if self.direction is Direction.BULLISH else self.bottom

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top

    def fill_fraction(self, price: float) -> float:
        """Wie weit `price` die Zone durchhandelt hat, in [0, 1]."""
        size = self.size
        if size <= 0:
            return 1.0
        raw = (
            (self.top - price) / size
            if self.direction is Direction.BULLISH
            else (price - self.bottom) / size
        )
        return float(min(max(raw, 0.0), 1.0))

    def mitigation_price(self, threshold: float) -> float:
        """Kurs, ab dem die Zone als mitigiert gilt."""
        size = self.size
        return (
            self.top - threshold * size
            if self.direction is Direction.BULLISH
            else self.bottom + threshold * size
        )


class FvgTracker:
    """Inkrementelle FVG-Erkennung und -Verwaltung fuer einen Timeframe."""

    __slots__ = ("params", "tick_size", "zones", "_next_id")

    def __init__(self, params: FvgParams, tick_size: float) -> None:
        self.params = params
        self.tick_size = tick_size
        self.zones: list[Fvg] = []
        self._next_id = 1

    # ------------------------------------------------------------------- API
    def update(self, series: BarSeries, index: int, atr_value: float) -> list[Fvg]:
        """Bar `index` verarbeiten. Liefert die in dieser Bar NEU entstandenen Zonen.

        Reihenfolge ist wichtig: erst bestehende Zonen gegen die neue Bar
        fortschreiben, dann neue erkennen. Eine gerade entstandene Zone kann von
        der Bar, die sie erzeugt hat, nicht bereits mitigiert werden.
        """
        self._advance_existing(series, index)
        created = self._detect(series, index, atr_value)
        self._prune()
        return created

    # ------------------------------------------------------------ Lebenszyklus
    def _advance_existing(self, series: BarSeries, index: int) -> None:
        high = float(series.high[index])
        low = float(series.low[index])
        close = float(series.close[index])
        threshold = self.params.mitigation_threshold
        use_close = self.params.mitigation_on == "close"

        for zone in self.zones:
            if not zone.is_active:
                continue
            if index <= zone.created_index:
                continue

            overlaps = low <= zone.top and high >= zone.bottom
            if overlaps and zone.state is FvgState.OPEN:
                zone.state = FvgState.TOUCHED
                zone.touched_index = index

            probe = close if use_close else (low if zone.direction is Direction.BULLISH else high)
            zone.max_fill = max(zone.max_fill, zone.fill_fraction(probe))

            if zone.max_fill >= threshold:
                zone.state = FvgState.MITIGATED
                zone.mitigated_index = index
                zone.closed_ts = int(series.ts[index])
                if zone.touched_index is None:
                    zone.touched_index = index
            elif index - zone.created_index > self.params.max_age_bars:
                # Ablauf wird NACH der Mitigation geprueft: faellt beides auf
                # dieselbe Bar, ist "mitigiert" die aussagekraeftigere Information.
                zone.state = FvgState.EXPIRED
                zone.expired_index = index
                zone.closed_ts = int(series.ts[index])

    # ---------------------------------------------------------------- Erkennung
    def _detect(self, series: BarSeries, index: int, atr_value: float) -> list[Fvg]:
        if index < 2:
            return []
        if self.params.skip_roll_boundary and spans_roll(series.roll_boundary, index - 2, index):
            return []
        # Ohne belastbaren ATR gibt es keinen relativen Groessenfilter - dann
        # wird die Zone verworfen statt ungeprueft akzeptiert.
        if not np.isfinite(atr_value):
            return []

        created: list[Fvg] = []
        first_high = float(series.high[index - 2])
        first_low = float(series.low[index - 2])
        last_high = float(series.high[index])
        last_low = float(series.low[index])

        if last_low > first_high:
            zone = self._make(index, series, Direction.BULLISH, first_high, last_low, atr_value)
            if zone is not None:
                created.append(zone)
        elif last_high < first_low:
            zone = self._make(index, series, Direction.BEARISH, last_high, first_low, atr_value)
            if zone is not None:
                created.append(zone)

        self.zones.extend(created)
        return created

    def _make(
        self,
        index: int,
        series: BarSeries,
        direction: Direction,
        bottom: float,
        top: float,
        atr_value: float,
    ) -> Fvg | None:
        size = top - bottom
        size_ticks = size / self.tick_size
        if size_ticks < self.params.min_size_ticks:
            return None
        if size < self.params.min_atr_mult * atr_value:
            return None

        zone = Fvg(
            id=self._next_id,
            direction=direction,
            created_index=index,
            created_ts=int(series.ts[index]),
            bottom=bottom,
            top=top,
            size_ticks=size_ticks,
            atr_at_creation=float(atr_value),
        )
        self._next_id += 1
        return zone

    # -------------------------------------------------------------------- Pflege
    def _prune(self) -> None:
        if len(self.zones) <= self.params.max_tracked:
            return
        # Aktive Zonen immer behalten; nur die aeltesten erledigten verwerfen.
        active = [z for z in self.zones if z.is_active]
        done = [z for z in self.zones if not z.is_active]
        keep_done = max(self.params.max_tracked - len(active), 0)
        retained = active + (done[-keep_done:] if keep_done else [])
        self.zones = sorted(retained, key=lambda z: z.created_index)

    # ------------------------------------------------------------------ Abfragen
    def active(self, direction: Direction | None = None) -> list[Fvg]:
        return [
            z
            for z in self.zones
            if z.is_active and (direction is None or z.direction is direction)
        ]

    def nearest_active(self, price: float, direction: Direction | None = None) -> Fvg | None:
        """Naechstgelegene aktive Zone, gemessen an ihrer Eintrittskante."""
        candidates = self.active(direction)
        if not candidates:
            return None
        return min(candidates, key=lambda z: abs(z.entry_edge - price))

    def containing(self, price: float, direction: Direction | None = None) -> list[Fvg]:
        """Aktive Zonen, in denen der Preis gerade steht (Spec §7 Schritt 5)."""
        return [z for z in self.active(direction) if z.contains(price)]
