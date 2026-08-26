"""Orderanbindung an die NinjaTrader-Bridge.

Der einzige Ort mit einem Order-Socket. Alles, was hier hereinkommt, ist schon
in `protocol.py` auf brokerunabhaengige Typen abgebildet - dieses Modul fuegt
das hinzu, was Zustand braucht: Verbindung, Faden, Zuordnung von Kennungen.

Warum eine EIGENE Verbindung neben dem Feed
-------------------------------------------
Das AddOn sendet Orderereignisse per Broadcast an jeden Client; man koennte
sie also auch aus der Feed-Verbindung lesen. Trotzdem eine eigene:

1. Der Orderweg darf nicht daran haengen, ob gerade ein Feed laeuft. Eine
   Sitzung ohne Marktbeobachtung muss trotzdem stornieren koennen.
2. `NinjaTraderFeed` verbindet bei Stoerungen selbstaendig neu und wirft dabei
   seinen Zustand weg. Fuer Bars ist das richtig, fuer offene Orders nicht.

Der Preis ist ein zweiter Socket auf Loopback. Das ist billig; eine
Verschraenkung von Datenstrom und Orderweg waere es nicht.

Was hier NICHT passiert
-----------------------
Keine Handelsregel, keine Positionsgroesse, keine Entscheidung. Ein Broker
fuehrt aus. Was gesendet wird, kommt fertig aus `SymbolBook`.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
from dataclasses import replace
from datetime import UTC, datetime
from queue import Empty, Queue
from typing import Any

from tradex.broker.base import BrokerError, BrokerNotConnected
from tradex.broker.guard import confirm_simulated_account
from tradex.broker.nt8 import protocol
from tradex.broker.types import (
    ROLE_ENTRY,
    AccountInfo,
    BrokerEvent,
    BrokerOrder,
    BrokerPosition,
    OrderKind,
    OrderRequest,
    OrderState,
    order_ref,
    parse_order_ref,
)
from tradex.live.nt8_feed import DEFAULT_HOST, DEFAULT_PORT
from tradex.logging_setup import get_logger

log = get_logger(__name__)

_SOCKET_TIMEOUT = 1.0
_RECEIVE_BYTES = 65536


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class NinjaTraderBroker:
    """`BrokerInterface` ueber die Bridge. Ausschliesslich Simulationskonten."""

    name = "nt8"

    def __init__(
        self,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        account: str = "",
        allow_orders: bool = False,
        tradeable_symbols: tuple[str, ...] = (),
        allowed_accounts: tuple[str, ...] = (),
        contracts: dict[str, str] | None = None,
        connect_timeout_seconds: float = 10.0,
    ) -> None:
        self.host = host
        self.port = port
        # NinjaTrader kennt Kontrakte ("MNQ SEP26"), TradeX rechnet mit dem
        # Wurzelsymbol ("MNQ"). Dieselbe Uebersetzung wie im Feed, und aus
        # demselben Grund: der Adapter ist der Adapter. Ohne sie geht die
        # Order an den generischen `MNQ`-Eintrag, der keine Marktdaten hat -
        # NinjaTrader nimmt sie an und lehnt sie zwanzig Sekunden spaeter ab.
        self.contracts = {k.upper(): v for k, v in (contracts or {}).items() if v}
        self._back = {v.upper(): k.upper() for k, v in self.contracts.items()}
        #: Gewuenschtes Konto. Leer heisst "das einzige Simulationskonto" - das
        #: AddOn lehnt ab, wenn es mehr als eines gibt, statt zu waehlen.
        self.wanted_account = account
        #: Zusaetzliche Einschraenkung, ausgewertet in `guard.py`. Sie kann ein
        #: Simulationskonto ausschliessen, aber nie eines freischalten.
        self.allowed_accounts = allowed_accounts
        self.allow_orders = allow_orders
        self.tradeable = {s.upper() for s in tradeable_symbols}
        self.connect_timeout_seconds = connect_timeout_seconds

        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()

        self._events: Queue[BrokerEvent] = Queue()
        self._orders: dict[str, BrokerOrder] = {}
        """Je `order_ref` (also Entry UND Klammerteile) ein Zustand."""
        self._positions: dict[str, BrokerPosition] = {}
        self._account: AccountInfo | None = None
        self._account_seen = threading.Event()
        #: Begruendung und Kandidatenliste aus einer abgelehnten Kontoabfrage.
        #: Nur dann gefuellt - im Erfolgsfall gibt es nichts zu erklaeren.
        self._account_detail = ""
        self._account_candidates: str = ""

        #: NinjaTrader vergibt Order-IDs als Zeichenketten, TradeX rechnet mit
        #: `int`. Die Zuordnung lebt hier, weil sie Zustand braucht.
        self._order_ids: dict[str, int] = {}
        self._next_order_id = 1

    # --------------------------------------------------------------- Verbindung
    def connect(self) -> None:
        """Verbinden und das Konto bestaetigen lassen.

        Die Reihenfolge ist der Sicherheitsschritt: erst wenn ein
        `account`-Ereignis mit `is_simulation=true` da ist, gilt die Anbindung
        als brauchbar. Ohne Antwort wird die Verbindung wieder geschlossen -
        eine halb aufgebaute Anbindung, die spaeter "schon irgendwie" Orders
        sendet, ist genau der Zustand, den es nicht geben darf.
        """
        if self.is_connected():
            return
        self._stop.clear()
        try:
            sock = socket.create_connection((self.host, self.port), timeout=_SOCKET_TIMEOUT)
        except OSError as fehler:
            raise BrokerNotConnected(f"Bridge {self.host}:{self.port} nicht erreichbar: {fehler}") from fehler

        self._sock = sock
        self._account_seen.clear()
        self._thread = threading.Thread(target=self._read_loop, name="nt8-broker", daemon=True)
        self._thread.start()

        self._send(protocol.account_query_command(self.wanted_account))
        if not self._account_seen.wait(self.connect_timeout_seconds):
            self.disconnect()
            raise BrokerNotConnected(
                f"Bridge antwortet nicht auf account_query ({self.connect_timeout_seconds:.0f}s). "
                "Laeuft NinjaTrader, und ist das AddOn uebersetzt?"
            )

        info = self._account
        if info is None or not info.is_paper:
            grund = self._account_detail or (info.paper_evidence if info else "keine Antwort")
            self.disconnect()
            # Die Kandidatenliste gehoert in die Meldung. "Konto  ist kein
            # Simulationskonto" nannte weder das gesuchte noch die
            # vorhandenen Konten - und liess damit genau die Frage offen, die
            # man als Naechstes stellt.
            raise BrokerError(
                f"{protocol.REJECT_ACCOUNT_NOT_SIMULATED}: {grund}"
                + (f"\n  Vorhandene Konten: {self._account_candidates}" if self._account_candidates else "")
            )
        log.info("nt8_broker_verbunden", konto=info.account, nachweis=info.paper_evidence)

    def disconnect(self) -> None:
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            # Beides darf scheitern - die Gegenstelle kann schon weg sein.
            # Ein Fehler beim Aufraeumen ist kein Grund, das Aufraeumen
            # abzubrechen.
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                sock.close()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    def is_connected(self) -> bool:
        return self._sock is not None and self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------ Auskunft
    def get_account_info(self) -> AccountInfo:
        info = self._account
        if info is None:
            raise BrokerNotConnected("Kontodaten liegen erst nach connect() vor")
        return info

    def get_positions(self) -> list[BrokerPosition]:
        """Aus dem laufenden Zustand - ohne zu fragen und ohne zu warten.

        Das AddOn schickt Positionsereignisse von sich aus. Hier nachzufragen
        und auf Antwort zu warten hiesse, die Statusanzeige an den Broker zu
        haengen: klemmt er, steht sie.
        """
        with self._state_lock:
            return [replace(position) for position in self._positions.values()]

    def get_open_orders(self) -> list[BrokerOrder]:
        with self._state_lock:
            return [replace(order) for order in self._orders.values() if order.state.is_live]

    def can_trade(self, symbol: str) -> tuple[bool, str]:
        if not self.is_connected():
            return False, "keine Verbindung zur Bridge"
        if self._account is None or not self._account.is_paper:
            return False, protocol.REJECT_ACCOUNT_NOT_SIMULATED
        symbol = symbol.upper()
        if self.tradeable and symbol not in self.tradeable:
            return False, f"{symbol} ist fuer diese Anbindung nicht freigegeben"
        # Ob NinjaTrader den Kontrakt aufloesen kann, weiss nur NinjaTrader.
        # Das AddOn lehnt mit `instrument_unknown` ab - und diese Ablehnung
        # kommt an, bevor irgendetwas an die Boerse geht.
        return True, ""

    # -------------------------------------------------------------------- Orders
    def place_market_order(self, request: OrderRequest) -> BrokerOrder:
        return self._place(request)

    def place_limit_order(self, request: OrderRequest) -> BrokerOrder:
        if request.kind is not OrderKind.LIMIT:
            request = replace(request, kind=OrderKind.LIMIT)
        return self._place(request)

    def _place(self, request: OrderRequest) -> BrokerOrder:
        """Eintragen, dann senden - in dieser Reihenfolge.

        Die erste Rueckmeldung kann eintreffen, waehrend `sendall` noch laeuft.
        Faende der Lesefaden die Order dann nicht, ginge die erste
        Zustandsmeldung verloren - und die ist oft die einzige, die sagt, dass
        etwas abgelehnt wurde.
        """
        if not self.allow_orders:
            raise BrokerError("Dieser Adapter wurde ohne Orderrecht gebaut (allow_orders=False)")
        if not self.is_connected():
            raise BrokerNotConnected(protocol.REJECT_NOT_CONNECTED)

        symbol = request.symbol.upper()
        handelbar, grund = self.can_trade(symbol)
        if not handelbar:
            raise BrokerError(f"{symbol} ist nicht handelbar: {grund}")

        entry = self._register(request.order_key, ROLE_ENTRY, request, request.quantity)
        # Die Klammerteile werden mit eingetragen, obwohl das AddOn sie erzeugt:
        # ihre Zustandsmeldungen kommen unter `order_key#stop` bzw. `#target`
        # herein, und eine Meldung ohne Eintrag waere ein unbekannter Faden.
        if request.stop_loss > 0:
            self._register(request.order_key, "stop", request, request.quantity, opposite=True)
        if request.take_profit > 0:
            self._register(request.order_key, "target", request, request.quantity, opposite=True)

        account = self._account.account if self._account is not None else self.wanted_account
        self._send(protocol.submit_command(request, account, self.contracts.get(symbol, "")))
        log.info(
            "nt8_order_gesendet",
            symbol=symbol,
            order_key=request.order_key,
            klammer=request.has_bracket,
        )
        return replace(entry)

    def _register(
        self,
        order_key: str,
        role: str,
        request: OrderRequest,
        quantity: int,
        *,
        opposite: bool = False,
    ) -> BrokerOrder:
        ref = order_ref(order_key, role)
        order = BrokerOrder(
            order_id=self._id_for(ref),
            order_key=order_key,
            symbol=request.symbol.upper(),
            side=request.side.opposite if opposite else request.side,
            quantity=quantity,
            kind=request.kind if role == ROLE_ENTRY else OrderKind.STOP,
            state=OrderState.SUBMITTED,
            limit_price=request.limit_price if role == ROLE_ENTRY else 0.0,
            stop_price=request.stop_loss if role == "stop" else 0.0,
        )
        with self._state_lock:
            self._orders[ref] = order
        return order

    def cancel_order(self, order_id: int) -> None:
        """Storniert Entry UND Klammer - die Bridge kennt nur den `order_key`.

        Einen Klammerteil einzeln zu stornieren gaebe es hier nicht: eine
        Position ohne Stop ist der Zustand, den niemand haben will, und das
        Protokoll bietet ihn deshalb gar nicht erst an.
        """
        with self._state_lock:
            ref = next((r for r, o in self._orders.items() if o.order_id == order_id), "")
        if not ref:
            raise BrokerError(f"Order {order_id} ist diesem Adapter nicht bekannt")
        order_key, _ = parse_order_ref(ref)
        self._send(protocol.cancel_command(order_key))

    def cancel_all_orders(self) -> None:
        with self._state_lock:
            keys = {o.order_key for o in self._orders.values() if o.state.is_live}
        for key in sorted(keys):
            self._send(protocol.cancel_command(key))

    def close_position(self, symbol: str) -> BrokerOrder | None:
        """Glattstellen ueber `flatten` - das AddOn storniert zuerst.

        Liefert None: `flatten` erzeugt keine Order mit eigener Kennung, die
        man zurueckgeben koennte. Was daraus wird, sagen die
        `execution`-Meldungen - und die kommen ohnehin ueber `drain_events()`.
        """
        symbol = symbol.upper()
        with self._state_lock:
            vorhanden = symbol in self._positions and self._positions[symbol].quantity != 0
        if not vorhanden:
            return None
        # Auch hier der Kontrakt: `flatten` mit dem Wurzelsymbol traefe wieder
        # den generischen Eintrag - und liesse die Position stehen.
        self._send(
            protocol.flatten_command(self._account_name(), self.contracts.get(symbol, symbol))
        )
        return None

    def close_all_positions(self) -> list[BrokerOrder]:
        self._send(protocol.flatten_command(self._account_name()))
        return []

    def _account_name(self) -> str:
        return self._account.account if self._account is not None else self.wanted_account

    # ------------------------------------------------------------------- Ablauf
    def drain_events(self) -> list[BrokerEvent]:
        """Alles, was seit dem letzten Aufruf hereinkam. Blockiert nie."""
        gesammelt: list[BrokerEvent] = []
        while True:
            try:
                gesammelt.append(self._events.get_nowait())
            except Empty:
                return gesammelt

    # -------------------------------------------------------------- Lesefaden
    def _read_loop(self) -> None:
        sock = self._sock
        if sock is None:
            return
        puffer = b""
        while not self._stop.is_set():
            try:
                stueck = sock.recv(_RECEIVE_BYTES)
            except TimeoutError:
                continue
            except OSError:
                break
            if not stueck:
                break
            puffer += stueck
            while b"\n" in puffer:
                zeile, puffer = puffer.split(b"\n", 1)
                self._handle_line(zeile)

        if not self._stop.is_set():
            # Abriss, nicht Abschalten. Das muss heraus: eine Sitzung, die den
            # Broker fuer verbunden haelt, nimmt weiter Positionen auf.
            self._emit(BrokerEvent(kind="connection", ts_utc=_now_iso(), payload={"connected": False}))
            log.warning("nt8_broker_verbindung_verloren")

    def _handle_line(self, roh: bytes) -> None:
        zeile = roh.strip()
        if not zeile:
            return
        try:
            nachricht: dict[str, Any] = json.loads(zeile.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            # Halbe Zeilen beim Abriss sind normal. Der Lesefaden darf daran
            # nicht sterben - sonst laeuft die Sitzung weiter, waehrend
            # niemand mehr zuhoert.
            return

        ereignis = protocol.parse_event(nachricht)
        if ereignis is None:
            return  # Bars und Ticks gehoeren dem Feed.
        ereignis.ts_utc = _now_iso()
        self._apply(ereignis)
        self._emit(ereignis)

    def _apply(self, ereignis: BrokerEvent) -> None:
        """Den eigenen Zustand nachfuehren, BEVOR das Ereignis herausgeht."""
        if ereignis.kind == "account":
            payload = ereignis.payload
            # Das Urteil faellt `guard.py` und nicht dieses Modul: die
            # Sicherheitskette gehoert an EINE Stelle, sonst gibt es zwei
            # Fassungen davon, was ein Simulationskonto ist. Hier wird nur
            # eingesetzt, was die Bridge gemeldet hat.
            geprueft = confirm_simulated_account(
                str(payload.get("account", "")),
                is_simulation=bool(payload.get("is_simulation", False)),
                provider=str(payload.get("provider", "")),
                allowed_accounts=self.allowed_accounts,
            )
            self._account_detail = str(payload.get("detail", ""))
            kandidaten = payload.get("candidates") or []
            if isinstance(kandidaten, list):
                self._account_candidates = ", ".join(
                    f"{k.get('name')} (Provider={k.get('account_provider')})"
                    for k in kandidaten
                    if isinstance(k, dict)
                )
            self._account = replace(
                geprueft,
                currency=str(payload.get("currency", "")),
                net_liquidation=float(payload.get("net_liquidation", 0.0)),  # type: ignore[arg-type]
                available_funds=float(payload.get("buying_power", 0.0)),  # type: ignore[arg-type]
            )
            self._account_seen.set()
            return

        if ereignis.kind == "position":
            payload = ereignis.payload
            # Zurueck auf das Wurzelsymbol. Ohne das fuehrte der Adapter eine
            # Position unter "MNQ SEP26", waehrend `close_position("MNQ")`
            # danach sucht und nichts findet - die Position bliebe offen, und
            # das Aufraeumen meldete trotzdem Erfolg.
            gemeldet = str(payload.get("symbol", ""))
            symbol = self._back.get(gemeldet.upper(), gemeldet)
            payload["symbol"] = symbol
            with self._state_lock:
                self._positions[symbol] = BrokerPosition(
                    symbol=symbol,
                    quantity=int(payload.get("quantity", 0)),  # type: ignore[arg-type]
                    avg_price=float(payload.get("avg_price", 0.0)),  # type: ignore[arg-type]
                    account=str(payload.get("account", "")),
                )
            return

        if ereignis.kind in ("order", "error"):
            self._apply_order(ereignis)
            return

        if ereignis.kind == "fill":
            self._apply_fill(ereignis)

    def _apply_order(self, ereignis: BrokerEvent) -> None:
        ref = self._ref_of(ereignis)
        with self._state_lock:
            order = self._orders.get(ref)
            if order is None:
                # Eine fremde Order - etwa nach einem Neustart von TradeX,
                # waehrend NinjaTrader weiterlief. Sie wird aufgenommen statt
                # verworfen: genau diese Orders muss man wiederfinden koennen.
                order_key, _ = parse_order_ref(ref)
                order = BrokerOrder(
                    order_id=self._id_for(ref),
                    order_key=order_key,
                    symbol="",
                    side=protocol.side_of(ereignis.payload.get("side")),
                    quantity=0,
                    kind=OrderKind.MARKET,
                )
                self._orders[ref] = order

            if ereignis.state is not None:
                order.state = ereignis.state
            payload = ereignis.payload
            roh_id = str(payload.get("broker_order_id", ""))
            if roh_id:
                # Die Zeichenketten-ID der Bridge auf die int-Kennung abbilden,
                # unter der TradeX diese Order fuehrt.
                self._order_ids.setdefault(roh_id, order.order_id)
            if "filled_quantity" in payload:
                order.filled_quantity = int(payload["filled_quantity"])  # type: ignore[arg-type]
            if "avg_fill_price" in payload:
                order.avg_fill_price = float(payload["avg_fill_price"])  # type: ignore[arg-type]
            if ereignis.kind == "error":
                order.error = f"{payload.get('code', '')}: {payload.get('detail', '')}"
                # Eine Ablehnung beendet die GANZE Familie. Das AddOn lehnt
                # ab, BEVOR etwas hinausgeht - dann gibt es auch keine
                # Klammerorders. Blieben sie als `submitted` stehen, fuehrte
                # TradeX zwei Orders, die es beim Broker nie gab: sie tauchten
                # dauerhaft in `get_open_orders()` auf, und
                # `cancel_all_orders()` versuchte sie bei jedem Aufruf erneut
                # zu stornieren.
                for anderer_ref, andere in self._orders.items():
                    if andere.order_key == order.order_key and anderer_ref != ref:
                        andere.state = OrderState.REJECTED
                        andere.error = order.error
            elif payload.get("error"):
                order.error = str(payload["error"])
            order.updated_ts = int(payload.get("ts_ns", 0))  # type: ignore[arg-type]
            ereignis.order_id = order.order_id

    def _apply_fill(self, ereignis: BrokerEvent) -> None:
        ref = self._ref_of(ereignis)
        payload = ereignis.payload
        with self._state_lock:
            order = self._orders.get(ref)
            if order is None:
                return
            menge = int(payload.get("quantity", 0))  # type: ignore[arg-type]
            preis = float(payload.get("price", 0.0))  # type: ignore[arg-type]
            vorher = order.filled_quantity
            gesamt = vorher + menge
            if gesamt > 0:
                # Mengengewichtet: bei Teilfuellungen ist der Durchschnitt die
                # einzige Zahl, die den tatsaechlichen Einstand trifft.
                order.avg_fill_price = (order.avg_fill_price * vorher + preis * menge) / gesamt
            order.filled_quantity = gesamt
            if bool(payload.get("commission_reported", False)):
                order.commission += float(payload.get("commission", 0.0))  # type: ignore[arg-type]
                order.commission_reported = True
            ereignis.order_id = order.order_id

    def _ref_of(self, ereignis: BrokerEvent) -> str:
        """Der Schluessel, unter dem diese Order gefuehrt wird.

        Das AddOn schickt `order_key` bereits als `order_ref` - also mit
        `#stop`/`#target` bei den Klammerteilen.
        """
        return ereignis.order_key

    def _id_for(self, ref: str) -> int:
        """Eine stabile int-Kennung je `order_ref`.

        NinjaTraders GUIDs taugen dafuer nicht: `BrokerOrder.order_id` und
        `cancel_order()` rechnen mit `int`. Vergeben wird je Referenz einmal.
        """
        vorhanden = self._order_ids.get(ref)
        if vorhanden is not None:
            return vorhanden
        neu = self._next_order_id
        self._next_order_id += 1
        self._order_ids[ref] = neu
        return neu

    def _emit(self, ereignis: BrokerEvent) -> None:
        self._events.put(ereignis)

    def _send(self, command: dict[str, Any]) -> None:
        sock = self._sock
        if sock is None:
            raise BrokerNotConnected(protocol.REJECT_NOT_CONNECTED)
        roh = protocol.encode(command)
        with self._send_lock:
            try:
                sock.sendall(roh)
            except OSError as fehler:
                raise BrokerNotConnected(f"Senden fehlgeschlagen: {fehler}") from fehler
