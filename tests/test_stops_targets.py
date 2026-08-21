"""Stop-Loss- und Take-Profit-Bestimmung (Spec §11, §12)."""

from __future__ import annotations

import math

import pytest

from tradex.analysis.liquidity import LiquidityTracker, Sweep
from tradex.analysis.swings import Swing
from tradex.config import Config, StopsConfig, TargetsConfig
from tradex.domain.enums import (
    Direction,
    LiquidityKind,
    LiquiditySide,
    SwingType,
)
from tradex.domain.instruments import Instrument
from tradex.strategy.setup import SetupCandidate
from tradex.strategy.stops import place_stop
from tradex.strategy.targets import place_target

ATR = 8.0


def _sweep(direction: Direction, pool_price: float, extreme: float) -> Sweep:
    return Sweep(
        pool_id=1,
        pool_kind=LiquidityKind.SWING,
        pool_price=pool_price,
        side=(
            LiquiditySide.SELL_SIDE if direction is Direction.BULLISH else LiquiditySide.BUY_SIDE
        ),
        direction=direction,
        penetration_index=10,
        penetration_ts=10,
        reclaim_index=11,
        reclaim_ts=11,
        depth_ticks=abs(pool_price - extreme) / 0.25,
        bars_to_reclaim=1,
        extreme_price=extreme,
    )


def _candidate(direction: Direction, pool_price: float, extreme: float) -> SetupCandidate:
    return SetupCandidate(
        id=1,
        symbol="MNQ",
        direction=direction,
        sweep=_sweep(direction, pool_price, extreme),
        created_index=11,
        created_ts=11,
    )


# --------------------------------------------------------------------- Stops
def test_stop_liegt_unter_dem_sweep_extrem(config: Config, mnq: Instrument):
    """Anker ist der Kurs, den der Markt getestet und abgelehnt hat."""
    candidate = _candidate(Direction.BULLISH, 21000.0, 20995.0)
    result = place_stop(candidate, 21010.0, ATR, mnq, config.stops)

    assert result.ok
    assert result.anchor_kind == "sweep"
    assert result.anchor_price == 20995.0
    # Puffer = max(0.25 * 8, 4 Ticks * 0.25) = max(2.0, 1.0) = 2.0
    assert math.isclose(result.buffer_points, 2.0)
    assert result.price == 20993.0
    assert result.price < 21010.0


def test_stop_bei_short_liegt_darueber(config: Config, mnq: Instrument):
    candidate = _candidate(Direction.BEARISH, 21000.0, 21005.0)
    result = place_stop(candidate, 20990.0, ATR, mnq, config.stops)

    assert result.ok
    assert result.price == 21007.0
    assert result.price > 20990.0


def test_puffer_waechst_mit_der_volatilitaet(config: Config, mnq: Instrument):
    """Ein fester Tickwert waere im NY-Open zu eng und nachts unnoetig weit."""
    candidate = _candidate(Direction.BULLISH, 21000.0, 20995.0)
    quiet = place_stop(candidate, 21010.0, 2.0, mnq, config.stops)
    wild = place_stop(candidate, 21010.0, 40.0, mnq, config.stops)

    assert wild.buffer_points > quiet.buffer_points
    assert wild.price < quiet.price


def test_mindestpuffer_greift_bei_sehr_kleinem_atr(config: Config, mnq: Instrument):
    candidate = _candidate(Direction.BULLISH, 21000.0, 20995.0)
    result = place_stop(candidate, 21010.0, 0.1, mnq, config.stops)
    # buffer_min_ticks = 4 -> 1.0 Punkt
    assert math.isclose(result.buffer_points, 1.0)


def test_zu_enger_stop_wird_abgelehnt(config: Config, mnq: Instrument):
    """Ein Stop im Rauschen wird nicht kuenstlich verbreitert, sondern verworfen."""
    params = StopsConfig(**{**config.stops.model_dump(), "min_stop_ticks": 100})
    candidate = _candidate(Direction.BULLISH, 21000.0, 20995.0)
    result = place_stop(candidate, 21010.0, ATR, mnq, params)

    assert not result.ok
    assert result.rejection == "too_tight"


def test_zu_weiter_stop_wird_abgelehnt(config: Config, mnq: Instrument):
    candidate = _candidate(Direction.BULLISH, 21000.0, 20800.0)
    result = place_stop(candidate, 21010.0, ATR, mnq, config.stops)

    assert not result.ok
    assert result.rejection == "too_wide"


def test_swing_anker_auf_falscher_seite_faellt_auf_sweep_zurueck(
    config: Config, mnq: Instrument
):
    """Ein Swing kann oberhalb des Einstiegs liegen - daraus waere der "Stop"
    sofort ein Verlust. Dann muss der Sweep-Anker greifen."""
    params = StopsConfig(**{**config.stops.model_dump(), "anchor": "swing"})
    candidate = _candidate(Direction.BULLISH, 21000.0, 20995.0)
    swing_above = Swing(
        index=5, ts=5, price=21050.0, type=SwingType.LOW, strength=2, confirmed_at_index=7
    )

    result = place_stop(candidate, 21010.0, ATR, mnq, params, swing_above)
    assert result.anchor_kind == "sweep", "Anker auf der falschen Seite darf nicht benutzt werden"
    assert result.price < 21010.0


def test_swing_anker_auf_richtiger_seite_wird_benutzt(config: Config, mnq: Instrument):
    params = StopsConfig(**{**config.stops.model_dump(), "anchor": "swing"})
    candidate = _candidate(Direction.BULLISH, 21000.0, 20990.0)
    swing_below = Swing(
        index=5, ts=5, price=21004.0, type=SwingType.LOW, strength=2, confirmed_at_index=7
    )

    result = place_stop(candidate, 21010.0, ATR, mnq, params, swing_below)
    assert result.anchor_kind == "swing"
    assert result.anchor_price == 21004.0


# ------------------------------------------------------------------- Targets
def _liquidity_with(config: Config, mnq: Instrument, levels: list[tuple[float, LiquiditySide]]):
    tracker = LiquidityTracker(config.analysis.liquidity, config.analysis.sweep, mnq.tick_size)
    for i, (price, side) in enumerate(levels, start=1):
        tracker._add_pool_at(
            price=price,
            side=side,
            kind=LiquidityKind.SWING,
            created_index=i,
            created_ts=i,
            label="test",
            source=(i,),
        )
    return tracker


def test_ziel_ist_naechste_ausreichende_liquiditaet(config: Config, mnq: Instrument):
    """Genommen wird das ERSTE Level, das das Mindest-CRV schafft - nicht das naechste."""
    liquidity = _liquidity_with(
        config,
        mnq,
        [
            (21015.0, LiquiditySide.BUY_SIDE),  # nur 1.5R
            (21030.0, LiquiditySide.BUY_SIDE),  # 3.0R -> genommen
            (21060.0, LiquiditySide.BUY_SIDE),  # weiter, aber nicht noetig
        ],
    )
    result = place_target(
        Direction.BULLISH, 21000.0, 10.0, liquidity, mnq, config.targets, min_rr=2.0
    )

    assert result.ok
    assert result.price == 21030.0
    assert math.isclose(result.rr, 3.0)
    assert result.source == "liquidity"


def test_kein_ziel_mit_ausreichendem_crv_wird_abgelehnt(config: Config, mnq: Instrument):
    """Spec §12: schlechtes Chance-Risiko-Verhaeltnis fuehrt zur Ablehnung."""
    liquidity = _liquidity_with(
        config, mnq, [(21010.0, LiquiditySide.BUY_SIDE), (21015.0, LiquiditySide.BUY_SIDE)]
    )
    result = place_target(
        Direction.BULLISH, 21000.0, 10.0, liquidity, mnq, config.targets, min_rr=2.0
    )

    assert not result.ok
    assert result.rejection == "rr_too_low"
    assert math.isclose(result.best_available_rr, 1.5)


def test_liquiditaet_hinter_dem_einstieg_zaehlt_nicht(config: Config, mnq: Instrument):
    """Ein Level unter dem Long-Einstieg ist kein Ziel."""
    liquidity = _liquidity_with(
        config, mnq, [(20950.0, LiquiditySide.BUY_SIDE), (21040.0, LiquiditySide.BUY_SIDE)]
    )
    result = place_target(
        Direction.BULLISH, 21000.0, 10.0, liquidity, mnq, config.targets, min_rr=2.0
    )
    assert result.ok
    assert result.price == 21040.0


def test_short_ziel_liegt_unterhalb(config: Config, mnq: Instrument):
    liquidity = _liquidity_with(
        config, mnq, [(20970.0, LiquiditySide.SELL_SIDE), (20940.0, LiquiditySide.SELL_SIDE)]
    )
    result = place_target(
        Direction.BEARISH, 21000.0, 10.0, liquidity, mnq, config.targets, min_rr=2.0
    )
    assert result.ok
    assert result.price == 20970.0
    assert math.isclose(result.rr, 3.0)


def test_ohne_liquiditaet_greift_das_feste_r_vielfache(config: Config, mnq: Instrument):
    liquidity = _liquidity_with(config, mnq, [])
    result = place_target(
        Direction.BULLISH, 21000.0, 10.0, liquidity, mnq, config.targets, min_rr=2.0
    )

    assert result.ok
    assert result.source == "fallback_r_multiple"
    assert math.isclose(result.rr, config.targets.fallback_r_multiple)
    assert result.price == 21030.0


def test_r_multiple_modus(config: Config, mnq: Instrument):
    params = TargetsConfig(**{**config.targets.model_dump(), "mode": "r_multiple"})
    liquidity = _liquidity_with(config, mnq, [(21010.0, LiquiditySide.BUY_SIDE)])
    result = place_target(Direction.BULLISH, 21000.0, 10.0, liquidity, mnq, params, min_rr=2.0)

    assert result.ok
    assert result.source == "fallback_r_multiple"
    assert result.price == 21030.0


@pytest.mark.parametrize("stop_distance", [0.0, -5.0])
def test_ungueltiger_stopabstand(config: Config, mnq: Instrument, stop_distance: float):
    liquidity = _liquidity_with(config, mnq, [(21040.0, LiquiditySide.BUY_SIDE)])
    result = place_target(
        Direction.BULLISH, 21000.0, stop_distance, liquidity, mnq, config.targets, min_rr=2.0
    )
    assert not result.ok
    assert result.rejection == "invalid_stop"
