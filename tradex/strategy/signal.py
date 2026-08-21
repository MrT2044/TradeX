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
