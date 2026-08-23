"""Brokerunabhaengige Datentypen der Orderanbindung.

Hier steht kein einziger IBKR-Begriff. Das ist der ganze Zweck: der Rest des
Programms spricht ausschliesslich diese Typen, und ein zweiter Broker waere
ein zweiter Adapter - nicht ein zweiter Datenfluss.

Der wichtigste Typ ist `OrderState`. Eine gesendete Order ist KEINE
ausgefuehrte Order; wer das verwechselt, fuehrt intern Positionen, die es beim
Broker nicht gibt. Maßgeblich ist immer die Rueckmeldung des Brokers, nie der
erfolgreiche Aufruf.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from tradex.domain.enums import Direction


class OrderState(StrEnum):
    """Zustand einer Order beim Broker.

    `SUBMITTED` heisst: hinausgegangen, noch nicht bestaetigt. Erst `ACCEPTED`
    bedeutet, dass die Boerse sie angenommen hat, und erst `FILLED`, dass eine
    Position existiert.
    """

    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    INACTIVE = "inactive"

    @property
    def is_terminal(self) -> bool:
        """Ist der Zustand endgueltig? Danach kommt keine Fuellung mehr."""
        return self in _TERMINAL

    @property
    def is_live(self) -> bool:
        """Liegt die Order noch beim Broker und kann sich noch aendern?"""
        return not self.is_terminal


_TERMINAL: frozenset[OrderState] = frozenset(
    {OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.INACTIVE}
)


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> OrderSide:
        return OrderSide.SELL if self is OrderSide.BUY else OrderSide.BUY

    @classmethod
    def from_direction(cls, direction: Direction) -> OrderSide:
        return cls.BUY if direction is Direction.BULLISH else cls.SELL


class OrderKind(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


ROLE_ENTRY = "entry"
ROLE_STOP = "stop"
ROLE_TARGET = "target"

_ROLE_SEPARATOR = "#"


def order_ref(order_key: str, role: str) -> str:
    """Die Kennung, die beim Broker mitlaeuft.

    Sie ist der einzige Faden, an dem sich nach einem Verbindungsabriss - oder
    nach einem Neustart des Programms - wiederfinden laesst, welche fremde
    Order zu welchem eigenen Signal gehoert. Order-IDs taugen dafuer nicht: sie
    vergibt der Broker, und das Programm kennt sie nach einem Neustart nicht
    mehr.
    """
    return order_key if role == ROLE_ENTRY else f"{order_key}{_ROLE_SEPARATOR}{role}"


def parse_order_ref(ref: str) -> tuple[str, str]:
    """`order_ref` rueckwaerts: (order_key, rolle)."""
    if _ROLE_SEPARATOR in ref:
        key, _, role = ref.partition(_ROLE_SEPARATOR)
        return key, role
    return ref, ROLE_ENTRY


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """Was gesendet werden soll - vollstaendig, brokerunabhaengig.

    `order_key` ist der Duplikatschutz und muss ueber Prozessneustarts hinweg
    eindeutig sein. Die interne `trade_id` allein reicht dafuer NICHT: das
    Risikobuch lebt im Speicher, nach einem Neustart beginnt der Zaehler
    wieder bei 1 - und der Broker haette zwei verschiedene Trades unter
    derselben Kennung.
    """

    order_key: str
    symbol: str
    side: OrderSide
    quantity: int
    kind: OrderKind = OrderKind.MARKET
    limit_price: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    planned_entry: float = 0.0
    """Der Kurs, mit dem die Strategie gerechnet hat. Geht NICHT an den Broker -
    eine Market-Order hat keinen Wunschkurs. Er steht hier, weil die Differenz
    zum tatsaechlichen Fuellkurs die eigentlich interessante Zahl ist."""
    #: Nur zur Protokollierung - der Broker kennt sie nicht.
    signal_id: int = 0
    strategy: str = ""

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"{self.order_key}: quantity muss > 0 sein")
        if self.kind is OrderKind.LIMIT and self.limit_price <= 0:
            raise ValueError(f"{self.order_key}: LIMIT braucht einen limit_price")

    @property
    def has_bracket(self) -> bool:
        return self.stop_loss > 0 or self.take_profit > 0


@dataclass(slots=True)
class Fill:
    """Eine einzelne Ausfuehrung. Teilfuellungen liefern mehrere."""

    order_id: int
    quantity: int
    price: float
    ts_utc: str
    commission: float = 0.0
    exec_id: str = ""


@dataclass(slots=True)
class BrokerOrder:
    """Der bekannte Zustand einer Order beim Broker.

    `filled_quantity` und `avg_fill_price` kommen ausschliesslich aus
    Broker-Rueckmeldungen. Sie werden nie aus "wir haben ja gesendet"
    abgeleitet.
    """

    order_id: int
    order_key: str
    symbol: str
    side: OrderSide
    quantity: int
    kind: OrderKind
    state: OrderState = OrderState.SUBMITTED
    filled_quantity: int = 0
    avg_fill_price: float = 0.0
    limit_price: float = 0.0
    stop_price: float = 0.0
    parent_order_id: int = 0
    commission: float = 0.0
    """Vom Broker gemeldete Gebuehren. 0 heisst "noch nicht gemeldet", nicht
    "keine" - der Unterschied entscheidet, ob unten geschaetzt werden muss."""
    commission_reported: bool = False
    error: str = ""
    updated_ts: int = 0

    @property
    def remaining(self) -> int:
        return max(self.quantity - self.filled_quantity, 0)

    @property
    def is_child(self) -> bool:
        return self.parent_order_id > 0


@dataclass(slots=True)
class BrokerPosition:
    """Eine Position, wie der BROKER sie sieht.

    Bewusst getrennt vom internen Risikobuch: die Differenz zwischen beiden ist
    genau die Information, die man nach einem Verbindungsabriss braucht.
    """

    symbol: str
    quantity: int
    """Vorzeichenbehaftet: negativ = short."""
    avg_price: float = 0.0
    account: str = ""

    @property
    def side(self) -> OrderSide:
        return OrderSide.BUY if self.quantity > 0 else OrderSide.SELL


@dataclass(slots=True)
class AccountInfo:
    """Kontostammdaten - Grundlage des Paper-Nachweises."""

    account: str
    currency: str = ""
    net_liquidation: float = 0.0
    available_funds: float = 0.0
    account_type: str = ""
    """Was der Broker selbst ueber die Kontoart sagt; oft leer."""
    is_paper: bool = False
    paper_evidence: str = ""
    """WORAUF sich `is_paper` stuetzt. Ohne diese Angabe waere das Flag eine
    Behauptung - so laesst es sich im Protokoll nachlesen und bestreiten."""


@dataclass(slots=True)
class BrokerEvent:
    """Eine Rueckmeldung des Brokers, aus dem Ereignisfaden herausgereicht.

    Der Adapterfaden schreibt sie in eine Queue und tut sonst nichts. Alles
    Weitere passiert im Sitzungsfaden - der API-Ereignisfaden darf nie auf
    Strategie, Datenbank oder Netz warten.
    """

    kind: str
    """`order` | `fill` | `connection` | `error` | `account` | `position`."""
    ts_utc: str = ""
    order_key: str = ""
    order_id: int = 0
    state: OrderState | None = None
    payload: dict[str, object] = field(default_factory=dict)
