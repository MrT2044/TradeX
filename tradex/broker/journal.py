"""Protokoll der Orderanbindung.

Warum eigene Ereignisnamen statt freier Sätze
---------------------------------------------
Dieselbe Ueberlegung wie bei den Reason-Codes: eine Zeile Fliesstext laesst
sich lesen, aber nicht abfragen. Die Frage am naechsten Morgen lautet "warum
stand der Betrieb um 03:12?" - und dann ist ein `category`-Wert mehr wert als
eine gut formulierte Meldung.

Jedes Ereignis geht an zwei Stellen:

    structlog  - die laufende Ausgabe und die rotierende JSONL-Datei
    system_events - die Datenbank, ueber den `event_sink` der Sitzung

Die Datenbank ist der Teil, der einen Neustart ueberlebt. Ins Textlog allein zu
schreiben hiesse, sich auf eine Datei zu verlassen, die genau dann rotiert,
wenn viel passiert ist.

Was hier NIE hineingeschrieben wird
-----------------------------------
Zugangsdaten. Die TWS-API kennt ohnehin keine - die Anmeldung passiert im IB
Gateway, das Programm sieht nur einen Socket. Die Kontonummer dagegen WIRD
protokolliert: sie ist der Paper-Nachweis, und ein Nachweis, den man nicht
nachlesen kann, ist keiner.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from tradex.broker.types import BrokerOrder, OrderRequest, OrderState
from tradex.logging_setup import get_logger

log = get_logger(__name__)

# --- Signalweg --------------------------------------------------------------
SIGNAL_RECEIVED: Final = "SIGNAL_RECEIVED"
SIGNAL_REJECTED: Final = "SIGNAL_REJECTED"
RISK_REJECTED: Final = "RISK_REJECTED"

# --- Orderweg ---------------------------------------------------------------
ORDER_SUBMITTED: Final = "ORDER_SUBMITTED"
ORDER_ACCEPTED: Final = "ORDER_ACCEPTED"
ORDER_PARTIALLY_FILLED: Final = "ORDER_PARTIALLY_FILLED"
ORDER_FILLED: Final = "ORDER_FILLED"
ORDER_CANCELLED: Final = "ORDER_CANCELLED"
ORDER_REJECTED: Final = "ORDER_REJECTED"
ORDER_INACTIVE: Final = "ORDER_INACTIVE"

# --- Verbindung und Freigabe ------------------------------------------------
CONNECTION_ESTABLISHED: Final = "CONNECTION_ESTABLISHED"
CONNECTION_LOST: Final = "CONNECTION_LOST"
CONNECTION_RESTORED: Final = "CONNECTION_RESTORED"
ACCOUNT_VALIDATED: Final = "ACCOUNT_VALIDATED"
PAPER_ACCOUNT_CONFIRMED: Final = "PAPER_ACCOUNT_CONFIRMED"
TRADING_BLOCKED: Final = "TRADING_BLOCKED"
TRADING_ENABLED: Final = "TRADING_ENABLED"
CONTRACT_RESOLVED: Final = "CONTRACT_RESOLVED"
STATE_SYNCHRONISED: Final = "STATE_SYNCHRONISED"

#: Welcher Orderzustand welches Ereignis ausloest.
STATE_EVENTS: Final[dict[OrderState, str]] = {
    OrderState.SUBMITTED: ORDER_SUBMITTED,
    OrderState.ACCEPTED: ORDER_ACCEPTED,
    OrderState.PARTIALLY_FILLED: ORDER_PARTIALLY_FILLED,
    OrderState.FILLED: ORDER_FILLED,
    OrderState.CANCELLED: ORDER_CANCELLED,
    OrderState.REJECTED: ORDER_REJECTED,
    OrderState.INACTIVE: ORDER_INACTIVE,
}

#: Ereignisse, die als Warnung oder Fehler gelten. Der Rest ist `info`.
_WARNING_EVENTS: Final[frozenset[str]] = frozenset(
    {
        SIGNAL_REJECTED,
        RISK_REJECTED,
        ORDER_CANCELLED,
        ORDER_INACTIVE,
        CONNECTION_LOST,
        TRADING_BLOCKED,
    }
)
_ERROR_EVENTS: Final[frozenset[str]] = frozenset({ORDER_REJECTED})

EventSink = Callable[[int, str, str], None]
"""(ts_ns, Art, Text) - dieselbe Senke, die die Sitzung fuer ihre eigenen
Betriebsereignisse benutzt."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass(slots=True)
class TradeJournal:
    """Schreibt die Ereignisse der Orderanbindung.

    Traegt Modus und Konto mit sich, damit jede Zeile fuer sich lesbar ist. Ein
    Protokolleintrag, dem man ansehen muss, in welchem Modus er entstand, indem
    man die Startmeldung sucht, taugt fuer die Nacht um drei nicht.
    """

    broker: str
    trading_mode: str
    account: str = ""
    event_sink: EventSink | None = None
    clock: Callable[[], int] = lambda: 0

    def bind_account(self, account: str) -> None:
        self.account = account

    # ------------------------------------------------------------------ Basis
    def event(self, name: str, message: str = "", **fields: Any) -> None:
        payload: dict[str, Any] = {
            "ts_utc": utc_now(),
            "event_name": name,
            "trading_mode": self.trading_mode,
            "broker": self.broker,
            "account": self.account,
            **fields,
        }
        if name in _ERROR_EVENTS:
            log.error("broker_ereignis", **payload)
        elif name in _WARNING_EVENTS:
            log.warning("broker_ereignis", **payload)
        else:
            log.info("broker_ereignis", **payload)

        if self.event_sink is not None:
            text = message or _summarise(name, fields)
            kind = "error" if name in _ERROR_EVENTS else (
                "halt" if name in _WARNING_EVENTS else "broker"
            )
            self.event_sink(self.clock(), kind, f"{name}: {text}")

    # ------------------------------------------------------------- Bequemlich
    def signal(self, name: str, request: OrderRequest, reason: str = "", error: str = "") -> None:
        """Ein Signal auf seinem Weg - mit allen Feldern aus Spec Paragraph 24."""
        self.event(
            name,
            signal_id=request.signal_id,
            order_key=request.order_key,
            symbol=request.symbol,
            strategy=request.strategy,
            direction=request.side.value,
            quantity=request.quantity,
            order_type=request.kind.value,
            entry=request.limit_price or None,
            stop_loss=request.stop_loss or None,
            take_profit=request.take_profit or None,
            reason=reason,
            error=error,
        )

    def order(self, order: BrokerOrder, reason: str = "") -> None:
        """Eine Zustandsaenderung beim Broker."""
        self.event(
            STATE_EVENTS.get(order.state, ORDER_SUBMITTED),
            order_key=order.order_key,
            symbol=order.symbol,
            direction=order.side.value,
            quantity=order.quantity,
            order_type=order.kind.value,
            order_id=order.order_id,
            parent_order_id=order.parent_order_id,
            status=order.state.value,
            filled_quantity=order.filled_quantity,
            avg_fill_price=order.avg_fill_price or None,
            stop_loss=order.stop_price or None,
            take_profit=order.limit_price or None,
            reason=reason,
            error=order.error,
        )

    def blocked(self, reason_code: str, detail: str = "", **fields: Any) -> None:
        self.event(TRADING_BLOCKED, message=detail or reason_code, reason=reason_code, **fields)


def _summarise(name: str, fields: dict[str, Any]) -> str:
    """Kurztext fuer `system_events`, wenn keiner mitgegeben wurde."""
    parts = [
        f"{key}={value}"
        for key, value in fields.items()
        if value not in (None, "", 0) and key not in {"strategy"}
    ]
    return ", ".join(parts) if parts else name
