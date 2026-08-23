"""Orderaufbau und Rueckuebersetzung dessen, was IBKR meldet.

Zwei Richtungen, beide heikel:

    hinaus   OrderRequest -> Bracket aus drei Orders (Einstieg, Stop, Ziel)
    herein   Statusstring / Fehlercode -> `OrderState`

Warum die Reihenfolge beim Bracket zaehlt
-----------------------------------------
IBKR uebertraegt eine Ordergruppe erst, wenn die letzte Order `transmit=True`
traegt. Wird ein Kind zu frueh uebertragen, geht es OHNE Elternorder an die
Boerse - und liegt dort als nackte Stop-Order ohne Position dahinter. Das ist
kein theoretischer Fall, sondern der Standardfehler bei Brackets. Deshalb baut
`build_bracket` die Kette in einem Stueck und prueft sie selbst nach, statt
sich auf die Aufrufreihenfolge zu verlassen.

Warum die Zustandsabbildung nicht rechnet
-----------------------------------------
IBKR kennt **keinen** Status "Rejected". Eine abgelehnte Order kommt als
`error()`-Callback und behaelt oft den letzten Status. Wer nur `orderStatus`
auswertet, sieht eine Order ewig auf "PreSubmitted" stehen und haelt sie fuer
unterwegs. Beide Quellen zusammen ergeben erst das Bild - deshalb stehen sie
hier nebeneinander.

Der ganze Modul ist frei von `ibapi` bis auf `to_ibapi_order`: der Aufbau soll
sich pruefen lassen, ohne dass eine TWS-Installation vorhanden ist.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from tradex.broker.types import (
    ROLE_ENTRY,
    ROLE_STOP,
    ROLE_TARGET,
    OrderKind,
    OrderRequest,
    OrderSide,
    OrderState,
    order_ref,
)
from tradex.domain.instruments import Instrument

if TYPE_CHECKING:  # pragma: no cover - nur fuer die Typpruefung
    from ibapi.order import Order

#: IBKR-Ordertypen. Nur diese drei werden gesendet.
IB_MARKET: Final = "MKT"
IB_LIMIT: Final = "LMT"
IB_STOP: Final = "STP"

_KIND_TO_IB: Final[dict[OrderKind, str]] = {
    OrderKind.MARKET: IB_MARKET,
    OrderKind.LIMIT: IB_LIMIT,
    OrderKind.STOP: IB_STOP,
}


# --------------------------------------------------------------- Ordernummern
class OrderIdAllocator:
    """Vergibt Ordernummern - fortlaufend, ohne Wiederholung, threadsicher.

    Die erste gueltige Nummer kommt vom Broker (`nextValidId`) und wird
    danach nur noch hochgezaehlt. Zwei Faeden duerfen sich hier nicht ins
    Gehege kommen: eine doppelt vergebene Nummer waere beim Broker eine
    Aenderung der bestehenden Order statt einer neuen - also im schlimmsten
    Fall eine stillschweigend verdoppelte oder verschobene Position.

    Steht bewusst hier und nicht im Adapter: so laesst sie sich ohne `ibapi`
    pruefen.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next = 0
        self._seeded = threading.Event()

    def seed(self, order_id: int) -> None:
        """`nextValidId` vom Broker uebernehmen.

        Nur nach oben: IBKR schickt die Nummer auch bei jedem Reconnect, und
        sie kann dann NIEDRIGER sein als das, was diese Sitzung schon vergeben
        hat. Zurueckzusetzen hiesse, Nummern ein zweites Mal auszugeben.
        """
        with self._lock:
            self._next = max(self._next, int(order_id))
        self._seeded.set()

    def wait_until_seeded(self, timeout: float) -> bool:
        return self._seeded.wait(timeout)

    @property
    def is_seeded(self) -> bool:
        return self._seeded.is_set()

    def take(self, count: int = 1) -> tuple[int, ...]:
        if count <= 0:
            raise ValueError("count muss > 0 sein")
        if not self._seeded.is_set():
            raise RuntimeError("Ordernummern ohne nextValidId - der Broker hat nie geantwortet")
        with self._lock:
            first = self._next
            self._next += count
        return tuple(range(first, first + count))

    def peek(self) -> int:
        with self._lock:
            return self._next


# ------------------------------------------------------------------- Aufbau
@dataclass(frozen=True, slots=True)
class OrderPlan:
    """Eine einzelne Order, fertig beschrieben, aber noch ohne `ibapi`.

    Der Zwischenschritt existiert, damit die eine Frage, auf die es ankommt -
    uebertraegt hier ein Kind vor seinem Elternteil? - beantwortbar ist, ohne
    eine Verbindung aufzubauen.
    """

    role: str
    order_id: int
    action: str
    quantity: int
    order_type: str
    order_ref: str
    parent_id: int = 0
    limit_price: float = 0.0
    stop_price: float = 0.0
    transmit: bool = False
    tif: str = "DAY"
    outside_rth: bool = False
    account: str = ""

    @property
    def is_child(self) -> bool:
        return self.parent_id > 0


def required_order_ids(request: OrderRequest) -> int:
    """Wie viele Ordernummern dieses Signal braucht."""
    return 1 + int(request.stop_loss > 0) + int(request.take_profit > 0)


def build_bracket(
    request: OrderRequest,
    instrument: Instrument,
    order_ids: Sequence[int],
    outside_rth: bool = True,
    account: str = "",
    entry_tif: str = "DAY",
    child_tif: str = "GTC",
) -> tuple[OrderPlan, ...]:
    """Einstieg, Stop und Ziel als eine Gruppe.

    Die Kinder laufen `GTC`: sie sollen die Sitzung ueberleben. Eine Position,
    deren Schutzorders um 22 Uhr verfallen, waere ueber Nacht ungesichert - und
    genau darauf beruht die Entscheidung in `BrokerExecutor.finish()`, offene
    Positionen offen zu lassen.

    `outside_rth` ist Vorgabe aus der Config und kein fester Wert: bei Futures
    laeuft der Handel fast rund um die Uhr, und ein Stop, der ausserhalb der
    Kernzeit nicht ausloest, ist kein Stop. Wer das ausschaltet, soll es
    ausdruecklich tun.
    """
    needed = required_order_ids(request)
    if len(order_ids) < needed:
        raise ValueError(f"{request.order_key}: {needed} Ordernummern noetig, {len(order_ids)} da")

    entry_side = request.side
    exit_side = entry_side.opposite
    parent_id = int(order_ids[0])
    has_stop = request.stop_loss > 0
    has_target = request.take_profit > 0

    plans: list[OrderPlan] = [
        OrderPlan(
            role=ROLE_ENTRY,
            order_id=parent_id,
            action=entry_side.value,
            quantity=request.quantity,
            order_type=_KIND_TO_IB[request.kind],
            order_ref=order_ref(request.order_key, ROLE_ENTRY),
            limit_price=request.limit_price if request.kind is OrderKind.LIMIT else 0.0,
            # Der Einstieg uebertraegt nur dann selbst, wenn er allein steht.
            transmit=not (has_stop or has_target),
            tif=entry_tif,
            account=account,
        )
    ]
    if has_stop:
        plans.append(
            OrderPlan(
                role=ROLE_STOP,
                order_id=int(order_ids[len(plans)]),
                action=exit_side.value,
                quantity=request.quantity,
                order_type=IB_STOP,
                order_ref=order_ref(request.order_key, ROLE_STOP),
                parent_id=parent_id,
                stop_price=instrument.round_to_tick(request.stop_loss),
                transmit=not has_target,
                tif=child_tif,
                outside_rth=outside_rth,
                account=account,
            )
        )
    if has_target:
        plans.append(
            OrderPlan(
                role=ROLE_TARGET,
                order_id=int(order_ids[len(plans)]),
                action=exit_side.value,
                quantity=request.quantity,
                order_type=IB_LIMIT,
                order_ref=order_ref(request.order_key, ROLE_TARGET),
                parent_id=parent_id,
                limit_price=instrument.round_to_tick(request.take_profit),
                transmit=True,
                tif=child_tif,
                outside_rth=outside_rth,
                account=account,
            )
        )

    problem = check_transmit_sequence(plans)
    if problem:
        # Ein Bauplan, der sich selbst widerspricht, wird nicht gesendet. Der
        # Fehler faellt hier auf und nicht als herrenlose Stop-Order im Markt.
        raise ValueError(f"{request.order_key}: {problem}")
    return tuple(plans)


def check_transmit_sequence(plans: Sequence[OrderPlan]) -> str:
    """Prueft die einzige Eigenschaft, die beim Bracket wirklich zaehlt.

    Liefert "" wenn alles stimmt, sonst die Beanstandung. Getrennt von
    `build_bracket`, damit ein Test dieselbe Pruefung auf selbst gebaute
    Faelle anwenden kann.
    """
    if not plans:
        return "leeres Bracket"
    for plan in plans[:-1]:
        if plan.transmit:
            return f"{plan.role} traegt transmit=True, ist aber nicht die letzte Order"
    if not plans[-1].transmit:
        return f"{plans[-1].role} ist die letzte Order, uebertraegt aber nicht"
    parent = plans[0]
    if parent.is_child:
        return "die erste Order des Brackets darf kein Kind sein"
    for plan in plans[1:]:
        if plan.parent_id != parent.order_id:
            return f"{plan.role} zeigt auf parentId={plan.parent_id}, nicht auf {parent.order_id}"
        if plan.order_id <= parent.order_id:
            return f"{plan.role} hat eine Ordernummer <= der Elternorder"
    return ""


# --------------------------------------------------------- Zustandsabbildung
#: IBKR-Statusstrings -> eigener Zustand. Was hier fehlt, wird NICHT geraten.
_STATUS_MAP: Final[dict[str, OrderState]] = {
    "ApiPending": OrderState.SUBMITTED,
    "PendingSubmit": OrderState.SUBMITTED,
    "PreSubmitted": OrderState.SUBMITTED,
    "Submitted": OrderState.ACCEPTED,
    # Storniert ist erst storniert, wenn IBKR es bestaetigt. "PendingCancel"
    # als Endzustand zu lesen waere der teure Irrtum: die Order liegt noch im
    # Markt und kann in genau diesem Moment fuellen.
    "PendingCancel": OrderState.ACCEPTED,
    "Cancelled": OrderState.CANCELLED,
    "ApiCancelled": OrderState.CANCELLED,
    "Filled": OrderState.FILLED,
    "Inactive": OrderState.INACTIVE,
}


def map_status(status: str, filled: int = 0, remaining: int = 0) -> OrderState | None:
    """`orderStatus` -> `OrderState`. None heisst: unbekannt, nichts aendern.

    Ein unbekannter Status wird nicht auf einen Endzustand abgebildet. Lieber
    eine Order, die im Protokoll als "unverstanden" auftaucht, als eine, die
    faelschlich als abgeschlossen gilt - im zweiten Fall stimmt das Risikobuch
    nicht mehr mit dem Markt ueberein.
    """
    state = _STATUS_MAP.get(status.strip())
    if state is None:
        return None
    if state is OrderState.FILLED and remaining > 0:
        # IBKR meldet "Filled" auch waehrend einer Teilfuellung.
        return OrderState.PARTIALLY_FILLED
    if state.is_live and filled > 0:
        return OrderState.PARTIALLY_FILLED
    return state


#: Fehlercodes, die eine Order endgueltig erledigen.
REJECTION_CODES: Final[frozenset[int]] = frozenset(
    {
        201,  # Order rejected - reason folgt im Text
        203,  # Instrument fuer dieses Konto nicht zugelassen
        321,  # Server error validating value
        382,  # Menge ueber dem Maximum
        383,  # Menge unter dem Minimum
        388,  # Order groesser als erlaubt
        434,  # Ordermenge 0
    }
)
CANCELLATION_CODES: Final[frozenset[int]] = frozenset({202})

#: Verbindungsereignisse. IBKR schickt sie als "Fehler", obwohl 1102 und 2158
#: gute Nachrichten sind - deshalb die Trennung.
CONNECTION_LOST_CODES: Final[frozenset[int]] = frozenset({1100, 1300, 502, 504, 1101})
CONNECTION_OK_CODES: Final[frozenset[int]] = frozenset({1102})

#: Laufende Meldungen der Datenfarmen. Sie als Fehler zu protokollieren wuerde
#: das Log so zuverlaessig fluten, dass echte Fehler darin untergehen.
INFO_CODES: Final[frozenset[int]] = frozenset(
    {2100, 2103, 2104, 2105, 2106, 2107, 2108, 2119, 2158, 2168, 2169, 399}
)


def state_for_error(code: int) -> OrderState | None:
    """Fehlercode -> Endzustand, oder None wenn er keiner ist.

    Hier entsteht der Zustand `REJECTED`, den IBKR selbst nicht kennt.
    """
    if code in REJECTION_CODES:
        return OrderState.REJECTED
    if code in CANCELLATION_CODES:
        return OrderState.CANCELLED
    return None


def is_connection_code(code: int) -> bool:
    return code in CONNECTION_LOST_CODES or code in CONNECTION_OK_CODES


# --------------------------------------------------------------------- ibapi
def to_ibapi_order(plan: OrderPlan) -> Order:
    """`OrderPlan` -> `ibapi.order.Order`. Reines Umfuellen, keine Logik."""
    from decimal import Decimal

    from ibapi.order import Order

    order = Order()
    order.orderId = plan.order_id
    order.action = plan.action
    order.orderType = plan.order_type
    order.totalQuantity = Decimal(plan.quantity)
    order.tif = plan.tif
    order.transmit = plan.transmit
    order.orderRef = plan.order_ref
    order.outsideRth = plan.outside_rth
    if plan.parent_id:
        order.parentId = plan.parent_id
    if plan.limit_price:
        order.lmtPrice = plan.limit_price
    if plan.stop_price:
        order.auxPrice = plan.stop_price
    if plan.account:
        # Ausdruecklich setzen, obwohl nur ein Konto zugelassen ist. Genau
        # deshalb: was ohnehin feststeht, soll auch dranstehen.
        order.account = plan.account

    for veraltet in ("eTradeOnly", "firmQuoteOnly"):
        # Aeltere `ibapi`-Fassungen setzen diese Felder auf True und handeln
        # sich damit Fehler 10268 ein; neuere kennen sie nicht mehr. Beides
        # abgedeckt, ohne die Version abzufragen.
        if hasattr(order, veraltet):
            setattr(order, veraltet, False)
    return order


def side_from_action(action: str) -> OrderSide:
    return OrderSide.BUY if action.upper() == "BUY" else OrderSide.SELL


def kind_from_ib(order_type: str) -> OrderKind:
    upper = order_type.strip().upper()
    if upper.startswith("LMT"):
        return OrderKind.LIMIT
    if upper.startswith("STP"):
        return OrderKind.STOP
    return OrderKind.MARKET
