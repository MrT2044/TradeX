"""Swing-Erkennung.

Zentral geprueft werden drei Eigenschaften, an denen spaeter alles haengt:
    1. Die Definition trifft genau die erwarteten Bars.
    2. Die Plateau-Regel (links strikt, rechts nicht-strikt) liefert genau EINEN
       Swing pro Plateau.
    3. Batch- und Streaming-Implementierung stimmen exakt ueberein.
"""

from __future__ import annotations

import pytest

from tests.conftest import make_series
from tradex.analysis.swings import SwingDetector, detect_swings
from tradex.domain.enums import SwingType


def _bar(high: float, low: float) -> tuple[float, float, float, float]:
    """Bar mit vorgegebenem High/Low; Open/Close in der Mitte."""
    mid = (high + low) / 2
    return (mid, high, low, mid)


def test_erkennt_einfachen_swing_high():
    # Index:      0    1    2    3    4
    # Highs:     10   11   15   12   11   -> Index 2 ist Swing High (Staerke 2)
    series = make_series(
        [_bar(10, 5), _bar(11, 6), _bar(15, 9), _bar(12, 7), _bar(11, 6)]
    )
    swings = detect_swings(series, strength=2)
    highs = [s for s in swings if s.type is SwingType.HIGH]
    assert len(highs) == 1
    assert highs[0].index == 2
    assert highs[0].price == 15.0
    assert highs[0].confirmed_at_index == 4, "Bestaetigung erst nach `strength` weiteren Bars"
    assert highs[0].confirmation_lag == 2


def test_erkennt_einfachen_swing_low():
    series = make_series(
        [_bar(15, 10), _bar(14, 9), _bar(13, 4), _bar(14, 8), _bar(15, 9)]
    )
    lows = [s for s in detect_swings(series, strength=2) if s.type is SwingType.LOW]
    assert len(lows) == 1
    assert lows[0].index == 2
    assert lows[0].price == 4.0


def test_plateau_liefert_genau_einen_swing():
    """Zwei exakt gleich hohe Bars: nur die erste zaehlt.

    Das ist die dokumentierte Tie-Break-Regel (links strikt, rechts >=). Ohne sie
    wuerde ein Plateau entweder doppelt gemeldet oder gar nicht.
    """
    # Index:   0    1    2    3    4    5    6
    # Highs:  10   11   20   20   11   10    9
    # Bei Staerke 2 sind nur die Indizes 2..4 pruefbar. Index 2 erfuellt beide
    # Seiten, Index 3 scheitert an der strikten linken Bedingung (20 > 20 ist falsch).
    series = make_series(
        [
            _bar(10, 5),
            _bar(11, 6),
            _bar(20, 9),
            _bar(20, 9),
            _bar(11, 6),
            _bar(10, 5),
            _bar(9, 4),
        ]
    )
    highs = [s for s in detect_swings(series, strength=2) if s.type is SwingType.HIGH]
    assert len(highs) == 1
    assert highs[0].index == 2


def test_kein_swing_bei_monotonem_anstieg():
    series = make_series([_bar(10 + i, 5 + i) for i in range(10)])
    assert detect_swings(series, strength=2) == ()


def test_zu_kurze_serie_liefert_nichts():
    series = make_series([_bar(10, 5), _bar(12, 6), _bar(11, 5)])
    assert detect_swings(series, strength=2) == ()


@pytest.mark.parametrize("strength", [1, 2, 3, 5])
def test_streaming_entspricht_batch(strength: int):
    """Die inkrementelle Erkennung muss exakt dieselben Swings liefern wie die Batch-Variante.

    Das ist die wichtigste Zusicherung des Moduls: der Live-Pfad benutzt den
    Detector, Analysen und Tests oft die Batch-Funktion. Weichen sie ab, waeren
    Backtest und Live verschieden.
    """
    prices = [10, 12, 9, 14, 11, 16, 13, 18, 12, 20, 15, 11, 17, 9, 13, 8, 14, 19, 10, 16]
    series = make_series([_bar(p + 2, p - 2) for p in prices])

    batch = detect_swings(series, strength)
    detector = SwingDetector(strength)
    streamed = []
    for i in range(len(series)):
        streamed.extend(detector.update(series, i))

    assert len(streamed) == len(batch)
    for got, want in zip(streamed, batch, strict=True):
        assert (got.index, got.type, got.price, got.confirmed_at_index) == (
            want.index,
            want.type,
            want.price,
            want.confirmed_at_index,
        )


def test_detector_verlangt_steigende_indizes():
    series = make_series([_bar(10, 5)] * 10)
    detector = SwingDetector(2)
    detector.update(series, 5)
    with pytest.raises(ValueError, match="Index muss steigen"):
        detector.update(series, 5)


def test_max_tracked_begrenzt_speicher():
    prices = [10 + (i % 7) * 3 for i in range(300)]
    series = make_series([_bar(p + 2, p - 2) for p in prices])
    detector = SwingDetector(strength=1, max_tracked=10)
    for i in range(len(series)):
        detector.update(series, i)
    assert len(detector.highs) <= 10
    assert len(detector.lows) <= 10
