"""Strategieschicht (Phase 3, Spec §6-§13).

Verkettet die Detektoren aus `tradex.analysis` zur Pflichtkette, bestimmt Stop
und Ziel und faellt ein Urteil. Sie erzeugt keine eigenen Marktaussagen - jede
Bedingung stammt aus der Analyse-Engine.
"""

from tradex.strategy.engine import StrategyEngine
from tradex.strategy.setup import SetupCandidate, SetupStage, StageChange
from tradex.strategy.signal import StrategyDecision, TradeSignal
from tradex.strategy.stops import StopResult, place_stop
from tradex.strategy.targets import TargetResult, place_target

__all__ = [
    "SetupCandidate",
    "SetupStage",
    "StageChange",
    "StopResult",
    "StrategyDecision",
    "StrategyEngine",
    "TargetResult",
    "TradeSignal",
    "place_stop",
    "place_target",
]
