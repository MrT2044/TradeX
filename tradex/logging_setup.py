"""Strukturiertes Logging (Spec §25).

Jede Entscheidung des Systems muss nachvollziehbar sein. Deshalb wird
durchgaengig strukturiert geloggt (Key/Value statt formatierter Saetze), damit
Logs spaeter maschinell nach Setups, Ablehnungsgruenden und Marktphasen
auswertbar sind.

Drei Senken:
    Konsole  - menschenlesbar, waehrend der Entwicklung
    Datei    - JSON Lines, rotierend, vollstaendig
    UI       - ein Ringpuffer, den das Log-Panel ueber die API abholt
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from collections import deque
from pathlib import Path
from typing import Any

import structlog

# Ringpuffer fuer das Log-Panel im UI. Bewusst klein - die vollstaendige
# Historie liegt in der Datei und in SQLite, nicht im Arbeitsspeicher.
_UI_BUFFER: deque[dict[str, Any]] = deque(maxlen=1000)

_configured = False


class _UiBufferProcessor:
    """structlog-Prozessor, der jeden Eintrag zusaetzlich in den UI-Puffer legt."""

    def __call__(
        self, logger: Any, method_name: str, event_dict: structlog.types.EventDict
    ) -> structlog.types.EventDict:
        _UI_BUFFER.append(dict(event_dict))
        return event_dict


def get_ui_log_entries(limit: int = 200) -> list[dict[str, Any]]:
    """Neueste Log-Eintraege fuer das UI-Panel, aelteste zuerst."""
    entries = list(_UI_BUFFER)
    return entries[-limit:]


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    """Logging einmalig konfigurieren. Mehrfachaufrufe sind No-Ops."""
    global _configured
    if _configured:
        return

    numeric_level = getattr(logging, level.upper(), logging.INFO)

    # Diese Kette laeuft sowohl fuer eigene structlog-Ereignisse als auch - je
    # Handler einmal - fuer fremde Logrecords (uvicorn, Bibliotheken).
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    # Der UI-Puffer haengt BEWUSST nur in der structlog-Kette, nicht in der
    # foreign_pre_chain der Handler. Zwei Gruende:
    #   1. Die foreign_pre_chain laeuft einmal PRO HANDLER - dort eingehaengt
    #      landete jeder Eintrag mehrfach im Puffer.
    #   2. So enthaelt das Protokoll im UI nur Ereignisse der Engine und nicht
    #      jede HTTP-Zugriffszeile von uvicorn. Die bleiben in Konsole und Datei.
    structlog.configure(
        processors=[
            *shared_processors,
            _UiBufferProcessor(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(numeric_level)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.dev.ConsoleRenderer(colors=False),
            foreign_pre_chain=shared_processors,
        )
    )
    root.addHandler(console)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "tradex.jsonl",
            maxBytes=20 * 1024 * 1024,
            backupCount=10,
            encoding="utf-8",
        )
        file_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processor=structlog.processors.JSONRenderer(),
                foreign_pre_chain=shared_processors,
            )
        )
        root.addHandler(file_handler)

    # Uvicorns eigene Handler abschalten - alles laeuft ueber die Root-Handler.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv = logging.getLogger(name)
        uv.handlers.clear()
        uv.propagate = True

    _configured = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
