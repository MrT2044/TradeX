"""Der Order-Manager: EINE Instanz je Konto.

Dieselbe Ueberlegung wie beim Risikobuch eine Ebene tiefer - Duplikatschutz,
Orderfrequenz und die Zuordnung "welche fremde Order gehoert zu welchem
Signal" gelten dem Konto, nicht dem einzelnen Instrument. Haette jedes Symbol
seinen eigenen Manager, waere `max_orders_per_minute` in Wahrheit "pro Minute
und Instrument", und nach einem Reconnect wuesste niemand mehr, wem eine
gefundene Order gehoert.

Was der Manager NICHT tut
-------------------------
Er entscheidet nicht, ob gehandelt wird. Wenn eine Anfrage hier ankommt, hat
die Risk Engine bereits zugestimmt und die Position steht im Risikobuch. Der
Manager prueft ausschliesslich Dinge, die die Risk Engine gar nicht wissen
kann: steht die Verbindung, ist das Konto bestaetigt, kam dieses Signal schon
einmal, ist die Bar zu alt.

Der Duplikatschutz
------------------
Drei Ebenen, absichtlich redundant, weil eine doppelt ausgefuehrte Order das
teuerste einzelne Versagen waere:

    1. `_submitted` im Speicher - faengt Wiederholungen im laufenden Betrieb
    2. `order_key` UNIQUE in `broker_orders` - faengt sie ueber Neustarts
    3. `orderRef` beim Broker - faengt sie nach einem Reconnect

Nur die erste allein waere zu wenig: `RiskLedger.next_trade_id()` zaehlt im
Speicher und faengt nach einem Neustart wieder bei 1 an. Deshalb traegt der
Schluessel die Sitzungskennung mit sich.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field

from tradex.analysis import reasons as R
from tradex.broker import journal as J
from tradex.broker.base import BrokerError, BrokerInterface, BrokerNotConnected
from tradex.broker.guard import check_simulated_account
from tradex.broker.journal import TradeJournal
from tradex.broker.store import BrokerOrderStore
from tradex.broker.types import (
    ROLE_ENTRY,
    AccountInfo,
    BrokerEvent,
    BrokerOrder,
    OrderRequest,
    OrderState,
    parse_order_ref,
)
from tradex.config import BrokerConfig
from tradex.logging_setup import get_logger
from tradex.persistence.models import Reason

log = get_logger(__name__)


def build_order_key(session_id: int | None, trade_id: int) -> str:
    """Der kontoweit und neustartfest eindeutige Schluessel eines Signals.

    Ohne Sitzungskennung waere er es nicht: das Risikobuch lebt im Speicher und
    vergibt nach einem Neustart wieder die 1. Eine noch laufende Order mit
    demselben Schluessel wuerde dann faelschlich als Duplikat gelten - oder,
    schlimmer, ein neues Signal wuerde unter der Kennung eines alten gefuehrt.
    """
    prefix = f"s{session_id}" if session_id is not None else "s0"
    return f"tx-{prefix}-{trade_id}"


@dataclass(slots=True)
class OrderUpdate:
    """Eine Zustandsaenderung, aufgeloest auf Signal und Rolle."""

    order_key: str
    role: str
    order: BrokerOrder


@dataclass(slots=True)
class _Tracked:
    """Was der Manager ueber ein gesendetes Signal weiss."""

    request: OrderRequest
    orders: dict[str, BrokerOrder] = field(default_factory=dict)
    """Rolle -> Order. `entry`, `stop`, `target`."""


class OrderManager:
    """Sendet Orders und fuehrt ihren tatsaechlichen Zustand nach."""

    def __init__(
        self,
        broker: BrokerInterface,
        params: BrokerConfig,
        journal: TradeJournal,
        store: BrokerOrderStore | None = None,
        session_id: int | None = None,
        clock: object = None,
    ) -> None:
        self.broker = broker
        self.params = params
        self.journal = journal
        self.store = store
        self.session_id = session_id
        self.account: AccountInfo | None = None

        self._lock = threading.Lock()
        self._submitted: set[str] = set()
        self._tracked: dict[str, _Tracked] = {}
        self._pending: dict[str, list[OrderUpdate]] = {}
        """Symbol -> noch nicht abgeholte Rueckmeldungen. Siehe `drain()`."""
        self._recent: deque[float] = deque()
        self._blocked_reason = ""

        if store is not None:
            # Was frueher schon einmal hinausging, geht nicht noch einmal
            # hinaus - auch nicht nach einem Absturz mitten im Betrieb.
            self._submitted |= store.known_keys()

    # ------------------------------------------------------------------ Status
    @property
    def is_ready(self) -> bool:
        """Darf ueberhaupt gesendet werden?"""
        return (
            not self._blocked_reason
            and self.broker.is_connected()
            and self.account is not None
            and self.account.is_paper
        )

    @property
    def blocked_reason(self) -> str:
        return self._blocked_reason

    def block(self, reason: str) -> None:
        """Den Manager sperren. Wird nicht von selbst wieder aufgehoben."""
        if self._blocked_reason == reason:
            return
        self._blocked_reason = reason
        self.journal.blocked(reason)

    def unblock(self) -> None:
        self._blocked_reason = ""

    def bind_account(self, account: AccountInfo) -> None:
        self.account = account
        self.journal.bind_account(account.account)

    # ------------------------------------------------------------- Vorpruefung
    def check(self, order_key: str, now: float, data_age_seconds: float) -> Reason | None:
        """Die Stufen, die erst hier bekannt sind. None heisst: gruen.

        Reihenfolge ist Absicht: erst die Zustaende, die alles betreffen
        (Verbindung, Konto), dann die signalbezogenen. Ein Duplikat zu melden,
        waehrend in Wahrheit die Verbindung fehlt, waere eine irrefuehrende
        Begruendung im Protokoll.
        """
        if self._blocked_reason:
            return Reason(R.BROKER_NOT_CONNECTED, False, {"grund": self._blocked_reason})
        if not self.broker.is_connected():
            return Reason(R.BROKER_NOT_CONNECTED, False, {"grund": "keine Verbindung"})

        account_reason = check_simulated_account(self.account)
        if not account_reason.ok:
            return account_reason

        if data_age_seconds > self.params.max_data_age_seconds:
            return Reason(
                R.BROKER_DATA_STALE,
                False,
                {
                    "alter_sekunden": round(data_age_seconds, 2),
                    "erlaubt_sekunden": self.params.max_data_age_seconds,
                },
            )

        with self._lock:
            if order_key in self._submitted:
                return Reason(R.BROKER_DUPLICATE_SIGNAL, False, {"order_key": order_key})
            if self._rate_limited(now):
                return Reason(
                    R.BROKER_RATE_LIMITED,
                    False,
                    {"limit": self.params.max_orders_per_minute},
                )
        return None

    def _rate_limited(self, now: float) -> bool:
        """Gleitendes Fenster ueber eine Minute. Aufrufer haelt das Lock."""
        cutoff = now - 60.0
        while self._recent and self._recent[0] < cutoff:
            self._recent.popleft()
        return len(self._recent) >= self.params.max_orders_per_minute

    # ---------------------------------------------------------------- Senden
    def submit(self, request: OrderRequest, now: float) -> BrokerOrder | None:
        """Order senden. None heisst: es ist keine entstanden.

        Der Schluessel wird VOR dem Senden vermerkt. Bliebe er bis zur
        Bestaetigung offen, koennte ein zweiter Durchgang in genau diesem
        Fenster dieselbe Order noch einmal absetzen - und dieses Fenster ist
        ein Netzwerkaufruf lang.
        """
        with self._lock:
            if request.order_key in self._submitted:
                log.warning("order_duplikat_verhindert", order_key=request.order_key)
                return None
            self._submitted.add(request.order_key)
            self._recent.append(now)

        self.journal.signal(J.SIGNAL_RECEIVED, request)
        try:
            order = self.broker.place_market_order(request)
        except BrokerNotConnected as exc:
            # Nicht sammeln, nicht wiederholen: eine spaeter nachgesendete
            # Order handelt auf einer Marktlage, die es nicht mehr gibt.
            self._fail(request, R.BROKER_NOT_CONNECTED, str(exc))
            return None
        except BrokerError as exc:
            self._fail(request, R.BROKER_ORDER_FAILED, str(exc))
            return None

        with self._lock:
            self._tracked[request.order_key] = _Tracked(
                request=request, orders={ROLE_ENTRY: order}
            )
        self.journal.order(order)
        if self.store is not None:
            self.store.record_submitted(
                request,
                order,
                broker=self.broker.name,
                account=self._account_name,
                trading_mode=self.journal.trading_mode,
                session_id=self.session_id,
            )
        return order

    def reject(self, request: OrderRequest, reason: Reason) -> None:
        """Ein Signal festhalten, aus dem bewusst keine Order wurde."""
        self.journal.signal(J.RISK_REJECTED, request, reason=reason.code)
        if self.store is not None:
            self.store.record_rejected(
                request,
                reason.code,
                broker=self.broker.name,
                account=self._account_name,
                trading_mode=self.journal.trading_mode,
                session_id=self.session_id,
                error=str(reason.params),
            )

    def _fail(self, request: OrderRequest, code: str, error: str) -> None:
        self.journal.signal(J.ORDER_REJECTED, request, reason=code, error=error)
        if self.store is not None:
            self.store.record_rejected(
                request,
                code,
                broker=self.broker.name,
                account=self._account_name,
                trading_mode=self.journal.trading_mode,
                session_id=self.session_id,
                error=error,
            )

    @property
    def _account_name(self) -> str:
        return self.account.account if self.account is not None else ""

    # ------------------------------------------------------------ Rueckmeldung
    def drain(self, symbol: str | None = None) -> list[OrderUpdate]:
        """Alle Broker-Rueckmeldungen abholen und den Zustand nachfuehren.

        Massgeblich ist ausschliesslich, was hier ankommt. Dass `submit()`
        ohne Ausnahme zurueckkehrte, heisst nur, dass die Order das Programm
        verlassen hat.

        Warum nach Symbol sortiert wird
        -------------------------------
        Der Manager gehoert dem KONTO, die Executoren gehoeren je einem
        Instrument - und die Broker-Queue gibt jedes Ereignis nur einmal
        heraus. Wuerde jeder Executor blind alles abholen und das Fremde
        wegwerfen, verschluckte der erste Aufrufer die Fuellmeldungen aller
        anderen. Bei einem Symbol faellt das nie auf; bei zweien verschwindet
        stillschweigend ein Ausstieg.

        Ohne `symbol` kommt alles zurueck - der Weg fuer Tests und fuer einen
        Aufrufer, der ohnehin alle Buecher fuehrt.
        """
        frisch: list[tuple[str, OrderUpdate]] = []
        for event in self.broker.drain_events():
            resolved = self._absorb(event)
            if resolved is None:
                continue
            ziel = self._owner_of(resolved)
            if ziel is None:
                # Eine Order, die zu keinem laufenden Vorgang gehoert: aus einer
                # frueheren Sitzung oder von Hand im Gateway erteilt. Sie wird
                # protokolliert (das ist in `_absorb` schon passiert), aber
                # nicht aufgehoben - sonst waechst der Puffer unbegrenzt.
                log.info(
                    "rueckmeldung_ohne_vorgang",
                    order_key=resolved.order_key,
                    symbol=resolved.order.symbol,
                    status=resolved.order.state.value,
                )
                continue
            frisch.append((ziel, resolved))

        with self._lock:
            for ziel, resolved in frisch:
                self._pending.setdefault(ziel, []).append(resolved)
            if symbol is None:
                alle = [update for queue in self._pending.values() for update in queue]
                self._pending.clear()
                return alle
            return self._pending.pop(symbol.upper(), [])

    def _owner_of(self, update: OrderUpdate) -> str | None:
        """Welches Instrument fuehrt diesen Vorgang?

        Ueber den verfolgten Auftrag, nicht ueber `order.symbol`: was der
        Broker als Symbol meldet, ist sein Name fuer den Kontrakt ("MNQU6"),
        nicht der von TradeX ("MNQ").
        """
        with self._lock:
            tracked = self._tracked.get(update.order_key)
        if tracked is not None:
            return tracked.request.symbol.upper()
        return None

    def _absorb(self, event: BrokerEvent) -> OrderUpdate | None:
        if event.kind == "connection":
            connected = bool(event.payload.get("connected"))
            if not connected:
                self.block("verbindung_verloren")
                self.journal.event(J.CONNECTION_LOST, **_str_payload(event.payload))
            return None
        if event.kind == "error":
            self.journal.event(
                J.ORDER_REJECTED if event.order_key else J.TRADING_BLOCKED,
                order_key=event.order_key,
                error=str(event.payload.get("message", "")),
                code=event.payload.get("code"),
            )
            return None
        if event.kind != "order":
            return None

        order = event.payload.get("order")
        if not isinstance(order, BrokerOrder):
            return None
        # `order.order_key` traegt die volle Referenz inklusive Rolle
        # ("tx-s3-17#stop"); der Vorgang laeuft unter dem Teil davor.
        key, role = parse_order_ref(order.order_key)

        with self._lock:
            tracked = self._tracked.get(key)
            if tracked is not None:
                tracked.orders[role] = order

        self.journal.order(order)
        if self.store is not None and role == ROLE_ENTRY:
            self.store.update_state(order)
        return OrderUpdate(order_key=key, role=role, order=order)

    # ---------------------------------------------------------------- Auskunft
    def tracked_request(self, order_key: str) -> OrderRequest | None:
        with self._lock:
            tracked = self._tracked.get(order_key)
        return tracked.request if tracked is not None else None

    def forget(self, order_key: str) -> None:
        """Einen abgeschlossenen Vorgang aus der laufenden Fuehrung nehmen.

        Der Schluessel bleibt in `_submitted` - er darf nie wieder verwendet
        werden, sonst waere der Duplikatschutz nur ein Gedaechtnis auf Zeit.
        """
        with self._lock:
            self._tracked.pop(order_key, None)

    def open_keys(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._tracked)

    def live_orders(self) -> tuple[BrokerOrder, ...]:
        """Alle noch offenen Orders aller Vorgaenge - OHNE den Broker zu fragen.

        Bewusst aus dem nachgefuehrten Zustand und nicht ueber
        `broker.get_open_orders()`: dieser Aufruf wartet auf eine Antwort des
        Brokers. Aus der Anzeige heraus gerufen wuerde er den Webserver
        blockieren - und zwar am zuverlaessigsten dann, wenn der Broker
        Probleme hat und man am dringendsten hinsehen will.
        """
        with self._lock:
            return tuple(
                order
                for tracked in self._tracked.values()
                for order in tracked.orders.values()
                if order.state.is_live
            )

    def state_of(self, order_key: str, role: str = ROLE_ENTRY) -> OrderState | None:
        with self._lock:
            tracked = self._tracked.get(order_key)
            if tracked is None:
                return None
            order = tracked.orders.get(role)
        return order.state if order is not None else None


def _str_payload(payload: dict[str, object]) -> dict[str, object]:
    return {str(key): value for key, value in payload.items()}
