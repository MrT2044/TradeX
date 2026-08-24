"""Laden von Instrumenten und Ausliefern von Bars."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from tradex.api.schemas import BarsDto, IntegrityDto
from tradex.api.state import get_service
from tradex.domain.enums import Timeframe

router = APIRouter(tags=["market"])


class LoadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    start_ts: int | None = None
    end_ts: int | None = None
    max_bars: int = Field(default=200_000, ge=1, le=400_000)
    feed_all: bool = True
    """False laedt nur, ohne zu analysieren - Ausgangspunkt fuer schrittweisen Replay."""


class LoadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    base_timeframe: str
    base_bars: int
    cursor: int
    progress: float
    integrity: IntegrityDto | None
    warnings: tuple[str, ...]


@router.post("/load")
def load(request: LoadRequest) -> LoadResponse:
    """Basis-Bars aus dem lokalen Speicher laden und analysieren."""
    service = get_service()
    state = service.load(
        request.symbol,
        request.start_ts,
        request.end_ts,
        request.max_bars,
        feed_all=request.feed_all,
    )
    return LoadResponse(
        symbol=state.symbol,
        base_timeframe=service.config.data.base_timeframe.value,
        base_bars=len(state.base),
        cursor=state.cursor,
        progress=state.progress,
        integrity=IntegrityDto.of(state.integrity) if state.integrity else None,
        warnings=tuple(service.warnings()),
    )


class HistoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    days: int = Field(default=0, ge=0, le=30)
    """0 nimmt den Wert aus `live.nt8_history_days`."""
    host: str = ""
    port: int = Field(default=0, ge=0, le=65535)


class HistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    bars: int
    first_ts: int
    last_ts: int
    complete: bool
    """False heisst: der Abruf wurde nicht sauber beendet. Die Bars koennen
    trotzdem brauchbar sein - aber der Betrachter muss den Unterschied sehen."""
    detail: str


@router.post("/history/nt8")
def import_nt8_history(request: HistoryRequest) -> HistoryResponse:
    """Historie aus NinjaTrader nachladen - ohne den Handel scharfzuschalten.

    Fuer MNQ und NQ liegt lokal nichts. Bisher blieb der Chart deshalb leer,
    bis jemand eine Sitzung startete - und das heisst, den Handel zu
    aktivieren. Zwischen "ich will sehen, wo der Kurs steht" und "ab jetzt darf
    gehandelt werden" liegt aber alles.
    """
    service = get_service()
    try:
        result = service.import_nt8_history(
            request.symbol, days=request.days, host=request.host, port=request.port
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return HistoryResponse(
        symbol=result.symbol,
        bars=result.bars,
        first_ts=result.first_ts,
        last_ts=result.last_ts,
        complete=result.complete,
        detail=result.detail,
    )


@router.get("/bars")
def bars(
    symbol: str,
    timeframe: str = Query(default="5m"),
    limit: int = Query(default=1500, ge=1, le=20_000),
) -> BarsDto:
    """Bars eines Timeframes samt der noch laufenden Bar.

    Die laufende Bar wird getrennt uebertragen: sie darf gezeichnet, aber
    niemals ausgewertet werden (Architektur-Invariante 1).
    """
    service = get_service()
    try:
        tf = Timeframe.parse(timeframe)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    series = service.bars(symbol, tf)
    if limit < len(series):
        from tradex.domain.bars import BarSeries

        series = BarSeries.from_arrays(
            ts=series.ts[-limit:],
            open_=series.open[-limit:],
            high=series.high[-limit:],
            low=series.low[-limit:],
            close=series.close[-limit:],
            volume=series.volume[-limit:],
            roll_boundary=series.roll_boundary[-limit:],
        )
    return BarsDto.of(symbol.upper(), tf.value, series, service.forming(symbol, tf))
