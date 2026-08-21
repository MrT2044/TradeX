"""ATR, True Range und gleitende Durchschnitte."""

from __future__ import annotations

import math

import numpy as np
import pytest

from tradex.analysis.volatility import RollingAtr, RollingSma, atr, sma, true_range


def test_true_range_erste_bar_ohne_vorgaenger():
    high = np.array([10.0, 12.0])
    low = np.array([8.0, 9.0])
    close = np.array([9.0, 11.0])
    result = true_range(high, low, close)
    assert result[0] == 2.0, "Erste Bar: schlicht High - Low"
    assert result[1] == 3.0


def test_true_range_beruecksichtigt_gaps():
    """Ein Gap nach oben macht die True Range groesser als die reine Bar-Range."""
    high = np.array([10.0, 20.0])
    low = np.array([9.0, 18.0])
    close = np.array([9.5, 19.0])
    result = true_range(high, low, close)
    assert result[1] == 20.0 - 9.5, "High minus vorheriger Close"


def test_atr_ist_vor_aufwaermphase_nan():
    """NaN statt 0: mit ATR=0 waere jede Vergleichsbedingung immer wahr."""
    n = 20
    period = 14
    rng = np.random.default_rng(1)
    high = 100 + rng.random(n) * 5
    low = high - 2
    close = (high + low) / 2

    result = atr(high, low, close, period)
    assert np.all(np.isnan(result[: period - 1]))
    assert np.all(np.isfinite(result[period - 1 :]))


def test_atr_wilder_gegen_handrechnung():
    # Konstante True Range von 2 -> ATR muss ebenfalls 2 sein
    n = 30
    high = np.full(n, 12.0)
    low = np.full(n, 10.0)
    close = np.full(n, 11.0)
    result = atr(high, low, close, 14, "wilder")
    assert math.isclose(result[-1], 2.0)


def test_atr_sma_variante():
    n = 30
    high = np.full(n, 12.0)
    low = np.full(n, 10.0)
    close = np.full(n, 11.0)
    assert math.isclose(atr(high, low, close, 14, "sma")[-1], 2.0)


def test_atr_lehnt_unbekannte_methode_ab():
    high = np.full(30, 12.0)
    low = np.full(30, 10.0)
    close = np.full(30, 11.0)
    with pytest.raises(ValueError, match="Unbekannte ATR-Methode"):
        atr(high, low, close, 14, "ema")


@pytest.mark.parametrize("method", ["wilder", "sma"])
def test_rolling_atr_entspricht_batch(method: str):
    """Streaming und Batch muessen identische Werte liefern - sonst weicht Live vom Backtest ab."""
    rng = np.random.default_rng(42)
    n = 200
    close = 21000 + np.cumsum(rng.normal(0, 3, n))
    high = close + np.abs(rng.normal(0, 2, n))
    low = close - np.abs(rng.normal(0, 2, n))

    period = 14
    batch = atr(high, low, close, period, method)
    rolling = RollingAtr(period, method)
    streamed = [rolling.update(float(high[i]), float(low[i]), float(close[i])) for i in range(n)]

    for i in range(n):
        if np.isnan(batch[i]):
            assert np.isnan(streamed[i])
        else:
            assert math.isclose(batch[i], streamed[i], rel_tol=1e-12)


def test_rolling_sma_entspricht_batch():
    rng = np.random.default_rng(3)
    values = rng.integers(50, 500, 100).astype(float)
    period = 20

    batch = sma(values, period)
    rolling = RollingSma(period)
    streamed = [rolling.update(float(v)) for v in values]

    for i in range(len(values)):
        if np.isnan(batch[i]):
            assert np.isnan(streamed[i])
        else:
            assert math.isclose(batch[i], streamed[i], rel_tol=1e-12)


def test_rolling_atr_ready_flag():
    rolling = RollingAtr(5)
    for _ in range(4):
        rolling.update(12.0, 10.0, 11.0)
        assert not rolling.ready
        assert math.isnan(rolling.value)
    rolling.update(12.0, 10.0, 11.0)
    assert rolling.ready
    assert math.isclose(rolling.value, 2.0)


def test_leere_eingaben():
    empty = np.empty(0)
    assert true_range(empty, empty, empty).size == 0
    assert atr(empty, empty, empty, 14).size == 0
    assert sma(empty, 14).size == 0
