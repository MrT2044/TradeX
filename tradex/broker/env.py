"""Umgebungsvariablen als Sperre - niemals als Freischaltung.

Warum diese Richtungsvorgabe
----------------------------
Die Konfiguration dieses Projekts steht in `config/default.yaml` und wird ueber
`config_hash` an jeden gespeicherten Lauf geheftet (Invariante 4). Eine zweite
Quelle, die dieselben Schalter umstellen kann, wuerde diese Etikettierung
entwerten: derselbe Lauf haette je nach Datei im Arbeitsverzeichnis eine andere
Bedeutung.

Ganz weglassen laesst sich die Umgebung trotzdem nicht - ein Not-Aus muss ohne
Editor erreichbar sein. Der Ausweg ist die Richtung: **`.env` darf nur
verbieten.** `TRADING_ENABLED=false` wirkt sofort; `LIVE_TRADING_ENABLED=true`
schaltet nichts frei.

Widersprueche werden nicht aufgeloest, sondern blockiert. Wer `.env` und YAML
gegeneinander stellt, hat zwei verschiedene Absichten formuliert - und dann ist
Nichthandeln die einzige Antwort, die keine davon falsch rat.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

from tradex.config import PROJECT_ROOT, BrokerConfig, ExecutionConfig
from tradex.domain.enums import TradingMode

#: Variablen, die ueberhaupt gelesen werden. Alles andere in `.env` geht die
#: Orderanbindung nichts an.
KNOWN_VARIABLES: tuple[str, ...] = (
    "TRADING_MODE",
    "LIVE_TRADING_ENABLED",
    "TRADING_ENABLED",
    "IBKR_HOST",
    "IBKR_PAPER_PORT",
    "IBKR_LIVE_PORT",
    "IBKR_CLIENT_ID",
)

_TRUE = {"1", "true", "yes", "on", "ja"}
_FALSE = {"0", "false", "no", "off", "nein"}


def _as_bool(raw: str) -> bool | None:
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return None


@dataclass(slots=True)
class EnvOverrides:
    """Was die Umgebung sagt - und ob es der Konfiguration widerspricht."""

    values: dict[str, str] = field(default_factory=dict)
    trading_enabled: bool = True
    """`TRADING_ENABLED=false` ist der Not-Aus aus der Umgebung."""
    conflicts: tuple[str, ...] = ()
    """Widersprueche zur YAML. Nicht leer heisst: nicht handeln."""

    @property
    def blocks_trading(self) -> bool:
        return not self.trading_enabled or bool(self.conflicts)

    @property
    def source(self) -> str:
        if not self.trading_enabled:
            return "TRADING_ENABLED=false"
        return "; ".join(self.conflicts)


def read_env(
    execution: ExecutionConfig,
    broker: BrokerConfig,
    path: Path | None = None,
    environ: dict[str, str] | None = None,
) -> EnvOverrides:
    """`.env` und Prozessumgebung gegen die Konfiguration pruefen.

    Echte Prozessvariablen schlagen die Datei - sonst liesse sich ein Not-Aus
    nicht von aussen setzen, solange eine `.env` daneben liegt.
    """
    dotenv_path = path if path is not None else PROJECT_ROOT / ".env"
    values: dict[str, str] = {}
    if dotenv_path.exists():
        values.update(
            {
                key: value
                for key, value in dotenv_values(dotenv_path).items()
                if key in KNOWN_VARIABLES and value is not None
            }
        )
    source = os.environ if environ is None else environ
    for key in KNOWN_VARIABLES:
        found = source.get(key)
        if found is not None:
            values[key] = found

    conflicts: list[str] = []
    trading_enabled = True

    raw_enabled = values.get("TRADING_ENABLED")
    if raw_enabled is not None:
        parsed = _as_bool(raw_enabled)
        if parsed is None:
            conflicts.append(f"TRADING_ENABLED={raw_enabled!r} ist weder wahr noch falsch")
        elif not parsed:
            trading_enabled = False

    raw_mode = values.get("TRADING_MODE")
    if raw_mode is not None:
        mode = raw_mode.strip().upper()
        if mode == "LIVE":
            # Die Umgebung kann Live nicht freischalten - aber sie soll auch
            # nicht stillschweigend ignoriert werden. Wer das setzt, meint es
            # ernst, und genau deshalb wird hier angehalten.
            conflicts.append(
                "TRADING_MODE=LIVE - die Umgebung kann Live-Trading nicht freischalten "
                f"(execution.mode={execution.mode.value})"
            )
        elif mode == "PAPER":
            if execution.mode not in _PAPER_MODES:
                conflicts.append(
                    f"TRADING_MODE=PAPER, aber execution.mode={execution.mode.value}"
                )
        else:
            conflicts.append(f"TRADING_MODE={raw_mode!r} ist unbekannt (PAPER | LIVE)")

    raw_live = values.get("LIVE_TRADING_ENABLED")
    if raw_live is not None:
        parsed = _as_bool(raw_live)
        if parsed is None:
            conflicts.append(f"LIVE_TRADING_ENABLED={raw_live!r} ist weder wahr noch falsch")
        elif parsed and not execution.live_trading_enabled:
            conflicts.append(
                "LIVE_TRADING_ENABLED=true in der Umgebung, aber "
                "execution.live_trading_enabled=false - die Umgebung schaltet nichts frei"
            )

    conflicts.extend(_check_connection(values, broker))

    return EnvOverrides(
        values=values,
        trading_enabled=trading_enabled,
        conflicts=tuple(conflicts),
    )


_PAPER_MODES = frozenset({TradingMode.PAPER_MANUAL, TradingMode.PAPER_AUTO})


def _check_connection(values: dict[str, str], broker: BrokerConfig) -> list[str]:
    """Verbindungsdaten duerfen bestaetigen, aber nicht umlenken.

    Ein abweichender Port in `.env` waere die gefaehrlichste Form der
    Freischaltung: er koennte auf 4001 zeigen, ohne dass irgendein Schalter
    "live" heisst.
    """
    expected: tuple[tuple[str, str], ...] = (
        ("IBKR_HOST", broker.ibkr.host),
        ("IBKR_PAPER_PORT", str(broker.ibkr.paper_port)),
        ("IBKR_LIVE_PORT", str(broker.ibkr.live_port)),
        ("IBKR_CLIENT_ID", str(broker.ibkr.client_id)),
    )
    conflicts: list[str] = []
    for key, configured in expected:
        found = values.get(key)
        if found is not None and found.strip() != configured:
            conflicts.append(
                f"{key}={found.strip()!r} weicht von der Konfiguration ({configured}) ab - "
                "massgeblich ist config/default.yaml"
            )
    return conflicts
