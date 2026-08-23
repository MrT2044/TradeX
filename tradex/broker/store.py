"""Ablage der gesendeten Orders (Tabelle `broker_orders`, Migration 4).

Warum das persistiert wird, obwohl der Broker es auch weiss
-----------------------------------------------------------
Weil beides zusammen erst die Frage beantwortet, die nach einem Absturz
gestellt wird: existiert diese Position, oder war sie nur geplant? Der Broker
kennt seine Orders, aber nicht die Signale dahinter; das Programm kennt seine
Signale, aber nach einem Neustart keine Order-IDs mehr. Die Bruecke ist
`order_key` - er steht hier UND als `orderRef` bei IBKR.

Schichtung: die Persistenzschicht weiss nichts ueber Broker (das Schema in
`tradex/persistence/db.py` ist neutrales SQL), und dieses Modul ist der
einzige Zugriff darauf - dieselbe Aufteilung wie bei `backtest/store.py`.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from tradex.broker.journal import utc_now
from tradex.broker.types import BrokerOrder, OrderRequest, OrderState
from tradex.logging_setup import get_logger
from tradex.persistence.db import connect

log = get_logger(__name__)


class BrokerOrderStore:
    """Schreibender und lesender Zugriff auf `broker_orders`.

    Haelt wie `DecisionLog` eine Verbindung ueber die ganze Lebensdauer und
    serialisiert ueber ein Lock: die Rueckmeldungen des Brokers kommen aus
    einem anderen Thread als die Ordererteilung.
    """

    def __init__(self, database: Path | sqlite3.Connection) -> None:
        self._owns_connection = not isinstance(database, sqlite3.Connection)
        self._conn = (
            connect(database, check_same_thread=False)
            if self._owns_connection
            else database  # type: ignore[assignment]
        )
        self._lock = threading.Lock()

    def close(self) -> None:
        if self._owns_connection:
            with self._lock:
                self._conn.close()

    def __enter__(self) -> BrokerOrderStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------- Schreiben
    def record_submitted(
        self,
        request: OrderRequest,
        order: BrokerOrder,
        *,
        broker: str,
        account: str,
        trading_mode: str,
        session_id: int | None,
        stop_order_id: int = 0,
        target_order_id: int = 0,
    ) -> None:
        """Eine gesendete Order festhalten - SOFORT, nicht am Ende.

        `INSERT OR IGNORE` statt `INSERT`: der `order_key` ist UNIQUE, und ein
        zweiter Versuch mit demselben Schluessel soll nicht abstuerzen, sondern
        wirkungslos bleiben. Das ist der Duplikatschutz auf seiner untersten
        Ebene - falls die Ebene darueber je versagt.
        """
        now = utc_now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO broker_orders (
                    order_key, session_id, broker, account, trading_mode,
                    symbol, strategy, signal_id, side, quantity, order_type,
                    order_id, parent_order_id, stop_order_id, target_order_id,
                    state, filled_quantity, avg_fill_price,
                    planned_entry, stop_loss, take_profit,
                    reason, error, submitted_utc, updated_utc
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    request.order_key,
                    session_id,
                    broker,
                    account,
                    trading_mode,
                    request.symbol,
                    request.strategy,
                    request.signal_id,
                    request.side.value,
                    request.quantity,
                    request.kind.value,
                    order.order_id,
                    order.parent_order_id,
                    stop_order_id,
                    target_order_id,
                    order.state.value,
                    order.filled_quantity,
                    order.avg_fill_price,
                    request.limit_price,
                    request.stop_loss,
                    request.take_profit,
                    "",
                    order.error,
                    now,
                    now,
                ),
            )

    def record_rejected(
        self,
        request: OrderRequest,
        reason_code: str,
        *,
        broker: str,
        account: str,
        trading_mode: str,
        session_id: int | None,
        error: str = "",
    ) -> None:
        """Ein Signal festhalten, aus dem KEINE Order wurde.

        Genauso wichtig wie die gesendeten: "warum hat der Bot heute nichts
        gemacht?" ist ohne diese Zeilen nicht zu beantworten.
        """
        now = utc_now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO broker_orders (
                    order_key, session_id, broker, account, trading_mode,
                    symbol, strategy, signal_id, side, quantity, order_type,
                    state, planned_entry, stop_loss, take_profit,
                    reason, error, submitted_utc, updated_utc
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    request.order_key,
                    session_id,
                    broker,
                    account,
                    trading_mode,
                    request.symbol,
                    request.strategy,
                    request.signal_id,
                    request.side.value,
                    request.quantity,
                    request.kind.value,
                    OrderState.REJECTED.value,
                    request.limit_price,
                    request.stop_loss,
                    request.take_profit,
                    reason_code,
                    error,
                    now,
                    now,
                ),
            )

    def update_state(self, order: BrokerOrder) -> None:
        """Zustandswechsel nachziehen. Massgeblich ist immer der Broker."""
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE broker_orders
                   SET state = ?, filled_quantity = ?, avg_fill_price = ?,
                       order_id = ?, parent_order_id = ?, error = ?, updated_utc = ?
                 WHERE order_key = ?
                """,
                (
                    order.state.value,
                    order.filled_quantity,
                    order.avg_fill_price,
                    order.order_id,
                    order.parent_order_id,
                    order.error,
                    utc_now(),
                    order.order_key,
                ),
            )

    # ---------------------------------------------------------------- Lesen
    def known_keys(self, session_id: int | None = None) -> set[str]:
        """Alle bereits verwendeten `order_key`s.

        Der Duplikatschutz ueber einen Prozessneustart hinweg: eine Order, die
        schon einmal hinausging, geht nicht noch einmal hinaus - auch dann
        nicht, wenn der interne Zaehler wieder bei 1 anfaengt.
        """
        with self._lock:
            if session_id is None:
                rows = self._conn.execute("SELECT order_key FROM broker_orders").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT order_key FROM broker_orders WHERE session_id = ?",
                    (session_id,),
                ).fetchall()
        return {str(row["order_key"]) for row in rows}

    def open_orders(self, session_id: int | None = None) -> list[sqlite3.Row]:
        """Orders, die laut Datenbank noch nicht endgueltig sind.

        Nach einem Neustart der Ausgangspunkt fuer den Abgleich mit dem, was
        der Broker tatsaechlich fuehrt.
        """
        terminal = [state.value for state in OrderState if state.is_terminal]
        placeholders = ",".join("?" for _ in terminal)
        query = f"SELECT * FROM broker_orders WHERE state NOT IN ({placeholders})"
        params: list[object] = list(terminal)
        if session_id is not None:
            query += " AND session_id = ?"
            params.append(session_id)
        with self._lock:
            return list(self._conn.execute(query, params).fetchall())
