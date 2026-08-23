"""Der laufende Betrieb: beobachten und anhalten (Phase 7, Spec Paragraph 24).

Warum das Starten hier erlaubt ist und das Anhalten wichtiger
-------------------------------------------------------------
Diese Endpunkte starten Papertrading - simulierte Ausfuehrung ohne jede
Broker-Anbindung. Es fliesst kein Geld, und `execution.live_trading_enabled`
aendert daran nichts, weil dieser Pfad keinen Broker kennt.

Der eigentliche Zweck ist der andere: `POST /api/session/halt` ist der Kill
Switch. Er setzt ein Feld in der Risk Engine und wirkt sofort - er wartet auf
keinen Faden und kann deshalb nicht daran scheitern, dass die Sitzung gerade
beschaeftigt ist.

Angehalten heisst NICHT abgeschaltet
------------------------------------
Eine angehaltene Sitzung verarbeitet weiter Bars und fuehrt offene Positionen
zu Ende; sie nimmt nur keine neuen auf. Wer stattdessen den Betrieb hart
beendet, laesst offene Positionen ohne Stopueberwachung zurueck. Deshalb gibt
es `halt` und `stop` getrennt, und deshalb ist `halt` das, was das UI
prominent anbietet.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from tradex.api.schemas import SessionRunDto, SessionStatusDto, SimulatedTradeDto
from tradex.api.state import get_service
from tradex.live.manager import SessionRequest

router = APIRouter(tags=["session"])


class SessionStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbols: list[str]
    feed: str = "replay"
    speed: float = Field(default=3600.0, ge=0)
    start_ts: int | None = None
    end_ts: int | None = None
    notes: str = ""
    save: bool = True
    max_bars: int = Field(default=0, ge=0)


@router.get("/session")
def status() -> SessionStatusDto:
    """Zustand des Betriebs. Aussagefaehig auch dann, wenn nichts laeuft."""
    return SessionStatusDto.of(get_service().sessions.state())


@router.post("/session/start")
def start(request: SessionStartRequest) -> SessionStatusDto:
    manager = get_service().sessions
    return SessionStatusDto.of(
        manager.start(
            SessionRequest(
                symbols=tuple(s.strip().upper() for s in request.symbols if s.strip()),
                feed=request.feed,
                speed=request.speed,
                start_ts=request.start_ts,
                end_ts=request.end_ts,
                notes=request.notes,
                save=request.save,
                max_bars=request.max_bars,
            )
        )
    )


@router.post("/session/halt")
def halt() -> SessionStatusDto:
    """Kill Switch: keine neuen Positionen. Offene laufen zu ihrem Stop."""
    return SessionStatusDto.of(get_service().sessions.halt())


@router.post("/session/resume")
def resume() -> SessionStatusDto:
    return SessionStatusDto.of(get_service().sessions.resume())


@router.post("/session/stop")
def stop() -> SessionStatusDto:
    """Sitzung beenden. Offene Positionen bleiben offen - das ist Absicht."""
    return SessionStatusDto.of(get_service().sessions.stop())


@router.get("/session/trades")
def trades(limit: int = Query(default=100, ge=1, le=1000)) -> list[SimulatedTradeDto]:
    return [SimulatedTradeDto.of(t) for t in get_service().sessions.trades(limit)]


@router.get("/sessions")
def archive(limit: int = Query(default=20, ge=1, le=200)) -> list[SessionRunDto]:
    """Frueher gelaufene Sitzungen. Ohne `ended_utc` = abgestuerzt oder aktiv."""
    return [SessionRunDto(**row) for row in get_service().session_runs(limit)]
