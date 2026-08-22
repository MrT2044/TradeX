"""Der Nachrichtenkalender: sperrt Zeitfenster, sonst nichts.

Was er tut
----------
Aus den Ereignissen im Speicher entstehen Sperrfenster. Faellt ein Zeitpunkt in
eines, gibt es KEINEN neuen Einstieg. Mehr ist es nicht - der Kalender loest
nie einen Trade aus, er verhindert nur welche (Spec Paragraph 13, gleiche Rolle
wie das Handelsfenster).

Was er ausdruecklich NICHT tut
------------------------------
**Er beendet keine Positionen.** Eine offene Position hat einen Stop; den in
einem Sperrfenster zu ignorieren waere das Gegenteil von Risikosenkung. Die
Sperre gilt fuer Einstiege, nie fuer Ausstiege. Deshalb kennt dieses Modul die
Ausfuehrung gar nicht - es kann diesen Fehler nicht machen.

Der teuerste Fehler waere ein stiller Filter
--------------------------------------------
Ein Kalender ohne Daten fuer den geprueften Zeitraum laesst alles durch und
sieht dabei aus wie ein funktionierender Filter. Deshalb kennt er seine eigene
Abdeckung und meldet ausdruecklich, wenn ein Zeitpunkt ausserhalb liegt. Was
dann geschieht, entscheidet die Konfiguration - stillschweigend durchwinken
gehoert nicht zu den Moeglichkeiten.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass

from tradex.config import NewsConfig
from tradex.news.events import Impact, NewsEvent, TimePrecision

_MINUTE_NS = 60_000_000_000
_DAY_NS = 86_400_000_000_000


@dataclass(frozen=True, slots=True)
class Blackout:
    """Ein zusammenhaengendes Sperrfenster."""

    start: int
    end: int
    events: tuple[str, ...]
    impact: Impact

    @property
    def label(self) -> str:
        """Kurzbezeichnung fuer Protokoll und UI."""
        if len(self.events) == 1:
            return self.events[0]
        return f"{self.events[0]} +{len(self.events) - 1}"


class NewsCalendar:
    """Sperrfenster aus einer Ereignismenge. Reine Funktion, kein I/O."""

    __slots__ = ("config", "blackouts", "_starts", "first_ts", "last_ts", "considered", "skipped")

    def __init__(self, events: Sequence[NewsEvent], config: NewsConfig) -> None:
        self.config = config
        # Die Abdeckung richtet sich nach ALLEN Ereignissen im Speicher, nicht
        # nur nach den relevanten: ein Zeitraum, in dem die Quelle nur Termine
        # geringer Wucht kennt, ist abgedeckt und nicht etwa unbekannt.
        self.first_ts = min((e.ts for e in events), default=0)
        self.last_ts = max((e.ts for e in events), default=0)

        minimum = Impact(config.min_impact)
        countries = {c.upper() for c in config.countries}
        relevant = [
            event
            for event in events
            if event.impact.at_least(minimum)
            and (not countries or event.country.upper() in countries)
        ]
        self.considered = len(relevant)

        windows: list[tuple[int, int, NewsEvent]] = []
        skipped = 0
        for event in sorted(relevant):
            window = self._window(event)
            if window is None:
                skipped += 1
                continue
            windows.append((*window, event))
        self.skipped = skipped

        self.blackouts = _merge(windows)
        self._starts = [b.start for b in self.blackouts]

    # ------------------------------------------------------------------ Fenster
    def _window(self, event: NewsEvent) -> tuple[int, int] | None:
        """Sperrfenster eines Ereignisses, oder None wenn es nicht sperrt."""
        params = self.config
        if event.precision is TimePrecision.DAY_ONLY:
            if params.day_only_policy == "ignore":
                return None
            # Nur der Tag ist bekannt: entweder der ganze UTC-Tag oder gar
            # nichts. Ein Fenster von 15 Minuten um Mitternacht waere eine
            # Praezision, die die Quelle nicht hergibt.
            day_start = event.ts - (event.ts % _DAY_NS)
            return day_start, day_start + _DAY_NS

        extra = params.assumed_time_extra_minutes if event.precision is TimePrecision.ASSUMED else 0
        before = (params.block_before_minutes + extra) * _MINUTE_NS
        after = (params.block_after_minutes + extra) * _MINUTE_NS
        return event.ts - before, event.ts + after

    # ------------------------------------------------------------------ Abfrage
    def blackout_at(self, ts: int) -> Blackout | None:
        """Das Sperrfenster, in dem `ts` liegt - oder None."""
        index = bisect_right(self._starts, ts) - 1
        if index < 0:
            return None
        candidate = self.blackouts[index]
        return candidate if ts < candidate.end else None

    def covers(self, ts: int) -> bool:
        """Liegt `ts` im Zeitraum, fuer den ueberhaupt Termine vorliegen?"""
        return bool(self.blackouts or self.first_ts) and self.first_ts <= ts <= self.last_ts

    @property
    def is_empty(self) -> bool:
        return not self.blackouts


def _merge(windows: Sequence[tuple[int, int, NewsEvent]]) -> tuple[Blackout, ...]:
    """Ueberlappende Fenster zu einem zusammenziehen.

    Ohne das lieferte eine Haeufung von Terminen - CPI und Retail Sales zur
    selben Minute - mehrere Fenster fuer denselben Zeitraum, und die Suche
    muesste alle pruefen statt genau eines.
    """
    merged: list[Blackout] = []
    for start, end, event in sorted(windows, key=lambda w: (w[0], w[1])):
        if merged and start <= merged[-1].end:
            last = merged[-1]
            merged[-1] = Blackout(
                start=last.start,
                end=max(last.end, end),
                events=(*last.events, event.name),
                impact=max(last.impact, event.impact, key=lambda i: i.rank),
            )
            continue
        merged.append(Blackout(start=start, end=end, events=(event.name,), impact=event.impact))
    return tuple(merged)
