"""FastAPI-Anwendung.

Die Oberflaeche spricht ausschliesslich ueber diese Schnittstelle mit der Engine
(Spec §27). Sie bekommt fertige DTOs und rechnet selbst nichts nach - das ist
die Trennung, die spaeter erlaubt, die Engine headless im Backtest oder als
Dienst laufen zu lassen, ohne irgendetwas am UI zu aendern.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from tradex.api.routes import analysis, market, system
from tradex.api.state import get_service, shutdown_service
from tradex.config import Config, get_config
from tradex.logging_setup import get_logger, setup_logging

log = get_logger(__name__)

UI_DIST = Path(__file__).resolve().parent.parent.parent / "ui" / "dist"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    get_service()
    log.info("api_ready")
    yield
    shutdown_service()
    log.info("api_stopped")


def create_app(config: Config | None = None) -> FastAPI:
    config = config or get_config()
    setup_logging(config.app.log_level, config.path(config.data.log_dir))

    app = FastAPI(
        title="TradeX",
        version="0.1.0",
        description="Regelbasierte Analyse fuer Nasdaq-100 Futures (MNQ/NQ)",
        lifespan=_lifespan,
    )

    app.include_router(system.router, prefix="/api")
    app.include_router(market.router, prefix="/api")
    app.include_router(analysis.router, prefix="/api")

    @app.exception_handler(LookupError)
    async def _lookup_error(_request: object, exc: LookupError) -> JSONResponse:
        # LookupError bedeutet hier immer "so nicht vorhanden" - eine
        # Bediensituation, kein Serverfehler. Das UI zeigt den Text direkt an.
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    if UI_DIST.is_dir():
        app.mount("/assets", StaticFiles(directory=UI_DIST / "assets"), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def _spa(full_path: str) -> FileResponse:
            """Alle uebrigen Pfade an die Single-Page-App ausliefern."""
            candidate = UI_DIST / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(UI_DIST / "index.html")
    else:

        @app.get("/", include_in_schema=False)
        async def _no_ui() -> JSONResponse:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": (
                        "Die Oberflaeche ist noch nicht gebaut. "
                        "Im Ordner ui/ ausfuehren:  npm install && npm run build"
                    )
                },
            )

    return app


app = create_app()
