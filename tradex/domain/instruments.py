"""Instrument- und Session-Modelle.

Reine Datenstrukturen ohne I/O. Geladen werden sie von `tradex.config`
aus `config/instruments.yaml`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import time
from zoneinfo import ZoneInfo

from tradex.domain.enums import SessionName


@dataclass(frozen=True, slots=True)
class SessionWindow:
    """Ein benanntes Zeitfenster in Boersenzeit.

    `crosses_midnight` markiert Fenster wie Asia (18:00 -> 02:00), bei denen
    `end` am Folgetag liegt.
    """

    name: SessionName
    start: time
    end: time
    crosses_midnight: bool = False

    def contains(self, t: time) -> bool:
        if self.crosses_midnight:
            return t >= self.start or t < self.end
        return self.start <= t < self.end


@dataclass(frozen=True, slots=True)
class DailyBreak:
    start: time
    end: time

    def contains(self, t: time) -> bool:
        return self.start <= t < self.end


@dataclass(frozen=True, slots=True)
class WeekBoundary:
    weekday: int  # Python-Konvention: Montag=0 ... Sonntag=6
    time: time


@dataclass(frozen=True, slots=True)
class TradingHours:
    """Globex-Handelswoche inklusive taeglicher Wartungspause."""

    week_open: WeekBoundary
    week_close: WeekBoundary
    daily_break: DailyBreak
    daily_reset: time


@dataclass(frozen=True, slots=True)
class Instrument:
    """Vollstaendige Instrumentbeschreibung.

    Preise werden durchgaengig in Indexpunkten gefuehrt. Umrechnung in Ticks
    oder USD passiert ausschliesslich ueber die Methoden dieser Klasse - nirgends
    sonst im Code darf `0.25` oder `2.0` auftauchen.
    """

    symbol: str
    name: str
    exchange: str
    exchange_timezone: str
    currency: str
    tick_size: float
    tick_value: float
    point_value: float
    contract_size: int
    price_decimals: int
    databento_dataset: str
    databento_continuous: str
    contract_months: tuple[str, ...]
    trading_hours: TradingHours
    sessions: tuple[SessionWindow, ...]
    rth: SessionWindow
    _tzinfo: ZoneInfo = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.tick_size <= 0:
            raise ValueError(f"{self.symbol}: tick_size muss > 0 sein")
        if self.point_value <= 0:
            raise ValueError(f"{self.symbol}: point_value muss > 0 sein")
        # Konsistenzpruefung: tick_value muss aus tick_size * point_value folgen.
        expected = self.tick_size * self.point_value
        if not math.isclose(expected, self.tick_value, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError(
                f"{self.symbol}: tick_value {self.tick_value} passt nicht zu "
                f"tick_size {self.tick_size} * point_value {self.point_value} = {expected}"
            )
        object.__setattr__(self, "_tzinfo", ZoneInfo(self.exchange_timezone))

    # ------------------------------------------------------------- Umrechnung
    @property
    def tzinfo(self) -> ZoneInfo:
        return self._tzinfo

    def to_ticks(self, points: float) -> float:
        """Preisdifferenz in Indexpunkten -> Anzahl Ticks."""
        return points / self.tick_size

    def to_points(self, ticks: float) -> float:
        """Anzahl Ticks -> Preisdifferenz in Indexpunkten."""
        return ticks * self.tick_size

    def round_to_tick(self, price: float) -> float:
        """Auf das naechste gueltige Kursraster runden."""
        return round(round(price / self.tick_size) * self.tick_size, self.price_decimals)

    def points_to_currency(self, points: float, quantity: int = 1) -> float:
        """Preisdifferenz in Indexpunkten -> USD fuer `quantity` Kontrakte."""
        return points * self.point_value * quantity * self.contract_size

    # ---------------------------------------------------------------- Session
    def session_at(self, local_time: time) -> SessionName:
        """Session zu einer Boersen-Lokalzeit. CLOSED waehrend der Wartungspause."""
        if self.trading_hours.daily_break.contains(local_time):
            return SessionName.CLOSED
        for window in self.sessions:
            if window.contains(local_time):
                return window.name
        return SessionName.CLOSED

    def is_rth(self, local_time: time) -> bool:
        return self.rth.contains(local_time)
