"""Volatilitaet und Volumen-Normalisierung.

ATR ist der Massstab, an dem Displacement und FVG-Groesse gemessen werden. Ohne
ihn waere "grosse Kerze" eine subjektive Aussage - genau das, was Spec §29
verbietet. 14 Punkte Range sind im NY-Open normal und in der Asia-Session
aussergewoehnlich; erst das Verhaeltnis zur aktuellen Volatilitaet macht die
Aussage vergleichbar.

Jede Groesse existiert zweimal:
    - als Batch-Funktion ueber ein ganzes Array (Referenz, gut testbar)
    - als inkrementelle Klasse fuer den Streaming-Pfad (O(1) pro Bar)
Dass beide identische Werte liefern, prueft `tests/test_volatility.py`.
"""

from __future__ import annotations

from collections import deque

import numpy as np


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """True Range je Bar.

    TR[0] = high[0] - low[0], weil es keinen Vorgaenger-Close gibt.
    """
    high = np.asarray(high, dtype=np.float64)
    low = np.asarray(low, dtype=np.float64)
    close = np.asarray(close, dtype=np.float64)
    if high.size == 0:
        return np.empty(0, dtype=np.float64)

    tr = np.empty(high.size, dtype=np.float64)
    tr[0] = high[0] - low[0]
    if high.size > 1:
        prev_close = close[:-1]
        tr[1:] = np.maximum(
            high[1:] - low[1:],
            np.maximum(np.abs(high[1:] - prev_close), np.abs(low[1:] - prev_close)),
        )
    return tr


def atr(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int, method: str = "wilder"
) -> np.ndarray:
    """Average True Range. Vor dem Ende der Aufwaermphase NaN.

    NaN statt 0 ist Absicht: eine Bedingung wie `range > 1.5 * ATR` waere mit
    ATR=0 immer wahr und wuerde am Serienanfang Geistersignale erzeugen. NaN
    laesst jeden Vergleich zu False werden - der sichere Ausgang.
    """
    if period < 2:
        raise ValueError("ATR-Periode muss >= 2 sein")
    tr = true_range(high, low, close)
    n = tr.size
    result = np.full(n, np.nan, dtype=np.float64)
    if n < period:
        return result

    if method == "sma":
        cumulative = np.cumsum(tr, dtype=np.float64)
        result[period - 1] = cumulative[period - 1] / period
        result[period:] = (cumulative[period:] - cumulative[:-period]) / period
        return result

    if method != "wilder":
        raise ValueError(f"Unbekannte ATR-Methode {method!r}. Gueltig: wilder, sma")

    value = float(tr[:period].mean())
    result[period - 1] = value
    for i in range(period, n):
        value = (value * (period - 1) + tr[i]) / period
        result[i] = value
    return result


def sma(values: np.ndarray, period: int) -> np.ndarray:
    """Einfacher gleitender Durchschnitt, NaN waehrend der Aufwaermphase."""
    values = np.asarray(values, dtype=np.float64)
    n = values.size
    result = np.full(n, np.nan, dtype=np.float64)
    if n < period:
        return result
    cumulative = np.cumsum(values, dtype=np.float64)
    result[period - 1] = cumulative[period - 1] / period
    result[period:] = (cumulative[period:] - cumulative[:-period]) / period
    return result


class RollingAtr:
    """Inkrementeller ATR fuer den Streaming-Pfad."""

    __slots__ = ("period", "method", "_warmup", "_value", "_prev_close", "_count")

    def __init__(self, period: int, method: str = "wilder") -> None:
        if period < 2:
            raise ValueError("ATR-Periode muss >= 2 sein")
        if method not in ("wilder", "sma"):
            raise ValueError(f"Unbekannte ATR-Methode {method!r}")
        self.period = period
        self.method = method
        self._warmup: deque[float] = deque(maxlen=period)
        self._value = float("nan")
        self._prev_close: float | None = None
        self._count = 0

    @property
    def value(self) -> float:
        """Aktueller ATR oder NaN waehrend der Aufwaermphase."""
        return self._value

    @property
    def ready(self) -> bool:
        return self._count >= self.period

    def update(self, high: float, low: float, close: float) -> float:
        if self._prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - self._prev_close), abs(low - self._prev_close))
        self._prev_close = close
        self._count += 1
        self._warmup.append(tr)

        if self._count < self.period:
            return self._value
        if self._count == self.period or self.method == "sma":
            self._value = sum(self._warmup) / self.period
        else:
            self._value = (self._value * (self.period - 1) + tr) / self.period
        return self._value


class RollingSma:
    """Inkrementeller gleitender Durchschnitt (fuer Volumen)."""

    __slots__ = ("period", "_window", "_sum")

    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("SMA-Periode muss >= 1 sein")
        self.period = period
        self._window: deque[float] = deque(maxlen=period)
        self._sum = 0.0

    @property
    def value(self) -> float:
        if len(self._window) < self.period:
            return float("nan")
        return self._sum / self.period

    @property
    def ready(self) -> bool:
        return len(self._window) >= self.period

    def update(self, value: float) -> float:
        if len(self._window) == self.period:
            self._sum -= self._window[0]
        self._window.append(value)
        self._sum += value
        return self.value
