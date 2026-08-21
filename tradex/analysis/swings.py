"""Swing Highs und Swing Lows - die Grundlage von Struktur und Liquiditaet.

Definition (Staerke n)
----------------------
Bar i ist ein Swing High, wenn
    high[i] >  high[j]  fuer alle j in [i-n, i-1]     (links strikt groesser)
    high[i] >= high[j]  fuer alle j in [i+1, i+n]     (rechts groesser oder gleich)

Warum links strikt und rechts nicht
-----------------------------------
Bei mehreren exakt gleich hohen Kerzen (einem Plateau) wuerde eine beidseitig
nicht-strikte Regel jede davon als Swing melden, eine beidseitig strikte gar
keine. Die asymmetrische Variante liefert genau EINEN Swing pro Plateau, naemlich
den ersten. Das ist eine bewusste Festlegung, damit dieselben Daten immer
dieselben Swings ergeben.

Bestaetigungsverzoegerung
------------------------
Ein Swing bei Index i steht erst fest, wenn n weitere Bars geschlossen sind. Er
wird deshalb mit `confirmed_at_index = i + n` gefuehrt, und alle nachgelagerten
Detektoren duerfen ihn erst ab diesem Index benutzen. Genau das verhindert
Look-ahead: im Live-Betrieb weiss man an Index i schlicht noch nicht, dass dort
ein Hoch war.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from tradex.domain.bars import BarSeries
from tradex.domain.enums import SwingType


@dataclass(frozen=True, slots=True)
class Swing:
    """Ein bestaetigter Swing-Punkt."""

    index: int
    """Index der Bar, die das Extrem gebildet hat."""
    ts: int
    price: float
    type: SwingType
    strength: int
    confirmed_at_index: int
    """Index, ab dem dieser Swing bekannt sein DARF (= index + strength)."""

    @property
    def is_high(self) -> bool:
        return self.type is SwingType.HIGH

    @property
    def confirmation_lag(self) -> int:
        return self.confirmed_at_index - self.index


def _is_swing_high(high: np.ndarray, i: int, n: int) -> bool:
    pivot = high[i]
    for k in range(1, n + 1):
        if not pivot > high[i - k]:
            return False
        if not pivot >= high[i + k]:
            return False
    return True


def _is_swing_low(low: np.ndarray, i: int, n: int) -> bool:
    pivot = low[i]
    for k in range(1, n + 1):
        if not pivot < low[i - k]:
            return False
        if not pivot <= low[i + k]:
            return False
    return True


def detect_swings(series: BarSeries, strength: int) -> tuple[Swing, ...]:
    """Alle Swings einer Serie in einem Durchgang (Referenzimplementierung).

    Wird in Tests gegen `SwingDetector` gestellt und fuer die einmalige
    historische Vorberechnung benutzt.
    """
    if strength < 1:
        raise ValueError("Swing-Staerke muss >= 1 sein")
    n = len(series)
    if n < 2 * strength + 1:
        return ()

    high, low, ts = series.high, series.low, series.ts
    swings: list[Swing] = []
    for i in range(strength, n - strength):
        if _is_swing_high(high, i, strength):
            swings.append(
                Swing(i, int(ts[i]), float(high[i]), SwingType.HIGH, strength, i + strength)
            )
        if _is_swing_low(low, i, strength):
            swings.append(
                Swing(i, int(ts[i]), float(low[i]), SwingType.LOW, strength, i + strength)
            )
    swings.sort(key=lambda s: (s.confirmed_at_index, s.index, s.type.value))
    return tuple(swings)


class SwingDetector:
    """Inkrementelle Swing-Erkennung fuer den Streaming-Pfad.

    Zustandsbehaftet, aber deterministisch: der Zustand ist eine reine Funktion
    der bisher gesehenen Bars und der Parameter. Kein I/O, keine Uhr.
    """

    __slots__ = ("strength", "max_tracked", "highs", "lows", "_last_index")

    def __init__(self, strength: int, max_tracked: int = 300) -> None:
        if strength < 1:
            raise ValueError("Swing-Staerke muss >= 1 sein")
        self.strength = strength
        self.max_tracked = max_tracked
        self.highs: list[Swing] = []
        self.lows: list[Swing] = []
        self._last_index = -1

    def update(self, series: BarSeries, index: int) -> list[Swing]:
        """Nach dem Schliessen von Bar `index` pruefen, ob ein Swing bestaetigt wurde.

        Kandidat ist immer Bar `index - strength`: erst dort liegen beidseitig
        `strength` Bars vor.
        """
        if index <= self._last_index:
            raise ValueError(f"Index muss steigen (bekam {index} nach {self._last_index})")
        self._last_index = index

        n = self.strength
        candidate = index - n
        if candidate < n:
            return []

        found: list[Swing] = []
        high, low, ts = series.high, series.low, series.ts
        if _is_swing_high(high, candidate, n):
            swing = Swing(
                candidate, int(ts[candidate]), float(high[candidate]), SwingType.HIGH, n, index
            )
            self.highs.append(swing)
            found.append(swing)
        if _is_swing_low(low, candidate, n):
            swing = Swing(
                candidate, int(ts[candidate]), float(low[candidate]), SwingType.LOW, n, index
            )
            self.lows.append(swing)
            found.append(swing)

        if len(self.highs) > self.max_tracked:
            del self.highs[: len(self.highs) - self.max_tracked]
        if len(self.lows) > self.max_tracked:
            del self.lows[: len(self.lows) - self.max_tracked]
        return found

    # ------------------------------------------------------------------ Zugriff
    @property
    def last_high(self) -> Swing | None:
        return self.highs[-1] if self.highs else None

    @property
    def last_low(self) -> Swing | None:
        return self.lows[-1] if self.lows else None

    def all_swings(self) -> list[Swing]:
        return sorted([*self.highs, *self.lows], key=lambda s: (s.index, s.type.value))

    def recent_highs(self, count: int) -> list[Swing]:
        return self.highs[-count:] if count > 0 else []

    def recent_lows(self, count: int) -> list[Swing]:
        return self.lows[-count:] if count > 0 else []
