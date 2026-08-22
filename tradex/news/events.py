"""Was ein Nachrichtenereignis fuer die Engine ist (Spec Paragraph 14/15).

Bewusst eine winzige Domaene: Zeitpunkt, Name, Land, Wucht. Alles, was eine
Quelle sonst noch liefert - Prognose, Vorwert, tatsaechlicher Wert - bleibt
draussen, weil die Engine daraus nichts ableiten darf. Ein Filter, der den
INHALT einer Zahl auswertet, waere eine Handelsstrategie und keine Sperre.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Impact(StrEnum):
    """Wucht eines Ereignisses. Die Reihenfolge ist Teil der Bedeutung."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def rank(self) -> int:
        return _RANKS[self]

    def at_least(self, minimum: Impact) -> bool:
        return self.rank >= minimum.rank

    @classmethod
    def parse(cls, raw: str) -> Impact:
        """Aus der Schreibweise einer Quelle. Unbekanntes wird NICHT geraten."""
        key = raw.strip().lower()
        if key in _ALIASES:
            return _ALIASES[key]
        raise ValueError(f"Unbekannte Impact-Stufe: {raw!r}")


_RANKS: dict[Impact, int] = {Impact.LOW: 0, Impact.MEDIUM: 1, Impact.HIGH: 2}

#: Schreibweisen der Quellen. "holiday" zaehlt als hoch: an einem Feiertag ist
#: der Markt duenn, und genau das ist der Zustand, den der Filter meiden soll.
_ALIASES: dict[str, Impact] = {
    "low": Impact.LOW,
    "medium": Impact.MEDIUM,
    "moderate": Impact.MEDIUM,
    "high": Impact.HIGH,
    "holiday": Impact.HIGH,
    "1": Impact.LOW,
    "2": Impact.MEDIUM,
    "3": Impact.HIGH,
}


class TimePrecision(StrEnum):
    """Woher der Zeitpunkt stammt - und wie sehr man ihm trauen darf.

    Der Unterschied ist keine Feinheit. Eine Quelle, die nur den TAG einer
    Veroeffentlichung kennt (FRED liefert historisch genau das), erlaubt kein
    Fenster von 15 Minuten. Wer beides gleich behandelt, sperrt entweder zu
    wenig oder den ganzen Tag - und merkt es nicht.
    """

    EXACT = "exact"
    """Uhrzeit von der Quelle gemeldet."""
    ASSUMED = "assumed"
    """Uhrzeit aus der ueblichen Veroeffentlichungszeit ergaenzt."""
    DAY_ONLY = "day_only"
    """Nur der Tag ist bekannt."""


@dataclass(frozen=True, slots=True, order=True)
class NewsEvent:
    """Ein Termin, an dem der Markt springen kann."""

    ts: int
    """Epoch-Nanosekunden UTC - dieselbe Konvention wie bei Bars."""
    name: str
    country: str
    impact: Impact
    source: str
    """Welche Quelle das geliefert hat. Steht im Speicher, damit sich zwei
    Quellen spaeter gegeneinander pruefen lassen."""
    precision: TimePrecision = TimePrecision.EXACT

    @property
    def key(self) -> tuple[int, str, str]:
        """Kennung fuer die Dublettenpruefung.

        Ohne Quelle: dasselbe Ereignis aus zwei Quellen ist EIN Ereignis, sonst
        wuerde ein zweiter Abruf jede Sperre verdoppeln.
        """
        return (self.ts, self.country.upper(), self.name.strip().lower())
