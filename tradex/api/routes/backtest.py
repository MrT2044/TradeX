"""Backtest und gespeicherte Laeufe (Spec §19).

Der Lauf ist ABSICHTLICH synchron. Ein Backtest ueber Monate dauert Sekunden
bis Minuten - waehrenddessen einen Fortschrittsbalken zu zeigen waere schoener,
aber ein Hintergrundauftrag mit eigenem Zustand ist eine ganze
Nebenlaeufigkeitsschicht, die das UI hier nicht braucht. Fuer lange Zeitraeume
gibt es `scripts/run_backtest.py`; das UI begrenzt seinen Zeitraum.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from tradex.api.schemas import BacktestReportDto, BacktestRunDto
from tradex.api.state import get_service
from tradex.service import MAX_BACKTEST_BARS

router = APIRouter(tags=["backtest"])


class BacktestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    start_ts: int | None = None
    end_ts: int | None = None
    max_bars: int = Field(default=400_000, ge=1, le=MAX_BACKTEST_BARS)
    save: bool = True
    """Lauf in der Datenbank festhalten - Grundlage fuer Vergleiche (Spec §21)."""


@router.post("/backtest")
def run(request: BacktestRequest) -> BacktestReportDto:
    """Backtest ueber den lokalen Datenbestand rechnen.

    Laesst den geladenen Replay-Zustand des Symbols unberuehrt: das Ergebnis
    darf nicht davon abhaengen, wie weit man im Chart vorgespult hat.
    """
    service = get_service()
    report = service.backtest(
        request.symbol,
        request.start_ts,
        request.end_ts,
        request.max_bars,
        save=request.save,
    )
    return BacktestReportDto.of(report)


@router.get("/backtest")
def last(symbol: str) -> BacktestReportDto:
    """Das Ergebnis des letzten Laufs dieser Sitzung."""
    return BacktestReportDto.of(get_service().last_backtest(symbol))


@router.get("/backtest/runs")
def runs(
    symbol: str | None = None, limit: int = Query(default=20, ge=1, le=200)
) -> list[BacktestRunDto]:
    """Gespeicherte Laeufe - was hat welche Konfiguration wann ergeben?"""
    rows = get_service().backtest_runs(symbol, limit)
    return [BacktestRunDto(**row) for row in rows]  # type: ignore[arg-type]
