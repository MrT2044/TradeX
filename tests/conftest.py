"""Gemeinsame Test-Hilfsmittel.

Die Detektoren werden gegen HANDGEBAUTE Bar-Sequenzen geprueft, deren erwartetes
Ergebnis man von Hand nachrechnen kann. Zufallsdaten wuerden zwar Abstuerze
finden, aber nicht beweisen, dass eine Regel das tut, was sie soll.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tradex.config import Config, load_config, load_instruments
from tradex.domain.bars import BarSeries, to_ns
from tradex.domain.instruments import Instrument

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Montag, 2025-03-03 23:00 UTC = 17:00 CT = Globex-Wochenstart.
DEFAULT_START = datetime(2025, 3, 3, 23, 0, tzinfo=UTC)


def make_series(
    bars: list[tuple[float, ...]],
    start: datetime = DEFAULT_START,
    step_minutes: int = 1,
    roll_at: set[int] | None = None,
) -> BarSeries:
    """Serie aus (open, high, low, close[, volume])-Tupeln bauen.

    `roll_at` markiert Indizes als Kontrakt-Rollgrenze.
    """
    roll_at = roll_at or set()
    series = BarSeries()
    for i, values in enumerate(bars):
        if len(values) == 4:
            o, h, low, c = values
            volume = 100.0
        elif len(values) == 5:
            o, h, low, c, volume = values
        else:
            raise ValueError(f"Bar {i}: erwartet 4 oder 5 Werte, bekam {len(values)}")
        series.append(
            ts=to_ns(start + timedelta(minutes=step_minutes * i)),
            open_=o,
            high=h,
            low=low,
            close=c,
            volume=volume,
            roll_boundary=i in roll_at,
        )
    return series


def flat_bars(count: int, price: float = 21000.0, span: float = 2.0) -> list[tuple[float, ...]]:
    """Ruhige Fuellbars - um Aufwaermphasen zu ueberbruecken, ohne Signale zu erzeugen.

    Bewusst mit winzigem Body und konstanter Range: erzeugt weder Displacement
    noch FVG noch Swings mit Aussagekraft.
    """
    return [(price, price + span, price - span, price) for _ in range(count)]


@pytest.fixture(scope="session")
def config() -> Config:
    return load_config(PROJECT_ROOT / "config" / "default.yaml")


@pytest.fixture(scope="session")
def instruments() -> dict[str, Instrument]:
    return load_instruments(PROJECT_ROOT / "config" / "instruments.yaml")


@pytest.fixture(scope="session")
def mnq(instruments: dict[str, Instrument]) -> Instrument:
    return instruments["MNQ"]
