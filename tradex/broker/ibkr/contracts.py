"""Vom TradeX-Symbol zum handelbaren IBKR-Kontrakt.

"MNQ" bezeichnet bei IBKR keinen Kontrakt, sondern eine Familie. Erst Typ,
Boerse, Waehrung und Verfallmonat machen daraus etwas Handelbares - und selbst
dann kann die Angabe mehrdeutig sein, etwa wenn dieselbe Wurzel an mehreren
Boersen gefuehrt wird.

Warum nicht geraten wird
------------------------
Genau dieser Fehler ist in diesem Projekt schon einmal passiert: bei
NinjaTrader war das Wurzelsymbol unbrauchbar, weil es auf keinen konkreten
Kontrakt zeigte. Der Unterschied ist, dass ein falsch geratener Future ein
STILLER Fehler ist - die Order geht durch, sie handelt nur etwas anderes als
gedacht, und auffallen tut es in der Abrechnung. Deshalb: **0 oder mehr als 1
Treffer sperren das Symbol**, sie waehlen nicht aus.

Der Grossteil dieses Moduls ist reine Logik ohne `ibapi`. Nur `build_contract`
und `match_from_details` beruehren die Bibliothek - damit die eigentliche
Entscheidung ("darf dieses Symbol gehandelt werden?") ohne TWS-Installation
pruefbar bleibt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from tradex.analysis import reasons as R
from tradex.domain.instruments import IbkrContract

if TYPE_CHECKING:  # pragma: no cover - nur fuer die Typpruefung
    from ibapi.contract import Contract


@dataclass(frozen=True, slots=True)
class ContractMatch:
    """Ein Treffer aus `reqContractDetails`, auf das Noetige eingedampft."""

    con_id: int
    local_symbol: str = ""
    expiry: str = ""
    exchange: str = ""
    multiplier: str = ""
    trading_class: str = ""
    currency: str = ""

    def describe(self) -> str:
        parts = [self.local_symbol or "?", self.exchange, self.currency, self.expiry]
        return " ".join(part for part in parts if part)


@dataclass(frozen=True, slots=True)
class ContractResolution:
    """Das Urteil ueber ein Symbol. `ok=False` heisst: gesperrt, nicht "unklar"."""

    symbol: str
    ok: bool
    detail: str
    reason_code: str = ""
    con_id: int = 0
    local_symbol: str = ""
    matches: int = 0

    def describe(self) -> str:
        state = "eindeutig" if self.ok else "GESPERRT"
        return f"{self.symbol} -> {self.detail} ({self.matches} Treffer, {state})"


def judge_matches(
    symbol: str,
    spec: IbkrContract | None,
    candidates: tuple[ContractMatch, ...],
) -> ContractResolution:
    """Reine Entscheidung: genau ein Treffer, oder gesperrt.

    Bewusst ohne jede Aufloesungsstrategie. Es waere leicht, bei mehreren
    Treffern den naechsten Verfall zu nehmen - und genau das ist der Griff, der
    im Zweifel den falschen Kontrakt handelt. Wer mehrere Treffer bekommt, hat
    die Angabe in `config/instruments.yaml` nicht eng genug gefasst; dann ist
    ein Fehler die richtige Antwort.
    """
    if spec is None:
        return ContractResolution(
            symbol=symbol,
            ok=False,
            detail="kein ibkr-Block in config/instruments.yaml",
            reason_code=R.BROKER_CONTRACT_UNKNOWN,
        )
    if not spec.is_complete:
        return ContractResolution(
            symbol=symbol,
            ok=False,
            detail=(
                f"unvollstaendig: symbol={spec.symbol!r} exchange={spec.exchange!r} "
                f"expiry={spec.expiry!r} local_symbol={spec.local_symbol!r}"
            ),
            reason_code=R.BROKER_CONTRACT_UNKNOWN,
        )
    if not candidates:
        return ContractResolution(
            symbol=symbol,
            ok=False,
            detail=f"kein Treffer bei IBKR fuer {spec.symbol} {spec.exchange} {spec.expiry}",
            reason_code=R.BROKER_CONTRACT_UNKNOWN,
        )
    if len(candidates) > 1:
        gefunden = "; ".join(match.describe() for match in candidates[:5])
        return ContractResolution(
            symbol=symbol,
            ok=False,
            detail=f"mehrdeutig: {gefunden}",
            reason_code=R.BROKER_CONTRACT_AMBIGUOUS,
            matches=len(candidates),
        )

    single = candidates[0]
    return ContractResolution(
        symbol=symbol,
        ok=True,
        detail=single.describe(),
        con_id=single.con_id,
        local_symbol=single.local_symbol,
        matches=1,
    )


class ContractRegistry:
    """Was ueber jedes Symbol bekannt ist - und ob es gehandelt werden darf.

    Faellt aus dem Rahmen der uebrigen Zustandshaltung, weil hier bewusst der
    NEGATIVE Zustand der Standard ist: ein Symbol, das nie aufgeloest wurde,
    ist gesperrt. Ein Symbol, das man vergessen hat einzutragen, soll nicht
    versehentlich handelbar sein.
    """

    def __init__(self, required: bool = True) -> None:
        self.required = required
        """`broker.ibkr.require_contract_details`. Aus heisst raten - deshalb
        steht der Wert in der Config und nicht als Konstante hier."""
        self._resolutions: dict[str, ContractResolution] = {}
        self._contracts: dict[str, Any] = {}

    def record(self, resolution: ContractResolution, contract: Any = None) -> None:
        self._resolutions[resolution.symbol.upper()] = resolution
        if contract is not None:
            self._contracts[resolution.symbol.upper()] = contract

    def resolution(self, symbol: str) -> ContractResolution | None:
        return self._resolutions.get(symbol.upper())

    def contract(self, symbol: str) -> Any | None:
        """Der Kontrakt, der an den Broker geht. None heisst: nicht handelbar."""
        return self._contracts.get(symbol.upper())

    def can_trade(self, symbol: str) -> tuple[bool, str]:
        """`BrokerInterface.can_trade` - (ok, Begruendung)."""
        found = self._resolutions.get(symbol.upper())
        if found is None:
            return False, "nicht aufgeloest"
        if not found.ok:
            return False, found.detail
        if self._contracts.get(symbol.upper()) is None:
            return False, "aufgeloest, aber kein Kontrakt gebaut"
        return True, found.detail

    def all(self) -> tuple[ContractResolution, ...]:
        return tuple(self._resolutions.values())

    @property
    def tradeable(self) -> tuple[str, ...]:
        return tuple(sorted(sym for sym, res in self._resolutions.items() if res.ok))


# --------------------------------------------------------------------- ibapi
def build_contract(spec: IbkrContract) -> Contract:
    """Die Anfrage-Form des Kontrakts, direkt aus `instruments.yaml`.

    `local_symbol` schlaegt `expiry`: es bezeichnet genau einen Kontrakt
    ("MNQU6"), waehrend ein Verfallmonat bei manchen Produkten noch mehrere
    zulaesst. Beides gleichzeitig zu senden waere ein Widerspruchsrisiko ohne
    Gewinn.
    """
    from ibapi.contract import Contract

    contract = Contract()
    contract.symbol = spec.symbol
    contract.secType = spec.sec_type
    contract.exchange = spec.exchange
    contract.currency = spec.currency
    if spec.local_symbol:
        contract.localSymbol = spec.local_symbol
    elif spec.expiry:
        contract.lastTradeDateOrContractMonth = spec.expiry
    if spec.multiplier:
        contract.multiplier = spec.multiplier
    if spec.trading_class:
        contract.tradingClass = spec.trading_class
    return contract


def order_contract(match: ContractMatch, exchange: str) -> Contract:
    """Die Order-Form: ueber `conId`, nicht ueber die Beschreibung.

    Nach der Aufloesung ist die Kontraktnummer bekannt, und sie ist eindeutig.
    Weiterhin die Beschreibung zu senden hiesse, IBKR bei jeder Order erneut
    raten zu lassen - mit der Moeglichkeit, dass die Antwort morgen eine andere
    ist als heute.
    """
    from ibapi.contract import Contract

    contract = Contract()
    contract.conId = match.con_id
    contract.exchange = exchange or match.exchange
    return contract


def match_from_details(details: Any) -> ContractMatch:
    """`ibapi.contract.ContractDetails` -> `ContractMatch`."""
    contract = details.contract
    return ContractMatch(
        con_id=int(contract.conId),
        local_symbol=str(contract.localSymbol or ""),
        expiry=str(contract.lastTradeDateOrContractMonth or ""),
        exchange=str(contract.exchange or ""),
        multiplier=str(contract.multiplier or ""),
        trading_class=str(contract.tradingClass or ""),
        currency=str(contract.currency or ""),
    )
