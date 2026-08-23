"""Der IBKR-Adapter - der einzige Ort im Projekt, der `ibapi` kennt.

    contracts.py  Symbol -> Kontrakt, aufgeloest ueber `reqContractDetails`
    orders.py     Bracket-Aufbau, Ordernummern, Zustandsabbildung
    adapter.py    Verbindung, Ereignisfaden, `BrokerInterface`

Warum die Grenze hier verlaeuft
-------------------------------
Alles ausserhalb dieses Pakets spricht `BrokerInterface` und die DTOs aus
`tradex/broker/types.py`. Wuerde ein IBKR-Begriff nach draussen sickern -
`permId`, `orderRef`, ein Statusstring -, waere ein zweiter Broker keine zweite
Datei mehr, sondern ein Eingriff in den Betriebscode. Ein Test haelt die Grenze
fest, damit sie nicht mit der Zeit erodiert.

`ibapi` wird ERST beim Zugriff auf `IbkrAdapter` importiert
-----------------------------------------------------------
Die Bibliothek kommt aus dem TWS-API-Installer, nicht von PyPI (siehe
`pyproject.toml`, Extra `ibkr`). `import tradex.broker.ibkr` muss deshalb auch
auf einer Maschine ohne Installation durchlaufen - sonst haengt die gesamte
Testsammlung an einer Datei, die man von Hand herunterladen muss.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tradex.broker.ibkr.contracts import (
    ContractRegistry,
    ContractResolution,
    judge_matches,
)
from tradex.broker.ibkr.orders import (
    OrderIdAllocator,
    OrderPlan,
    build_bracket,
    map_status,
    state_for_error,
)

if TYPE_CHECKING:  # pragma: no cover - nur fuer die Typpruefung
    from tradex.broker.ibkr.adapter import IbkrAdapter

__all__ = [
    "ContractRegistry",
    "ContractResolution",
    "IbkrAdapter",
    "OrderIdAllocator",
    "OrderPlan",
    "build_bracket",
    "judge_matches",
    "map_status",
    "state_for_error",
]


def __getattr__(name: str) -> Any:
    """`IbkrAdapter` nachladen, ohne `ibapi` beim Paketimport zu verlangen."""
    if name == "IbkrAdapter":
        from tradex.broker.ibkr.adapter import IbkrAdapter

        return IbkrAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
