"""Die brokerunabhaengige Schnittstelle.

Der Rest des Programms spricht ausschliesslich mit diesem Protokoll. IBKR-
spezifischer Code liegt vollstaendig in `tradex/broker/ibkr/` - ein Test
verbietet `ibapi`-Importe ausserhalb davon, damit die Trennung nicht mit der
Zeit erodiert.

Was hier bewusst NICHT steht: irgendeine Handelsregel. Ein Broker fuehrt aus,
er entscheidet nicht. Position, Groesse und Ausstiegskurse kommen fertig aus
`SymbolBook`; diese Schnittstelle nimmt sie entgegen und meldet zurueck, was
tatsaechlich passiert ist.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tradex.broker.types import (
    AccountInfo,
    BrokerEvent,
    BrokerOrder,
    BrokerPosition,
    OrderRequest,
)


class BrokerError(RuntimeError):
    """Der Broker konnte einen Auftrag nicht ausfuehren."""


class BrokerNotConnected(BrokerError):
    """Kein Kanal zum Broker.

    Eine eigene Ausnahme, weil die Reaktion darauf eine andere ist: nicht
    wiederholen, nicht sammeln, sondern verwerfen. Eine Order, die waehrend
    eines Ausfalls entstand und nach dem Wiederanlauf nachgesendet wird,
    handelt auf einer Marktlage, die es nicht mehr gibt.
    """


@runtime_checkable
class BrokerInterface(Protocol):
    """Was jeder Broker koennen muss."""

    @property
    def name(self) -> str: ...

    # ------------------------------------------------------------- Verbindung
    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def is_connected(self) -> bool: ...

    # ---------------------------------------------------------------- Auskunft
    def get_account_info(self) -> AccountInfo: ...

    def get_positions(self) -> list[BrokerPosition]: ...

    def get_open_orders(self) -> list[BrokerOrder]: ...

    def can_trade(self, symbol: str) -> tuple[bool, str]:
        """Ist der Kontrakt fuer dieses Symbol eindeutig aufgeloest?

        Liefert (ok, Begruendung). Ein Broker, der das nicht beantworten kann,
        antwortet mit False - nicht mit "vermutlich schon".
        """
        ...

    # ----------------------------------------------------------------- Orders
    def place_market_order(self, request: OrderRequest) -> BrokerOrder:
        """Market-Order senden, bei Bedarf mit Stop und Ziel als echte Orders.

        Liefert den Zustand NACH dem Senden - also in aller Regel
        `OrderState.SUBMITTED`. Dass daraus eine Position wird, sagt erst eine
        spaetere Rueckmeldung.
        """
        ...

    def place_limit_order(self, request: OrderRequest) -> BrokerOrder: ...

    def cancel_order(self, order_id: int) -> None: ...

    def cancel_all_orders(self) -> None: ...

    def close_position(self, symbol: str) -> BrokerOrder | None:
        """Eine Position glattstellen. None, wenn es dort keine gibt."""
        ...

    def close_all_positions(self) -> list[BrokerOrder]: ...

    # ---------------------------------------------------------------- Ablauf
    def drain_events(self) -> list[BrokerEvent]:
        """Alle seit dem letzten Aufruf eingegangenen Rueckmeldungen.

        Muss ohne Blockieren zurueckkehren: der Aufrufer ist der Sitzungsfaden,
        und der darf nicht auf den Broker warten - sonst steht bei einer
        Stoerung ausgerechnet die Ueberwachung still.
        """
        ...
