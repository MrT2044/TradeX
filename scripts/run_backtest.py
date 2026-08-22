"""Backtest ueber den lokalen Datenbestand (Phase 4, Spec §19).

Beantwortet die Frage, fuer die dieses Projekt gebaut wurde: Hat die
Pflichtkette aus §7 einen Edge - oder nicht? Der Bericht ist so aufgebaut, dass
die Antwort "nein" genauso sichtbar wird wie ein "ja".

    python scripts/run_backtest.py --symbol MNQ_PROXY
    python scripts/run_backtest.py --symbol MNQ_PROXY,MES_PROXY --from 2023-01-01
    python scripts/run_backtest.py --symbol MNQ_PROXY --out backtest.json --save

Mehrere Symbole laufen an EINEM Konto: die Bars werden chronologisch
verschraenkt und teilen sich ein Risikobuch. Das ist der einzige Hebel, der die
Trade-Anzahl erhoeht, ohne eine Regel anzufassen.

Was das Skript NICHT tut
------------------------
Es sucht keine besseren Schwellenwerte. Ein Suchlauf ueber Parameter findet
zuverlaessig eine Kombination, die auf der Vergangenheit gut aussieht, und sagt
nichts ueber die Zukunft. Wer Werte aendern will, aendert `config/default.yaml`
und rechnet erneut - die Aenderung steht dann im `config_hash` jedes
gespeicherten Laufs.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tradex.backtest.report import build, render_text, to_dict
from tradex.backtest.runner import run_multi_backtest
from tradex.backtest.store import BacktestStore
from tradex.config import get_config, get_instrument, resolved_config_path
from tradex.data.integrity import check
from tradex.data.sessions import SessionCalendar
from tradex.data.store import BarStore
from tradex.domain.bars import to_ns
from tradex.logging_setup import setup_logging
from tradex.persistence.db import init_database
from tradex.persistence.decision_log import DecisionLog
from tradex.service import STRATEGY_VERSION


def _parse_date(raw: str | None) -> int | None:
    if not raw:
        return None
    return to_ns(datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC))


def _progress(done: int, total: int) -> None:
    share = 100.0 * done / total if total else 100.0
    sys.stderr.write(f"\r  {done:>9,} / {total:,} Bars  ({share:5.1f} %)")
    sys.stderr.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbol",
        default="MNQ_PROXY",
        help="ein Symbol oder mehrere durch Komma getrennt (gemeinsames Konto)",
    )
    parser.add_argument("--from", dest="start", help="Startdatum YYYY-MM-DD (UTC)")
    parser.add_argument("--to", dest="end", help="Enddatum YYYY-MM-DD (UTC), exklusiv")
    parser.add_argument("--out", type=Path, help="Bericht als JSON hierhin schreiben")
    parser.add_argument(
        "--save", action="store_true", help="Lauf in der Datenbank festhalten (Spec §21)"
    )
    parser.add_argument("--notes", default="", help="Notiz zum gespeicherten Lauf")
    parser.add_argument("--max-bars", type=int, default=2_000_000)
    parser.add_argument("--quiet", action="store_true", help="ohne Fortschrittsanzeige")
    args = parser.parse_args()

    config = get_config()
    setup_logging("WARNING")

    symbols = [s.strip().upper() for s in args.symbol.split(",") if s.strip()]
    instruments = {name: get_instrument(name) for name in symbols}
    store = BarStore(config.path(config.data.parquet_dir))
    base_tf = config.data.base_timeframe

    series_by_symbol = {}
    for name in symbols:
        series = store.read(
            name, base_tf, _parse_date(args.start), _parse_date(args.end), limit=args.max_bars
        )
        if len(series) == 0:
            print(f"Keine {base_tf.value}-Daten fuer {name}.")
            print("Erst importieren: scripts/fetch_dukascopy.py oder scripts/generate_demo_data.py")
            return 1
        series_by_symbol[name] = series

        report_integrity = check(
            series, name, base_tf, SessionCalendar(instruments[name]), config.data.min_gap_bars
        )
        if not report_integrity.is_clean:
            print(f"Hinweis Datenqualitaet: {report_integrity.summary()}")

    result = run_multi_backtest(
        instruments, config, series_by_symbol, progress=None if args.quiet else _progress
    )
    if not args.quiet:
        sys.stderr.write("\r" + " " * 48 + "\r")

    report = build(result, config)
    print(render_text(report))

    if args.out:
        args.out.write_text(
            json.dumps(to_dict(report), indent=2, sort_keys=True, default=str), encoding="utf-8"
        )
        print(f"\n  Bericht geschrieben: {args.out}")

    if args.save:
        database = config.path(config.data.database)
        init_database(database)
        # Der Hash MUSS von der tatsaechlich geladenen Datei stammen. Laeuft der
        # Backtest unter TRADEX_CONFIG mit einer Variante, wuerde ein fest
        # verdrahtetes default.yaml den Lauf falsch etikettieren.
        with DecisionLog(database) as log:
            config_hash = log.register_config(resolved_config_path())
        with BacktestStore(database) as backtests:
            run_id = backtests.record(report, config_hash, STRATEGY_VERSION, args.notes)
        print(f"  Lauf gespeichert: id={run_id}  config={config_hash}")

    # Ein Backtest ohne Trades ist kein Erfolg - er ist ein Befund, der eine
    # Antwort verlangt. Deshalb ein von 0 verschiedener Rueckgabewert.
    return 0 if report.overall.trades else 2


if __name__ == "__main__":
    raise SystemExit(main())
