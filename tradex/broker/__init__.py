"""Orderanbindung (Phase 8, Spec Paragraph 24).

    types.py     brokerunabhaengige DTOs - kein einziger IBKR-Begriff
    base.py      das Protokoll, gegen das der Rest des Programms spricht
    env.py       `.env` als Sperre, niemals als Freischaltung
    guard.py     die Sicherheitskette, reine Funktionen ohne I/O
    journal.py   Protokoll der Orderanbindung (structlog + system_events)
    store.py     `broker_orders` - was hinausging, ueberlebt den Neustart
    manager.py   EINE Instanz je Konto: Duplikatschutz, Ratenlimit, Zustand
    executor.py  `TradeExecutor` mit echten Fills statt simulierten
    ibkr/        der einzige Ort mit `ibapi`-Importen

Der Aufbau folgt derselben Regel wie `tradex/live/`: brokerspezifischer Code
liegt vollstaendig in einem Adapter, alles darueber kennt nur `BrokerInterface`.
Ein zweiter Broker waere ein zweiter Adapter - nicht ein zweiter Datenfluss.

Das Paket ist ohne `ibapi` vollstaendig importierbar. Die Bibliothek kommt aus
dem TWS-API-Installer und nicht von PyPI; wer sie nicht hat, soll trotzdem
Backtests rechnen koennen.
"""

from __future__ import annotations

from tradex.broker.base import BrokerError, BrokerInterface, BrokerNotConnected
from tradex.broker.env import EnvOverrides, read_env
from tradex.broker.executor import BrokerExecutor
from tradex.broker.guard import GateResult, check_configuration, check_port, confirm_paper_account
from tradex.broker.journal import TradeJournal
from tradex.broker.manager import OrderManager, OrderUpdate, build_order_key
from tradex.broker.store import BrokerOrderStore
from tradex.broker.types import (
    AccountInfo,
    BrokerEvent,
    BrokerOrder,
    BrokerPosition,
    Fill,
    OrderKind,
    OrderRequest,
    OrderSide,
    OrderState,
)

__all__ = [
    "AccountInfo",
    "BrokerError",
    "BrokerEvent",
    "BrokerExecutor",
    "BrokerInterface",
    "BrokerNotConnected",
    "BrokerOrder",
    "BrokerOrderStore",
    "BrokerPosition",
    "EnvOverrides",
    "Fill",
    "GateResult",
    "OrderKind",
    "OrderManager",
    "OrderRequest",
    "OrderSide",
    "OrderState",
    "OrderUpdate",
    "TradeJournal",
    "build_order_key",
    "check_configuration",
    "check_port",
    "confirm_paper_account",
    "read_env",
]
