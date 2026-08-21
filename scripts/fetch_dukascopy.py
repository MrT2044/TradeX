"""Kostenlose Nasdaq-100-Historie von Dukascopy laden.

KOSTEN: keine. Kein Konto, keine Kreditkarte, keine laufenden Gebuehren.

WAS GELADEN WIRD
----------------
Der Nasdaq-100-INDEX als CFD, nicht der MNQ-Future. Das ist eine sehr gute
Naeherung, um zu pruefen, ob die Regelmechanik einen Edge hat - aber es ist
nicht dasselbe Instrument:

    - Der Future notiert mit Auf-/Abschlag zum Index (Basis)
    - Ein Index rollt nicht; der Future wechselt quartalsweise den Kontrakt
    - Das Volumen ist eine Aktivitaetskennzahl, keine gehandelten Kontrakte

Deshalb liegen die Daten unter dem eigenen Symbol MNQ_PROXY, und die
Oberflaeche weist darauf hin. Vor der Freigabe von Echtgeld braucht es echte
MNQ-Daten (NinjaTrader ab Phase 5, ~4 USD/Monat).

Kurse werden auf das MNQ-Tickraster (0,25) gerundet, damit alle tickbasierten
Schwellenwerte auf demselben Raster messen wie spaeter im Livebetrieb.

AUFRUF
------
    python scripts/fetch_dukascopy.py --from 2023-01-01
    python scripts/fetch_dukascopy.py --from 2019-01-01 --to 2026-01-01

Der Abruf laeuft tageweise. Ein Abbruch ist unkritisch: bereits geschriebene
Monate bleiben erhalten, der Speicher ist idempotent, und ein Wiederholungslauf
ueberspringt vorhandene Tage.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tradex.config import get_config, get_instrument
from tradex.data.dukascopy_provider import decode_day, fetch_day, has_data
from tradex.data.integrity import check
from tradex.data.provider import ProviderError
from tradex.data.sessions import SessionCalendar
from tradex.data.store import BarStore
from tradex.domain.bars import NS_PER_SECOND, BarSeries
from tradex.domain.enums import Timeframe
from tradex.logging_setup import setup_logging

SYMBOL = "MNQ_PROXY"

#: Nach so vielen Tagen wird zwischengespeichert. Klein genug, dass ein
#: Abbruch wenig Arbeit kostet, gross genug fuer wenige Schreibvorgaenge.
FLUSH_EVERY_DAYS = 30

#: So viele fehlgeschlagene Tage in Folge gelten als Drosselung. Dann hat
#: Weitermachen keinen Zweck - besser spaeter neu starten.
MAX_FAILED_DAYS = 5


def _parse_date(raw: str) -> datetime:
    return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="start", required=True, help="Startdatum YYYY-MM-DD")
    parser.add_argument("--to", dest="end", help="Enddatum YYYY-MM-DD (Default: heute)")
    parser.add_argument("--symbol", default=SYMBOL)
    parser.add_argument(
        "--force", action="store_true", help="vorhandene Tage erneut laden"
    )
    args = parser.parse_args()

    config = get_config()
    setup_logging("WARNING")

    instrument = get_instrument(args.symbol)
    if not instrument.dukascopy_symbol:
        print(f"{args.symbol} hat kein dukascopy_symbol in config/instruments.yaml.")
        return 1

    start = _parse_date(args.start)
    end = _parse_date(args.end) if args.end else datetime.now(UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    if end <= start:
        print("Enddatum muss nach dem Startdatum liegen.")
        return 1

    store = BarStore(config.path(config.data.parquet_dir))
    tick = instrument.tick_size
    decimals = instrument.price_decimals

    existing = set()
    if not args.force:
        coverage = store.coverage(args.symbol, Timeframe.M1)
        if coverage:
            stamps = store.existing_timestamps(
                args.symbol,
                Timeframe.M1,
                int(start.timestamp() * NS_PER_SECOND),
                int(end.timestamp() * NS_PER_SECOND),
            )
            existing = {int(ts) // (86_400 * NS_PER_SECOND) for ts in stamps}

    total_days = (end - start).days
    print("=" * 70)
    print(f"  Dukascopy -> {args.symbol}   ({instrument.dukascopy_symbol})")
    print("=" * 70)
    print(f"  Zeitraum : {start:%Y-%m-%d} bis {end:%Y-%m-%d}  ({total_days} Tage)")
    print("  Quelle   : Nasdaq-100 Index CFD - NICHT der MNQ-Future")
    print("  Kosten   : keine")
    if existing:
        print(f"  Vorhanden: {len(existing)} Tage werden uebersprungen")
    print()

    series = BarSeries()
    written = 0
    loaded_days = 0
    empty_days = 0
    failed_days: list[tuple[datetime, str]] = []
    day = start

    try:
        while day < end:
            day_key = int(day.timestamp()) // 86_400
            if day_key in existing or not has_data(day):
                # Samstage gar nicht erst anfragen - dort ruht der Handel.
                day += timedelta(days=1)
                continue

            try:
                payload = fetch_day(instrument.dukascopy_symbol, day)
                rows = decode_day(payload, day)
            except ProviderError as exc:
                # Ein einzelner unerreichbarer Tag darf nicht Stunden an Arbeit
                # verwerfen. Er wird vermerkt und uebersprungen; ein erneuter
                # Lauf holt genau die fehlenden Tage nach, weil vorhandene
                # uebersprungen werden.
                failed_days.append((day, str(exc)))
                if len(failed_days) >= MAX_FAILED_DAYS:
                    print(
                        f"\n  Abbruch: {len(failed_days)} Tage in Folge nicht erreichbar."
                    )
                    print("  Vermutlich eine Drosselung. Spaeter erneut starten -")
                    print("  bereits geladene Tage werden dann uebersprungen.")
                    break
                day += timedelta(days=1)
                continue

            if rows.size == 0:
                empty_days += 1
            else:
                for row in rows:
                    series.append(
                        ts=int(row["ts"]),
                        open_=round(round(float(row["open"]) / tick) * tick, decimals),
                        high=round(round(float(row["high"]) / tick) * tick, decimals),
                        low=round(round(float(row["low"]) / tick) * tick, decimals),
                        close=round(round(float(row["close"]) / tick) * tick, decimals),
                        volume=float(row["volume"]),
                    )
                loaded_days += 1

            done = (day - start).days + 1
            if done % 10 == 0 or day + timedelta(days=1) >= end:
                pct = 100.0 * done / max(total_days, 1)
                print(
                    f"  [{pct:5.1f} %] {day:%Y-%m-%d}  "
                    f"Handelstage {loaded_days}  Bars {len(series):,}",
                    flush=True,
                )

            # Regelmaessig sichern, damit ein Abbruch wenig Arbeit kostet.
            if loaded_days and loaded_days % FLUSH_EVERY_DAYS == 0 and len(series):
                written += store.write(args.symbol, Timeframe.M1, series)
                series = BarSeries()

            day += timedelta(days=1)

    except KeyboardInterrupt:
        print("\n  Abgebrochen - bereits geladene Daten werden gesichert ...")

    if len(series):
        written += store.write(args.symbol, Timeframe.M1, series)

    print()
    print(f"  Geschrieben     : {written:,} Bars")
    print(f"  Handelstage     : {loaded_days}")
    print(f"  Tage ohne Daten : {empty_days} (Wochenenden und Feiertage)")
    if failed_days:
        print(f"  Nicht erreicht  : {len(failed_days)} Tage")
        for failed_day, message in failed_days[:3]:
            print(f"    {failed_day:%Y-%m-%d}: {message[:80]}")
        print("  Einfach erneut starten - vorhandene Tage werden uebersprungen.")

    coverage = store.coverage(args.symbol, Timeframe.M1)
    if coverage:
        print(f"  Bestand gesamt  : {coverage.bar_count:,} Bars")
        print(f"                    {coverage.first} bis {coverage.last}")

        stored = store.read(args.symbol, Timeframe.M1)
        report = check(
            stored,
            args.symbol,
            Timeframe.M1,
            SessionCalendar(instrument),
            config.data.min_gap_bars,
        )
        print()
        print(f"  Datenqualitaet  : {report.summary()}")
        if report.gaps:
            print("  Groesste Luecken:")
            for gap in sorted(report.gaps, key=lambda g: g.missing_bars, reverse=True)[:5]:
                print(f"    {gap}")
            print()
            print("  Hinweis: Feiertage und Handelsunterbrechungen sind legitime Luecken.")

    print()
    print("Naechster Schritt:")
    print(f"  python scripts/run_analysis.py --symbol {args.symbol}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
