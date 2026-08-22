"""Einstiegssignal und Entscheidungsprotokoll-Eintrag der Strategie."""

from __future__ import annotations

from dataclasses import dataclass

from tradex.domain.enums import Direction
from tradex.persistence.models import Reason


@dataclass(frozen=True, slots=True)
class TradeSignal:
    """Ein vollstaendig durchgerechneter Einstieg.

    Enthaelt alles, was zur Ausfuehrung noetig waere. In Phase 3 wird nichts
    ausgefuehrt - das Signal wird berechnet, protokolliert und angezeigt.
    """

    trade_id: int
    """Kontoweit eindeutige Kennung, vergeben vom Portfolio.

    `setup_id` zaehlt JE STRATEGIE - Kette #5 und Opening-Range #5 sind zwei
    verschiedene Setups mit derselben Nummer. Als Schluessel im Risikobuch
    waere das eine Verwechslung mit Ansage: die eine Position wuerde die andere
    schliessen. Deshalb eine eigene, durchlaufende Nummer.
    """
    setup_id: int
    symbol: str
    direction: Direction
    entry: float
    stop: float
    target: float
    stop_ticks: float
    target_points: float
    rr: float
    quantity: int
    risk_amount: float
    reward_amount: float
    entry_ts: int
    entry_index: int
    stop_anchor: str
    target_source: str
    timeframe: str = "1m"
    """Zeitebene der Signalbar.

    Wird gebraucht, um zu beurteilen, ob ein Signal noch aktuell ist: eine
    5m-Bar gilt erst fuenf Minuten nach ihrer Eroeffnung als geschlossen. In
    Basis-Bars gerechnet waere sie damit immer "veraltet"."""
    strategy: str = "ict_chain"
    """Welche Strategie den Vorschlag gemacht hat.

    Ab mehreren Strategien am selben Konto ist das die wichtigste Spalte der
    Auswertung: ohne sie liesse sich nicht sagen, welche davon das Ergebnis
    getragen hat und welche nur Gebuehren produziert."""

    @property
    def side(self) -> str:
        return "LONG" if self.direction is Direction.BULLISH else "SHORT"


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    """Eine protokollierte Entscheidung - mit oder ohne Trade (Spec §25).

    Es werden bewusst NICHT alle Bars protokolliert, sondern die Zeitpunkte, an
    denen tatsaechlich etwas entschieden wurde:

        - ein Setup wurde vollstaendig und wurde angenommen oder abgelehnt
        - ein Setup ist gestorben (ungueltig geworden oder verfallen)

    Damit bleibt die Frage "warum wurde dieser Trade nicht gemacht?"
    beantwortbar, ohne das Protokoll mit Millionen ereignisloser Bars zu fluten.
    """

    ts: int
    index: int
    symbol: str
    timeframe: str
    setup_id: int
    direction: Direction
    decision: str
    """LONG | SHORT | NO_TRADE"""
    stage: str
    checklist: dict[str, bool]
    reasons: tuple[Reason, ...]
    signal: TradeSignal | None = None
    htf_bias: str = "neutral"
    strategy: str = "ict_chain"

    @property
    def is_trade(self) -> bool:
        return self.decision in ("LONG", "SHORT")

    @property
    def missing(self) -> list[str]:
        return [name for name, ok in self.checklist.items() if not ok]

    @property
    def blocking_reason(self) -> str:
        """Erster nicht erfuellter Grund - der eigentliche Ablehnungsgrund."""
        for reason in self.reasons:
            if not reason.ok:
                return reason.code
        return ""
