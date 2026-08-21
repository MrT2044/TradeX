"""Deterministische Analyseschicht.

Jeder Detektor ist zustandsbehaftet, aber reproduzierbar: sein Zustand ist eine
reine Funktion aus (bisher gesehene Bars, Parameter). Kein I/O, keine Uhr, keine
Zufallszahlen. Derselbe Input ergibt immer denselben Output - das ist die
Voraussetzung dafuer, dass ein Backtest ueberhaupt etwas aussagt (Spec §29).
"""

from tradex.analysis import reasons
from tradex.analysis.bias import BiasResult, TimeframeBias, combine, evaluate_timeframe
from tradex.analysis.context import (
    ContextSnapshot,
    MarketContext,
    TimeframeSnapshot,
    TimeframeState,
    TimeframeUpdate,
)
from tradex.analysis.displacement import Displacement, DisplacementDetector
from tradex.analysis.fvg import Fvg, FvgTracker
from tradex.analysis.liquidity import LiquidityPool, LiquidityTracker, Sweep
from tradex.analysis.structure import StructureEvent, StructureTracker, SwingLabel
from tradex.analysis.swings import Swing, SwingDetector, detect_swings
from tradex.analysis.volatility import RollingAtr, RollingSma, atr, sma, true_range

__all__ = [
    "BiasResult",
    "ContextSnapshot",
    "Displacement",
    "DisplacementDetector",
    "Fvg",
    "FvgTracker",
    "LiquidityPool",
    "LiquidityTracker",
    "MarketContext",
    "RollingAtr",
    "RollingSma",
    "StructureEvent",
    "StructureTracker",
    "Sweep",
    "Swing",
    "SwingDetector",
    "SwingLabel",
    "TimeframeBias",
    "TimeframeSnapshot",
    "TimeframeState",
    "TimeframeUpdate",
    "atr",
    "combine",
    "detect_swings",
    "evaluate_timeframe",
    "reasons",
    "sma",
    "true_range",
]
