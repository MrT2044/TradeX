"""NT8-Feed gegen einen nachgebauten Bridge-Server.

Kein Mock des Sockets, sondern ein echter TCP-Server auf Loopback: was hier
geprueft wird, ist Rahmenverarbeitung ueber Paketgrenzen hinweg, und genau die
laesst sich mit einem Mock nicht pruefen. Ein Mock, der ganze Zeilen liefert,
uebersieht den einzigen Fehler, der hier wirklich vorkommt.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator

import pytest

from tradex.domain.enums import Timeframe
from tradex.live.feed import BarMessage, HeartbeatMessage, StatusMessage
from tradex.live.nt8_feed import NinjaTraderFeed

TIMEOUT = 5.0


class BridgeServer:
    """Ein Standin fuer das NinjaScript-AddOn."""

    def __init__(self) -> None:
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self.received: list[dict] = []
        self.client: socket.socket | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self) -> None:
        try:
            client, _ = self._sock.accept()
        except OSError:
            return
        self.client = client
        self._ready.set()
        buffer = b""
        try:
            client.settimeout(TIMEOUT)
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    return
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if line.strip():
                        self.received.append(json.loads(line))
        except (OSError, ValueError):
            return

    def wait_for_client(self) -> None:
        assert self._ready.wait(TIMEOUT), "Feed hat sich nicht verbunden"

    def send_raw(self, payload: bytes) -> None:
        assert self.client is not None
        self.client.sendall(payload)

    def send(self, message: dict) -> None:
        self.send_raw((json.dumps(message) + "\n").encode("utf-8"))

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
        self._sock.close()


@pytest.fixture
def bridge() -> Iterator[BridgeServer]:
    server = BridgeServer()
    yield server
    server.close()


def collect(feed: NinjaTraderFeed, count: int, seconds: float = TIMEOUT) -> list:
    """So lange sammeln, bis `count` Nachrichten da sind oder die Zeit ablaeuft."""
    gesammelt: list = []
    ende = time.monotonic() + seconds
    while len(gesammelt) < count and time.monotonic() < ende:
        gesammelt.extend(feed.messages(0.2))
    return gesammelt


def bar_message(ts: int, contract: str = "MNQH5", timeframe: str = "1m") -> dict:
    return {
        "type": "bar",
        "symbol": "MNQ",
        "timeframe": timeframe,
        "ts": ts,
        "open": 21000.25,
        "high": 21005.50,
        "low": 20998.75,
        "close": 21003.00,
        "volume": 1420,
        "contract": contract,
    }


# ---------------------------------------------------------------- Verbindung
def test_feed_abonniert_beim_verbinden(bridge: BridgeServer):
    feed = NinjaTraderFeed(("MNQ", "MES"), Timeframe.M1, port=bridge.port)
    feed.start()
    bridge.wait_for_client()
    ende = time.monotonic() + TIMEOUT
    while len(bridge.received) < 2 and time.monotonic() < ende:
        time.sleep(0.05)
    feed.stop()

    assert [m["symbol"] for m in bridge.received] == ["MNQ", "MES"]
    assert all(m["type"] == "subscribe" and m["timeframe"] == "1m" for m in bridge.received)


def test_verbindung_wird_als_status_gemeldet(bridge: BridgeServer):
    """Der Feed faellt keine Handelsentscheidung - auch keine negative.

    Er meldet den Zustand; was daraus folgt, entscheidet die Sitzung.
    """
    feed = NinjaTraderFeed(("MNQ",), port=bridge.port)
    feed.start()
    bridge.wait_for_client()
    nachrichten = collect(feed, 1)
    feed.stop()

    assert isinstance(nachrichten[0], StatusMessage)
    assert nachrichten[0].connected


def test_ein_marktfeed_ist_nie_fertig(bridge: BridgeServer):
    """Sonst beendete die Schleife den Betrieb bei jeder Stoerung."""
    feed = NinjaTraderFeed(("MNQ",), port=bridge.port)
    assert feed.is_finished is False


def test_ohne_symbole_gibt_es_nichts_zu_abonnieren():
    with pytest.raises(ValueError, match="ohne Symbole"):
        NinjaTraderFeed(())


def test_kontraktname_wird_hin_und_zurueck_uebersetzt(bridge: BridgeServer):
    """NinjaTrader kennt "MNQ SEP26", TradeX rechnet mit "MNQ".

    Ohne die Rueckuebersetzung fande die Sitzung fuer die ankommenden Bars
    kein Buch - der Betrieb liefe leer, ohne dass etwas nach einem Fehler
    aussieht. Genau diese Sorte Fehler faellt im Betrieb nicht auf.
    """
    feed = NinjaTraderFeed(
        ("MNQ",), Timeframe.M1, port=bridge.port, contracts={"MNQ": "MNQ SEP26"}
    )
    feed.start()
    bridge.wait_for_client()
    ende = time.monotonic() + TIMEOUT
    while not bridge.received and time.monotonic() < ende:
        time.sleep(0.05)

    assert bridge.received[0]["symbol"] == "MNQ SEP26", "abonniert wird der Kontrakt"

    payload = bar_message(1_740_000_000_000_000_000)
    payload["symbol"] = "MNQ SEP26"
    bridge.send(payload)
    bars = [m for m in collect(feed, 2) if isinstance(m, BarMessage)]
    feed.stop()

    assert bars and bars[0].symbol == "MNQ", "zurueck kommt das Wurzelsymbol"


# ---------------------------------------------------------------------- Bars
def test_bar_wird_vollstaendig_uebernommen(bridge: BridgeServer):
    feed = NinjaTraderFeed(("MNQ",), port=bridge.port)
    feed.start()
    bridge.wait_for_client()
    bridge.send(bar_message(1_740_000_000_000_000_000))
    nachrichten = [m for m in collect(feed, 2) if isinstance(m, BarMessage)]
    feed.stop()

    assert len(nachrichten) == 1
    bar = nachrichten[0].bar
    assert nachrichten[0].symbol == "MNQ"
    assert bar.ts == 1_740_000_000_000_000_000
    assert (bar.open, bar.high, bar.low, bar.close) == (21000.25, 21005.50, 20998.75, 21003.00)
    assert bar.volume == 1420
    assert not bar.roll_boundary
    assert nachrichten[0].received_ts > 0


def test_nachricht_ueber_zwei_pakete_hinweg(bridge: BridgeServer):
    """TCP kennt keine Zeilen, nur Bytes.

    Genau dieser Fall - eine JSON-Zeile, die in zwei Paketen ankommt - ist der
    Fehler, den ein Socket-Mock nie zeigt.
    """
    feed = NinjaTraderFeed(("MNQ",), port=bridge.port)
    feed.start()
    bridge.wait_for_client()

    payload = (json.dumps(bar_message(1_740_000_060_000_000_000)) + "\n").encode("utf-8")
    bridge.send_raw(payload[:40])
    time.sleep(0.2)
    bridge.send_raw(payload[40:])

    bars = [m for m in collect(feed, 2) if isinstance(m, BarMessage)]
    feed.stop()
    assert len(bars) == 1
    assert bars[0].bar.ts == 1_740_000_060_000_000_000


def test_mehrere_bars_in_einem_paket(bridge: BridgeServer):
    feed = NinjaTraderFeed(("MNQ",), port=bridge.port)
    feed.start()
    bridge.wait_for_client()

    zusammen = b"".join(
        (json.dumps(bar_message(1_740_000_000_000_000_000 + i * 60_000_000_000)) + "\n").encode()
        for i in range(3)
    )
    bridge.send_raw(zusammen)
    bars = [m for m in collect(feed, 4) if isinstance(m, BarMessage)]
    feed.stop()
    assert len(bars) == 3


def test_kontraktwechsel_wird_zur_rollgrenze(bridge: BridgeServer):
    """Der Preissprung an der Naht sieht aus wie eine riesige Imbalance."""
    feed = NinjaTraderFeed(("MNQ",), port=bridge.port)
    feed.start()
    bridge.wait_for_client()
    bridge.send(bar_message(1_740_000_000_000_000_000, contract="MNQH5"))
    bridge.send(bar_message(1_740_000_060_000_000_000, contract="MNQH5"))
    bridge.send(bar_message(1_740_000_120_000_000_000, contract="MNQM5"))

    bars = [m for m in collect(feed, 4) if isinstance(m, BarMessage)]
    feed.stop()

    assert [b.bar.roll_boundary for b in bars] == [False, False, True], (
        "der erste gesehene Kontrakt ist keine Grenze, der Wechsel danach schon"
    )


def test_fremde_zeitebene_wird_verworfen(bridge: BridgeServer):
    """Zwei Zeitebenen in einem Detektorzustand waeren schlimmer als ein Verlust."""
    feed = NinjaTraderFeed(("MNQ",), Timeframe.M1, port=bridge.port)
    feed.start()
    bridge.wait_for_client()
    bridge.send(bar_message(1_740_000_000_000_000_000, timeframe="5m"))
    bridge.send(bar_message(1_740_000_060_000_000_000, timeframe="1m"))

    bars = [m for m in collect(feed, 3) if isinstance(m, BarMessage)]
    feed.stop()
    assert len(bars) == 1
    assert feed.malformed == 1


# ------------------------------------------------------------- Robustheit
def test_kaputte_zeile_beendet_den_lesefaden_nicht(bridge: BridgeServer):
    """Beim Verbindungsabriss bleibt regelmaessig eine halbe Zeile zurueck.

    Stirbt der Lesefaden daran, laeuft die Sitzung weiter, waehrend niemand
    mehr zuhoert - und merkt es erst ueber die Stille-Ueberwachung.
    """
    feed = NinjaTraderFeed(("MNQ",), port=bridge.port)
    feed.start()
    bridge.wait_for_client()
    bridge.send_raw(b'{"type":"bar","symbol":"MNQ","ts":kaputt\n')
    bridge.send_raw(b"\n")
    bridge.send_raw(b'{"kein_typ":true}\n')
    bridge.send(bar_message(1_740_000_180_000_000_000))

    bars = [m for m in collect(feed, 2) if isinstance(m, BarMessage)]
    feed.stop()

    assert len(bars) == 1, "nach dem Muell muss die naechste Bar ankommen"
    assert feed.malformed >= 2


def test_bar_mit_fehlendem_feld_wird_verworfen(bridge: BridgeServer):
    feed = NinjaTraderFeed(("MNQ",), port=bridge.port)
    feed.start()
    bridge.wait_for_client()
    bridge.send({"type": "bar", "symbol": "MNQ", "ts": 1, "open": 1.0})
    bridge.send(bar_message(1_740_000_240_000_000_000))

    bars = [m for m in collect(feed, 2) if isinstance(m, BarMessage)]
    feed.stop()
    assert len(bars) == 1


def test_herzschlag_kommt_durch(bridge: BridgeServer):
    feed = NinjaTraderFeed(("MNQ",), port=bridge.port)
    feed.start()
    bridge.wait_for_client()
    bridge.send({"type": "heartbeat", "ts": 1_740_000_000_000_000_000})

    beats = [m for m in collect(feed, 2) if isinstance(m, HeartbeatMessage)]
    feed.stop()
    assert beats and beats[0].ts == 1_740_000_000_000_000_000


def test_ticks_werden_gezaehlt_aber_nicht_analysiert(bridge: BridgeServer):
    """Invariante 1: analysiert wird nur auf geschlossenen Bars.

    Gezaehlt werden sie trotzdem - die Zahl zeigt, ob der Feed lebt, wenn
    gerade keine Bar schliesst.
    """
    feed = NinjaTraderFeed(("MNQ",), port=bridge.port)
    feed.start()
    bridge.wait_for_client()
    for _ in range(3):
        bridge.send({"type": "tick", "symbol": "MNQ", "ts": 1, "price": 21000.0, "size": 2})

    nachrichten = collect(feed, 2, seconds=1.5)
    feed.stop()

    assert not [m for m in nachrichten if isinstance(m, BarMessage)]
    assert feed.ticks_seen == 3


def test_der_letzte_tickkurs_wird_gemerkt(bridge: BridgeServer):
    """Zur Anzeige zwischen zwei Bar-Schluessen - nicht zur Analyse.

    Ein Chart, der eine Minute lang stillsteht, waehrend sich der Markt
    bewegt, sieht eingefroren aus. Der Kurs geht in keinen Detektor ein; das
    prueft der Test daneben, indem er auf BarMessages besteht.
    """
    feed = NinjaTraderFeed(("MNQ",), port=bridge.port)
    feed.start()
    bridge.wait_for_client()
    bridge.send({"type": "tick", "symbol": "MNQ", "ts": 1, "price": 29433.25, "size": 2})

    ende = time.monotonic() + TIMEOUT
    while not feed.last_price and time.monotonic() < ende:
        collect(feed, 1, seconds=0.2)
    feed.stop()

    assert feed.last_price["MNQ"] == 29433.25


# ------------------------------------------------------------------ Historie
def test_historie_wird_angefordert_und_gilt_nicht_als_verbunden(bridge: BridgeServer):
    """Der gefaehrlichste Fehler waere, auf der Historie zu handeln.

    Die Sitzung beginnt angehalten und nimmt Positionen erst auf, wenn sie
    "verbunden" gesehen hat. Meldete der Feed das schon beim Socket-Aufbau,
    liefe die gesamte nachgeladene Historie durch eine handelsbereite Sitzung
    - drei Tage alte Bars wuerden echte Orders ausloesen. Die Meldung kommt
    deshalb erst nach `history_end`.
    """
    feed = NinjaTraderFeed(("MNQ",), port=bridge.port, history_days=3)
    feed.start()
    bridge.wait_for_client()

    ende = time.monotonic() + TIMEOUT
    while len(bridge.received) < 2 and time.monotonic() < ende:
        time.sleep(0.05)

    angefordert = [m for m in bridge.received if m["type"] == "history"]
    assert angefordert, "es wurde gar keine Historie angefordert"
    assert angefordert[0]["symbol"] == "MNQ"
    assert angefordert[0]["to"] > angefordert[0]["from"]

    # Waehrend die Historie laeuft: Bars ja, "verbunden" nein.
    bridge.send(bar_message(1_740_000_000_000_000_000))
    waehrend = collect(feed, 1, seconds=1.0)
    feed_stop_spaeter = [m for m in waehrend if isinstance(m, StatusMessage)]
    assert not feed_stop_spaeter, "verbunden gemeldet, waehrend die Historie noch laeuft"
    assert [m for m in waehrend if isinstance(m, BarMessage)], "die Bar kam nicht durch"

    bridge.send({"type": "history_end", "symbol": "MNQ", "bars": 1})
    danach = collect(feed, 1, seconds=TIMEOUT)
    feed.stop()

    status = [m for m in danach if isinstance(m, StatusMessage)]
    assert status and status[0].connected, "nach history_end fehlt die Verbindungsmeldung"
    assert feed.history_bars == 1


def test_ohne_antwort_laeuft_der_betrieb_trotzdem_an(bridge: BridgeServer):
    """Ein AddOn, das `history` nicht kennt, antwortet nie.

    Ohne Deckel bliebe die Sitzung fuer immer angehalten - sie bekaeme Bars,
    analysierte sie und naehme nie eine Position auf. Von aussen sieht das aus
    wie ein Markt ohne Signale, und niemand sucht dann nach einem Fehler.
    """
    feed = NinjaTraderFeed(
        ("MNQ",), port=bridge.port, history_days=3, history_timeout_seconds=0.3
    )
    feed.start()
    bridge.wait_for_client()

    status = [m for m in collect(feed, 1, seconds=TIMEOUT) if isinstance(m, StatusMessage)]
    feed.stop()

    assert status and status[0].connected
    assert "ohne Historie" in status[0].detail, "der Notausgang muss im Protokoll stehen"


def test_ohne_historie_meldet_der_feed_sofort(bridge: BridgeServer):
    """Waechter zur Gegenprobe: die Verzoegerung gilt NUR mit history_days."""
    feed = NinjaTraderFeed(("MNQ",), port=bridge.port, history_days=0)
    feed.start()
    bridge.wait_for_client()
    status = [m for m in collect(feed, 1) if isinstance(m, StatusMessage)]
    feed.stop()

    assert status and status[0].connected
    assert "ohne Historie" not in status[0].detail


def test_abbruch_der_gegenstelle_wird_gemeldet(bridge: BridgeServer):
    feed = NinjaTraderFeed(("MNQ",), port=bridge.port)
    feed.start()
    bridge.wait_for_client()
    assert bridge.client is not None
    bridge.client.close()

    zustaende = [m for m in collect(feed, 2) if isinstance(m, StatusMessage)]
    feed.stop()
    assert any(not m.connected for m in zustaende)


def test_ohne_gegenstelle_wird_es_erneut_versucht():
    """Ein geschlossener Port ist kein Grund aufzugeben - NinjaTrader startet
    manchmal spaeter als TradeX."""
    frei = socket.socket()
    frei.bind(("127.0.0.1", 0))
    port = frei.getsockname()[1]
    frei.close()

    feed = NinjaTraderFeed(("MNQ",), port=port)
    feed.start()
    zustaende = [m for m in collect(feed, 1, seconds=3.0) if isinstance(m, StatusMessage)]
    feed.stop()

    assert zustaende and not zustaende[0].connected
    assert feed.reconnects >= 1
