"""Marktstruktur: BOS, MSS und die Zustandsmaschine dahinter."""

from __future__ import annotations

from tests.conftest import make_series
from tradex.analysis.structure import StructureTracker
from tradex.analysis.swings import Swing, SwingDetector
from tradex.config import Config
from tradex.domain.enums import StructureEventType, StructureState, SwingType

TICK = 0.25


def _swing(index: int, price: float, kind: SwingType, confirmed_at: int) -> Swing:
    return Swing(
        index=index,
        ts=index,
        price=price,
        type=kind,
        strength=confirmed_at - index,
        confirmed_at_index=confirmed_at,
    )


def test_erster_bruch_nach_oben_ist_bos(config: Config):
    series = make_series([(100, 101, 99, 100)] * 5 + [(100, 106, 99, 105)])
    tracker = StructureTracker(config.analysis.structure, TICK)
    tracker.register_swings([_swing(1, 102.0, SwingType.HIGH, 3)])

    event = tracker.update(series, 5)
    assert event is not None
    assert event.type is StructureEventType.BOS_BULLISH
    assert event.previous_state is StructureState.RANGE
    assert event.new_state is StructureState.BULLISH
    assert event.broken_price == 102.0
    assert event.break_price == 105.0
    assert not event.is_mss


def test_bruch_gegen_den_zustand_ist_mss(config: Config):
    """Nach einem bearishen Zustand ist der Bruch nach oben ein MSS, kein BOS.

    Das ist die Pflichtbedingung aus Spec §7 Schritt 6.
    """
    tracker = StructureTracker(config.analysis.structure, TICK)

    # Erst nach unten brechen -> Zustand BEARISH
    tracker.register_swings([_swing(1, 98.0, SwingType.LOW, 3)])
    down = make_series([(100, 101, 99, 100)] * 5 + [(100, 101, 94, 95)])
    assert tracker.update(down, 5).type is StructureEventType.BOS_BEARISH
    assert tracker.state is StructureState.BEARISH

    # Dann nach oben brechen -> MSS
    tracker.register_swings([_swing(6, 102.0, SwingType.HIGH, 8)])
    up = make_series([(100, 101, 99, 100)] * 9 + [(100, 107, 99, 106)])
    event = tracker.update(up, 9)
    assert event is not None
    assert event.type is StructureEventType.MSS_BULLISH
    assert event.is_mss
    assert tracker.state is StructureState.BULLISH


def test_gebrochener_swing_feuert_nicht_erneut(config: Config):
    """Ohne Verbrauch wuerde jede Folgebar ueber demselben Hoch ein BOS melden."""
    series = make_series([(100, 101, 99, 100)] * 5 + [(100, 106, 99, 105)] * 3)
    tracker = StructureTracker(config.analysis.structure, TICK)
    tracker.register_swings([_swing(1, 102.0, SwingType.HIGH, 3)])

    assert tracker.update(series, 5) is not None
    assert tracker.update(series, 6) is None
    assert tracker.update(series, 7) is None
    assert len(tracker.events) == 1


def test_docht_ueber_dem_hoch_ist_kein_bruch(config: Config):
    """Default `confirm_on: close` - genau das trennt Sweep von Strukturbruch."""
    series = make_series([(100, 101, 99, 100)] * 5 + [(100, 108, 99, 101)])
    tracker = StructureTracker(config.analysis.structure, TICK)
    tracker.register_swings([_swing(1, 102.0, SwingType.HIGH, 3)])
    assert tracker.update(series, 5) is None


def test_wick_modus_wertet_dochte(config: Config):
    from tradex.config import StructureParams

    params = StructureParams(**{**config.analysis.structure.model_dump(), "confirm_on": "wick"})
    series = make_series([(100, 101, 99, 100)] * 5 + [(100, 108, 99, 101)])
    tracker = StructureTracker(params, TICK)
    tracker.register_swings([_swing(1, 102.0, SwingType.HIGH, 3)])

    event = tracker.update(series, 5)
    assert event is not None
    assert event.break_price == 108.0


def test_swing_labels_hh_hl_lh_ll(config: Config):
    tracker = StructureTracker(config.analysis.structure, TICK)
    tracker.register_swings([_swing(1, 100.0, SwingType.HIGH, 3)])
    tracker.register_swings([_swing(5, 105.0, SwingType.HIGH, 7)])  # HH
    tracker.register_swings([_swing(9, 103.0, SwingType.HIGH, 11)])  # LH
    tracker.register_swings([_swing(2, 90.0, SwingType.LOW, 4)])
    tracker.register_swings([_swing(6, 92.0, SwingType.LOW, 8)])  # HL
    tracker.register_swings([_swing(10, 88.0, SwingType.LOW, 12)])  # LL

    assert [label.label for label in tracker.labels] == ["HH", "LH", "HL", "LL"]


def test_mss_within_zeitfenster(config: Config):
    tracker = StructureTracker(config.analysis.structure, TICK)
    tracker.register_swings([_swing(1, 98.0, SwingType.LOW, 3)])
    down = make_series([(100, 101, 99, 100)] * 5 + [(100, 101, 94, 95)])
    tracker.update(down, 5)
    tracker.register_swings([_swing(6, 102.0, SwingType.HIGH, 8)])
    up = make_series([(100, 101, 99, 100)] * 9 + [(100, 107, 99, 106)])
    tracker.update(up, 9)

    assert tracker.mss_within(index=11, lookback_bars=5) is not None
    assert tracker.mss_within(index=20, lookback_bars=5) is None, "MSS zu alt"
    assert tracker.last_mss() is not None


def test_juengster_swing_ist_referenz_auch_wenn_niedriger(config: Config):
    """Referenz ist der zuletzt bestaetigte Swing, nicht der hoechste.

    Liegt er bei Bestaetigung bereits unter dem Kurs, feuert der Bruch sofort -
    im Live-Betrieb weiss man vorher nichts von ihm, und Backtest und Live
    muessen sich identisch verhalten.
    """
    series = make_series([(100, 101, 99, 100)] * 5 + [(100, 106, 99, 105)])
    tracker = StructureTracker(config.analysis.structure, TICK)
    tracker.register_swings([_swing(1, 120.0, SwingType.HIGH, 3)])
    tracker.register_swings([_swing(2, 102.0, SwingType.HIGH, 4)])

    event = tracker.update(series, 5)
    assert event is not None
    assert event.broken_price == 102.0


def test_zusammenspiel_mit_swingdetector(config: Config):
    """Ende-zu-Ende: echte Bars, echte Swings, echter Strukturbruch."""
    bars = [
        (100, 102, 98, 101),
        (101, 103, 99, 102),
        (102, 110, 101, 109),  # Swing High bei 110
        (109, 108, 104, 105),
        (105, 106, 100, 101),
        (101, 103, 99, 100),
        (100, 112, 99, 111),  # Close 111 > 110 -> BOS bullish
    ]
    series = make_series(bars)
    detector = SwingDetector(strength=2)
    tracker = StructureTracker(config.analysis.structure, TICK)

    events = []
    for i in range(len(series)):
        tracker.register_swings(detector.update(series, i))
        event = tracker.update(series, i)
        if event:
            events.append(event)

    assert len(events) == 1
    assert events[0].type is StructureEventType.BOS_BULLISH
    assert events[0].broken_price == 110.0
    assert events[0].index == 6
