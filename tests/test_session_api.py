"""Der laufende Betrieb ueber die API: sichtbar und anhaltbar (Phase 7).

Der wichtigste Test hier ist der Kill Switch. Ein Not-Aus, der nur in der
Theorie wirkt, ist schlimmer als keiner - er erzeugt Vertrauen, das er nicht
traegt.
"""

from __future__ import annotations

import time
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
from tradex.persistence.db import connect
from tradex.service import TradexService

SYMBOL = "MNQ_DEMO"
BARS = 60 * 24 * 3
TIMEOUT = 20.0


def _series(minutes: int) -> BarSeries:
    rng = np.random.default_rng(7)
    series = BarSeries()
    price = 21000.0
    for i in range(minutes):
        close = price + 0.05 + float(rng.normal(0, 2.5))
        high = max(price, close) + abs(float(rng.normal(0, 1.5)))
        low = min(price, close) - abs(float(rng.normal(0, 1.5)))
        series.append(
            to_ns(DEFAULT_START + timedelta(minutes=i)), price, high, low, close,
            float(rng.integers(50, 600)),
        )
        price = close
    return series


@pytest.fixture
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    """Eigener Datenbestand je Test.

    Bewusst NICHT modulweit wie in `test_api.py`: hier laeuft ein Faden mit
    Zustand. Ein Test, der eine Sitzung stehen laesst, wuerde sonst den
    naechsten zum Scheitern bringen - und zwar an einer voellig anderen
    Stelle.
    """
    tmp: Path = tmp_path_factory.mktemp("session_api")
    raw = yaml.safe_load((PROJECT_ROOT / "config" / "default.yaml").read_text(encoding="utf-8"))
    raw["data"]["parquet_dir"] = str(tmp / "parquet")
    raw["data"]["database"] = str(tmp / "tradex.db")
    raw["data"]["log_dir"] = str(tmp / "logs")
    raw["data"]["default_symbol"] = SYMBOL
    config_path = tmp / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    config = load_config(config_path)
    BarStore(config.path(config.data.parquet_dir)).write(SYMBOL, Timeframe.M1, _series(BARS))

    service = TradexService(config, config_path=config_path)
    api_state._service = service
    try:
        with TestClient(create_app(config)) as test_client:
            yield test_client
    finally:
        service.close()
        api_state._service = None


def start(client: TestClient, **overrides: object) -> dict:
    payload = {
        "symbols": [SYMBOL],
        "feed": "replay",
        "speed": 0.0,
        "save": False,
        "max_bars": 1500,
        **overrides,
    }
    response = client.post("/api/session/start", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def wait_until(client: TestClient, predicate, seconds: float = TIMEOUT) -> dict:
    """Auf einen Zustand warten, statt eine Dauer zu raten.

    Ein `sleep(2)` waere auf einer langsamen Maschine zu kurz und auf einer
    schnellen Zeitverschwendung - und beides faellt erst auf, wenn der Test
    sporadisch scheitert.
    """
    ende = time.monotonic() + seconds
    body = client.get("/api/session").json()
    while time.monotonic() < ende and not predicate(body):
        time.sleep(0.05)
        body = client.get("/api/session").json()
    return body


# ------------------------------------------------------------------- Zustand
def test_ohne_sitzung_ist_der_zustand_trotzdem_aussagefaehig(client: TestClient):
    """Eine leere Anzeige waere von einer kaputten nicht zu unterscheiden."""
    body = client.get("/api/session").json()
    assert body["active"] is False
    assert body["running"] is False
    assert body["accepts_entries"] is False
    assert body["warnings"] == []


def test_start_meldet_den_laufenden_betrieb(client: TestClient):
    body = start(client)
    assert body["active"] is True
    assert body["feed"] == "replay"
    assert body["symbols"] == [SYMBOL]

    laeuft = wait_until(client, lambda b: b["bars_seen"] > 100)
    assert laeuft["connected"] is True
    assert laeuft["accepts_entries"] is True
    assert laeuft["start_equity"] > 0


def test_eine_frisch_gestartete_sitzung_nimmt_erst_nach_verbindung_positionen_auf(
    client: TestClient,
):
    """Sonst handelte sie auf Bars aus einer nie bestaetigten Quelle."""
    assert start(client)["accepts_entries"] is False


def test_zwei_sitzungen_gleichzeitig_werden_abgelehnt(client: TestClient):
    """Sie haetten getrennte Risikobuecher - zusammen das doppelte Risiko."""
    start(client)
    zweite = client.post(
        "/api/session/start",
        json={"symbols": [SYMBOL], "feed": "replay", "speed": 0.0, "save": False},
    )
    assert zweite.status_code == 404
    assert "bereits eine Sitzung" in zweite.json()["detail"]


def test_unbekanntes_symbol_und_unbekannter_feed(client: TestClient):
    fehlt = client.post("/api/session/start", json={"symbols": ["GIBTESNICHT"], "save": False})
    assert fehlt.status_code == 404
    assert "Unbekannte Symbole" in fehlt.json()["detail"]

    feed = client.post(
        "/api/session/start", json={"symbols": [SYMBOL], "feed": "quelle", "save": False}
    )
    assert feed.status_code == 404
    assert "Unbekannter Feed" in feed.json()["detail"]


# ---------------------------------------------------------------- Kill Switch
def test_kill_switch_stoppt_neue_positionen_sofort(client: TestClient):
    start(client)
    wait_until(client, lambda b: b["accepts_entries"])

    body = client.post("/api/session/halt").json()
    assert body["halted_reason"] == "manual"
    assert body["accepts_entries"] is False
    assert any("Angehalten" in w for w in body["warnings"])


def test_angehalten_heisst_nicht_abgeschaltet(client: TestClient):
    """Eine angehaltene Sitzung verarbeitet WEITER Bars.

    Wer stattdessen die Bars abklemmt, laesst offene Positionen ohne
    Stopueberwachung zurueck - die gefaehrlichste Reaktion auf eine Stoerung.
    """
    start(client, max_bars=0)
    wait_until(client, lambda b: b["bars_seen"] > 50)
    client.post("/api/session/halt")

    vorher = client.get("/api/session").json()["bars_seen"]
    nachher = wait_until(client, lambda b: b["bars_seen"] > vorher, seconds=10.0)
    assert nachher["bars_seen"] > vorher, "im Not-Aus muessen Bars weiterlaufen"
    assert nachher["halted_reason"] == "manual"
    client.post("/api/session/stop")


def test_resume_hebt_den_not_aus_auf(client: TestClient):
    start(client)
    wait_until(client, lambda b: b["accepts_entries"])
    client.post("/api/session/halt")
    body = client.post("/api/session/resume").json()
    assert body["halted_reason"] == ""


def test_stop_beendet_und_meldet_den_grund(client: TestClient):
    start(client, max_bars=0)
    wait_until(client, lambda b: b["bars_seen"] > 50)
    body = client.post("/api/session/stop").json()

    assert body["active"] is False
    assert body["running"] is False
    assert body["stopped_by"] in {"abbruch", "feed_ende", "max_bars"}


@pytest.mark.parametrize("pfad", ["/api/session/halt", "/api/session/resume", "/api/session/stop"])
def test_steuerbefehle_ohne_sitzung_erklaeren_sich(client: TestClient, pfad: str):
    response = client.post(pfad)
    assert response.status_code == 404
    assert "keine Sitzung" in response.json()["detail"]


# ------------------------------------------------------------------ Warnungen
def test_wiedergabe_wird_als_solche_ausgewiesen(client: TestClient):
    """Historische Bars duerfen nicht wie ein Marktbetrieb aussehen."""
    start(client)
    body = wait_until(client, lambda b: b["warnings"])
    assert any("Wiedergabe" in w for w in body["warnings"])


# -------------------------------------------------------------------- Archiv
def test_gespeicherte_sitzung_landet_im_archiv(client: TestClient):
    start(client, save=True, max_bars=200, notes="Testlauf")
    wait_until(client, lambda b: not b["active"], seconds=TIMEOUT)

    runs = client.get("/api/sessions").json()
    assert runs, "die Sitzung muss im Archiv stehen"
    assert runs[0]["notes"] == "Testlauf"
    assert runs[0]["feed"] == "replay"
    assert runs[0]["ended_utc"], "eine planmaessig beendete Sitzung hat ein Ende"


def test_nicht_gespeicherte_sitzung_landet_nicht_im_archiv(client: TestClient):
    start(client, save=False, max_bars=200)
    wait_until(client, lambda b: not b["active"])
    assert client.get("/api/sessions").json() == []


def test_betriebsereignisse_landen_in_der_datenbank(client: TestClient):
    """Ein Not-Aus um drei Uhr nachts muss am Morgen nachweisbar sein.

    Das Textlog rotiert und ist nicht abfragbar. `system_events` gibt es seit
    Migration 1 - der laufende Betrieb hat bisher nichts hineingeschrieben.
    """
    start(client, save=False, max_bars=300)
    wait_until(client, lambda b: b["bars_seen"] > 50)
    client.post("/api/session/halt")
    wait_until(client, lambda b: not b["active"])

    service = api_state.get_service()
    with connect(service.database) as conn:
        rows = conn.execute(
            "SELECT category, level, message FROM system_events "
            "WHERE category LIKE 'session.%' ORDER BY id"
        ).fetchall()

    kategorien = [row["category"] for row in rows]
    assert "session.halt" in kategorien
    assert "session.feed" in kategorien
    halts = [row for row in rows if row["category"] == "session.halt"]
    assert all(row["level"] == "warning" for row in halts), "ein Not-Aus ist keine Randnotiz"
    assert any("manual" in row["message"] for row in halts)


def test_ereignisse_werden_auch_ohne_archivierung_geschrieben(client: TestClient):
    """"Warum stand der Betrieb?" ist unabhaengig davon, ob die Trades
    interessant genug zum Aufheben waren."""
    start(client, save=False, max_bars=200)
    wait_until(client, lambda b: not b["active"])

    service = api_state.get_service()
    with connect(service.database) as conn:
        anzahl = conn.execute(
            "SELECT COUNT(*) AS n FROM system_events WHERE category LIKE 'session.%'"
        ).fetchone()["n"]
    assert anzahl > 0
    assert client.get("/api/sessions").json() == [], "archiviert wurde trotzdem nichts"


def test_trades_der_laufenden_sitzung_sind_abrufbar(client: TestClient):
    start(client, max_bars=500)
    wait_until(client, lambda b: b["bars_seen"] > 100)
    response = client.get("/api/session/trades?limit=10")
    assert response.status_code == 200
    assert isinstance(response.json(), list)
