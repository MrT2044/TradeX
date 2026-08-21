"""Bar-Datenstrukturen.

`BarSeries` ist spaltenweise (numpy) organisiert, weil die Analyse ueber
Millionen von Bars laufen muss. `Bar` ist das zeilenweise Wertobjekt fuer
Schnittstellen, Logs und Tests.

Zeitkonvention (systemweit):
    Timestamps sind int64 Epoch-**Nanosekunden in UTC** und bezeichnen immer den
    **OEFFNUNGSzeitpunkt** der Bar. Eine 5m-Bar mit ts=09:30:00 deckt
    [09:30:00, 09:35:00) ab. Anzeige in lokaler Zeit passiert erst im UI.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self

import numpy as np

if TYPE_CHECKING:
    import pandas as pd

NS_PER_SECOND = 1_000_000_000

# Spaltennamen in kanonischer Reihenfolge - auch die Parquet-Schema-Reihenfolge.
COLUMNS: tuple[str, ...] = ("ts", "open", "high", "low", "close", "volume", "roll_boundary")


def to_ns(dt: datetime) -> int:
    """datetime -> Epoch-Nanosekunden UTC. Naive datetimes gelten als UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * NS_PER_SECOND)


def from_ns(ts: int) -> datetime:
    """Epoch-Nanosekunden UTC -> timezone-aware datetime."""
    return datetime.fromtimestamp(ts / NS_PER_SECOND, tz=UTC)


@dataclass(frozen=True, slots=True)
class Bar:
    """Eine einzelne, abgeschlossene Kerze."""

    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    roll_boundary: bool = False

    @property
    def datetime(self) -> datetime:
        return from_ns(self.ts)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    def validate(self) -> None:
        """Wirft bei physikalisch unmoeglichen Kerzen.

        Solche Bars deuten immer auf ein Datenproblem hin und duerfen niemals
        stillschweigend in die Analyse laufen (Spec §24).
        """
        if not (self.low <= self.open <= self.high):
            raise ValueError(f"Bar {self.datetime}: open {self.open} ausserhalb [{self.low}, {self.high}]")
        if not (self.low <= self.close <= self.high):
            raise ValueError(f"Bar {self.datetime}: close {self.close} ausserhalb [{self.low}, {self.high}]")
        if self.high < self.low:
            raise ValueError(f"Bar {self.datetime}: high {self.high} < low {self.low}")
        if self.volume < 0:
            raise ValueError(f"Bar {self.datetime}: negatives Volumen {self.volume}")


class BarSeries:
    """Wachsende, spaltenweise Bar-Sammlung mit amortisiert O(1)-Append.

    Die Serie garantiert **streng monoton steigende** Timestamps. Ein Append mit
    gleichem oder kleinerem ts wirft - das faengt doppelt gelieferte oder
    verdrehte Provider-Daten sofort ab, statt sie in die Analyse zu lassen.
    """

    __slots__ = ("_ts", "_open", "_high", "_low", "_close", "_volume", "_roll", "_len")

    _INITIAL_CAPACITY = 1024

    def __init__(self, capacity: int = _INITIAL_CAPACITY) -> None:
        capacity = max(capacity, 1)
        self._ts = np.empty(capacity, dtype=np.int64)
        self._open = np.empty(capacity, dtype=np.float64)
        self._high = np.empty(capacity, dtype=np.float64)
        self._low = np.empty(capacity, dtype=np.float64)
        self._close = np.empty(capacity, dtype=np.float64)
        self._volume = np.empty(capacity, dtype=np.float64)
        self._roll = np.zeros(capacity, dtype=bool)
        self._len = 0

    # ------------------------------------------------------------------ Basis
    def __len__(self) -> int:
        return self._len

    def __bool__(self) -> bool:
        return self._len > 0

    def __getitem__(self, index: int) -> Bar:
        i = self._normalize_index(index)
        return Bar(
            ts=int(self._ts[i]),
            open=float(self._open[i]),
            high=float(self._high[i]),
            low=float(self._low[i]),
            close=float(self._close[i]),
            volume=float(self._volume[i]),
            roll_boundary=bool(self._roll[i]),
        )

    def __iter__(self) -> Iterator[Bar]:
        for i in range(self._len):
            yield self[i]

    def _normalize_index(self, index: int) -> int:
        i = index + self._len if index < 0 else index
        if not 0 <= i < self._len:
            raise IndexError(f"Bar-Index {index} ausserhalb Serie der Laenge {self._len}")
        return i

    # ----------------------------------------------------------- Spaltenzugriff
    # Alle Properties liefern READ-ONLY Views auf den befuellten Bereich.
    # Views statt Kopien, damit Analyse ueber Millionen Bars nicht allokiert.
    def _view(self, array: np.ndarray) -> np.ndarray:
        view = array[: self._len]
        view.flags.writeable = False
        return view

    @property
    def ts(self) -> np.ndarray:
        return self._view(self._ts)

    @property
    def open(self) -> np.ndarray:
        return self._view(self._open)

    @property
    def high(self) -> np.ndarray:
        return self._view(self._high)

    @property
    def low(self) -> np.ndarray:
        return self._view(self._low)

    @property
    def close(self) -> np.ndarray:
        return self._view(self._close)

    @property
    def volume(self) -> np.ndarray:
        return self._view(self._volume)

    @property
    def roll_boundary(self) -> np.ndarray:
        return self._view(self._roll)

    # ----------------------------------------------------------------- Append
    def append(
        self,
        ts: int,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: float,
        roll_boundary: bool = False,
    ) -> int:
        """Haengt eine Bar an und liefert deren Index."""
        if self._len and ts <= int(self._ts[self._len - 1]):
            previous = from_ns(int(self._ts[self._len - 1]))
            raise ValueError(
                f"Timestamps muessen streng steigen: {from_ns(ts)} folgt auf {previous}"
            )
        if self._len == self._ts.shape[0]:
            self._grow()
        i = self._len
        self._ts[i] = ts
        self._open[i] = open_
        self._high[i] = high
        self._low[i] = low
        self._close[i] = close
        self._volume[i] = volume
        self._roll[i] = roll_boundary
        self._len += 1
        return i

    def append_bar(self, bar: Bar) -> int:
        return self.append(
            bar.ts, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.roll_boundary
        )

    def _grow(self) -> None:
        new_capacity = self._ts.shape[0] * 2
        self._ts = np.resize(self._ts, new_capacity)
        self._open = np.resize(self._open, new_capacity)
        self._high = np.resize(self._high, new_capacity)
        self._low = np.resize(self._low, new_capacity)
        self._close = np.resize(self._close, new_capacity)
        self._volume = np.resize(self._volume, new_capacity)
        roll = np.zeros(new_capacity, dtype=bool)
        roll[: self._len] = self._roll[: self._len]
        self._roll = roll

    # -------------------------------------------------------------- Konversion
    @classmethod
    def from_arrays(
        cls,
        ts: np.ndarray,
        open_: np.ndarray,
        high: np.ndarray,
        low: np.ndarray,
        close: np.ndarray,
        volume: np.ndarray,
        roll_boundary: np.ndarray | None = None,
    ) -> Self:
        n = len(ts)
        lengths = {len(open_), len(high), len(low), len(close), len(volume)}
        if lengths != {n}:
            raise ValueError("Alle Spalten muessen dieselbe Laenge haben")
        series = cls(capacity=max(n, 1))
        series._ts[:n] = np.asarray(ts, dtype=np.int64)
        series._open[:n] = np.asarray(open_, dtype=np.float64)
        series._high[:n] = np.asarray(high, dtype=np.float64)
        series._low[:n] = np.asarray(low, dtype=np.float64)
        series._close[:n] = np.asarray(close, dtype=np.float64)
        series._volume[:n] = np.asarray(volume, dtype=np.float64)
        if roll_boundary is not None:
            series._roll[:n] = np.asarray(roll_boundary, dtype=bool)
        series._len = n
        if n > 1 and not np.all(np.diff(series._ts[:n]) > 0):
            raise ValueError("Timestamps sind nicht streng monoton steigend")
        return series

    @classmethod
    def from_dataframe(cls, df: pd.DataFrame) -> Self:
        missing = [c for c in ("ts", "open", "high", "low", "close", "volume") if c not in df.columns]
        if missing:
            raise ValueError(f"DataFrame fehlen Spalten: {missing}")
        roll = df["roll_boundary"].to_numpy() if "roll_boundary" in df.columns else None
        return cls.from_arrays(
            df["ts"].to_numpy(),
            df["open"].to_numpy(),
            df["high"].to_numpy(),
            df["low"].to_numpy(),
            df["close"].to_numpy(),
            df["volume"].to_numpy(),
            roll,
        )

    def to_dataframe(self) -> pd.DataFrame:
        import pandas as pd

        return pd.DataFrame(
            {
                "ts": self.ts.copy(),
                "open": self.open.copy(),
                "high": self.high.copy(),
                "low": self.low.copy(),
                "close": self.close.copy(),
                "volume": self.volume.copy(),
                "roll_boundary": self.roll_boundary.copy(),
            }
        )

    # ------------------------------------------------------------------ Utils
    def last(self) -> Bar:
        if not self._len:
            raise IndexError("BarSeries ist leer")
        return self[self._len - 1]

    def index_of_ts(self, ts: int) -> int:
        """Index einer Bar mit exakt diesem ts, sonst -1."""
        i = int(np.searchsorted(self.ts, ts))
        if i < self._len and int(self._ts[i]) == ts:
            return i
        return -1

    def __repr__(self) -> str:
        if not self._len:
            return "BarSeries(leer)"
        return (
            f"BarSeries({self._len} Bars, "
            f"{from_ns(int(self._ts[0])):%Y-%m-%d %H:%M} .. "
            f"{from_ns(int(self._ts[self._len - 1])):%Y-%m-%d %H:%M} UTC)"
        )
