"""Welche Strategien laufen - an genau einer Stelle.

Neue Strategien werden hier eingetragen und nirgends sonst. Ohne diese eine
Stelle muesste jeder Aufrufer (Dienst, Backtest, Tests) seine eigene Liste
fuehren - und frueher oder spaeter liefe der Backtest mit einer anderen
Zusammenstellung als der Live-Betrieb. Genau das waere ein Verstoss gegen
Spec §29 ("Backtest ≡ Live"), nur eine Ebene hoeher als bisher.

Ein- und ausgeschaltet wird ueber die Konfiguration der jeweiligen Strategie,
nicht durch Auskommentieren.
"""

from __future__ import annotations

from tradex.config import Config
from tradex.domain.instruments import Instrument
from tradex.risk.ledger import RiskLedger
from tradex.strategy.base import Strategy
from tradex.strategy.chain import ChainStrategy
from tradex.strategy.opening_range import OpeningRangeStrategy
from tradex.strategy.portfolio import StrategyPortfolio


def build_strategies(symbol: str, instrument: Instrument, config: Config) -> list[Strategy]:
    """Alle aktiven Strategien fuer ein Instrument."""
    strategies: list[Strategy] = []
    if config.strategy.enabled:
        strategies.append(ChainStrategy(symbol, instrument, config))
    if config.opening_range.enabled:
        strategies.append(OpeningRangeStrategy(symbol, instrument, config))
    return strategies


def build_portfolio(
    symbol: str,
    instrument: Instrument,
    config: Config,
    ledger: RiskLedger | None = None,
) -> StrategyPortfolio:
    """Das komplette Portfolio - der einzige Weg, Strategien zu erzeugen.

    Sind ALLE Strategien abgeschaltet, gaebe es nichts zu entscheiden. Statt
    stillschweigend nichts zu tun, laeuft dann eine leere, aber gueltige
    Registry mit der Kette - abgeschaltet ueber ihre eigene Config. So bleibt
    der Fehler sichtbar, statt sich als "keine Signale" zu tarnen.
    """
    strategies = build_strategies(symbol, instrument, config)
    if not strategies:
        strategies = [ChainStrategy(symbol, instrument, config)]
    return StrategyPortfolio(symbol, instrument, config, strategies, ledger)
