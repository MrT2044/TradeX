"""Ein Broker ohne Netz - der Test bestimmt, was zurueckkommt.

Warum das die einzige Art ist, die Orderanbindung zu pruefen
------------------------------------------------------------
Ein Test gegen ein echtes IB Gateway prueft an einem guten Tag den Gutfall.
Was er nie prueft, sind die Faelle, auf die es ankommt: die abgelehnte Order,
die Teilfuellung, der Verbindungsabriss mitten im Vorgang, das doppelte Signal
nach einem Neustart. Diese Zustaende auf Bestellung herbeizufuehren ist bei
einem echten Broker unmoeglich - hier sind sie ein Methodenaufruf.

Der `FakeBroker` erfuellt `BrokerInterface` vollstaendig und verhaelt sich in
einem Punkt bewusst wie das Original: **er meldet nichts von selbst.** Eine
gesendete Order bleibt `SUBMITTED`, bis der Test eine Fuellung oder eine
Ablehnung ausloest. Genau das ist die Eigenschaft, die im Betrieb zaehlt - der
erfolgreiche Aufruf von `place_market_order` ist keine Position.
"""

from __future__ import annotations

from dataclasses import replace

from tradex.broker.base import BrokerError, BrokerNotConnected
from tradex.broker.types import (
    ROLE_ENTRY,
    ROLE_STOP,
    ROLE_TARGET,
    AccountInfo,
    BrokerEvent,
    BrokerOrder,
    BrokerPosition,
    OrderKind,
    OrderRequest,
    OrderSide,
    OrderState,
    order_ref,
)


class FakeBroker:
    """Erfuellt `BrokerInterface`, ohne irgendetwas zu verbinden."""

    name = "fake"

    def __init__(
        self,
        account: str = "DU123456",
        connected: bool = True,
        tradeable: bool = True,
    ) -> None:
        self._connected = connected
        self._tradeable = tradeable
        self.account = AccountInfo(
            account=account,
            currency="USD",
            net_liquidation=100_000.0,
            is_paper=account.startswith(("DU", "DF")),
            paper_evidence="praefix (Test)",
        )

        self.requests: list[OrderRequest] = []
        """Was tatsaechlich hinausgegangen ist - die Zeugenliste des Tests."""
        self.orders: dict[str, BrokerOrder] = {}
        """voller orderRef (inkl. Rolle) -> Order."""
        self.cancelled: list[int] = []
        self.positions: list[BrokerPosition] = []

        self._events: list[BrokerEvent] = []
        self._next_id = 1
        self.fail_next_with: Exception | None = None
        """Wird beim naechsten `place_market_order` geworfen und dann geloescht."""

    # ------------------------------------------------------------ Verbindung
    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def drop_connection(self) -> None:
        """Verbindungsabriss wie im Betrieb: erst die Meldung, dann der Zustand."""
        self._connected = False
        self._events.append(
            BrokerEvent(kind="connection", payload={"connected": False, "message": "Testabriss"})
        )

    # ---------------------------------------------------------------- Auskunft
    def get_account_info(self) -> AccountInfo:
        return self.account

    def get_positions(self) -> list[BrokerPosition]:
        return list(self.positions)

    def get_open_orders(self) -> list[BrokerOrder]:
        return [replace(order) for order in self.orders.values() if order.state.is_live]

    def can_trade(self, symbol: str) -> tuple[bool, str]:
        if not self._tradeable:
            return False, "Kontrakt im Test gesperrt"
        return True, "Test"

    # ------------------------------------------------------------------ Orders
    def place_market_order(self, request: OrderRequest) -> BrokerOrder:
        if self.fail_next_with is not None:
            fehler, self.fail_next_with = self.fail_next_with, None
            raise fehler
        if not self._connected:
            raise BrokerNotConnected("FakeBroker ist getrennt")

        self.requests.append(request)
        entry = self._register(request, ROLE_ENTRY, OrderKind.MARKET, request.side)
        if request.stop_loss > 0:
            self._register(
                request, ROLE_STOP, OrderKind.STOP, request.side.opposite,
                stop_price=request.stop_loss, parent=entry.order_id,
            )
        if request.take_profit > 0:
            self._register(
                request, ROLE_TARGET, OrderKind.LIMIT, request.side.opposite,
                limit_price=request.take_profit, parent=entry.order_id,
            )
        return replace(entry)

    def place_limit_order(self, request: OrderRequest) -> BrokerOrder:
        return self.place_market_order(request)

    def _register(
        self,
        request: OrderRequest,
        role: str,
        kind: OrderKind,
        side: OrderSide,
        limit_price: float = 0.0,
        stop_price: float = 0.0,
        parent: int = 0,
    ) -> BrokerOrder:
        order = BrokerOrder(
            order_id=self._next_id,
            order_key=order_ref(request.order_key, role),
            symbol=request.symbol,
            side=side,
            quantity=request.quantity,
            kind=kind,
            state=OrderState.SUBMITTED,
            limit_price=limit_price,
            stop_price=stop_price,
            parent_order_id=parent,
        )
        self._next_id += 1
        self.orders[order.order_key] = order
        return order

    def cancel_order(self, order_id: int) -> None:
        self.cancelled.append(order_id)
        for order in self.orders.values():
            if order.order_id == order_id and order.state.is_live:
                self._emit(order, OrderState.CANCELLED)

    def cancel_all_orders(self) -> None:
        for order in list(self.orders.values()):
            if order.state.is_live:
                self.cancel_order(order.order_id)

    def close_position(self, symbol: str) -> BrokerOrder | None:
        raise BrokerError("Im Test nicht vorgesehen")

    def close_all_positions(self) -> list[BrokerOrder]:
        return []

    # ------------------------------------------------------------------ Ablauf
    def drain_events(self) -> list[BrokerEvent]:
        events, self._events = self._events, []
        return events

    # ------------------------------------------------- Steuerung durch den Test
    def fill(
        self,
        order_key: str,
        role: str = ROLE_ENTRY,
        price: float = 0.0,
        quantity: int = 0,
        commission: float = 0.0,
    ) -> BrokerOrder:
        """Eine Fuellung ausloesen. `quantity=0` heisst: vollstaendig."""
        order = self._require(order_key, role)
        menge = quantity or order.quantity
        order.filled_quantity = menge
        order.avg_fill_price = price or order.limit_price or order.stop_price
        order.commission = commission
        order.commission_reported = commission > 0
        zustand = (
            OrderState.FILLED if menge >= order.quantity else OrderState.PARTIALLY_FILLED
        )
        return self._emit(order, zustand)

    def partial_fill(
        self, order_key: str, quantity: int, price: float, role: str = ROLE_ENTRY
    ) -> BrokerOrder:
        """Teilfuellung, die danach ENDET - der Rest kommt nie.

        Der interessante Fall: IBKR meldet dafuer einen Endzustand mit
        `filled < quantity`. Wer nur auf `FILLED` prueft, sieht die Position
        nie; wer nur auf den Endzustand prueft, haelt sie faelschlich fuer
        voll.
        """
        order = self._require(order_key, role)
        order.filled_quantity = quantity
        order.avg_fill_price = price
        return self._emit(order, OrderState.CANCELLED)

    def reject(self, order_key: str, role: str = ROLE_ENTRY, message: str = "abgelehnt") -> None:
        order = self._require(order_key, role)
        order.error = message
        self._emit(order, OrderState.REJECTED)

    def _require(self, order_key: str, role: str) -> BrokerOrder:
        ref = order_ref(order_key, role)
        order = self.orders.get(ref)
        if order is None:
            raise AssertionError(f"Der Test verlangt {ref!r}, aber die Order gibt es nicht")
        return order

    def _emit(self, order: BrokerOrder, state: OrderState) -> BrokerOrder:
        order.state = state
        momentaufnahme = replace(order)
        self._events.append(
            BrokerEvent(
                kind="order",
                order_key=order.order_key,
                order_id=order.order_id,
                state=state,
                payload={"order": momentaufnahme},
            )
        )
        return momentaufnahme
