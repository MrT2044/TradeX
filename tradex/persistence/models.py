"""Zeilenmodelle fuer die SQLite-Tabellen."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Reason:
    """Ein strukturierter Begruendungsbaustein.

    Bewusst KEIN fertiger Satz: der Backend liefert Code + Parameter, das UI
    uebersetzt (Spec §23). Damit sind Begruendungen testbar und mehrsprachig.
    """

    code: str
    ok: bool
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "ok": self.ok, "params": self.params}


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """Ein Eintrag im Entscheidungsprotokoll (Spec §25).

    In Phase 1+2 wird `decision="ANALYSIS"` geschrieben: es gibt noch keine
    Trades, aber der vollstaendige Analysezustand wird bereits protokolliert,
    damit spaetere Phasen auf echter Historie aufsetzen koennen.
    """

    ts_utc: str
    bar_ts: int
    symbol: str
    timeframe: str
    decision: str
    config_hash: str
    strategy_version: str = "phase2-analysis"
    htf_bias: str | None = None
    liquidity_sweep: bool | None = None
    displacement: bool | None = None
    fvg: bool | None = None
    retracement: bool | None = None
    mss: bool | None = None
    news_risk: str | None = None
    rr: float | None = None
    ai_score: float | None = None
    risk_pct: float | None = None
    reasons: tuple[Reason, ...] = ()
    context: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class SystemEvent:
    ts_utc: str
    level: str
    category: str
    message: str
    payload: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class DataGapRecord:
    symbol: str
    timeframe: str
    gap_start_ts: int
    gap_end_ts: int
    missing_bars: int
    detected_at: str


@dataclass(frozen=True, slots=True)
class StrategyVersion:
    """Spec §21: jede Strategieversion bleibt nachvollziehbar erhalten."""

    version: str
    created_at: str
    description: str
    config_hash: str
    approved_by: str
    backtest_ref: str
    is_active: bool
