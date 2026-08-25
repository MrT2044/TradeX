"""Gemeinsame Test-Hilfsmittel.

Die Detektoren werden gegen HANDGEBAUTE Bar-Sequenzen geprueft, deren erwartetes
Ergebnis man von Hand nachrechnen kann. Zufallsdaten wuerden zwar Abstuerze
finden, aber nicht beweisen, dass eine Regel das tut, was sie soll.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from tradex.config import (
    Config,
    RiskConfig,
    StopsConfig,
    TradingWindowsConfig,
    load_config,
    load_instruments,
)
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


def trending_market(minutes: int, seed: int = 3) -> BarSeries:
    """Markt mit klarem Trend, Ruecksetzern und gelegentlichen Impulsen.

    Ein Trend ist noetig, damit der HTF-Bias ueberhaupt eine Richtung annimmt -
    ohne ihn entsteht per Spec §7 Schritt 1 gar kein Kandidat. Bewusst mit
    festem Seed: die Daten sind zwar zufaellig erzeugt, aber fuer jeden Lauf
    dieselben, sonst waeren Determinismus-Aussagen nicht pruefbar.
    """
    rng = np.random.default_rng(seed)
    series = BarSeries()
    price = 21000.0
    for i in range(minutes):
        # Laengere Aufwaertsphasen mit eingestreuten Ruecksetzern.
        drift = 0.25 if (i // 900) % 3 != 2 else -0.20
        shock = 9.0 if i % 260 == 0 else 1.6
        close = price + drift + float(rng.normal(0, shock))
        high = max(price, close) + abs(float(rng.normal(0, shock * 0.45)))
        low = min(price, close) - abs(float(rng.normal(0, shock * 0.45)))
        series.append(
            to_ns(DEFAULT_START + timedelta(minutes=i)),
            price,
            high,
            low,
            close,
            float(rng.integers(60, 900)),
        )
        price = close
    return series


def tradeable_config(config: Config, **overrides: object) -> Config:
    """Konfiguration, unter der die Kette auch tatsaechlich zu Trades kommt.

    Die Auslieferungswerte sind bewusst so eng, dass auf den vorliegenden Daten
    fast nichts durchkommt. Fuer den Test des Erfolgsfalls wird das Konto
    vergroessert und der erlaubte Stop an das Budget angeglichen - das sind
    Testbedingungen, keine Strategieaenderung.
    """
    risk = RiskConfig(
        **{
            **config.risk.model_dump(),
            "account_size": 100_000.0,
            "risk_per_trade_pct": 0.5,
            "max_trades_per_day": 50,
            "max_open_positions": 50,
            "min_rr": 1.2,
        }
    )
    stops = StopsConfig(**{**config.stops.model_dump(), "max_stop_atr_mult": 8.0})
    # Die synthetischen Daten haben keine Sessionstruktur - der Sessionfilter
    # wuerde hier nur zufaellig aussortieren. Er wird in test_risk.py geprueft.
    windows = TradingWindowsConfig(**{**config.trading_windows.model_dump(), "enabled": False})
    return Config(
        **{
            **config.model_dump(),
            "risk": risk,
            "stops": stops,
            "trading_windows": windows,
            **overrides,
        }
    )


@pytest.fixture(scope="session")
def config() -> Config:
    return load_config(PROJECT_ROOT / "config" / "default.yaml")


@pytest.fixture(scope="session")
def instruments() -> dict[str, Instrument]:
    return load_instruments(PROJECT_ROOT / "config" / "instruments.yaml")


@pytest.fixture(scope="session")
def mnq(instruments: dict[str, Instrument]) -> Instrument:
    return instruments["MNQ"]
