"""Nachrichtenfilter (Spec Paragraph 14/15).

Aufgabenteilung - dieselbe wie bei Kursdaten:

    providers.py  woher Termine kommen (nur das Abrufskript benutzt das)
    store.py      lokaler Bestand, JSON Lines - die EINZIGE Quelle der Engine
    calendar.py   daraus Sperrfenster, reine Funktion ohne I/O
    events.py     die Domaene: Zeitpunkt, Name, Land, Wucht

Zwei Regeln, die dieses Paket traegt:

1. **Die Engine fragt nie eine API.** Ein HTTP-Aufruf mitten in einer
   Entscheidung waere nicht wiederholbar - Invariante 2 waere hin, und
   Backtest und Live saehen Verschiedenes.
2. **Gesperrt werden nur Einstiege.** Dieses Paket kennt die Ausfuehrung
   nicht und kann deshalb keine Position am Aussteigen hindern.
"""

from __future__ import annotations

from tradex.news.calendar import Blackout, NewsCalendar
from tradex.news.events import Impact, NewsEvent, TimePrecision
from tradex.news.store import NewsStore

__all__ = [
    "Blackout",
    "Impact",
    "NewsCalendar",
    "NewsEvent",
    "NewsStore",
    "TimePrecision",
]
