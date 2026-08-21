"""Prozessweiter Zugriff auf den Anwendungsdienst.

Bewusst ein einfaches Singleton statt eines Dependency-Injection-Rahmens: die
Anwendung ist ein Einzelplatz-Desktop-Programm mit genau einer Engine-Instanz.
"""

from __future__ import annotations

from tradex.config import get_config
from tradex.service import TradexService

_service: TradexService | None = None


def get_service() -> TradexService:
    global _service
    if _service is None:
        _service = TradexService(get_config())
    return _service


def shutdown_service() -> None:
    global _service
    if _service is not None:
        _service.close()
        _service = None
