"""Der Orderadapter gegen einen nachgebauten Bridge-Server.

Echter TCP-Server auf Loopback, kein Mock - dieselbe Begruendung wie in
`test_nt8_feed.py`: geprueft wird Rahmenverarbeitung ueber Paketgrenzen
hinweg, und die zeigt ein Mock nie.

**Kein Test fasst je ein laufendes NinjaTrader an.** Der Server hier antwortet
nach Skript.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator

import pytest

from tradex.broker.base import BrokerError, BrokerNotConnected
from tradex.broker.nt8.adapter import NinjaTraderBroker
from tradex.broker.types import OrderRequest, OrderSide, OrderState

TIMEOUT = 5.0


class FakeBridge:
    """Standin fuer das AddOn - beantwortet `account_query`, sammelt Befehle."""

    def __init__(self, *, is_simulation: bool = True, account: str = "Sim101") -> None:
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self.received: list[dict] = []
        self.client: socket.socket | None = None
        self.is_simulation = is_simulation
        self.account = account
        self.antworte_auf_account = True
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
        puffer = b""
        try:
            client.settimeout(TIMEOUT)
            while True:
                stueck = client.recv(4096)
                if not stueck:
                    return
                puffer += stueck
                while b"\n" in puffer:
                    zeile, puffer = puffer.split(b"\n", 1)
                    if not zeile.strip():
                        continue
                    nachricht = json.loads(zeile)
                    self.received.append(nachricht)
                    if nachricht.get("type") == "account_query" and self.antworte_auf_account:
                        self.send(
                            {
                                "type": "account",
                                "name": self.account,
                                "provider": "Simulator" if self.is_simulation else "Playback",
                                "is_simulation": self.is_simulation,
                                "currency": "USD",
                                "net_liquidation": 100000.0,
                                "buying_power": 100000.0,
                            }
                        )
        except (OSError, ValueError):
            return

    def wait_for_client(self) -> None:
        assert self._ready.wait(TIMEOUT), "Adapter hat sich nicht verbunden"

    def send(self, message: dict) -> None:
        assert self.client is not None
        self.client.sendall((json.dumps(message) + "\n").encode("utf-8"))

    def send_raw(self, payload: bytes) -> None:
        assert self.client is not None
        self.client.sendall(payload)

    def befehle(self, art: str) -> list[dict]:
        return [m for m in self.received if m.get("type") == art]

    def warte_auf(self, art: str) -> dict:
        ende = time.monotonic() + TIMEOUT
        while time.monotonic() < ende:
            treffer = self.befehle(art)
            if treffer:
                return treffer[-1]
            time.sleep(0.02)
        raise AssertionError(f"Befehl {art} kam nie an; gesehen: {self.received}")

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
        self._sock.close()


@pytest.fixture
def bridge() -> Iterator[FakeBridge]:
    server = FakeBridge()
    yield server
    server.close()


def _broker(bridge: FakeBridge, **overrides: object) -> NinjaTraderBroker:
    felder: dict[str, object] = {
        "port": bridge.port,
        "allow_orders": True,
        "connect_timeout_seconds": TIMEOUT,
    }
    felder.update(overrides)
    return NinjaTraderBroker(**felder)  # type: ignore[arg-type]


def _request(**overrides: object) -> OrderRequest:
    felder: dict[str, object] = {
        "order_key": "S17-4",
        "symbol": "MNQ",
        "side": OrderSide.BUY,
        "quantity": 2,
        "stop_loss": 29180.25,
        "take_profit": 29310.50,
    }
    felder.update(overrides)
    return OrderRequest(**felder)  # type: ignore[arg-type]


def _warte(bedingung, dauer: float = TIMEOUT) -> bool:  # type: ignore[no-untyped-def]
    ende = time.monotonic() + dauer
    while time.monotonic() < ende:
        if bedingung():
            return True
        time.sleep(0.02)
    return False


# ------------------------------------------------------------------ Verbindung
def test_verbinden_bestaetigt_das_konto(bridge: FakeBridge):
    broker = _broker(bridge)
    try:
        broker.connect()
        info = broker.get_account_info()
        assert broker.is_connected()
        assert info.account == "Sim101"
        assert info.is_paper
        # Ohne Beleg waere das Flag eine Behauptung.
        assert "Provider" in info.paper_evidence
    finally:
        broker.disconnect()


def test_ein_fremdes_konto_wird_abgewiesen(bridge: FakeBridge):
    """Fail closed - und zwar hier, nicht erst beim Senden.

    Die Sperre steht doppelt: im AddOn (`Account.Provider`) und hier. Eine
    Sicherheitskette, die nur auf der Seite laeuft, die man selbst
    kontrolliert, beschreibt die Grenze, statt sie zu pruefen.
    """
    bridge.is_simulation = False
    bridge.account = "Playback101"
    broker = _broker(bridge)
    with pytest.raises(BrokerError, match="account_not_simulated"):
        broker.connect()
    assert not broker.is_connected(), "nach der Ablehnung darf keine Leitung offen bleiben"


def test_ohne_antwort_bleibt_die_anbindung_zu(bridge: FakeBridge):
    """Eine halb aufgebaute Anbindung, die spaeter "schon irgendwie" sendet,
    ist genau der Zustand, den es nicht geben darf."""
    bridge.antworte_auf_account = False
    broker = _broker(bridge, connect_timeout_seconds=0.5)
    with pytest.raises(BrokerNotConnected, match="account_query"):
        broker.connect()
    assert not broker.is_connected()


def test_ohne_gegenstelle_gibt_es_keine_verbindung():
    frei = socket.socket()
    frei.bind(("127.0.0.1", 0))
    port = frei.getsockname()[1]
    frei.close()

    broker = NinjaTraderBroker(port=port, connect_timeout_seconds=1.0)
    with pytest.raises(BrokerNotConnected, match="nicht erreichbar"):
        broker.connect()


# ---------------------------------------------------------------- Orderrecht
def test_ohne_orderrecht_darf_verbunden_werden(bridge: FakeBridge):
    """Sonst muesste man den Handel scharfschalten, nur um die Verbindung zu
    pruefen. Es gibt dann keinen Sendeweg, den die Kette schuetzen koennte."""
    broker = _broker(bridge, allow_orders=False)
    try:
        broker.connect()
        assert broker.is_connected()
        with pytest.raises(BrokerError, match="allow_orders"):
            broker.place_market_order(_request())
        assert not bridge.befehle("order_submit"), "es darf nichts hinausgegangen sein"
    finally:
        broker.disconnect()


# -------------------------------------------------------------------- Orders
def test_eine_order_geht_mit_klammer_hinaus(bridge: FakeBridge):
    broker = _broker(bridge)
    try:
        broker.connect()
        order = broker.place_market_order(_request())
        befehl = bridge.warte_auf("order_submit")

        assert befehl["order_key"] == "S17-4"
        assert befehl["account"] == "Sim101", "das bestaetigte Konto, nicht der Wunsch"
        assert befehl["stop_loss"] == 29180.25
        assert befehl["take_profit"] == 29310.50
        # Gesendet ist nicht ausgefuehrt.
        assert order.state is OrderState.SUBMITTED
        assert order.filled_quantity == 0
    finally:
        broker.disconnect()


def test_die_order_traegt_den_kontraktnamen(bridge: FakeBridge):
    """Wie beim Feed: hinaus geht "MNQ SEP26", nicht "MNQ".

    Ohne das loest NinjaTrader den generischen Eintrag auf, der keine
    Marktdaten hat - die Order wird angenommen und zwanzig Sekunden spaeter
    abgelehnt. Am 26.08.2026 im Betrieb genau so passiert.
    """
    broker = _broker(bridge, contracts={"MNQ": "MNQ SEP26"})
    try:
        broker.connect()
        broker.place_market_order(_request())
        assert bridge.warte_auf("order_submit")["symbol"] == "MNQ SEP26"
    finally:
        broker.disconnect()


def test_positionen_kommen_unter_dem_wurzelsymbol_zurueck(bridge: FakeBridge):
    """Die Gegenrichtung. Ohne sie fuehrte der Adapter die Position unter
    "MNQ SEP26", waehrend `close_position("MNQ")` danach sucht - die Position
    bliebe offen, und das Aufraeumen meldete trotzdem Erfolg."""
    broker = _broker(bridge, contracts={"MNQ": "MNQ SEP26"})
    try:
        broker.connect()
        bridge.send(
            {"type": "position", "account": "Sim101", "symbol": "MNQ SEP26", "quantity": -2}
        )
        assert _warte(lambda: bool(broker.get_positions()))
        assert broker.get_positions()[0].symbol == "MNQ"

        broker.close_position("MNQ")
        assert bridge.warte_auf("flatten")["symbol"] == "MNQ SEP26", (
            "flatten muss ebenfalls den Kontrakt nennen"
        )
    finally:
        broker.disconnect()


def test_die_klammerteile_werden_mitgefuehrt(bridge: FakeBridge):
    """Ihre Zustandsmeldungen kommen unter `order_key#stop` herein. Ohne
    Eintrag waere das ein unbekannter Faden - und die Meldung ginge verloren."""
    broker = _broker(bridge)
    try:
        broker.connect()
        broker.place_market_order(_request())
        offen = {o.order_key for o in broker.get_open_orders()}
        assert offen == {"S17-4"}
        assert len(broker.get_open_orders()) == 3, "Entry, Stop und Ziel"
    finally:
        broker.disconnect()


def test_ohne_verbindung_geht_nichts_hinaus(bridge: FakeBridge):
    broker = _broker(bridge)
    with pytest.raises(BrokerNotConnected):
        broker.place_market_order(_request())


def test_ein_nicht_freigegebenes_symbol_wird_abgelehnt(bridge: FakeBridge):
    broker = _broker(bridge, tradeable_symbols=("MNQ",))
    try:
        broker.connect()
        ok, grund = broker.can_trade("ES")
        assert not ok and "ES" in grund
        with pytest.raises(BrokerError, match="nicht handelbar"):
            broker.place_market_order(_request(symbol="ES"))
    finally:
        broker.disconnect()


# ------------------------------------------------------------- Rueckmeldungen
def test_zustandswechsel_kommen_an(bridge: FakeBridge):
    broker = _broker(bridge)
    try:
        broker.connect()
        broker.place_market_order(_request())
        bridge.warte_auf("order_submit")
        bridge.send(
            {
                "type": "order_update",
                "order_key": "S17-4",
                "order_id": "a91f",
                "state": "accepted",
                "filled_quantity": 0,
                "avg_fill_price": 0,
            }
        )
        def angenommen() -> bool:
            broker.drain_events()
            return (
                next((o.state for o in broker.get_open_orders() if o.kind.value == "MARKET"), None)
                is OrderState.ACCEPTED
            )

        assert _warte(angenommen), "der Zustandswechsel kam nicht an"
    finally:
        broker.disconnect()


def test_teilfuellungen_ergeben_einen_gewichteten_einstand(bridge: FakeBridge):
    """Bei Teilfuellungen ist der mengengewichtete Durchschnitt die einzige
    Zahl, die den tatsaechlichen Einstand trifft."""
    broker = _broker(bridge)
    try:
        broker.connect()
        broker.place_market_order(_request())
        bridge.warte_auf("order_submit")
        for menge, preis in ((1, 29240.0), (1, 29244.0)):
            bridge.send(
                {
                    "type": "execution",
                    "order_key": "S17-4",
                    "exec_id": f"e{preis}",
                    "quantity": menge,
                    "price": preis,
                    "commission": 0.37,
                }
            )

        def gefuellt() -> bool:
            broker.drain_events()
            order = next((o for o in broker.get_open_orders() if o.order_key == "S17-4"), None)
            return order is not None and order.filled_quantity == 2

        assert _warte(gefuellt), "die Fuellungen kamen nicht an"
        order = next(o for o in broker.get_open_orders() if o.order_key == "S17-4")
        assert order.avg_fill_price == pytest.approx(29242.0)
        assert order.commission == pytest.approx(0.74)
        assert order.commission_reported
    finally:
        broker.disconnect()


def test_eine_ablehnung_erreicht_die_order(bridge: FakeBridge):
    broker = _broker(bridge)
    try:
        broker.connect()
        broker.place_market_order(_request())
        bridge.warte_auf("order_submit")
        bridge.send(
            {
                "type": "order_rejected",
                "order_key": "S17-4",
                "code": "duplicate_order_key",
                "detail": "order_key bereits verwendet",
            }
        )

        def abgelehnt() -> bool:
            broker.drain_events()
            return not any(o.order_key == "S17-4" for o in broker.get_open_orders())

        assert _warte(abgelehnt), "die Ablehnung hat die Order nicht beendet"
    finally:
        broker.disconnect()


def test_positionen_kommen_ohne_nachfrage(bridge: FakeBridge):
    """Statusabfragen duerfen nie auf den Broker warten - sonst steht die
    Anzeige, wenn er klemmt."""
    broker = _broker(bridge)
    try:
        broker.connect()
        bridge.send(
            {
                "type": "position",
                "account": "Sim101",
                "symbol": "MNQ",
                "quantity": -2,
                "avg_price": 29245.75,
            }
        )
        assert _warte(lambda: bool(broker.get_positions())), "die Position kam nicht an"
        position = broker.get_positions()[0]
        assert position.quantity == -2, "negativ = short"
        assert position.side is OrderSide.SELL
    finally:
        broker.disconnect()


def test_marktdaten_stoeren_den_orderweg_nicht(bridge: FakeBridge):
    """Beide Wege teilen sich die Leitung. Bars und Ticks muessen hier
    spurlos durchlaufen."""
    broker = _broker(bridge)
    try:
        broker.connect()
        broker.drain_events()
        for nachricht in (
            {"type": "bar", "symbol": "MNQ", "ts": 1, "open": 1, "high": 1, "low": 1, "close": 1},
            {"type": "tick", "symbol": "MNQ", "ts": 1, "price": 29245.75},
            {"type": "heartbeat", "ts": 1},
        ):
            bridge.send(nachricht)
        time.sleep(0.4)
        assert broker.drain_events() == []
        assert broker.is_connected()
    finally:
        broker.disconnect()


def test_eine_kaputte_zeile_beendet_den_lesefaden_nicht(bridge: FakeBridge):
    broker = _broker(bridge)
    try:
        broker.connect()
        broker.drain_events()
        bridge.send_raw(b'{"type":"execution","order_key":kaputt\n')
        bridge.send({"type": "position", "account": "Sim101", "symbol": "MNQ", "quantity": 1})
        assert _warte(lambda: bool(broker.get_positions())), "nach dem Muell kam nichts mehr durch"
        assert broker.is_connected()
    finally:
        broker.disconnect()


def test_ein_abriss_wird_gemeldet(bridge: FakeBridge):
    """Eine Sitzung, die den Broker fuer verbunden haelt, nimmt weiter
    Positionen auf."""
    broker = _broker(bridge)
    try:
        broker.connect()
        broker.drain_events()
        assert bridge.client is not None
        bridge.client.close()

        gesehen: list[str] = []

        def abriss() -> bool:
            for ereignis in broker.drain_events():
                if ereignis.kind == "connection":
                    gesehen.append(ereignis.kind)
            return bool(gesehen)

        assert _warte(abriss), "der Verbindungsverlust wurde nicht gemeldet"
    finally:
        broker.disconnect()


# ------------------------------------------------------------------- Storno
def test_storno_geht_ueber_den_order_key(bridge: FakeBridge):
    broker = _broker(bridge)
    try:
        broker.connect()
        order = broker.place_market_order(_request())
        bridge.warte_auf("order_submit")
        broker.cancel_order(order.order_id)
        assert bridge.warte_auf("order_cancel")["order_key"] == "S17-4"
    finally:
        broker.disconnect()


def test_eine_unbekannte_order_wird_nicht_still_ignoriert(bridge: FakeBridge):
    broker = _broker(bridge)
    try:
        broker.connect()
        with pytest.raises(BrokerError, match="nicht bekannt"):
            broker.cancel_order(4711)
    finally:
        broker.disconnect()


def test_glattstellen_ohne_position_sendet_nichts(bridge: FakeBridge):
    """Ein `flatten` ins Leere waere folgenlos, aber es stuende im Protokoll -
    und ein Notaus, der staendig auftaucht, verliert seine Bedeutung."""
    broker = _broker(bridge)
    try:
        broker.connect()
        assert broker.close_position("MNQ") is None
        time.sleep(0.2)
        assert not bridge.befehle("flatten")
    finally:
        broker.disconnect()


def test_glattstellen_mit_position_sendet_flatten(bridge: FakeBridge):
    broker = _broker(bridge)
    try:
        broker.connect()
        bridge.send(
            {"type": "position", "account": "Sim101", "symbol": "MNQ", "quantity": -2}
        )
        assert _warte(lambda: bool(broker.get_positions()))
        broker.close_position("MNQ")
        befehl = bridge.warte_auf("flatten")
        assert befehl["symbol"] == "MNQ"
        assert befehl["account"] == "Sim101"
    finally:
        broker.disconnect()


def test_der_notaus_ohne_symbol_meint_alles(bridge: FakeBridge):
    broker = _broker(bridge)
    try:
        broker.connect()
        broker.close_all_positions()
        assert "symbol" not in bridge.warte_auf("flatten")
    finally:
        broker.disconnect()


def test_drain_events_blockiert_nie(bridge: FakeBridge):
    """Der Aufrufer ist der Sitzungsfaden. Wartet er auf den Broker, steht bei
    einer Stoerung ausgerechnet die Ueberwachung still."""
    broker = _broker(bridge)
    try:
        broker.connect()
        begonnen = time.monotonic()
        broker.drain_events()
        broker.drain_events()
        assert time.monotonic() - begonnen < 0.5
    finally:
        broker.disconnect()
