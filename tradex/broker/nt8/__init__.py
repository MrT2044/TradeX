"""Orderanbindung ueber die NinjaTrader-Bridge (Phase 9).

Der einzige Ort, an dem das Bridge-Orderprotokoll gesprochen wird - so wie
`tradex/broker/ibkr/` der einzige Ort mit `ibapi` ist. Der Rest des Programms
kennt nur `BrokerInterface` aus `tradex/broker/base.py`.

Warum NinjaTrader statt IBKR
----------------------------
Marktdaten und Ausfuehrung kommen damit aus demselben System. Der Preis dafuer
ist, dass Orders und Kursdaten sich einen Socket teilen - abgesichert durch
eine Befehls-Whitelist ohne Default-Zweig, getrennte Warteschlangen und vor
allem die Kontosperre im AddOn (`Account.Provider == Provider.Simulator`).

Der Paper-Nachweis ist hier STAERKER als bei IBKR: dort blieb er strukturell
indirekt (Port + `DU`-Praefix + Allowlist), weil die TWS-API kein Feld "ist
Paper" kennt. `Provider.Simulator` ist eine Eigenschaft des Kontos.
"""

from tradex.broker.nt8.protocol import ORDER_MESSAGE_TYPES, REJECT_CODES

__all__ = ["ORDER_MESSAGE_TYPES", "REJECT_CODES"]
