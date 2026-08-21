"""Headless-Analyse: Kennzahlen ausgeben und einen Snapshot schreiben.

Zwei Zwecke:

1. Schwellenwerte pruefen. Die Ausgabe zeigt, WIE OFT jeder Detektor auf einem
   Zeitraum anspringt. Ein Detektor, der nie feuert, ist genauso unbrauchbar wie
   einer, der auf jeder zweiten Bar feuert - beides sieht man erst an den Zahlen.

2. Regressionsbasis. Der geschriebene JSON-Snapshot wird eingecheckt und dient
   allen spaeteren Phasen als Vergleichspunkt: aendert eine Umbaumassnahme
   ungewollt das Analyseergebnis, faellt es im Diff auf.

Aufruf:
    python scripts/run_analysis.py --symbol MNQ_DEMO
    python scripts/run_analysis.py --symbol MNQ --from 2024-01-01 --to 2024-04-01 --out snap.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tradex.analysis.context import MarketContext
from tradex.api.schemas import ContextSnapshotDto
from tradex.config import get_config, get_instrument
from tradex.data.integrity import check
from tradex.data.sessions import SessionCalendar
from tradex.data.store import BarStore
from tradex.domain.bars import to_ns
from tradex.logging_setup import setup_logging


def _parse_date(raw: str | None) -> int | None:
    if not raw:
        return None
    return to_ns(datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="MNQ_DEMO")
    parser.add_argument("--from", dest="start", help="Startdatum YYYY-MM-DD (UTC)")
    parser.add_argument("--to", dest="end", help="Enddatum YYYY-MM-DD (UTC), exklusiv")
    parser.add_argument("--out", type=Path, help="Snapshot als JSON hierhin schreiben")
    parser.add_argument("--max-bars", type=int, default=400_000)
    args = parser.parse_args()

    config = get_config()
    setup_logging("WARNING")

    instrument = get_instrument(args.symbol)
    store = BarStore(config.path(config.data.parquet_dir))
    base_tf = config.data.base_timeframe

    series = store.read(
        args.symbol, base_tf, _parse_date(args.start), _parse_date(args.end), limit=args.max_bars
    )
    if len(series) == 0:
        print(f"Keine {base_tf.value}-Daten fuer {args.symbol}.")
        print("Erst importieren: scripts/fetch_databento.py oder scripts/generate_demo_data.py")
        return 1

    report = check(
        series, args.symbol, base_tf, SessionCalendar(instrument), config.data.min_gap_bars
    )
    context = MarketContext(args.symbol, instrument, config)
    updates = context.feed(series)

    # ------------------------------------------------------------------ Ausgabe
    print("=" * 72)
    print(f"  {args.symbol}  -  {instrument.name}")
    print("=" * 72)
    print(f"Basis-Bars   : {len(series):,} ({base_tf.value})")
    print(f"Zeitraum     : {report.summary()}")
    print()

    per_timeframe: dict[str, Counter[str]] = {}
    for update in updates:
        counter = per_timeframe.setdefault(update.timeframe.value, Counter())
        counter["bars"] += 1
        counter["swings"] += len(update.new_swings)
        counter["fvgs"] += len(update.new_fvgs)
        counter["sweeps"] += len(update.sweeps)
        if update.displacement:
            counter["displacements"] += 1
            if update.displacement.volume_confirmed:
                counter["displacement_volume_ok"] += 1
        if update.structure_event:
            counter["bos" if not update.structure_event.is_mss else "mss"] += 1

    header = f"{'TF':>4} {'Bars':>7} {'Swings':>7} {'FVG':>6} {'Displ':>6} {'BOS':>5} {'MSS':>5} {'Sweeps':>7}"
    print(header)
    print("-" * len(header))
    order = [tf.value for tf in config.timeframes.all]
    for tf in order:
        c = per_timeframe.get(tf, Counter())
        print(
            f"{tf:>4} {c['bars']:>7,} {c['swings']:>7,} {c['fvgs']:>6,} "
            f"{c['displacements']:>6,} {c['bos']:>5,} {c['mss']:>5,} {c['sweeps']:>7,}"
        )
    print()

    print("Anteil Bars mit Signal (Plausibilitaet der Schwellenwerte):")
    for tf in order:
        c = per_timeframe.get(tf, Counter())
        if not c["bars"]:
            continue
        share = 100.0 * c["displacements"] / c["bars"]
        fvg_share = 100.0 * c["fvgs"] / c["bars"]
        print(f"  {tf:>4}  Displacement {share:5.2f} %   FVG {fvg_share:5.2f} %")
    print()

    snapshot = context.snapshot()
    print(f"HTF Bias     : {snapshot.bias.bias.value.upper()}  (Score {snapshot.bias.score:+.3f})")
    for item in snapshot.bias.per_timeframe:
        print(
            f"   {item.timeframe.value:>3}: Struktur {item.structure_state.value:<8} "
            f"Score {item.score:+.3f}  "
            f"(S {item.structure_score:+.2f} / F {item.fvg_score:+.2f} / L {item.liquidity_score:+.2f})"
        )
    print()

    entry_tf = config.timeframes.entry[0]
    entry = snapshot.timeframe(entry_tf)
    if entry:
        print(f"Stand {entry_tf.value}:")
        print(f"   offene FVGs          : {len(entry.active_fvgs)}")
        print(f"   unberuehrte Level    : {len(entry.untapped_pools)}")
        print(f"   Struktur             : {entry.structure_state.value}")
        print(f"   letzter MSS          : {entry.last_mss.type.value if entry.last_mss else '-'}")

    if args.out:
        payload = json.loads(ContextSnapshotDto.of(snapshot).model_dump_json())
        args.out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print()
        print(f"Snapshot geschrieben: {args.out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
