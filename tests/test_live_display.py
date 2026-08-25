"""Die laufende Kerze: Ticks bewegen die Anzeige, nie die Analyse.

Drei Dinge werden hier festgehalten, und das dritte ist das wichtigste:

1. Der Feed baut aus Ticks eine laufende Bar im richtigen Bucket.
2. Der Service setzt daraus und aus `forming` die Kerze zusammen, die der
   Chart zeichnet - im Raster der Analyse, ohne doppelte Zeitstempel.
3. **Nichts davon erreicht `MarketContext`.** Die Tickbar liegt nie in der
   Nachrichten-Queue, aus der die Sitzung liest. Faellt dieser Test, ist die
   Aussage "Backtest = Live" wertlos, denn dann saehe die Engine live einen
   Zustand, den sie im Backtest nie sieht (Invariante 1).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from tests.conftest import DEFAULT_START, PROJECT_ROOT, flat_bars, make_series
from tests.test_nt8_feed import BridgeServer, bar_message, bridge  # noqa: F401
from tradex.data.store import BarStore
from tradex.domain.bars import Bar
from tradex.domain.enums import Timeframe
from tradex.live.nt8_feed import NinjaTraderFeed
from tradex.service import TradexService

SYMBOL = "MNQ_DEMO"
MINUTE_NS = 60_000_000_000
TIMEOUT = 5.0


def _tick(price: float, ts: int, size: float = 1.0) -> dict:
    return {"type": "tick", "symbol": "MNQ", "ts": ts, "price": price, "size": size}


def _warte_auf_tickbar(feed: NinjaTraderFeed, anzahl: int) -> None:
    ende = time.monotonic() + TIMEOUT
    while feed.ticks_seen < anzahl and time.monotonic() < ende:
        list(feed.messages(0.05))


# ------------------------------------------------------------------- Feed
def test_ticks_bauen_eine_laufende_bar(bridge: BridgeServer):  # noqa: F811
    """Open vom ersten Tick, High/Low/Close von allen - im Minutenraster."""
    feed = NinjaTraderFeed(("MNQ",), Timeframe.M1, port=bridge.port)
    feed.start()
    bridge.wait_for_client()
    basis = 1_740_000_000_000_000_000  # auf die Minute ausgerichtet
    bridge.send(_tick(21000.0, basis + 1_000_000_000, size=2))
    bridge.send(_tick(21010.0, basis + 5_000_000_000, size=3))
    bridge.send(_tick(20995.0, basis + 9_000_000_000, size=1))
    _warte_auf_tickbar(feed, 3)
    bar = feed.live_bar("MNQ")
    feed.stop()

    assert bar is not None
    assert bar.ts == basis, "der Bucket ist der Minutenbeginn, nicht der Tickzeitpunkt"
    assert (bar.open, bar.high, bar.low, bar.close) == (21000.0, 21010.0, 20995.0, 20995.0)
    assert bar.volume == 6.0


def test_neue_minute_faengt_eine_neue_kerze_an(bridge: BridgeServer):  # noqa: F811
    """Sonst waechst eine einzige Kerze ueber Stunden."""
    feed = NinjaTraderFeed(("MNQ",), Timeframe.M1, port=bridge.port)
    feed.start()
    bridge.wait_for_client()
    basis = 1_740_000_000_000_000_000
    bridge.send(_tick(21000.0, basis + 1_000_000_000))
    bridge.send(_tick(21050.0, basis + MINUTE_NS + 1_000_000_000))
    _warte_auf_tickbar(feed, 2)
    bar = feed.live_bar("MNQ")
    feed.stop()

    assert bar is not None
    assert bar.ts == basis + MINUTE_NS
    assert bar.open == 21050.0, "die neue Minute faengt beim ersten Tick an, nicht beim alten"
    assert bar.high == bar.low == 21050.0


def test_die_tickbar_erreicht_die_analyse_nicht(bridge: BridgeServer):  # noqa: F811
    """Der Waechter fuer Invariante 1.

    Ticks duerfen `last_price` und `live_bar()` fuellen - und sonst nichts.
    Landete daraus je eine Bar in der Queue, wuerde sie analysiert.
    """
    feed = NinjaTraderFeed(("MNQ",), Timeframe.M1, port=bridge.port)
    feed.start()
    bridge.wait_for_client()
    for i in range(5):
        bridge.send(_tick(21000.0 + i, 1_740_000_000_000_000_000 + i * 1_000_000_000))

    from tradex.live.feed import BarMessage

    nachrichten = []
    ende = time.monotonic() + 1.5
    while time.monotonic() < ende:
        nachrichten.extend(feed.messages(0.1))
    feed.stop()

    assert not [m for m in nachrichten if isinstance(m, BarMessage)]
    assert feed.live_bar("MNQ") is not None, "gebaut wird sie trotzdem - nur nebenan"


def test_ein_kurs_von_null_ist_kein_geschaeft(bridge: BridgeServer):  # noqa: F811
    """Er wuerde die laufende Kerze auf die Nulllinie ziehen."""
    feed = NinjaTraderFeed(("MNQ",), Timeframe.M1, port=bridge.port)
    feed.start()
    bridge.wait_for_client()
    bridge.send(_tick(21000.0, 1_740_000_000_000_000_000))
    bridge.send(_tick(0.0, 1_740_000_000_000_000_000 + 1_000_000_000))
    _warte_auf_tickbar(feed, 2)
    bar = feed.live_bar("MNQ")
    feed.stop()

    assert bar is not None
    assert bar.low == 21000.0
    assert feed.malformed >= 1


# ---------------------------------------------------------------- Service
class _FakeWatch:
    """Beobachtung ohne Socket - nur die Schnittstelle, die der Service liest."""

    def __init__(self, symbol: str, bar: Bar | None, alter_sekunden: float = 0.0) -> None:
        self.symbol = symbol
        self.is_running = True
        self._bar = bar
        self.last_tick_ts = time.time_ns() - int(alter_sekunden * 1e9)

    def live_bar(self) -> Bar | None:
        return self._bar

    def stop(self) -> None:
        self.is_running = False


@pytest.fixture
def service(tmp_path: Path) -> Iterator[TradexService]:
    raw = yaml.safe_load((PROJECT_ROOT / "config" / "default.yaml").read_text(encoding="utf-8"))
    raw["data"]["parquet_dir"] = str(tmp_path / "parquet")
    raw["data"]["database"] = str(tmp_path / "tradex.db")
    raw["data"]["log_dir"] = str(tmp_path / "logs")
    raw["data"]["default_symbol"] = SYMBOL
    # Ohne das baut jede Sitzung in diesem Test einen echten IBKR-Adapter und
    # haengt in Timeouts - gruen oder rot je nachdem, ob hier gerade ein
    # Gateway laeuft.
    raw["broker"]["enabled"] = False
    pfad = tmp_path / "config.yaml"
    pfad.write_text(yaml.safe_dump(raw), encoding="utf-8")

    from tradex.config import load_config

    config = load_config(pfad)
    BarStore(config.path(config.data.parquet_dir)).write(
        # 118 Minuten enden MITTEN in einem 5m-Bucket ([115, 120)). Genau das
        # soll der 5m-Test pruefen: die naechste Minute gehoert noch dazu. Auf
        # einer runden Grenze waere er blind fuer den Fall, den es zu zeigen
        # gilt - dort faengt ohnehin eine neue Kerze an.
        SYMBOL,
        Timeframe.M1,
        make_series(flat_bars(118)),
    )
    dienst = TradexService(config, config_path=pfad)
    dienst.load(SYMBOL)
    try:
        yield dienst
    finally:
        dienst.close()


def _letzte_basis_ts() -> int:
    from tradex.domain.bars import to_ns

    return to_ns(DEFAULT_START + timedelta(minutes=117))


def test_ohne_ticks_bleibt_alles_wie_bisher(service: TradexService):
    """Waechter gegen leere Wahrheit: die Kerze entsteht NUR aus Ticks."""
    assert service.display_bar(SYMBOL, Timeframe.M1) is None
    assert service.display_bar(SYMBOL, Timeframe.M5) is None


def test_auf_1m_entsteht_die_kerze_der_laufenden_minute(service: TradexService):
    """Genau der Fehler, um den es geht.

    Das AddOn sendet nur geschlossene Bars: um 14:21:36 ist die letzte die von
    14:20, und `forming` zeigt genau die. Die Minute, die gerade laeuft, kam
    nirgends her - der Chart hing eine Minute hinterher.
    """
    naechste = _letzte_basis_ts() + MINUTE_NS
    service._watch = _FakeWatch(  # type: ignore[assignment]
        SYMBOL, Bar(ts=naechste, open=21000.0, high=21008.0, low=20999.0, close=21005.0, volume=0.0)
    )

    forming = service.forming(SYMBOL, Timeframe.M1)
    kerze = service.display_bar(SYMBOL, Timeframe.M1)

    assert forming is not None and forming.ts == _letzte_basis_ts()
    assert kerze is not None
    assert kerze.ts == naechste, "die laufende Kerze gehoert in die laufende Minute"
    assert kerze.ts != forming.ts, "zwei verschiedene Minuten, zwei Kerzen"
    assert kerze.close == 21005.0


def test_auf_5m_wird_der_zwischenstand_ergaenzt_statt_ersetzt(service: TradexService):
    """Eroeffnung und Extremwerte der schon geschlossenen Minuten bleiben.

    Sonst zeigte die 5m-Kerze nur die letzte Minute und sprang bei jedem
    Bar-Schluss - sie waere keine 5m-Kerze mehr.
    """
    naechste = _letzte_basis_ts() + MINUTE_NS
    forming = service.forming(SYMBOL, Timeframe.M5)
    assert forming is not None
    service._watch = _FakeWatch(  # type: ignore[assignment]
        SYMBOL,
        Bar(
            ts=naechste,
            open=forming.close,
            high=forming.high + 25.0,
            low=forming.low - 25.0,
            close=forming.close + 7.0, volume=0.0,
        ),
    )

    kerze = service.display_bar(SYMBOL, Timeframe.M5)
    assert kerze is not None
    assert kerze.ts == forming.ts, "derselbe 5m-Bucket - keine zweite Kerze"
    assert kerze.open == forming.open, "die Eroeffnung gehoert der ersten Minute"
    assert kerze.high == forming.high + 25.0
    assert kerze.low == forming.low - 25.0
    assert kerze.close == forming.close + 7.0


def test_ein_veralteter_tick_zeigt_keine_laufende_kerze(service: TradexService):
    """Eine stehende Kerze, die wie eine laufende aussieht, sieht nach Markt
    aus, wo in Wirklichkeit die Verbindung weg ist."""
    naechste = _letzte_basis_ts() + MINUTE_NS
    alter = service.config.live.display_tick_max_age_seconds + 5.0
    service._watch = _FakeWatch(  # type: ignore[assignment]
        SYMBOL,
        Bar(ts=naechste, open=21000.0, high=21000.0, low=21000.0, close=21000.0, volume=0.0),
        alter_sekunden=alter,
    )
    assert service.display_bar(SYMBOL, Timeframe.M1) is None


def test_ein_tick_in_einem_geschlossenen_bucket_wird_verworfen(service: TradexService):
    """Sonst stuenden zwei Kerzen mit demselben Zeitstempel im Chart."""
    serie = service.bars(SYMBOL, Timeframe.M1)
    service._watch = _FakeWatch(  # type: ignore[assignment]
        SYMBOL,
        Bar(ts=int(serie.ts[-1]), open=1.0, high=1.0, low=1.0, close=1.0, volume=0.0),
    )
    assert service.display_bar(SYMBOL, Timeframe.M1) is None


def test_die_laufende_kerze_veraendert_die_analyse_nicht(service: TradexService):
    """Der zweite Waechter fuer Invariante 1 - diesmal eine Ebene hoeher."""
    vorher = len(service.bars(SYMBOL, Timeframe.M1))
    schnappschuss = service.snapshot(SYMBOL)
    naechste = _letzte_basis_ts() + MINUTE_NS
    service._watch = _FakeWatch(  # type: ignore[assignment]
        SYMBOL,
        # Ein Ausreisser, den jeder Detektor bemerken wuerde - wenn er ihn saehe.
        Bar(ts=naechste, open=21000.0, high=99999.0, low=1.0, close=21000.0, volume=0.0),
    )

    assert service.display_bar(SYMBOL, Timeframe.M1) is not None
    assert len(service.bars(SYMBOL, Timeframe.M1)) == vorher
    assert service.snapshot(SYMBOL).last_ts == schnappschuss.last_ts


# ------------------------------------------------ Nach dem Sitzungsende
class _ToterManager:
    """Ein `SessionManager` nach `stop()`: Sitzung noch da, Faden tot.

    Genau dieser Zustand entsteht im Betrieb - der Manager raeumt `_session`
    und `_feed` absichtlich nicht weg, weil der Abschlussbericht sie braucht.
    """

    def __init__(self, context: object, bar: Bar) -> None:
        self.is_running = False
        self._context = context
        self._bar = bar

    def context(self, symbol: str) -> object:
        return self._context

    def strategy(self, symbol: str) -> object:
        return None

    def live_bar(self, symbol: str) -> Bar:
        return self._bar

    def last_tick_ts(self, symbol: str) -> int:
        return time.time_ns()


def test_eine_beendete_sitzung_friert_den_chart_nicht_ein(service: TradexService):
    """Der Kurs stand nach jedem Sitzungsende still.

    `SessionManager` haelt seine beendete Sitzung fest. Ohne die Pruefung auf
    `is_running` zeigte der Chart weiter auf deren toten Feed - der Kurs
    bewegte sich nicht mehr, waehrend die inzwischen wieder laufende
    Beobachtung daneben Ticks bekam. Ein eingefrorener Kurs sieht aus wie ein
    ruhiger Markt, und das ist die gefaehrliche Verwechslung.
    """
    eigener = service.state(SYMBOL).context
    veraltet = Bar(ts=1, open=1.0, high=1.0, low=1.0, close=1.0, volume=0.0)
    service.sessions = _ToterManager(object(), veraltet)  # type: ignore[assignment]

    assert not service.is_live(SYMBOL), "eine beendete Sitzung ist nicht 'live'"
    assert service.chart_context(SYMBOL) is eigener, "der Chart muss zurueckfallen"
    # Und die Tickquelle darf nicht mehr am toten Feed haengen.
    quelle, _ = service._tick_source(SYMBOL)
    assert quelle is not veraltet


# ------------------------------------------------------- Bucket-Raster teilen
def test_die_anzeige_rechnet_im_selben_raster_wie_die_analyse(service: TradexService):
    """Zwei Bucket-Rechnungen nebeneinander waeren zwei Wahrheiten.

    Sie fallen erst am Rand des Rasters auseinander - und dort erzeugen sie
    eine Kerze zu viel, also genau dort, wo niemand mehr hinsieht.
    """
    context = service.chart_context(SYMBOL)
    serie = context.series(Timeframe.M5)
    assert len(serie), "ohne 5m-Bars ist der Vergleich leer"
    for ts in (int(serie.ts[0]), int(serie.ts[-1])):
        assert context.bucket_start(ts, Timeframe.M5) == ts
        assert context.bucket_start(ts + MINUTE_NS, Timeframe.M5) == ts
