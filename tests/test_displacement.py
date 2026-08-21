"""Displacement: die quantitative Impulsdefinition."""

from __future__ import annotations

import math

from tests.conftest import make_series
from tradex.analysis.displacement import DisplacementDetector
from tradex.config import Config, DisplacementParams
from tradex.domain.enums import Direction

ATR = 5.0
VOLUME_AVG = 100.0

# Bar 0 ist die Referenz (High 101 / Low 99), Bar 1 der Impuls.
REFERENCE = (100.0, 101.0, 99.0, 100.0, 100.0)


def _detect(params: DisplacementParams, impulse, atr=ATR, volume_avg=VOLUME_AVG, roll=None):
    series = make_series([REFERENCE, impulse], roll_at=roll or set())
    detector = DisplacementDetector(params)
    detector.update(series, 0, atr, volume_avg)
    return detector.update(series, 1, atr, volume_avg)


def test_erkennt_bullishes_displacement(config: Config):
    # Range 11 > 1.5*5 = 7.5 ; Body 10/11 = 0.909 > 0.6 ; Close 110 > High[0] 101
    result = _detect(config.analysis.displacement, (100.0, 110.5, 99.5, 110.0, 200.0))
    assert result is not None
    assert result.direction is Direction.BULLISH
    assert result.range == 11.0
    assert math.isclose(result.body_ratio, 10.0 / 11.0)
    assert math.isclose(result.range_atr_mult, 11.0 / 5.0)
    assert result.volume_ratio == 2.0
    assert result.volume_confirmed is True


def test_erkennt_bearishes_displacement(config: Config):
    result = _detect(config.analysis.displacement, (100.0, 100.5, 89.5, 90.0, 200.0))
    assert result is not None
    assert result.direction is Direction.BEARISH


def test_zu_kleine_range_kein_displacement(config: Config):
    # Range 4 < 1.5 * 5 = 7.5
    assert _detect(config.analysis.displacement, (100.0, 104.0, 100.0, 103.9, 200.0)) is None


def test_zu_kleiner_body_kein_displacement(config: Config):
    # Range 12, Body 3 -> 0.25 < 0.6 : viel Docht, wenig Ueberzeugung
    assert _detect(config.analysis.displacement, (100.0, 109.0, 97.0, 103.0, 200.0)) is None


def test_ohne_ausbruch_ueber_vorheriges_hoch_kein_displacement(config: Config):
    """Grosse Kerze, die das vorherige Hoch nicht ueberbietet, ist kein Ausbruch."""
    # Range 11, Body-Anteil ok, aber Close 99 < High[0] = 101
    assert _detect(config.analysis.displacement, (88.5, 99.5, 88.5, 99.0, 200.0)) is None


def test_ausbruchsbedingung_abschaltbar(config: Config):
    params = DisplacementParams(
        **{**config.analysis.displacement.model_dump(), "require_break_prev_extreme": False}
    )
    assert _detect(params, (88.5, 99.5, 88.5, 99.0, 200.0)) is not None


def test_volumen_ist_standardmaessig_kein_gate(config: Config):
    """Volumenverfuegbarkeit haengt an der Datenquelle - sie darf die Regel nicht aendern."""
    result = _detect(config.analysis.displacement, (100.0, 110.5, 99.5, 110.0, 50.0))
    assert result is not None
    assert result.volume_confirmed is False


def test_volumen_als_gate_aktivierbar(config: Config):
    params = DisplacementParams(
        **{**config.analysis.displacement.model_dump(), "volume_is_gate": True}
    )
    assert _detect(params, (100.0, 110.5, 99.5, 110.0, 50.0)) is None
    assert _detect(params, (100.0, 110.5, 99.5, 110.0, 200.0)) is not None


def test_ohne_atr_kein_displacement(config: Config):
    assert _detect(config.analysis.displacement, (100.0, 110.5, 99.5, 110.0), atr=float("nan")) is None


def test_rollgrenze_erzeugt_kein_displacement(config: Config):
    assert _detect(config.analysis.displacement, (100.0, 110.5, 99.5, 110.0), roll={1}) is None


def test_staerke_ohne_volumen_bleibt_vergleichbar(config: Config):
    """Fehlt der Volumen-Referenzwert, faellt sein Gewicht aus dem Nenner.

    Sonst haetten Instrumente ohne Volumen systematisch niedrigere Werte.
    """
    impulse = (100.0, 110.5, 99.5, 110.0, 200.0)
    with_volume = _detect(config.analysis.displacement, impulse)
    without_volume = _detect(config.analysis.displacement, impulse, volume_avg=float("nan"))

    assert with_volume is not None and without_volume is not None
    assert 0.0 <= without_volume.strength <= 1.0
    assert math.isnan(without_volume.volume_ratio)

    weights = config.analysis.displacement.strength_weights
    range_score = min((11.0 / 5.0) / config.analysis.displacement.strength_range_cap_atr_mult, 1.0)
    body_score = 10.0 / 11.0
    expected = (weights.range * range_score + weights.body * body_score) / (
        weights.range + weights.body
    )
    assert math.isclose(without_volume.strength, expected)


def test_within_beruecksichtigt_zeitfenster(config: Config):
    series = make_series(
        [REFERENCE, (100.0, 110.5, 99.5, 110.0, 200.0), *[(110.0, 111.0, 109.0, 110.0)] * 10]
    )
    detector = DisplacementDetector(config.analysis.displacement)
    for i in range(len(series)):
        detector.update(series, i, ATR, VOLUME_AVG)

    assert detector.within(index=3, lookback_bars=5) is not None
    assert detector.within(index=11, lookback_bars=5) is None
    assert detector.last(Direction.BEARISH) is None
