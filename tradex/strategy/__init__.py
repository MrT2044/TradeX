"""Strategieschicht (Spec §6-§13).

Aufbau seit dem Day-Trading-Umbau:

    base.py           der Vertrag: Strategie schlaegt vor, Portfolio entscheidet
    chain.py          Strategie 1 - die ICT-Pflichtkette (niederfrequent)
    opening_range.py  Strategie 2 - Ausbruch aus der Eroeffnungsspanne
    portfolio.py      mehrere Strategien, EINE Risikopruefung
    registry.py       welche Strategien laufen - an genau einer Stelle

Strategien erzeugen keine eigenen Marktaussagen: jede Bedingung stammt aus der
Analyse-Engine und damit aus demselben Pfad, den Replay, Backtest und spaeter
Live benutzen.
"""

from tradex.strategy.base import Strategy, StrategyOutput, TradeProposal
from tradex.strategy.chain import CHAIN_NAME, ChainStrategy
from tradex.strategy.opening_range import OPENING_RANGE_NAME, OpeningRangeStrategy
from tradex.strategy.portfolio import StrategyPortfolio
from tradex.strategy.registry import build_portfolio, build_strategies
from tradex.strategy.setup import SetupCandidate, SetupStage, StageChange
from tradex.strategy.signal import StrategyDecision, TradeSignal
from tradex.strategy.stops import StopResult, place_stop
from tradex.strategy.targets import TargetResult, place_target

__all__ = [
    "CHAIN_NAME",
    "OPENING_RANGE_NAME",
    "ChainStrategy",
    "OpeningRangeStrategy",
    "SetupCandidate",
    "SetupStage",
    "StageChange",
    "StopResult",
    "Strategy",
    "StrategyDecision",
    "StrategyOutput",
    "StrategyPortfolio",
    "TargetResult",
    "TradeProposal",
    "TradeSignal",
    "build_portfolio",
    "build_strategies",
    "place_stop",
    "place_target",
]
