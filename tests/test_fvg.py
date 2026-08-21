"""Fair Value Gaps: Erkennung, Filter und Lebenszyklus."""

from __future__ import annotations

from tests.conftest import make_series
from tradex.analysis.fvg import FvgTracker
from tradex.config import Config
from tradex.domain.enums import Direction, FvgState

ATR = 10.0
TICK = 0.25


def _run(tracker: FvgTracker, series, atr: float = ATR) -> None:
    for i in range(len(series)):
        tracker.update(series, i, atr)


def test_erkennt_bullishe_fvg(config: Config):
    # Bar 0 High = 100, Bar 2 Low = 105  ->  Luecke [100, 105]
    series = make_series(
        [
            (98, 100, 97, 99),
            (99, 108, 99, 107),
            (107, 110, 105, 109),
        ]
    )
    tracker = FvgTracker(config.analysis.fvg, TICK)
    _run(tracker, series)

    assert len(tracker.zones) == 1
    zone = tracker.zones[0]
    assert zone.direction is Direction.BULLISH
    assert zone.bottom == 100.0
    assert zone.top == 105.0
    assert zone.size == 5.0
    assert zone.size_ticks == 20.0
    assert zone.created_index == 2
    assert zone.state is FvgState.OPEN


def test_erkennt_bearishe_fvg(config: Config):
    # Bar 0 Low = 105, Bar 2 High = 100  ->  Luecke [100, 105]
    series = make_series(
        [
            (108, 110, 105, 107),
            (107, 107, 98, 99),
            (99, 100, 95, 96),
        ]
    )
    tracker = FvgTracker(config.analysis.fvg, TICK)
    _run(tracker, series)

    zone = tracker.zones[0]
    assert zone.direction is Direction.BEARISH
    assert zone.bottom == 100.0
    assert zone.top == 105.0
    assert zone.entry_edge == 100.0, "Bearish tritt der Preis von unten ein"


def test_zu_kleine_luecke_wird_verworfen(config: Config):
    """min_size_ticks = 4 entspricht bei MNQ 1.0 Punkt."""
    series = make_series(
        [
            (98, 100.0, 97, 99),
            (99, 101.0, 99, 100.5),
            (100.5, 101.5, 100.5, 101),  # Luecke nur 0.5 Punkte = 2 Ticks
        ]
    )
    tracker = FvgTracker(config.analysis.fvg, TICK)
    _run(tracker, series)
    assert tracker.zones == []


def test_luecke_unter_atr_schwelle_wird_verworfen(config: Config):
    """min_atr_mult = 0.15: bei ATR 100 sind 15 Punkte noetig."""
    series = make_series(
        [
            (98, 100, 97, 99),
            (99, 108, 99, 107),
            (107, 110, 105, 109),  # 5 Punkte - genug in Ticks, zu wenig relativ
        ]
    )
    tracker = FvgTracker(config.analysis.fvg, TICK)
    _run(tracker, series, atr=100.0)
    assert tracker.zones == []


def test_ohne_atr_keine_fvg(config: Config):
    """Waehrend der ATR-Aufwaermphase wird verworfen, nicht durchgewinkt."""
    series = make_series(
        [(98, 100, 97, 99), (99, 108, 99, 107), (107, 110, 105, 109)]
    )
    tracker = FvgTracker(config.analysis.fvg, TICK)
    _run(tracker, series, atr=float("nan"))
    assert tracker.zones == []


def test_rollgrenze_erzeugt_keine_fvg(config: Config):
    """Der Preissprung an der Kontraktnaht ist ein Buchungsartefakt, kein Markt."""
    series = make_series(
        [
            (98, 100, 97, 99),
            (99, 108, 99, 107),
            (107, 110, 105, 109),
        ],
        roll_at={1},
    )
    tracker = FvgTracker(config.analysis.fvg, TICK)
    _run(tracker, series)
    assert tracker.zones == []


def test_lebenszyklus_open_touched_mitigated(config: Config):
    # Zone [100, 105], Mitigationsschwelle 50 % -> Kurs 102.5
    base = [
        (98, 100, 97, 99),
        (99, 108, 99, 107),
        (107, 110, 105, 109),
    ]
    tracker = FvgTracker(config.analysis.fvg, TICK)
    series = make_series(
        [
            *base,
            (109, 110, 108, 109),  # 3: bleibt oberhalb -> OPEN
            (109, 109, 104, 104.5),  # 4: beruehrt Zone, Close 104.5 -> nur TOUCHED
            (104.5, 105, 101, 101.5),  # 5: Close 101.5 < 102.5 -> MITIGATED
        ]
    )

    states: list[FvgState] = []
    for i in range(len(series)):
        tracker.update(series, i, ATR)
        if tracker.zones:
            states.append(tracker.zones[0].state)

    assert states == [
        FvgState.OPEN,
        FvgState.OPEN,
        FvgState.TOUCHED,
        FvgState.MITIGATED,
    ]
    zone = tracker.zones[0]
    assert zone.touched_index == 4
    assert zone.mitigated_index == 5
    assert not zone.is_active
    # closed_ts markiert, wo die Zone abgearbeitet wurde. Das Chart zeichnet
    # erledigte Zonen nur bis dorthin, statt sie bis zum rechten Rand zu ziehen.
    assert zone.closed_ts == int(series.ts[5])


def test_erzeugende_bar_mitigiert_nicht_sich_selbst(config: Config):
    """Bar 2 definiert die Obergrenze der Zone - sie darf sie nicht sofort schliessen."""
    series = make_series(
        [(98, 100, 97, 99), (99, 108, 99, 107), (107, 110, 105, 109)]
    )
    tracker = FvgTracker(config.analysis.fvg, TICK)
    _run(tracker, series)
    assert tracker.zones[0].state is FvgState.OPEN


def test_ablauf_nach_max_age(config: Config):
    from tradex.config import FvgParams

    params = FvgParams(**{**config.analysis.fvg.model_dump(), "max_age_bars": 2})
    series = make_series(
        [
            (98, 100, 97, 99),
            (99, 108, 99, 107),
            (107, 110, 105, 109),
            (109, 111, 108, 110),
            (110, 112, 109, 111),
            (111, 113, 110, 112),
        ]
    )
    tracker = FvgTracker(params, TICK)
    _run(tracker, series)
    zone = tracker.zones[0]
    assert zone.state is FvgState.EXPIRED
    assert zone.expired_index == 5
    assert zone.closed_ts == int(series.ts[5])


def test_aktive_zone_hat_kein_ende(config: Config):
    """Solange eine Zone aktiv ist, bleibt closed_ts leer - das Chart zieht sie
    dann bis zum rechten Rand durch."""
    series = make_series(
        [(98, 100, 97, 99), (99, 108, 99, 107), (107, 110, 105, 109), (109, 112, 108, 111)]
    )
    tracker = FvgTracker(config.analysis.fvg, TICK)
    _run(tracker, series)
    zone = tracker.zones[0]
    assert zone.is_active
    assert zone.closed_ts is None


def test_fuellgrad_und_mitigationspreis(config: Config):
    series = make_series(
        [(98, 100, 97, 99), (99, 108, 99, 107), (107, 110, 105, 109)]
    )
    tracker = FvgTracker(config.analysis.fvg, TICK)
    _run(tracker, series)
    zone = tracker.zones[0]

    assert zone.mid == 102.5
    assert zone.mitigation_price(0.5) == 102.5
    assert zone.mitigation_price(1.0) == 100.0
    assert zone.fill_fraction(105.0) == 0.0
    assert zone.fill_fraction(102.5) == 0.5
    assert zone.fill_fraction(100.0) == 1.0
    assert zone.fill_fraction(90.0) == 1.0, "Fuellgrad ist auf 1 begrenzt"
    assert zone.fill_fraction(120.0) == 0.0, "Kurs oberhalb der Zone fuellt nichts"


def test_abfragen_nach_aktiven_zonen(config: Config):
    series = make_series(
        [(98, 100, 97, 99), (99, 108, 99, 107), (107, 110, 105, 109), (109, 111, 108, 110)]
    )
    tracker = FvgTracker(config.analysis.fvg, TICK)
    _run(tracker, series)

    assert len(tracker.active(Direction.BULLISH)) == 1
    assert tracker.active(Direction.BEARISH) == []
    assert tracker.nearest_active(110.0) is tracker.zones[0]
    assert tracker.containing(103.0) == [tracker.zones[0]]
    assert tracker.containing(120.0) == []
