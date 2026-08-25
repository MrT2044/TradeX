"""Das Orderprotokoll der NinjaTrader-Bridge - als reine Funktionen.

Warum getrennt vom Adapter
--------------------------
Hier steht kein Socket, kein Faden und keine Uhr. Jede Funktion ist eine
Abbildung von Daten auf Daten und laesst sich ohne laufendes NinjaTrader
pruefen - genau wie `ibkr/orders.py` ohne `ibapi` pruefbar ist. Was hier
falsch ist, faellt in einem Test auf; was im Adapter falsch ist, faellt
fruehestens an der Bridge auf.

Die Gegenstelle ist `bridge_nt8/TradeXBridge.cs`, die Spezifikation
`bridge_nt8/README.md`. Beide Seiten muessen dasselbe Protokoll sprechen -
`tests/test_bridge_contract.py` haelt die C#-Seite fest, dieses Modul die
Python-Seite.

Eine Eigenheit, die man kennen muss
-----------------------------------
NinjaTrader vergibt Order-IDs als **Zeichenketten** (GUIDs), der Rest von
TradeX rechnet mit `int` (`BrokerOrder.order_id`, `cancel_order(order_id)`).
Die Uebersetzung passiert im Adapter, nicht hier: sie braucht Zustand, und
Zustand gehoert nicht in ein Modul, das nur abbildet.
"""

from __future__ import annotations

import json
from typing import Any

from tradex.broker.types import (
    BrokerEvent,
    OrderKind,
    OrderRequest,
    OrderSide,
    OrderState,
    order_ref,
)

# --------------------------------------------------------------- Reason-Codes
#
# Begruendungen sind Codes, keine Saetze (Projektkonvention) - `de.ts`
# uebersetzt sie. Diese Liste ist die Python-Seite von `order_rejected` und
# muss zur Aufzaehlung im README passen.
REJECT_ORDER_KEY_MISSING = "order_key_missing"
REJECT_ACCOUNT_NOT_SIMULATED = "account_not_simulated"
REJECT_INSTRUMENT_UNKNOWN = "instrument_unknown"
REJECT_DUPLICATE_ORDER_KEY = "duplicate_order_key"
REJECT_QUANTITY_INVALID = "quantity_invalid"
REJECT_BRACKET_INVALID = "bracket_invalid"
REJECT_SUBMIT_FAILED = "submit_failed"

#: Was das ADDON ablehnt. Ein Test haelt diese Menge gegen `TradeXBridge.cs` -
#: ein Code, den nur eine der beiden Seiten kennt, ist eine Ablehnung, die
#: niemand uebersetzen kann.
#:
#: `account_unknown` steht bewusst NICHT dabei, obwohl die Spezifikation es
#: einmal vorsah: das AddOn faltet ein unbekanntes Konto in
#: `account_not_simulated`. Das ist die sichere Richtung - ein Konto, das es
#: nicht gibt, ist kein Simulationskonto.
ADDON_REJECT_CODES: frozenset[str] = frozenset(
    {
        REJECT_ORDER_KEY_MISSING,
        REJECT_ACCOUNT_NOT_SIMULATED,
        REJECT_INSTRUMENT_UNKNOWN,
        REJECT_DUPLICATE_ORDER_KEY,
        REJECT_QUANTITY_INVALID,
        REJECT_BRACKET_INVALID,
        REJECT_SUBMIT_FAILED,
    }
)

#: Der Adapter lehnt selbst ab, wenn gar keine Leitung steht - dann kann das
#: AddOn nicht antworten. Kein AddOn-Code, deshalb getrennt gefuehrt.
REJECT_NOT_CONNECTED = "not_connected"

REJECT_CODES: frozenset[str] = ADDON_REJECT_CODES | {REJECT_NOT_CONNECTED}

#: Nachrichtenarten des ORDERWEGS. Der Marktdatenweg (`bar`, `tick`,
#: `heartbeat`, `status`, `history_end`) laeuft ueber dieselbe Leitung, geht
#: aber niemanden hier etwas an - und umgekehrt. `NinjaTraderFeed` benutzt
#: diese Menge, um Orderereignisse zu ueberspringen statt sie als kaputt zu
#: zaehlen: das AddOn sendet sie per Broadcast an JEDEN Client.
ORDER_MESSAGE_TYPES: frozenset[str] = frozenset(
    {"order_update", "execution", "position", "account", "order_rejected"}
)


# ------------------------------------------------------------------ Zustaende
def parse_state(raw: object) -> OrderState:
    """Zustandsname der Bridge -> `OrderState`.

    Das AddOn bildet NinjaTraders `OrderState` bereits auf DIESE Namen ab
    (`MapOrderState` in `TradeXBridge.cs`); hier wird nur noch eingelesen. Eine
    zweite Abbildung waere eine zweite Wahrheit.

    Unbekanntes wird zu `INACTIVE` und nicht zu einer Ausnahme. Der Grund ist
    die Richtung des Irrtums: `INACTIVE` ist endgueltig, TradeX nimmt darauf
    keine Position mehr auf. Wuerde hier geraten - etwa auf `ACCEPTED` -,
    fuehrte das Programm eine Order als lebend, die es vielleicht nicht mehr
    gibt.
    """
    try:
        return OrderState(str(raw))
    except ValueError:
        return OrderState.INACTIVE


# -------------------------------------------------------- Befehle hinaus
def submit_command(request: OrderRequest, account: str) -> dict[str, Any]:
    """`order_submit` aus einem `OrderRequest`.

    Stop und Ziel gehen als ECHTE Orders mit hinaus, nicht als Merkposten:
    sie muessen auch dann wirken, wenn TradeX nicht laeuft. Das AddOn haengt
    sie als OCO-Klammer an den Entry.
    """
    return {
        "type": "order_submit",
        "order_key": request.order_key,
        "symbol": request.symbol.upper(),
        "account": account,
        "side": request.side.value,
        "quantity": int(request.quantity),
        "kind": request.kind.value,
        "limit_price": float(request.limit_price),
        "stop_loss": float(request.stop_loss),
        "take_profit": float(request.take_profit),
    }


def cancel_command(order_key: str) -> dict[str, Any]:
    """Storniert Entry UND beide Klammerorders.

    Eine stornierte Entry-Order, deren Stop stehen bleibt, waere eine Order
    ohne Position - und die stellt beim naechsten Kurs eine Gegenposition auf.
    """
    return {"type": "order_cancel", "order_key": order_key}


def flatten_command(account: str, symbol: str = "") -> dict[str, Any]:
    """Der NOTAUS-Weg. Ohne `symbol`: alles.

    Das AddOn storniert ERST und stellt DANN glatt. Andersherum loeste eine
    noch stehende Klammerorder auf der geschlossenen Position eine
    Gegenposition aus - aus einem Notaus wuerde ein neuer Trade.
    """
    command: dict[str, Any] = {"type": "flatten", "account": account}
    if symbol:
        command["symbol"] = symbol.upper()
    return command


def account_query_command() -> dict[str, Any]:
    return {"type": "account_query"}


def encode(command: dict[str, Any]) -> bytes:
    """Eine Zeile JSON, UTF-8 - das Rahmenformat der Bridge."""
    return (json.dumps(command, separators=(",", ":")) + "\n").encode("utf-8")


# --------------------------------------------------------- Nachrichten herein
def parse_event(message: dict[str, Any]) -> BrokerEvent | None:
    """Eine Bridge-Nachricht -> `BrokerEvent`, oder None.

    None heisst "geht den Orderweg nichts an" (Bars, Ticks, Herzschlag) und
    ist kein Fehler. Der Adapter verwirft solche Nachrichten stillschweigend;
    sie gehoeren dem Feed.

    Der Zeitstempel des AddOns ist Epoch-Nanosekunden; `BrokerEvent.ts_utc`
    ist Text. Umgerechnet wird im Adapter, wo die Uhr steht.
    """
    kind = str(message.get("type", ""))
    if kind not in ORDER_MESSAGE_TYPES:
        return None

    if kind == "order_update":
        return BrokerEvent(
            kind="order",
            order_key=str(message.get("order_key", "")),
            state=parse_state(message.get("state")),
            payload={
                # Die Roh-ID der Bridge ist eine Zeichenkette. Sie wandert als
                # Nutzlast mit; die Uebersetzung auf `int` braucht Zustand und
                # steht deshalb im Adapter.
                "broker_order_id": str(message.get("order_id", "")),
                "filled_quantity": _as_int(message.get("filled_quantity")),
                "avg_fill_price": _as_float(message.get("avg_fill_price")),
                "error": str(message.get("error", "")),
                "ts_ns": _as_int(message.get("ts")),
            },
        )

    if kind == "execution":
        return BrokerEvent(
            kind="fill",
            order_key=str(message.get("order_key", "")),
            payload={
                "exec_id": str(message.get("exec_id", "")),
                "quantity": _as_int(message.get("quantity")),
                "price": _as_float(message.get("price")),
                # 0.0 heisst hier "nicht gemeldet", nicht "keine Gebuehr" -
                # der Unterschied entscheidet, ob `BrokerExecutor` schaetzen
                # muss. Ob gemeldet wurde, sagt `commission_reported`.
                "commission": _as_float(message.get("commission")),
                "commission_reported": "commission" in message,
                "ts_ns": _as_int(message.get("ts")),
            },
        )

    if kind == "position":
        return BrokerEvent(
            kind="position",
            payload={
                "account": str(message.get("account", "")),
                "symbol": str(message.get("symbol", "")).upper(),
                # Vorzeichenbehaftet: negativ = short.
                "quantity": _as_int(message.get("quantity")),
                "avg_price": _as_float(message.get("avg_price")),
                "unrealized_pnl": _as_float(message.get("unrealized_pnl")),
            },
        )

    if kind == "account":
        return BrokerEvent(
            kind="account",
            payload={
                "account": str(message.get("name", "")),
                "provider": str(message.get("provider", "")),
                # DER Paper-Nachweis. Kommt aus `Account.Provider ==
                # Provider.Simulator`, also einer Eigenschaft des KONTOS -
                # keine Namenskonvention wie das `DU`-Praefix bei IBKR.
                "is_simulation": bool(message.get("is_simulation", False)),
                "currency": str(message.get("currency", "")),
                "net_liquidation": _as_float(message.get("net_liquidation")),
                "buying_power": _as_float(message.get("buying_power")),
                "realized_pnl": _as_float(message.get("realized_pnl")),
            },
        )

    code = str(message.get("code", ""))
    return BrokerEvent(
        kind="error",
        order_key=str(message.get("order_key", "")),
        # Eine Ablehnung ist endgueltig: es ging nichts hinaus. Das als
        # `REJECTED` zu fuehren ist keine Auslegung, sondern genau das, was
        # passiert ist.
        state=OrderState.REJECTED,
        payload={
            "code": code,
            "detail": str(message.get("detail", "")),
            # Ein unbekannter Code ist selbst eine Meldung: dann sprechen AddOn
            # und Python verschiedene Fassungen des Protokolls.
            "known_code": code in REJECT_CODES,
        },
    )


def bracket_refs(order_key: str) -> tuple[str, str]:
    """Die Kennungen der beiden Klammerorders - spiegelbildlich zum AddOn."""
    from tradex.broker.types import ROLE_STOP, ROLE_TARGET

    return order_ref(order_key, ROLE_STOP), order_ref(order_key, ROLE_TARGET)


def _as_int(value: object) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def _as_float(value: object) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


def side_of(raw: object) -> OrderSide:
    try:
        return OrderSide(str(raw).upper())
    except ValueError:
        return OrderSide.BUY


def kind_of(raw: object) -> OrderKind:
    try:
        return OrderKind(str(raw).upper())
    except ValueError:
        return OrderKind.MARKET
