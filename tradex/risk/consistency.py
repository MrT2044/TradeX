"""Konsistenzpruefung zwischen Risiko-, Stop- und Instrumenteinstellungen.

Warum das noetig ist
--------------------
Kontogroesse, Risiko pro Trade, Punktwert des Instruments und erlaubte Stopweite
haengen zusammen, stehen aber an drei verschiedenen Stellen. Passen sie nicht
zueinander, laeuft das System einwandfrei - und lehnt trotzdem jedes Setup ab:

    Risikobudget      = Kontogroesse * Risiko% = 25 USD
    Punktwert MNQ     = 2 USD/Punkt
    -> bezahlbarer Stop = 12,5 Punkte = 50 Ticks

Steht `stops.max_stop_ticks` auf 240, erzeugt die Strategie munter Setups mit
100 Ticks Stop, die die Risk Engine anschliessend ausnahmslos mit
"Positionsgroesse waere 0" verwirft. Der Nutzer sieht nur, dass nie gehandelt
wird, und sucht den Fehler an der falschen Stelle.

Diese Pruefung macht den Widerspruch sichtbar, statt ihn auszurechnen und zu
verschweigen. Sie aendert AUSDRUECKLICH keine Werte: welche Seite angepasst
wird - kleineres Risiko pro Trade, groesseres Konto, engerer maximaler Stop
oder das kleinere Instrument - ist eine Entscheidung des Nutzers, keine des
Programms.
"""

from __future__ import annotations

from dataclasses import dataclass

from tradex.config import Config
from tradex.domain.instruments import Instrument


@dataclass(frozen=True, slots=True)
class ConsistencyIssue:
    """Ein erkannter Widerspruch in der Konfiguration."""

    code: str
    message: str
    severity: str = "warning"


def affordable_stop_ticks(config: Config, instrument: Instrument) -> float:
    """Groesster Stop in Ticks, den ein einzelner Kontrakt noch tragen kann.

    Darueber waere die berechnete Positionsgroesse null und es entstuende
    grundsaetzlich kein Trade.
    """
    budget = config.risk.risk_per_trade_amount
    per_point = instrument.point_value * instrument.contract_size
    if per_point <= 0:
        return 0.0
    return instrument.to_ticks(budget / per_point)


def check_configuration(config: Config, instrument: Instrument) -> list[ConsistencyIssue]:
    """Alle Widersprueche zwischen Risiko, Stops und Instrument sammeln."""
    issues: list[ConsistencyIssue] = []
    affordable = affordable_stop_ticks(config, instrument)

    if affordable < config.stops.min_stop_ticks:
        issues.append(
            ConsistencyIssue(
                code="risk.stop_unaffordable",
                severity="error",
                message=(
                    f"{instrument.symbol}: Mit {config.risk.risk_per_trade_amount:.2f} "
                    f"{instrument.currency} Risiko pro Trade sind hoechstens "
                    f"{affordable:.0f} Ticks Stop bezahlbar, der kleinste erlaubte Stop "
                    f"({config.stops.min_stop_ticks:.0f} Ticks) ist aber groesser. "
                    "So kann NIE ein Trade entstehen. Abhilfe: groesseres Konto, "
                    "hoeheres Risiko pro Trade oder ein kleineres Instrument."
                ),
            )
        )
    elif config.stops.max_stop_ticks > affordable:
        issues.append(
            ConsistencyIssue(
                code="risk.max_stop_exceeds_budget",
                message=(
                    f"{instrument.symbol}: stops.max_stop_ticks steht auf "
                    f"{config.stops.max_stop_ticks:.0f}, bezahlbar sind mit "
                    f"{config.risk.risk_per_trade_amount:.2f} {instrument.currency} "
                    f"Risiko aber nur {affordable:.0f} Ticks. Setups mit weiterem Stop "
                    "durchlaufen die ganze Kette und werden am Ende mit "
                    "'Positionsgroesse 0' verworfen."
                ),
            )
        )

    if config.targets.mode == "r_multiple" and config.targets.fallback_r_multiple < config.risk.min_rr:
        issues.append(
            ConsistencyIssue(
                code="targets.fallback_below_min_rr",
                severity="error",
                message=(
                    f"targets.fallback_r_multiple ({config.targets.fallback_r_multiple}) liegt "
                    f"unter risk.min_rr ({config.risk.min_rr}). Im Modus 'r_multiple' wird "
                    "damit jedes Ziel abgelehnt."
                ),
            )
        )

    # Opening Range: die Geometrie begrenzt das erreichbare CRV nach oben.
    #
    #   Stop  = Spannenbreite W + Puffer   (Gegenseite der Spanne)
    #   Ziel  = mult * W
    #   CRV   = mult * W / (W + Puffer)  <  mult
    #
    # Ist `target_range_mult` nicht groesser als `min_rr`, kann die Strategie
    # das Mindest-CRV NIE erreichen - sie erzeugt dann Vorschlaege, die
    # ausnahmslos verworfen werden. Genau das ist beim ersten Lauf passiert:
    # 387 Ablehnungen wegen "CRV zu niedrig", null Trades.
    if config.opening_range.enabled and config.opening_range.target_range_mult <= config.risk.min_rr:
        issues.append(
            ConsistencyIssue(
                code="opening_range.rr_unreachable",
                severity="error",
                message=(
                    f"opening_range.target_range_mult ({config.opening_range.target_range_mult}) "
                    f"ist nicht groesser als risk.min_rr ({config.risk.min_rr}). Der Stop liegt "
                    "auf der Gegenseite der Spanne, das CRV bleibt damit rechnerisch immer "
                    "unter dem Vielfachen - die Strategie kann NIE einen Trade erzeugen."
                ),
            )
        )

    if config.risk.max_daily_loss_amount < config.risk.risk_per_trade_amount:
        issues.append(
            ConsistencyIssue(
                code="risk.daily_limit_below_single_trade",
                severity="error",
                message=(
                    f"Das Tagesverlustlimit ({config.risk.max_daily_loss_amount:.2f} "
                    f"{instrument.currency}) ist kleiner als das Risiko eines einzelnen "
                    f"Trades ({config.risk.risk_per_trade_amount:.2f}). Nach dem ersten "
                    "Verlust waere sofort Schluss."
                ),
            )
        )

    return issues
