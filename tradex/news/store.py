"""Lokaler Ereignisspeicher - die einzige Quelle, die die Engine kennt.

Warum die Engine nie direkt eine API fragt
------------------------------------------
Architektur-Invariante 2: Detektoren und Filter sind reine Funktionen aus
(gesehene Bars, Parameter). Kein I/O, keine Uhr, keine Zufallszahlen. Ein
HTTP-Aufruf mitten in der Entscheidung verletzt das dreifach - er kann
scheitern, er liefert je nach Zeitpunkt etwas anderes, und er ist nicht
wiederholbar.

Deshalb dieselbe Aufteilung wie bei Kursdaten: ein Skript holt Termine ab und
legt sie hier ab, die Engine liest ausschliesslich diese Datei. Backtest und
Live sehen damit denselben Datenbestand - was Spec Paragraph 29 verlangt.

Format: JSON Lines, eine Zeile je Ereignis, nach Zeit sortiert. Kein Parquet:
es sind einige tausend Zeilen, und eine Datei, die man mit dem Editor
oeffnen und von Hand korrigieren kann, ist hier mehr wert als Kompression.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from tradex.news.events import Impact, NewsEvent, TimePrecision


class NewsStore:
    """Liest und schreibt Ereignisse als JSON Lines."""

    def __init__(self, path: Path) -> None:
        self.path = path

    # -------------------------------------------------------------------- Lesen
    def read(self) -> tuple[NewsEvent, ...]:
        """Alle Ereignisse, nach Zeit sortiert. Fehlende Datei = leer."""
        if not self.path.exists():
            return ()
        events: list[NewsEvent] = []
        for number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                events.append(_from_json(json.loads(line)))
            except (ValueError, KeyError) as error:
                # Eine kaputte Zeile darf den Betrieb nicht anhalten, aber sie
                # darf auch nicht still verschwinden.
                raise ValueError(f"{self.path}:{number} unlesbar: {error}") from error
        return tuple(sorted(events))

    # ------------------------------------------------------------------ Schreiben
    def merge(self, events: Iterable[NewsEvent]) -> tuple[int, int]:
        """Neue Ereignisse dazulegen. Liefert (neu, insgesamt).

        Zusammenfuehren statt ueberschreiben: die kostenlose Kalenderquelle
        liefert immer nur die laufende Woche. Der Bestand entsteht dadurch,
        dass wiederholt abgerufen und angehaengt wird - ein Ueberschreiben
        wuerde jede Woche die Historie loeschen.
        """
        known = {event.key: event for event in self.read()}
        before = len(known)
        for event in events:
            # Erster Eintrag gewinnt: ein spaeterer Abruf mit gerateter Uhrzeit
            # darf eine gemeldete Uhrzeit nicht ueberschreiben.
            known.setdefault(event.key, event)
        merged = sorted(known.values())

        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = "\n".join(json.dumps(_to_json(e), ensure_ascii=False) for e in merged)
        self.path.write_text(payload + "\n" if payload else "", encoding="utf-8")
        return len(merged) - before, len(merged)


def _to_json(event: NewsEvent) -> dict[str, object]:
    return {
        "ts": event.ts,
        "name": event.name,
        "country": event.country,
        "impact": event.impact.value,
        "source": event.source,
        "precision": event.precision.value,
    }


def _from_json(raw: dict[str, object]) -> NewsEvent:
    return NewsEvent(
        ts=int(raw["ts"]),  # type: ignore[arg-type]
        name=str(raw["name"]),
        country=str(raw["country"]),
        impact=Impact(str(raw["impact"])),
        source=str(raw.get("source", "unbekannt")),
        precision=TimePrecision(str(raw.get("precision", TimePrecision.EXACT.value))),
    )
