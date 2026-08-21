"""Historische MNQ/NQ-Daten von Databento laden.

KOSTEN (Spec §26)
-----------------
Dieses Skript laedt NIEMALS Daten, ohne vorher den exakten Preis zu zeigen und
nachzufragen. Der Kostenvoranschlag selbst ist kostenlos.

Databento arbeitet bei Historie nach Verbrauch, und Neukonten haben 125 USD
Startguthaben. Mehrere Jahre MNQ 1-Minuten-Daten liegen deutlich darunter -
in der Praxis kostet der Aufbau der Backtest-Historie damit nichts.

Nicht verwendet wird das Live-Abo (~199 USD/Monat). Echtzeitdaten kommen ab
Phase 5 ueber NinjaTrader (CME Level 1 non-professional, ~4 USD/Monat).

VORBEREITUNG
------------
    1. Konto auf databento.com anlegen (125 USD Startguthaben)
    2. API-Key erzeugen
    3. Datei .env im Projektordner anlegen (Vorlage: .env.example):
           DATABENTO_API_KEY=db-...
    4. pip install -e ".[databento]"

Der Key gehoert niemals ins Repository - .env ist in .gitignore.

AUFRUF
------
    python scripts/fetch_databento.py --symbol MNQ --from 2023-01-01 --dry-run
    python scripts/fetch_databento.py --symbol MNQ --from 2023-01-01 --to 2025-01-01
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tradex.config import get_config, get_instrument
from tradex.data.databento_provider import DatabentoProvider
from tradex.data.integrity import check
from tradex.data.provider import ProviderError
from tradex.data.sessions import SessionCalendar
from tradex.data.store import BarStore
from tradex.domain.enums import Timeframe
from tradex.logging_setup import setup_logging

#: Groesse eines Teilabrufs. Jahresweise, damit ein Abbruch nicht den ganzen
#: Download verwirft - bereits geschriebene Monate bleiben erhalten und der
#: Store ist idempotent, ein Wiederholungslauf schadet also nie.
CHUNK_DAYS = 365


def _load_env(project_root: Path) -> None:
    """.env einlesen, falls vorhanden. Bewusst ohne Zusatzabhaengigkeit."""
    env_file = project_root / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _parse_date(raw: str) -> datetime:
    return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC)


def _confirm(question: str) -> bool:
    try:
        return input(f"{question} [j/N] ").strip().lower() in ("j", "ja", "y", "yes")
    except EOFError:
        # Nicht-interaktiver Aufruf: im Zweifel NICHT herunterladen.
        print("Keine Eingabe moeglich - Abbruch. Fuer den nicht-interaktiven Lauf: --yes")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="MNQ", choices=["MNQ", "NQ"])
    parser.add_argument("--from", dest="start", required=True, help="Startdatum YYYY-MM-DD")
    parser.add_argument("--to", dest="end", help="Enddatum YYYY-MM-DD (Default: heute)")
    parser.add_argument(
        "--dry-run", action="store_true", help="nur den Kostenvoranschlag zeigen"
    )
    parser.add_argument("--yes", action="store_true", help="ohne Rueckfrage herunterladen")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    _load_env(project_root)

    config = get_config()
    setup_logging(config.app.log_level)

    instrument = get_instrument(args.symbol)
    start = _parse_date(args.start)
    end = _parse_date(args.end) if args.end else datetime.now(UTC).replace(microsecond=0)
    if end <= start:
        print("Enddatum muss nach dem Startdatum liegen.")
        return 1

    timeframe = Timeframe.M1
    provider = DatabentoProvider()

    # ------------------------------------------------------ Kostenvoranschlag
    chunks: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=CHUNK_DAYS), end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end

    print("=" * 72)
    print(f"  Kostenvoranschlag  -  {instrument.symbol} {timeframe.value}")
    print("=" * 72)

    total_usd = 0.0
    try:
        for chunk_start, chunk_end in chunks:
            estimate = provider.estimate_cost(instrument, timeframe, chunk_start, chunk_end)
            print(estimate.describe())
            print()
            total_usd += estimate.usd
    except ProviderError as exc:
        print(f"Fehler: {exc}")
        return 1

    print("-" * 72)
    print(f"  GESAMT: {total_usd:.4f} USD")
    print("-" * 72)
    print()

    if args.dry_run:
        print("--dry-run: es wurde nichts heruntergeladen und nichts berechnet.")
        return 0

    if not args.yes and not _confirm(f"{total_usd:.4f} USD abrufen und herunterladen?"):
        print("Abgebrochen. Es wurde nichts berechnet.")
        return 0

    # -------------------------------------------------------------- Download
    store = BarStore(config.path(config.data.parquet_dir))
    written = 0
    for index, (chunk_start, chunk_end) in enumerate(chunks, start=1):
        print(f"[{index}/{len(chunks)}] {chunk_start:%Y-%m-%d} .. {chunk_end:%Y-%m-%d} ...")
        series = provider.fetch(instrument, timeframe, chunk_start, chunk_end)
        if len(series) == 0:
            print("    keine Daten in diesem Abschnitt")
            continue
        written += store.write(instrument.symbol, timeframe, series)
        rolls = int(series.roll_boundary.sum())
        print(f"    {len(series):,} Bars, {rolls} Kontraktwechsel")

    # ------------------------------------------------------ Qualitaetspruefung
    print()
    print("Pruefe Datenqualitaet ...")
    stored = store.read(instrument.symbol, timeframe)
    report = check(
        stored,
        instrument.symbol,
        timeframe,
        SessionCalendar(instrument),
        config.data.min_gap_bars,
    )
    print(f"  {report.summary()}")
    if report.gaps:
        print(f"  Groesste Luecken (von {len(report.gaps)}):")
        for gap in sorted(report.gaps, key=lambda g: g.missing_bars, reverse=True)[:5]:
            print(f"    {gap}")
        print()
        print("  Hinweis: Feiertage und Handelsunterbrechungen der CME koennen")
        print("  legitime Luecken sein. Vor dem Backtest gegen den CME-Kalender pruefen.")

    coverage = store.coverage(instrument.symbol, timeframe)
    print()
    print(f"Geschrieben: {written:,} Bars")
    if coverage:
        print(f"Bestand    : {coverage.bar_count:,} Bars, {coverage.first} bis {coverage.last}")
    print()
    print("Naechster Schritt:")
    print(f"  python scripts/run_analysis.py --symbol {instrument.symbol}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
