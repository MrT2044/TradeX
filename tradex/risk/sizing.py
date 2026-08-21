"""Positionsgroessenberechnung (Spec §10).

Die Groesse wird IMMER gerechnet, nie gesetzt:

    Risikobetrag       = Kontogroesse * Risiko pro Trade in Prozent
    Risiko je Kontrakt = Stopabstand in Punkten * Punktwert
    Stueckzahl         = abrunden(Risikobetrag / Risiko je Kontrakt)

Abgerundet wird bewusst: aufrunden hiesse, das erlaubte Risiko zu ueberschreiten.

Der wichtige Sonderfall ist `Stueckzahl == 0`. Er bedeutet, dass schon EIN
Kontrakt mehr riskieren wuerde als erlaubt - typisch bei kleinem Konto und
weitem Stop. Dann entsteht kein Trade. Die Alternative waere, den Stop enger zu
setzen, damit es "passt" - das waere eine stillschweigende Regelaenderung und
genau die Art von Selbstbetrug, die Spec §29 ausschliesst.

Bei MNQ (2 USD je Punkt) und 25 USD Risiko sind das 12,5 Punkte Stopabstand
fuer einen Kontrakt. Ein 60-Punkte-Stop braucht also ein groesseres Konto -
oder es gibt keinen Trade.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from tradex.config import RiskConfig
from tradex.domain.instruments import Instrument


@dataclass(frozen=True, slots=True)
class PositionSize:
    """Ergebnis der Groessenberechnung."""

    quantity: int
    risk_amount: float
    """Tatsaechliches Risiko in USD bei dieser Stueckzahl."""
    risk_budget: float
    """Erlaubtes Risiko in USD."""
    risk_per_contract: float
    capped: bool
    """True, wenn `max_position_size` gegriffen hat."""
    ok: bool
    rejection: str = ""


def calculate_position_size(
    stop_distance_points: float,
    instrument: Instrument,
    params: RiskConfig,
) -> PositionSize:
    budget = params.risk_per_trade_amount
    risk_per_contract = instrument.points_to_currency(stop_distance_points)

    if stop_distance_points <= 0 or risk_per_contract <= 0:
        return PositionSize(
            quantity=0,
            risk_amount=0.0,
            risk_budget=budget,
            risk_per_contract=risk_per_contract,
            capped=False,
            ok=False,
            rejection="invalid_stop",
        )

    raw_quantity = math.floor(budget / risk_per_contract)
    if raw_quantity < 1:
        return PositionSize(
            quantity=0,
            risk_amount=0.0,
            risk_budget=budget,
            risk_per_contract=risk_per_contract,
            capped=False,
            ok=False,
            rejection="size_zero",
        )

    quantity = min(raw_quantity, params.max_position_size)
    return PositionSize(
        quantity=quantity,
        risk_amount=risk_per_contract * quantity,
        risk_budget=budget,
        risk_per_contract=risk_per_contract,
        capped=quantity < raw_quantity,
        ok=True,
    )
