"""Liquiditaetslevel und Sweeps.

Die Sweep-Definition ist die heikelste Regel der ganzen Strategie: sie muss den
Sweep (Liquiditaet geholt, Preis abgewiesen) sauber vom Ausbruch (Level
durchhandelt, Preis bleibt drueben) trennen. Beide Faelle werden hier explizit
geprueft.
"""

from __future__ import annotations

from tests.conftest import make_series
from tradex.analysis.liquidity import LiquidityTracker
from tradex.analysis.swings import Swing
from tradex.config import Config, SweepParams
from tradex.domain.enums import (
    Direction,
    LiquidityKind,
    LiquiditySide,
    LiquidityState,
    SessionName,
    SwingType,
)

TICK = 0.25
SESSION = SessionName.NY_AM.value
DAY = 1
WEEK = 1


def _tracker(config: Config, sweep_params: SweepParams | None = None) -> LiquidityTracker:
    return LiquidityTracker(
        config.analysis.liquidity, sweep_params or config.analysis.sweep, TICK
    )


def _swing_low(index: int, price: float, confirmed_at: int) -> Swing:
    return Swing(index, index, price, SwingType.LOW, confirmed_at - index, confirmed_at)


def _swing_high(index: int, price: float, confirmed_at: int) -> Swing:
    return Swing(index, index, price, SwingType.HIGH, confirmed_at - index, confirmed_at)


def _feed(tracker, series, swings_by_index: dict[int, list[Swing]]):
    all_sweeps = []
    for i in range(len(series)):
        all_sweeps.extend(
            tracker.update(series, i, swings_by_index.get(i, []), SESSION, DAY, WEEK)
        )
    return all_sweeps


def test_swing_erzeugt_liquiditaetslevel(config: Config):
    series = make_series([(100, 101, 99, 100)] * 5)
    tracker = _tracker(config)
    _feed(tracker, series, {2: [_swing_low(0, 95.0, 2)]})

    pools = [p for p in tracker.pools if p.kind is LiquidityKind.SWING]
    assert len(pools) == 1
    assert pools[0].price == 95.0
    assert pools[0].side is LiquiditySide.SELL_SIDE, "Ein Tief ist Sell-Side-Liquiditaet"
    assert pools[0].is_untapped


def test_bullisher_sweep_durchstich_und_rueckeroberung(config: Config):
    # Level bei 95. Bar 3 sticht auf 94 durch, Bar 4 erobert per Close zurueck.
    series = make_series(
        [
            (100, 101, 99, 100),
            (100, 101, 99, 100),
            (100, 101, 99, 100),
            (99, 100, 94.0, 94.5),  # Durchstich: 94 < 95 - 0.5
            (94.5, 97, 94.5, 96.5),  # Close 96.5 > 95 -> Rueckeroberung
        ]
    )
    tracker = _tracker(config)
    sweeps = _feed(tracker, series, {2: [_swing_low(0, 95.0, 2)]})

    assert len(sweeps) == 1
    sweep = sweeps[0]
    assert sweep.direction is Direction.BULLISH, "Sell-Side geholt -> bullishes Setup"
    assert sweep.side is LiquiditySide.SELL_SIDE
    assert sweep.pool_price == 95.0
    assert sweep.penetration_index == 3
    assert sweep.reclaim_index == 4
    assert sweep.bars_to_reclaim == 1
    assert sweep.depth_ticks == 4.0  # (95 - 94) / 0.25


def test_bearisher_sweep(config: Config):
    series = make_series(
        [
            (100, 101, 99, 100),
            (100, 101, 99, 100),
            (100, 101, 99, 100),
            (101, 106.0, 100, 105.5),  # Durchstich ueber 105
            (105.5, 105.5, 102, 103.0),  # Close 103 < 105
        ]
    )
    tracker = _tracker(config)
    sweeps = _feed(tracker, series, {2: [_swing_high(0, 105.0, 2)]})

    assert len(sweeps) == 1
    assert sweeps[0].direction is Direction.BEARISH
    assert sweeps[0].side is LiquiditySide.BUY_SIDE


def test_ausbruch_ohne_rueckeroberung_ist_kein_sweep(config: Config):
    """Das Level wurde durchhandelt, nicht geholt. Kein Sweep-Ereignis."""
    series = make_series(
        [
            (100, 101, 99, 100),
            (100, 101, 99, 100),
            (100, 101, 99, 100),
            (99, 100, 94.0, 94.5),  # Durchstich
            (94.5, 94.8, 93.0, 93.5),  # bleibt drunter
            (93.5, 94.0, 92.0, 92.5),
            (92.5, 93.0, 91.0, 91.5),
            (91.5, 92.0, 90.0, 90.5),
        ]
    )
    tracker = _tracker(config)
    sweeps = _feed(tracker, series, {2: [_swing_low(0, 95.0, 2)]})

    assert sweeps == []
    pool = next(p for p in tracker.pools if p.kind is LiquidityKind.SWING)
    assert pool.state is LiquidityState.SWEPT, "Level gilt trotzdem als verbraucht"
    assert pool.tapped_index == 3


def test_zu_spaete_rueckeroberung_zaehlt_nicht(config: Config):
    """max_reclaim_bars = 3: eine Rueckkehr in Bar 8 kommt zu spaet."""
    series = make_series(
        [
            (100, 101, 99, 100),
            (100, 101, 99, 100),
            (100, 101, 99, 100),
            (99, 100, 94.0, 94.5),  # Durchstich in Bar 3
            (94.5, 94.8, 94.0, 94.2),
            (94.2, 94.5, 93.8, 94.0),
            (94.0, 94.6, 93.9, 94.4),
            (94.4, 94.9, 94.0, 94.8),
            (94.8, 99.0, 94.8, 98.0),  # zu spaet
        ]
    )
    tracker = _tracker(config)
    assert _feed(tracker, series, {2: [_swing_low(0, 95.0, 2)]}) == []


def test_zu_flacher_durchstich_zaehlt_nicht(config: Config):
    """min_penetration_ticks = 2 entspricht 0.5 Punkten."""
    series = make_series(
        [
            (100, 101, 99, 100),
            (100, 101, 99, 100),
            (100, 101, 99, 100),
            (99, 100, 94.75, 95.5),  # nur 0.25 unter dem Level -> kein Durchstich
            (95.5, 97, 95.0, 96.5),
        ]
    )
    tracker = _tracker(config)
    assert _feed(tracker, series, {2: [_swing_low(0, 95.0, 2)]}) == []


def test_sweep_innerhalb_einer_bar(config: Config):
    """Durchstich und Rueckeroberung in derselben Kerze - der klassische Docht-Sweep."""
    series = make_series(
        [
            (100, 101, 99, 100),
            (100, 101, 99, 100),
            (100, 101, 99, 100),
            (99, 98.5, 93.0, 97.0),  # Low 93 < 94.5, Close 97 > 95
        ]
    )
    tracker = _tracker(config)
    sweeps = _feed(tracker, series, {2: [_swing_low(0, 95.0, 2)]})

    assert len(sweeps) == 1
    assert sweeps[0].bars_to_reclaim == 0
    assert sweeps[0].depth_ticks == 8.0  # (95 - 93) / 0.25


def test_level_der_gleichen_bar_wird_nicht_sofort_gesweept(config: Config):
    series = make_series([(100, 101, 90.0, 100)] * 3)
    tracker = _tracker(config)
    assert _feed(tracker, series, {2: [_swing_low(0, 95.0, 2)]}) == []


def test_equal_lows_werden_zu_einem_level(config: Config):
    """Drei Tiefs innerhalb der Toleranz bilden einen Cluster mit Staerke 3.

    Massgeblich ist das TIEFSTE der drei - dort liegen die Stops.
    """
    series = make_series([(100, 101, 99, 100)] * 12)
    tracker = _tracker(config)
    _feed(
        tracker,
        series,
        {
            2: [_swing_low(0, 95.00, 2)],
            5: [_swing_low(3, 95.25, 5)],
            8: [_swing_low(6, 94.75, 8)],
        },
    )

    equal = [p for p in tracker.pools if p.kind is LiquidityKind.EQUAL]
    assert len(equal) == 1
    assert equal[0].strength == 3
    assert equal[0].price == 94.75
    assert equal[0].side is LiquiditySide.SELL_SIDE


def test_weit_auseinanderliegende_tiefs_bilden_keinen_cluster(config: Config):
    series = make_series([(100, 101, 99, 100)] * 12)
    tracker = _tracker(config)
    _feed(
        tracker,
        series,
        {
            2: [_swing_low(0, 95.0, 2)],
            5: [_swing_low(3, 92.0, 5)],
            8: [_swing_low(6, 89.0, 8)],
        },
    )
    assert [p for p in tracker.pools if p.kind is LiquidityKind.EQUAL] == []


def test_tageswechsel_erzeugt_prior_day_level(config: Config):
    series = make_series(
        [
            (100, 105, 95, 100),
            (100, 108, 97, 102),
            (102, 104, 99, 103),
            (103, 106, 101, 105),
        ]
    )
    tracker = _tracker(config)
    for i in range(3):
        tracker.update(series, i, [], SESSION, 1, 1)
    tracker.update(series, 3, [], SESSION, 2, 1)  # Handelstag wechselt

    prior = [p for p in tracker.pools if p.kind is LiquidityKind.PRIOR_DAY]
    assert len(prior) == 2
    assert {p.price for p in prior} == {108.0, 95.0}
    assert {p.side for p in prior} == {LiquiditySide.BUY_SIDE, LiquiditySide.SELL_SIDE}


def test_naechstes_unberuehrtes_level(config: Config):
    """Grundlage der Take-Profit-Bestimmung in Phase 3 (Spec §12)."""
    series = make_series([(100, 101, 99, 100)] * 15)
    tracker = _tracker(config)
    _feed(
        tracker,
        series,
        {
            3: [_swing_high(0, 110.0, 3)],
            6: [_swing_high(4, 120.0, 6)],
            9: [_swing_low(7, 90.0, 9)],
            12: [_swing_low(10, 80.0, 12)],
        },
    )

    above = tracker.nearest_untapped(100.0, LiquiditySide.BUY_SIDE)
    below = tracker.nearest_untapped(100.0, LiquiditySide.SELL_SIDE)
    assert above is not None and above.price == 110.0
    assert below is not None and below.price == 90.0
    assert tracker.nearest_untapped(200.0, LiquiditySide.BUY_SIDE) is None


def test_doppelte_level_werden_nicht_angelegt(config: Config):
    """Sonst waere jede Statistik ueber gesweepte Level verzerrt."""
    series = make_series([(100, 101, 99, 100)] * 10)
    tracker = _tracker(config)
    _feed(
        tracker,
        series,
        {2: [_swing_low(0, 95.0, 2)], 5: [_swing_low(3, 95.25, 5)]},
    )
    swing_pools = [p for p in tracker.pools if p.kind is LiquidityKind.SWING]
    assert len(swing_pools) == 1
