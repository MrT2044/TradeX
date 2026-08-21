"""Ereignisse, die zwischen Datenschicht, Analyse und API fliessen.

ARCHITEKTUR-INVARIANTE 1 (Spec §29): Analysiert wird ausschliesslich auf
`BarClosed`. Die laufende Bar wird als `BarForming` nur zur Anzeige
weitergereicht und erreicht niemals einen Detektor.
"""

from __future__ import annotations

from dataclasses import dataclass

from tradex.domain.bars import Bar
from tradex.domain.enums import ProviderStatus, Timeframe


@dataclass(frozen=True, slots=True)
class BarClosed:
    """Eine Bar ist endgueltig abgeschlossen. Einziger Analyse-Ausloeser."""

    symbol: str
    timeframe: Timeframe
    bar: Bar
    index: int
    """Index der Bar in der zugehoerigen BarSeries - macht Logs reproduzierbar."""


@dataclass(frozen=True, slots=True)
class BarForming:
    """Zwischenstand der noch laufenden Bar. Nur fuer die Chart-Anzeige."""

    symbol: str
    timeframe: Timeframe
    bar: Bar


@dataclass(frozen=True, slots=True)
class ProviderStatusChanged:
    provider: str
    status: ProviderStatus
    detail: str = ""


@dataclass(frozen=True, slots=True)
class DataGapDetected:
    """Fehlende Bars zwischen zwei aufeinanderfolgenden Datenpunkten.

    Wird persistiert (Tabelle `data_gaps`), weil Luecken sowohl Backtest-
    Ergebnisse als auch Live-Entscheidungen verfaelschen koennen (Spec §24).
    """

    symbol: str
    timeframe: Timeframe
    gap_start_ts: int
    gap_end_ts: int
    missing_bars: int
