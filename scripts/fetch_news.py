"""Wirtschaftstermine abrufen und im lokalen Bestand ablegen.

    python scripts/fetch_news.py --source holidays --from 2023-01-01 --to 2027-01-01
    python scripts/fetch_news.py --source forexfactory
    python scripts/fetch_news.py --source fred --from 2023-01-01

Warum ein Skript und kein Aufruf aus der Engine
-----------------------------------------------
Die Engine darf waehrend einer Entscheidung kein I/O machen (Invariante 2) -
sonst haengt das Ergebnis daran, ob gerade Netz da war. Genau wie bei
Kursdaten gilt deshalb: Skript holt, Speicher haelt, Engine liest.

Welche Quelle wofuer
--------------------
    holidays      Boersenfeiertage, reine Rechnung, kein Netz, beliebig weit
                  in Vergangenheit und Zukunft
    forexfactory  laufende Woche, exakte Uhrzeiten, kein Schluessel
                  -> woechentlich laufen lassen, der Bestand waechst
    fred          Historie ab 2000, freier Schluessel (FRED_API_KEY),
                  aber nur der Tag - die Uhrzeit wird ergaenzt und als
                  solche gekennzeichnet

Abgerufene Termine werden ZUSAMMENGEFUEHRT, nie ueberschrieben. Ein bereits
bekannter Termin behaelt seine Fassung - eine gemeldete Uhrzeit wird also nie
nachtraeglich durch eine geratene ersetzt.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tradex.config import get_config
from tradex.news.calendar import NewsCalendar
from tradex.news.events import Impact, TimePrecision
from tradex.news.providers import provider_by_name
from tradex.news.store import NewsStore


def _parse_date(raw: str) -> date:
    return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC).date()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        default="forexfactory",
        help="forexfactory | fred | holidays",
    )
    parser.add_argument("--from", dest="start", help="Startdatum YYYY-MM-DD (Vorgabe: heute)")
    parser.add_argument(
        "--to", dest="end", help="Enddatum YYYY-MM-DD, exklusiv (Vorgabe: in einem Jahr)"
    )
    args = parser.parse_args()

    config = get_config()
    start = _parse_date(args.start) if args.start else datetime.now(UTC).date()
    end = _parse_date(args.end) if args.end else start + timedelta(days=365)
    if end <= start:
        print("Das Enddatum muss nach dem Startdatum liegen.")
        return 1

    api_key = os.environ.get("FRED_API_KEY", "")
    try:
        provider = provider_by_name(args.source, api_key)
    except ValueError as error:
        print(error)
        return 1

    print(f"Quelle {args.source}: {start} bis {end}")
    try:
        events = list(provider.fetch(start, end))
    except Exception as error:  # die Meldung IST das Ergebnis, nicht der Absturz
        print(f"Abruf gescheitert: {error}")
        return 1

    if not events:
        # Kein Fehler, aber auch kein Erfolg: wer das Skript in einer
        # Automatisierung laufen laesst, muss den Unterschied sehen.
        print("Keine Termine im Zeitraum gefunden.")
        return 2

    store = NewsStore(config.path(config.news.store))
    added, total = store.merge(events)
    print(f"  {len(events)} geliefert, {added} neu, {total} im Bestand")
    print(f"  Datei: {store.path}")

    _summarize(store, config)
    return 0


def _summarize(store: NewsStore, config) -> None:
    """Was der Filter aus dem Bestand machen wuerde.

    Wichtiger Teil der Ausgabe: ein Bestand voller Termine, von denen keiner
    die Schwelle erreicht, ergibt einen Filter, der nichts tut - und das sieht
    von aussen genauso aus wie ein Filter, der nichts zu beanstanden hat.
    """
    events = store.read()
    calendar = NewsCalendar(events, config.news)
    by_impact = {level.value: sum(1 for e in events if e.impact is level) for level in Impact}
    by_precision = {
        level.value: sum(1 for e in events if e.precision is level) for level in TimePrecision
    }
    print()
    print(f"  Bestand insgesamt : {len(events)}")
    print(f"  nach Wucht        : {by_impact}")
    print(f"  nach Zeitgenauig. : {by_precision}")
    if events:
        first = datetime.fromtimestamp(events[0].ts / 1e9, tz=UTC)
        last = datetime.fromtimestamp(events[-1].ts / 1e9, tz=UTC)
        print(f"  abgedeckt         : {first:%Y-%m-%d} bis {last:%Y-%m-%d}")
    print(
        f"  Filter (min_impact={config.news.min_impact}, "
        f"countries={list(config.news.countries)}): "
        f"{calendar.considered} relevant, {len(calendar.blackouts)} Sperrfenster"
    )
    if calendar.skipped:
        print(f"  uebersprungen     : {calendar.skipped} (nur Tag bekannt, day_only_policy)")
    if calendar.is_empty:
        print()
        print("  ACHTUNG: kein einziges Sperrfenster. Der Filter wuerde nichts tun.")


if __name__ == "__main__":
    raise SystemExit(main())
