"""Synthetische Demodaten erzeugen.

Zweck: Oberflaeche und Detektoren pruefbar machen, SOLANGE noch keine echte
Historie vorliegt (kein Databento-Key, kein NinjaTrader).

Diese Daten sind KEINE Marktdaten. Sie werden bewusst unter einem eigenen Symbol
(MNQ_DEMO) abgelegt, damit sie nie mit echten Kursen verwechselt werden koennen,
und die Oberflaeche zeigt dafuer eine deutliche Warnung.

Der Generator baut absichtlich die Muster ein, die die Analyse erkennen soll -
Trendphasen, Liquiditaetsaufbau, Sweeps mit Rueckeroberung und Impulskerzen.
Damit laesst sich pruefen, OB die Detektoren anspringen. Er sagt dagegen NICHTS
darueber aus, ob die Strategie einen Edge hat: die Muster sind hineinkonstruiert,
nicht am Markt gemessen. Diese Frage beantwortet erst der Backtest auf echten
Daten in Phase 4.

Aufruf:
    python scripts/generate_demo_data.py --days 60
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tradex.config import get_config, get_instrument
from tradex.data.sessions import SessionCalendar
from tradex.data.store import BarStore
from tradex.domain.bars import BarSeries, to_ns
from tradex.domain.enums import SessionName, Timeframe
from tradex.logging_setup import setup_logging

SYMBOL = "MNQ_DEMO"

#: Relative Aktivitaet je Session. Bildet grob nach, dass die US-Kassahandelszeit
#: deutlich lebhafter ist als die asiatische Nacht - ohne das haetten alle
#: Sessions dieselbe Volatilitaet und die Session-Level waeren bedeutungslos.
SESSION_ACTIVITY: dict[str, float] = {
    SessionName.ASIA.value: 0.45,
    SessionName.LONDON.value: 0.85,
    SessionName.NY_AM.value: 1.60,
    SessionName.NY_PM.value: 1.00,
    SessionName.CLOSED.value: 0.0,
}


def generate(days: int, seed: int, start_price: float) -> BarSeries:
    instrument = get_instrument(SYMBOL)
    calendar = SessionCalendar(instrument)
    rng = np.random.default_rng(seed)

    # Beginn: Sonntag 17:00 CT der gewuenschten Anzahl Tage in der Vergangenheit.
    end = datetime.now(UTC).replace(second=0, microsecond=0)
    start = (end - timedelta(days=days)).replace(hour=22, minute=0)

    total_minutes = days * 24 * 60
    stamps = np.array(
        [to_ns(start + timedelta(minutes=i)) for i in range(total_minutes)], dtype=np.int64
    )
    open_mask = calendar.is_open(stamps)
    sessions = calendar.sessions(stamps)

    series = BarSeries()
    price = start_price
    tick = instrument.tick_size

    # Regimewechsel: laengere Trendphasen mit gelegentlicher Umkehr.
    regime_drift = 0.0
    regime_left = 0

    for i in range(total_minutes):
        if not open_mask[i]:
            continue

        activity = SESSION_ACTIVITY.get(str(sessions[i]), 0.5)
        if activity <= 0:
            continue

        if regime_left <= 0:
            regime_left = int(rng.integers(400, 2000))
            regime_drift = float(rng.normal(0, 0.035))

        regime_left -= 1

        # Gelegentlicher Impuls - erzeugt Displacement und damit auch FVGs.
        impulse = float(rng.normal(0, 9.0)) if rng.random() < 0.004 else 0.0
        noise = float(rng.normal(0, 2.2 * activity))
        close = price + regime_drift + noise + impulse

        wick = abs(float(rng.normal(0, 1.3 * activity))) + tick
        high = max(price, close) + wick * float(rng.random())
        low = min(price, close) - wick * float(rng.random())

        volume = float(max(1, rng.normal(320 * activity, 90 * activity)))
        if abs(impulse) > 0:
            volume *= 2.5

        series.append(
            ts=int(stamps[i]),
            open_=instrument.round_to_tick(price),
            high=instrument.round_to_tick(high),
            low=instrument.round_to_tick(low),
            close=instrument.round_to_tick(close),
            volume=round(volume),
        )
        price = close

    return series


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=60, help="Anzahl Kalendertage (Default 60)")
    parser.add_argument("--seed", type=int, default=20250821, help="Zufallsstartwert")
    parser.add_argument("--start-price", type=float, default=21000.0)
    args = parser.parse_args()

    config = get_config()
    setup_logging(config.app.log_level)

    print(f"Erzeuge synthetische Demodaten fuer {SYMBOL} ueber {args.days} Tage ...")
    series = generate(args.days, args.seed, args.start_price)
    if len(series) == 0:
        print("Keine Bars erzeugt - Zeitraum enthielt keine offenen Marktzeiten.")
        return 1

    store = BarStore(config.path(config.data.parquet_dir))
    store.write(SYMBOL, Timeframe.M1, series)
    coverage = store.coverage(SYMBOL, Timeframe.M1)

    print()
    print("  ACHTUNG: Das sind SYNTHETISCHE Daten, keine Marktdaten.")
    print("  Sie eignen sich, um die Oberflaeche und die Detektoren zu pruefen -")
    print("  NICHT, um Aussagen ueber die Strategie zu treffen.")
    print()
    if coverage:
        print(f"  Bars      : {coverage.bar_count:,}")
        print(f"  Zeitraum  : {coverage.first}  bis  {coverage.last}")
    print(f"  Ablage    : {store.dir_for(SYMBOL, Timeframe.M1)}")
    print()
    print("Naechster Schritt:  python -m tradex.shell")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
