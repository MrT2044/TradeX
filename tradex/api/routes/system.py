"""Systemzustand, Instrumente, Datenbestand, Logs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from tradex.api.schemas import (
    CoverageDto,
    HealthDto,
    InstrumentDto,
    LogEntryDto,
    ProviderDto,
)
from tradex.api.state import get_service
from tradex.logging_setup import get_ui_log_entries
from tradex.service import STRATEGY_VERSION

router = APIRouter(tags=["system"])


@router.get("/health")
def health() -> HealthDto:
    """System Health fuer das Dashboard (Spec §22, §24)."""
    service = get_service()
    providers = tuple(
        ProviderDto.of(name, provider.health, provider.capabilities())
        for name, provider in sorted(service.providers.all().items())
    )
    warnings = tuple(service.warnings())
    return HealthDto(
        ok=not any("Datenqualitaet" in w for w in warnings),
        mode=service.mode().value,
        live_trading_enabled=service.config.execution.live_trading_enabled,
        symbol=service.config.data.default_symbol,
        config_hash=service.config_hash,
        # Dieselbe Kennung, die in jedem Protokolleintrag und jedem
        # gespeicherten Backtest-Lauf steht. Stuende hier ein anderer Wert,
        # zeigte das Dashboard eine Regelfassung an, nach der nie entschieden
        # wurde (Spec §21).
        strategy_version=STRATEGY_VERSION,
        # Aus der GELADENEN Konfiguration, nicht aus einer Konstante: sonst
        # zeigte die Oberflaeche eine Zeitzone an, nach der niemand rechnet.
        display_timezone=service.config.app.display_timezone,
        providers=providers,
        warnings=warnings,
    )


@router.get("/instruments")
def instruments() -> list[InstrumentDto]:
    return [InstrumentDto.of(item) for item in get_service().instruments().values()]


@router.get("/coverage")
def coverage() -> list[CoverageDto]:
    """Welche Daten lokal vorliegen - das UI baut daraus die Symbolauswahl."""
    return [CoverageDto.of(item) for item in get_service().coverage()]


@router.get("/logs")
def logs(limit: int = 200) -> list[LogEntryDto]:
    entries: list[LogEntryDto] = []
    for raw in get_ui_log_entries(limit):
        data: dict[str, Any] = dict(raw)
        entries.append(
            LogEntryDto(
                timestamp=str(data.pop("timestamp", "")),
                level=str(data.pop("level", "info")),
                event=str(data.pop("event", "")),
                fields={k: v for k, v in data.items() if isinstance(v, str | int | float | bool)},
            )
        )
    return entries
