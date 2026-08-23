"""API-Schicht: der Vertrag zwischen Engine und Oberflaeche.

Getestet wird gegen einen eigenen, temporaeren Datenbestand - nie gegen den
echten Speicher des Nutzers. Sonst haetten Tests je nach vorhandenen Daten
unterschiedliche Ergebnisse, und genau das soll dieses Projekt nicht haben.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest
import yaml
from fastapi.testclient import TestClient

from tests.conftest import DEFAULT_START, PROJECT_ROOT
from tradex.api import state as api_state
from tradex.api.server import create_app
from tradex.config import load_config
from tradex.data.store import BarStore
from tradex.domain.bars import BarSeries, to_ns
from tradex.domain.enums import Timeframe
from tradex.logging_setup import get_logger
from tradex.service import STRATEGY_VERSION, TradexService

SYMBOL = "MNQ_DEMO"


def _series(minutes: int) -> BarSeries:
    """Kursverlauf mit Trend und Rauschen - erzeugt genug Muster zum Testen."""
    rng = np.random.default_rng(5)
    series = BarSeries()
    price = 21000.0
    for i in range(minutes):
        close = price + 0.05 + float(rng.normal(0, 2.5))
        high = max(price, close) + abs(float(rng.normal(0, 1.5)))
        low = min(price, close) - abs(float(rng.normal(0, 1.5)))
        series.append(
            to_ns(DEFAULT_START + timedelta(minutes=i)),
            price,
            high,
            low,
            close,
            float(rng.integers(50, 600)),
        )
        price = close
    return series


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    tmp: Path = tmp_path_factory.mktemp("api")

    raw = yaml.safe_load((PROJECT_ROOT / "config" / "default.yaml").read_text(encoding="utf-8"))
    raw["data"]["parquet_dir"] = str(tmp / "parquet")
    raw["data"]["database"] = str(tmp / "tradex.db")
    raw["data"]["log_dir"] = str(tmp / "logs")
    raw["data"]["default_symbol"] = SYMBOL
    config_path = tmp / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    config = load_config(config_path)
    BarStore(config.path(config.data.parquet_dir)).write(SYMBOL, Timeframe.M1, _series(60 * 24 * 3))

    service = TradexService(config, config_path=config_path)
    api_state._service = service
    try:
        with TestClient(create_app(config)) as test_client:
            yield test_client
    finally:
        service.close()
        api_state._service = None


def test_health(client: TestClient):
    body = client.get("/api/health").json()
    # Gegen die geladene Konfiguration, nicht gegen einen festen Text: welcher
    # Modus eingestellt ist, aendert sich im Betrieb (analysis_only ->
    # paper_auto -> spaeter live). Was sich NICHT aendern darf, ist dass die
    # Anzeige denselben Modus meldet, unter dem auch entschieden wird - ein
    # fest verdrahteter Wert haette nur die Auslieferung geprueft, nicht den
    # Vertrag.
    erwartet = load_config(PROJECT_ROOT / "config" / "default.yaml")
    assert body["mode"] == erwartet.execution.mode.value
    assert body["live_trading_enabled"] is False, "Spec 24: Live ist standardmaessig aus"
    assert len(body["config_hash"]) == 16
    assert any(p["name"] == "replay" for p in body["providers"])
    # Spec 21: die angezeigte Regelfassung muss die sein, nach der auch
    # entschieden und protokolliert wird - sonst zeigt das Dashboard eine
    # Version an, unter der nie eine Entscheidung fiel.
    assert body["strategy_version"] == STRATEGY_VERSION


def test_instrumente_und_bestand(client: TestClient):
    instruments = client.get("/api/instruments").json()
    assert {item["symbol"] for item in instruments} >= {"MNQ", "NQ"}

    coverage = client.get("/api/coverage").json()
    assert len(coverage) == 1
    assert coverage[0]["symbol"] == SYMBOL
    assert coverage[0]["bar_count"] == 60 * 24 * 3


def test_laden_analysiert_und_meldet_datenqualitaet(client: TestClient):
    body = client.post("/api/load", json={"symbol": SYMBOL, "feed_all": True}).json()
    assert body["base_bars"] == 60 * 24 * 3
    assert body["cursor"] == body["base_bars"]
    assert body["progress"] == 1.0
    assert body["integrity"]["is_clean"] is True
    # Demodaten muessen als solche gekennzeichnet sein, sonst koennten sie mit
    # echten Marktdaten verwechselt werden.
    assert any("SYNTHETISCH" in warning for warning in body["warnings"])


def test_unbekanntes_symbol_liefert_erklaerenden_fehler(client: TestClient):
    response = client.post("/api/load", json={"symbol": "NQ", "feed_all": True})
    assert response.status_code == 404
    assert "Keine 1m-Daten" in response.json()["detail"]


@pytest.mark.parametrize("timeframe", ["1m", "5m", "15m", "1h", "4h"])
def test_bars_je_timeframe(client: TestClient, timeframe: str):
    client.post("/api/load", json={"symbol": SYMBOL, "feed_all": True})
    body = client.get(f"/api/bars?symbol={SYMBOL}&timeframe={timeframe}&limit=500").json()

    assert body["timeframe"] == timeframe
    assert len(body["bars"]) > 0
    assert len(body["bars"]) <= 500
    stamps = [bar["ts"] for bar in body["bars"]]
    assert stamps == sorted(stamps), "Bars muessen zeitlich aufsteigend kommen"

    # Die laufende Bar wird getrennt uebertragen und darf nicht in der Liste stehen
    # (Architektur-Invariante 1).
    if body["forming"]:
        assert body["forming"]["ts"] not in stamps


def test_ungueltiger_timeframe(client: TestClient):
    client.post("/api/load", json={"symbol": SYMBOL, "feed_all": True})
    response = client.get(f"/api/bars?symbol={SYMBOL}&timeframe=3m")
    assert response.status_code == 400
    assert "Unbekannter Timeframe" in response.json()["detail"]


def test_analyse_liefert_bias_mit_begruendungen(client: TestClient):
    client.post("/api/load", json={"symbol": SYMBOL, "feed_all": True})
    body = client.get(f"/api/analysis?symbol={SYMBOL}").json()

    assert body["symbol"] == SYMBOL
    assert body["bias"]["bias"] in ("bullish", "bearish", "neutral")
    assert len(body["bias"]["reasons"]) > 0
    assert {"4h", "1h", "15m", "5m", "1m"} == set(body["timeframes"])
    # Reason-Codes sind Codes, keine fertigen Saetze - uebersetzt wird im UI.
    for reason in body["bias"]["reasons"]:
        assert "." in reason["code"]
        assert isinstance(reason["ok"], bool)


def test_overlays_enthalten_alle_erkannten_muster(client: TestClient):
    client.post("/api/load", json={"symbol": SYMBOL, "feed_all": True})
    body = client.get(f"/api/overlays?symbol={SYMBOL}&timeframe=5m").json()

    assert len(body["swings"]) > 0
    assert len(body["fvgs"]) > 0
    assert len(body["structure_events"]) > 0

    # Erledigte Zonen tragen ein Ende, aktive nicht - daran haengt die Darstellung.
    for zone in body["fvgs"]:
        done = zone["state"] in ("mitigated", "expired")
        assert (zone["closed_ts"] is not None) == done, zone


def test_schrittweise_wiedergabe(client: TestClient):
    """Der Wiedergabepfad ist derselbe, den spaeter der Live-Feed benutzt."""
    load = client.post("/api/load", json={"symbol": SYMBOL, "feed_all": False}).json()
    assert load["cursor"] == 0

    stepped = client.post("/api/step", json={"symbol": SYMBOL, "count": 500}).json()
    assert stepped["cursor"] == 500
    assert stepped["exhausted"] is False

    again = client.post("/api/step", json={"symbol": SYMBOL, "count": 500}).json()
    assert again["cursor"] == 1000

    reset = client.post(f"/api/reset?symbol={SYMBOL}").json()
    assert reset["cursor"] == 0


def test_schrittweise_analyse_entspricht_komplettlauf(client: TestClient):
    """In Portionen gefuettert muss dasselbe herauskommen wie am Stueck.

    Das ist die API-Ebene von Architektur-Invariante 3.
    """
    client.post("/api/load", json={"symbol": SYMBOL, "feed_all": True})
    complete = client.get(f"/api/analysis?symbol={SYMBOL}").json()

    load = client.post("/api/load", json={"symbol": SYMBOL, "feed_all": False}).json()
    remaining = load["base_bars"]
    while remaining > 0:
        chunk = min(remaining, 777)  # bewusst krumm, damit Portionsgrenzen wandern
        client.post("/api/step", json={"symbol": SYMBOL, "count": chunk})
        remaining -= chunk
    chunked = client.get(f"/api/analysis?symbol={SYMBOL}").json()

    assert chunked == complete


def test_entscheidungsprotokoll_wird_geschrieben(client: TestClient):
    client.post("/api/load", json={"symbol": SYMBOL, "feed_all": True})
    body = client.post(f"/api/record?symbol={SYMBOL}").json()
    assert body["id"] > 0
    assert body["symbol"] == SYMBOL


# ------------------------------------------------------------- Backtest (Ph. 4)
def test_backtest_liefert_bericht_und_haelt_ihn_fest(client: TestClient):
    """Der Lauf muss ohne vorheriges /api/load funktionieren.

    Sonst haenge das Ergebnis davon ab, wie weit man im Chart vorgespult hat -
    und waere damit nicht reproduzierbar.
    """
    body = client.post("/api/backtest", json={"symbol": SYMBOL, "save": True}).json()

    assert body["symbol"] == SYMBOL
    assert body["bars"] == 60 * 24 * 3
    assert body["overall"]["trades"] == body["trades_total"]
    assert body["min_trades"] > 0
    # Demodaten muessen im Bericht als solche gekennzeichnet sein.
    assert any("DEMODATEN" in warning for warning in body["warnings"])

    again = client.get(f"/api/backtest?symbol={SYMBOL}").json()
    assert again["overall"] == body["overall"]

    runs = client.get(f"/api/backtest/runs?symbol={SYMBOL}").json()
    assert runs, "der Lauf wurde nicht gespeichert"
    assert runs[0]["trades"] == body["overall"]["trades"]
    assert len(runs[0]["config_hash"]) == 16
    assert runs[0]["backtest_version"] == body["backtest_version"]


def test_backtest_laesst_den_wiedergabezustand_unberuehrt(client: TestClient):
    client.post("/api/load", json={"symbol": SYMBOL, "feed_all": False})
    client.post("/api/step", json={"symbol": SYMBOL, "count": 300})

    client.post("/api/backtest", json={"symbol": SYMBOL, "save": False})

    stepped = client.post("/api/step", json={"symbol": SYMBOL, "count": 1}).json()
    assert stepped["cursor"] == 301, "der Backtest hat den Replay-Cursor verschoben"


def test_backtest_ohne_daten_erklaert_sich(client: TestClient):
    response = client.post("/api/backtest", json={"symbol": "NQ"})
    assert response.status_code == 404
    assert "Keine 1m-Daten" in response.json()["detail"]


def test_backtest_ohne_lauf_erklaert_sich(client: TestClient):
    response = client.get("/api/backtest?symbol=NQ")
    assert response.status_code == 404
    assert "noch kein Backtest" in response.json()["detail"]


def test_logs(client: TestClient):
    entries = client.get("/api/logs?limit=50").json()
    assert isinstance(entries, list)


def test_ui_puffer_zaehlt_jedes_ereignis_genau_einmal(client: TestClient):
    """Der UI-Puffer haengt nur in der structlog-Kette, nicht je Handler.

    Geprueft wird die Eigenschaft selbst statt der frueheren Naeherung "keine
    zwei Eintraege mit gleichem Zeitstempel und Text". Die war unter Windows
    falsch: die Systemuhr springt dort in Schritten von rund 15 ms, zwei
    unabhaengige gleichnamige Ereignisse bekommen also regelmaessig denselben
    Zeitstempel - der Test schlug zufaellig fehl, ohne dass etwas doppelt war.
    """
    marker = "test_ui_puffer_marker"
    get_logger(__name__).info(marker, quelle="structlog")

    # Ein fremder Logrecord (so kommt uvicorn herein). Er laeuft ueber die
    # foreign_pre_chain der Handler - dort haengt der Puffer bewusst NICHT,
    # sonst stuende jede HTTP-Zugriffszeile im Protokoll des UI, und zwar
    # einmal je Handler.
    logging.getLogger("uvicorn.access").info("test_ui_puffer_fremd")

    entries = client.get("/api/logs?limit=200").json()
    events = [entry["event"] for entry in entries]
    assert events.count(marker) == 1
    assert "test_ui_puffer_fremd" not in events
