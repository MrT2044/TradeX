"""Der Livestrom (SSE) und der Brokerzustand im API-Vertrag.

Was hier zaehlt, ist nicht "es kommt etwas an". Das waere schon bei einem
kaputten Strom erfuellt, der einmal sendet und dann schweigt. Geprueft wird:

    - das erste Ereignis kommt SOFORT, nicht erst bei der ersten Aenderung
      (sonst steht das Dashboard nach dem Laden leer da)
    - unveraenderter Zustand erzeugt keine Ereignisse (sonst waere es
      Dauerabfrage mit anderem Namen)
    - eine Aenderung wird gemeldet
    - der Brokerzustand ist immer vorhanden, auch ohne Anbindung

Warum der Generator direkt geprueft wird und nicht ueber HTTP
--------------------------------------------------------------
Ein SSE-Strom endet nicht von selbst. `TestClient` haelt einen endlosen
Generator offen, bis der Server schliesst - und der schliesst nie. Ein Test
darueber wuerde nicht den Strom pruefen, sondern haengen. Geprueft wird
deshalb `_events()` selbst, mit einer Anfrage, die sich nach einer
festgelegten Zahl von Durchgaengen als getrennt meldet. Das ist zugleich der
Fall, der im Betrieb zaehlt: ein Handy, das das Netz wechselt.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest
import yaml
from fastapi.testclient import TestClient

from tests.conftest import DEFAULT_START, PROJECT_ROOT
from tradex.api import state as api_state
from tradex.api.routes import stream as stream_route
from tradex.api.server import create_app
from tradex.config import load_config
from tradex.data.store import BarStore
from tradex.domain.bars import BarSeries, to_ns
from tradex.domain.enums import Timeframe
from tradex.service import TradexService

SYMBOL = "MNQ_DEMO"


def _series(minutes: int = 600) -> BarSeries:
    rng = np.random.default_rng(11)
    series = BarSeries()
    price = 21000.0
    for i in range(minutes):
        close = price + float(rng.normal(0, 2.0))
        series.append(
            to_ns(DEFAULT_START + timedelta(minutes=i)),
            price,
            max(price, close) + 1.0,
            min(price, close) - 1.0,
            close,
            100.0,
        )
        price = close
    return series


@pytest.fixture
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    tmp: Path = tmp_path_factory.mktemp("stream_api")
    raw = yaml.safe_load((PROJECT_ROOT / "config" / "default.yaml").read_text(encoding="utf-8"))
    raw["data"]["parquet_dir"] = str(tmp / "parquet")
    raw["data"]["database"] = str(tmp / "tradex.db")
    raw["data"]["log_dir"] = str(tmp / "logs")
    raw["data"]["default_symbol"] = SYMBOL
    # Wie in test_session_api.py: kein echter Broker, sonst haengt die Suite
    # an einem laufenden IB Gateway (Begruendung dort).
    raw["broker"]["enabled"] = False
    config_path = tmp / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    config = load_config(config_path)
    BarStore(config.path(config.data.parquet_dir)).write(SYMBOL, Timeframe.M1, _series())

    service = TradexService(config, config_path=config_path)
    api_state._service = service
    try:
        with TestClient(create_app(config)) as test_client:
            yield test_client
    finally:
        service.close()
        api_state._service = None


class _Anfrage:
    """Eine Anfrage, die sich nach `bis` Abfragen als getrennt meldet.

    Bildet genau den Fall ab, der im Betrieb der Normalfall ist: der Betrachter
    geht weg, und der Strom muss von selbst enden statt eine Schleife im Server
    stehen zu lassen.
    """

    def __init__(self, bis: int) -> None:
        self.bis = bis
        self.abfragen = 0

    async def is_disconnected(self) -> bool:
        self.abfragen += 1
        return self.abfragen > self.bis


def _lauf(durchgaenge: int) -> list[bytes]:
    """Den Generator `durchgaenge` Durchgaenge laufen lassen."""

    async def sammeln() -> list[bytes]:
        anfrage = _Anfrage(durchgaenge)
        stuecke: list[bytes] = []
        async for stueck in stream_route._events(anfrage):  # type: ignore[arg-type]
            stuecke.append(stueck)
        return stuecke

    return asyncio.run(sammeln())


def _session_ereignisse(stuecke: list[bytes]) -> list[dict]:
    ereignisse: list[dict] = []
    for stueck in stuecke:
        text = stueck.decode("utf-8")
        if not text.startswith("event: session"):
            continue
        for zeile in text.splitlines():
            if zeile.startswith("data: "):
                ereignisse.append(json.loads(zeile[6:]))
    return ereignisse


@pytest.fixture(autouse=True)
def _schneller_takt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Der Takt von einer Sekunde wuerde die Tests unnoetig lang machen.

    Geaendert wird nur die Wartezeit, nicht die Logik - was geprueft wird,
    ist die Frage "wann wird gesendet", und die haengt am Zustand, nicht an
    der Dauer.
    """
    monkeypatch.setattr(stream_route, "POLL_SECONDS", 0.01)


def test_der_strom_meldet_sofort_und_nicht_erst_bei_der_ersten_aenderung(client: TestClient):
    """Sonst steht das Dashboard nach dem Laden leer da - unter Umstaenden
    minutenlang, naemlich genau solange nichts passiert."""
    ereignisse = _session_ereignisse(_lauf(1))

    assert len(ereignisse) == 1
    assert ereignisse[0]["active"] is False
    assert "broker" in ereignisse[0]


def test_unveraenderter_zustand_erzeugt_keine_weiteren_ereignisse(client: TestClient):
    """Sonst waere es Dauerabfrage unter anderem Namen."""
    ereignisse = _session_ereignisse(_lauf(20))
    assert len(ereignisse) == 1, f"{len(ereignisse)} Ereignisse ohne Zustandsaenderung"


def test_der_heartbeat_ist_ein_benanntes_ereignis(client: TestClient):
    """Eine SSE-Kommentarzeile (`: ping`) haelt die Verbindung offen, loest im
    Browser aber KEINEN Listener aus.

    Genau dieser Fehler war schon einmal da: die Leitung lebte, der Client
    hielt sie fuer tot und meldete nach 20 Sekunden, seine Zahlen seien
    veraltet - auf einem Ueberwachungsschirm die gefaehrlichste Art von
    Falschaussage.
    """
    stuecke = _lauf(int(stream_route.HEARTBEAT_SECONDS / stream_route.POLL_SECONDS) + 5)
    roh = b"".join(stuecke).decode("utf-8")

    assert "event: heartbeat" in roh
    assert not roh.startswith(": "), "Kommentarzeile statt benanntem Ereignis"


def test_eine_aenderung_wird_gemeldet(client: TestClient):
    """Waechter gegen leere Wahrheit: der Strom muss auch senden KOENNEN.

    Ohne diesen Test waere ein Strom, der grundsaetzlich nur einmal sendet,
    von einem funktionierenden nicht zu unterscheiden.
    """
    client.post(
        "/api/session/start",
        json={"symbols": [SYMBOL], "feed": "replay", "speed": 3600.0, "save": False},
    )
    try:
        ereignisse = _session_ereignisse(_lauf(60))
        assert len(ereignisse) >= 2
        assert ereignisse[0]["active"] is True
        # Der Betrieb laeuft: irgendwann sind mehr Bars verarbeitet als am Anfang.
        assert ereignisse[-1]["bars_seen"] > ereignisse[0]["bars_seen"]
    finally:
        client.post("/api/session/stop")


def test_der_strom_endet_wenn_der_betrachter_geht(client: TestClient):
    """Ein Strom, der eine Schleife im Server stehen laesst, ist ein Leck."""
    stuecke = _lauf(3)
    assert stuecke, "es kam gar nichts"
    # Der Generator ist zurueckgekehrt - `_lauf` waere sonst nie fertig geworden.


def test_der_brokerzustand_ist_auch_ohne_anbindung_aussagefaehig(client: TestClient):
    """Ein fehlendes Feld waere von "nicht verbunden" nicht zu unterscheiden."""
    broker = client.get("/api/session").json()["broker"]

    # `enabled` spiegelt die Konfiguration - und die Fixture schaltet den
    # Broker ab. Die Aussage dieses Tests ist eine andere: solange keine
    # Sitzung laeuft, gibt es keine Anbindung, und das Feld sagt es, statt zu
    # fehlen.
    assert broker["enabled"] is False
    assert broker["connected"] is False
    assert broker["ready"] is False
    assert broker["account"] == ""
    assert broker["open_orders"] == 0
    assert broker["tradeable_symbols"] == []


def test_ready_fasst_die_kette_zu_einer_zahl_zusammen(client: TestClient):
    """Ohne Anbindung entstehen keine echten Orders - und genau das muss die
    Anzeige sagen, ohne dass man vier Felder zusammenreimt."""
    from tradex.live.manager import BrokerState

    assert not BrokerState(enabled=False).ready
    assert not BrokerState(enabled=True, connected=False, is_paper=True).ready
    assert not BrokerState(enabled=True, connected=True, is_paper=False).ready
    assert not BrokerState(
        enabled=True, connected=True, is_paper=True, blocked_reason="verbindung_verloren"
    ).ready
    assert BrokerState(enabled=True, connected=True, is_paper=True).ready
