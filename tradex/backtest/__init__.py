"""Backtesting und Statistik (Phase 4, Spec §19).

Aufgabenteilung:

    execution.py  wie ein Signal gefuellt und beendet worden waere
    runner.py     der Lauf ueber eine Bar-Serie - benutzt denselben
                  Analysepfad wie Replay und spaeter Live
    metrics.py    reine Kennzahlen ueber Trade-Mengen
    significance  t-Test, Vertrauensband, Mehrfachtest-Korrektur - reine Mathematik
    patterns.py   bedingte Verteilungen: haelt von den Aufschluesselungen etwas stand?
    report.py     Buendelung, Einordnung, Ausgabe
    store.py      Laeufe dauerhaft festhalten (Schema: persistence/db.py)

Was hier NICHT passiert: Parameteroptimierung. Ein Suchlauf ueber
Schwellenwerte findet zuverlaessig eine Kombination, die auf der Vergangenheit
gut aussieht - und sagt nichts ueber die Zukunft. Der Backtest beantwortet eine
Ja/Nein-Frage zu EINER festgelegten Regelfassung.

Auch `patterns.py` ist keine Suche: es prueft eine feste, vorher festgelegte
Liste von Bedingungen und korrigiert gegen deren Anzahl. Der Unterschied ist
genau diese Anzahl - eine Suche kennt sie nicht.
"""

from __future__ import annotations

from tradex.backtest.execution import OpenTrade, SimulatedTrade
from tradex.backtest.metrics import EquityPoint, Metrics, summarize
from tradex.backtest.patterns import Cell, PatternReport
from tradex.backtest.report import BacktestReport, build, render_text, to_dict
from tradex.backtest.runner import BACKTEST_VERSION, Backtester, BacktestResult, run_backtest
from tradex.backtest.store import BacktestStore

__all__ = [
    "BACKTEST_VERSION",
    "BacktestReport",
    "BacktestResult",
    "BacktestStore",
    "Backtester",
    "Cell",
    "EquityPoint",
    "Metrics",
    "OpenTrade",
    "PatternReport",
    "SimulatedTrade",
    "build",
    "render_text",
    "run_backtest",
    "summarize",
    "to_dict",
]
