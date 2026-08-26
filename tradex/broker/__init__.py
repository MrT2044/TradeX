"""Orderanbindung (Phase 9, Spec Paragraph 24).

    types.py     brokerunabhaengige DTOs - kein einziger NinjaTrader-Begriff
    base.py      das Protokoll, gegen das der Rest des Programms spricht
    env.py       `.env` als Sperre, niemals als Freischaltung
    guard.py     die Sicherheitskette, reine Funktionen ohne I/O
    journal.py   Protokoll der Orderanbindung (structlog + system_events)
    store.py     `broker_orders` - was hinausging, ueberlebt den Neustart
    manager.py   EINE Instanz je Konto: Duplikatschutz, Ratenlimit, Zustand
    executor.py  `TradeExecutor` mit echten Fills statt simulierten
    nt8/         der einzige Ort mit dem Bridge-Orderprotokoll

Der Aufbau folgt derselben Regel wie `tradex/live/`: brokerspezifischer Code
liegt vollstaendig in einem Adapter, alles darueber kennt nur `BrokerInterface`.
Ein zweiter Broker waere ein zweiter Adapter - nicht ein zweiter Datenfluss.
Die IBKR-Anbindung war genau so einer und ist in Phase 9 entfernt worden,
nachdem der NinjaTrader-Weg nachweislich trug; dass das ohne Aenderung an
`base.py`, `manager.py` oder `executor.py` ging, war die Probe auf die
Trennung.

Das Paket hat keine optionalen Abhaengigkeiten mehr: `nt8/` spricht ein
zeilenweises JSON-Protokoll ueber einen Socket, mehr braucht es nicht. Die
Zeit von `ibapi` aus dem TWS-Installer ist vorbei.
"""

from __future__ import annotations

from tradex.broker.base import BrokerError, BrokerInterface, BrokerNotConnected
from tradex.broker.env import EnvOverrides, read_env
from tradex.broker.executor import BrokerExecutor
from tradex.broker.guard import (
    GateResult,
    check_configuration,
    check_simulated_account,
    confirm_simulated_account,
)
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
    "check_simulated_account",
    "confirm_simulated_account",
    "read_env",
]
