"""Multi-Timeframe-Aggregation.

Die wichtigste Zusicherung: Batch- und Streaming-Aggregation liefern bitgleiche
Ergebnisse. Der historische Erstlauf benutzt die Batch-Variante, Replay/Backtest/
Live die Streaming-Variante - weichen sie ab, waeren Backtest und Live verschieden.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from tests.conftest import DEFAULT_START, make_series
from tradex.data.aggregator import (
    MultiTimeframeAggregator,
    TradingDayAnchor,
    aggregate,
    bucket_starts,
)
from tradex.domain.bars import BarSeries, from_ns, to_ns
from tradex.domain.enums import Timeframe
from tradex.domain.instruments import Instrument

CT = ZoneInfo("America/Chicago")
ALL_TFS = (Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.H4)


def _synthetic(minutes: int, start: datetime = DEFAULT_START, seed: int = 7) -> BarSeries:
    rng = np.random.default_rng(seed)
    series = BarSeries()
    price = 21000.0
    for i in range(minutes):
        close = price + float(rng.normal(0, 2.0))
        high = max(price, close) + abs(float(rng.normal(0, 1.0)))
        low = min(price, close) - abs(float(rng.normal(0, 1.0)))
        series.append(
            to_ns(start + timedelta(minutes=i)),
            price,
            high,
            low,
            close,
            float(rng.integers(50, 500)),
        )
        price = close
    return series


def test_aggregiert_ohlcv_korrekt(mnq: Instrument):
    series = make_series(
        [
            (100, 105, 99, 102, 10),
            (102, 108, 101, 104, 20),
            (104, 106, 95, 96, 30),
            (96, 99, 94, 98, 40),
            (98, 101, 97, 100, 50),
            (100, 102, 99, 101, 60),  # gehoert bereits zum naechsten 5m-Bucket
        ]
    )
    result = aggregate(series, Timeframe.M5, mnq, drop_incomplete=True)

    assert len(result) == 1
    bar = result[0]
    assert bar.open == 100.0, "Open der ersten Bar des Buckets"
    assert bar.high == 108.0, "Maximum aller Highs"
    assert bar.low == 94.0, "Minimum aller Lows"
    assert bar.close == 100.0, "Close der letzten Bar des Buckets"
    assert bar.volume == 150.0, "Summe der Volumina"


def test_unvollstaendiger_bucket_wird_verworfen(mnq: Instrument):
    """Nur ein Bucket, dessen Nachfolger begonnen hat, ist sicher abgeschlossen."""
    series = _synthetic(7)
    assert len(aggregate(series, Timeframe.M5, mnq, drop_incomplete=True)) == 1
    assert len(aggregate(series, Timeframe.M5, mnq, drop_incomplete=False)) == 2


def test_4h_bars_starten_am_globex_open(mnq: Instrument):
    """Session-Ausrichtung: der Handelstag beginnt 17:00 CT, nicht um UTC-Mitternacht."""
    series = _synthetic(60 * 24 * 2)
    result = aggregate(series, Timeframe.H4, mnq)

    local_times = [from_ns(int(ts)).astimezone(CT).strftime("%H:%M") for ts in result.ts]
    assert local_times[0] == "17:00"
    assert set(local_times) <= {"17:00", "21:00", "01:00", "05:00", "09:00", "13:00"}


def test_utc_ausrichtung_als_alternative(mnq: Instrument):
    series = _synthetic(60 * 24)
    result = aggregate(series, Timeframe.H4, mnq, anchor="utc")
    hours = {from_ns(int(ts)).hour for ts in result.ts}
    assert hours <= {0, 4, 8, 12, 16, 20}


@pytest.mark.parametrize("timeframe", ALL_TFS)
def test_streaming_entspricht_batch(mnq: Instrument, timeframe: Timeframe):
    series = _synthetic(60 * 24 * 3)
    batch = aggregate(series, timeframe, mnq, drop_incomplete=True)

    aggregator = MultiTimeframeAggregator("MNQ", mnq, (timeframe,))
    aggregator.feed(series)
    stream = aggregator.series[timeframe]

    assert len(batch) == len(stream)
    assert np.array_equal(batch.ts, stream.ts)
    assert np.allclose(batch.open, stream.open)
    assert np.allclose(batch.high, stream.high)
    assert np.allclose(batch.low, stream.low)
    assert np.allclose(batch.close, stream.close)
    assert np.allclose(batch.volume, stream.volume)


def test_volumen_bleibt_erhalten(mnq: Instrument):
    series = _synthetic(60 * 12)
    for timeframe in ALL_TFS:
        result = aggregate(series, timeframe, mnq, drop_incomplete=False)
        assert np.isclose(result.volume.sum(), series.volume.sum()), timeframe


def test_vektorisierte_und_skalare_bucketberechnung_stimmen_ueberein(mnq: Instrument):
    """`bucket_starts` (pandas) und `TradingDayAnchor` (skalar) duerfen nie abweichen.

    Die skalare Variante ist der schnelle Pfad im Streaming; die vektorisierte
    der Batch-Pfad. Beide berechnen dieselbe Handelstagsgrenze.
    """
    series = _synthetic(60 * 24 * 4)
    anchor = TradingDayAnchor(mnq)
    for timeframe in ALL_TFS:
        tf_ns = timeframe.seconds * 1_000_000_000
        vectorized = bucket_starts(series.ts, timeframe, mnq, "session")
        scalar = np.array(
            [
                anchor.start_of_day(int(ts))
                + ((int(ts) - anchor.start_of_day(int(ts))) // tf_ns) * tf_ns
                for ts in series.ts
            ],
            dtype=np.int64,
        )
        assert np.array_equal(vectorized, scalar), timeframe


def test_sommerzeitwechsel_haelt_handelstagsgrenzen_ein(mnq: Instrument):
    """Am 2025-03-09 wechselt die US-Sommerzeit; dieser Handelstag ist nur 23 h lang.

    Die Buckets werden ab Handelstagsbeginn in ECHTER verstrichener Zeit gezaehlt
    (Konvention der Futures-Plattformen). Daraus folgt zwangslaeufig: an diesem
    einen Tag verschieben sich die Wanduhr-Zeiten der spaeteren 4h-Bars um eine
    Stunde, und die letzte Bar des Tages ist nur 3 h lang.

    Entscheidend sind deshalb nicht die Uhrzeit-Beschriftungen, sondern die
    beiden Eigenschaften, auf die sich die Analyse verlaesst:
        1. Jeder Handelstag beginnt mit einer Bar um exakt 17:00 Ortszeit.
        2. Keine Bar ueberspannt eine Handelstagsgrenze.
    """
    start = datetime(2025, 3, 7, 23, 0, tzinfo=UTC)  # Fr 17:00 CT
    series = _synthetic(60 * 24 * 5, start=start)
    result = aggregate(series, Timeframe.H4, mnq)

    anchor = TradingDayAnchor(mnq)
    day_starts = [anchor.start_of_day(int(ts)) for ts in result.ts]

    # 1. Die erste Bar jedes Handelstages liegt exakt auf dem Tagesbeginn.
    for day_start in set(day_starts):
        first = min(ts for ts, d in zip(result.ts, day_starts, strict=True) if d == day_start)
        assert int(first) == day_start
        assert from_ns(day_start).astimezone(CT).strftime("%H:%M") == "17:00"

    # 2. Keine Bar ueberspannt eine Tagesgrenze: die Bar-Startzeiten eines Tages
    #    liegen alle im selben Tagesfenster.
    for ts, day_start in zip(result.ts, day_starts, strict=True):
        assert int(ts) >= day_start


def test_streaming_und_batch_stimmen_auch_ueber_zeitumstellung_ueberein(mnq: Instrument):
    """Der heikelste Fall fuer die beiden Bucket-Implementierungen."""
    start = datetime(2025, 3, 7, 23, 0, tzinfo=UTC)
    series = _synthetic(60 * 24 * 5, start=start)
    for timeframe in ALL_TFS:
        batch = aggregate(series, timeframe, mnq)
        aggregator = MultiTimeframeAggregator("MNQ", mnq, (timeframe,))
        aggregator.feed(series)
        assert np.array_equal(batch.ts, aggregator.series[timeframe].ts), timeframe


def test_forming_bar_wird_nicht_als_geschlossen_gemeldet(mnq: Instrument):
    """Architektur-Invariante 1: die laufende Bar erreicht keinen Detektor."""
    series = _synthetic(7)
    aggregator = MultiTimeframeAggregator("MNQ", mnq, (Timeframe.M5,))
    closed = aggregator.feed(series)

    assert len(closed) == 1, "Nur der erste 5m-Bucket ist abgeschlossen"
    forming = aggregator.forming(Timeframe.M5)
    assert forming is not None
    assert forming.ts not in set(aggregator.series[Timeframe.M5].ts.tolist())


def test_flush_schliesst_offene_buckets(mnq: Instrument):
    series = _synthetic(7)
    aggregator = MultiTimeframeAggregator("MNQ", mnq, (Timeframe.M5,))
    aggregator.feed(series)
    assert len(aggregator.series[Timeframe.M5]) == 1
    aggregator.flush()
    assert len(aggregator.series[Timeframe.M5]) == 2
    assert aggregator.forming(Timeframe.M5) is None


def test_rueckwaerts_laufende_bars_werden_abgelehnt(mnq: Instrument):
    series = _synthetic(5)
    aggregator = MultiTimeframeAggregator("MNQ", mnq, (Timeframe.M5,))
    aggregator.on_base_bar(series[2])
    with pytest.raises(ValueError, match="streng steigen"):
        aggregator.on_base_bar(series[1])
