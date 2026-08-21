"""Analyseergebnisse und Chart-Overlays."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from tradex.api.schemas import (
    ContextSnapshotDto,
    DisplacementDto,
    FvgDto,
    LiquidityPoolDto,
    StructureEventDto,
    SweepDto,
    SwingDto,
)
from tradex.api.state import get_service
from tradex.domain.enums import Timeframe

router = APIRouter(tags=["analysis"])


class OverlaysDto(BaseModel):
    """Alles, was das Chart zusaetzlich zu den Kerzen zeichnet.

    Bewusst eine eigene, vollstaendige Antwort statt der gekuerzten Listen aus
    dem Snapshot: fuer die visuelle Pruefung der Schwellenwerte muss man ALLE
    erkannten Muster des geladenen Zeitraums sehen, auch die bereits erledigten.
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str
    timeframe: str
    swings: tuple[SwingDto, ...]
    fvgs: tuple[FvgDto, ...]
    pools: tuple[LiquidityPoolDto, ...]
    sweeps: tuple[SweepDto, ...]
    structure_events: tuple[StructureEventDto, ...]
    displacements: tuple[DisplacementDto, ...]


class StepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    count: int = Field(default=1, ge=1, le=100_000)


class StepResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    cursor: int
    base_bars: int
    progress: float
    exhausted: bool
    new_swings: int
    new_fvgs: int
    new_sweeps: int
    new_structure_events: int
    new_displacements: int


def _timeframe(raw: str) -> Timeframe:
    try:
        return Timeframe.parse(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/analysis")
def analysis(symbol: str, max_items: int = Query(default=50, ge=1, le=500)) -> ContextSnapshotDto:
    """Gesamtzustand ueber alle Timeframes inklusive HTF-Bias und Begruendungen."""
    return ContextSnapshotDto.of(get_service().snapshot(symbol, max_items))


@router.get("/overlays")
def overlays(symbol: str, timeframe: str = Query(default="5m")) -> OverlaysDto:
    tf = _timeframe(timeframe)
    state = get_service().state(symbol)
    tf_state = state.context.states.get(tf)
    if tf_state is None:
        raise HTTPException(status_code=400, detail=f"Timeframe {tf.value} ist nicht konfiguriert")

    return OverlaysDto(
        symbol=state.symbol,
        timeframe=tf.value,
        swings=tuple(SwingDto.of(s) for s in tf_state.swings.all_swings()),
        fvgs=tuple(FvgDto.of(z) for z in tf_state.fvg.zones),
        pools=tuple(LiquidityPoolDto.of(p) for p in tf_state.liquidity.pools),
        sweeps=tuple(SweepDto.of(s) for s in tf_state.liquidity.sweeps),
        structure_events=tuple(StructureEventDto.of(e) for e in tf_state.structure.events),
        displacements=tuple(DisplacementDto.of(d) for d in tf_state.displacement.found),
    )


@router.post("/step")
def step(request: StepRequest) -> StepResponse:
    """Weitere Basis-Bars analysieren.

    Ruft denselben Pfad auf, den spaeter der Live-Feed benutzt - es gibt keinen
    gesonderten Replay-Code (Architektur-Invariante 3).
    """
    service = get_service()
    updates = service.step(request.symbol, request.count)
    state = service.state(request.symbol)
    return StepResponse(
        symbol=state.symbol,
        cursor=state.cursor,
        base_bars=len(state.base),
        progress=state.progress,
        exhausted=state.exhausted,
        new_swings=sum(len(u.new_swings) for u in updates),
        new_fvgs=sum(len(u.new_fvgs) for u in updates),
        new_sweeps=sum(len(u.sweeps) for u in updates),
        new_structure_events=sum(1 for u in updates if u.structure_event),
        new_displacements=sum(1 for u in updates if u.displacement),
    )


@router.post("/reset")
def reset(symbol: str) -> StepResponse:
    """Analyse zuruecksetzen, geladene Daten behalten."""
    service = get_service()
    state = service.reset(symbol)
    return StepResponse(
        symbol=state.symbol,
        cursor=0,
        base_bars=len(state.base),
        progress=0.0,
        exhausted=False,
        new_swings=0,
        new_fvgs=0,
        new_sweeps=0,
        new_structure_events=0,
        new_displacements=0,
    )


@router.post("/record")
def record(symbol: str) -> dict[str, int | str]:
    """Aktuellen Analysezustand ins Entscheidungsprotokoll schreiben (Spec §25)."""
    service = get_service()
    return {"id": service.record_analysis(symbol), "symbol": symbol.upper()}
