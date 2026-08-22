"""Mehrere Strategien an einem Konto (Spec §10, §24).

Der Punkt dieser Datei ist nicht, dass die Strategien funktionieren - das
pruefen ihre eigenen Tests. Hier geht es um das, was NUR im Zusammenspiel
schiefgehen kann: dass zwei Strategien sich dasselbe Risikobudget teilen,
sich nicht gegenseitig die Positionen ueberschreiben und in reproduzierbarer
Reihenfolge geprueft werden.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from tests.conftest import tradeable_config, trending_market
from tradex.analysis.context import MarketContext
from tradex.backtest.runner import Backtester
from tradex.config import Config, OpeningRangeConfig, RiskConfig, get_instrument
from tradex.domain.bars import BarSeries
from tradex.domain.instruments import Instrument
from tradex.risk.consistency import check_configuration
from tradex.strategy.base import Strategy, StrategyOutput
from tradex.strategy.chain import CHAIN_NAME
from tradex.strategy.opening_range import OPENING_RANGE_NAME
from tradex.strategy.portfolio import StrategyPortfolio
from tradex.strategy.registry import build_portfolio, build_strategies

SYMBOL = "MNQ"


def _run(config: Config, instrument: Instrument, series: BarSeries) -> StrategyPortfolio:
    context = MarketContext(SYMBOL, instrument, config)
    portfolio = build_portfolio(SYMBOL, instrument, config)
    for bar in series:
        updates = context.on_base_bar(bar)
        if updates:
            portfolio.on_updates(updates, context)
    return portfolio


def _with_risk(config: Config, **overrides: object) -> Config:
    return Config(
        **{
            **config.model_dump(),
            "risk": RiskConfig(**{**config.risk.model_dump(), **overrides}),
        }
    )


@pytest.fixture(scope="module")
def series() -> BarSeries:
    return trending_market(60 * 24 * 12)


@pytest.fixture(scope="module")
def portfolio(config: Config, mnq: Instrument, series: BarSeries) -> StrategyPortfolio:
    return _run(tradeable_config(config), mnq, series)


# ------------------------------------------------------------------- Registry
def test_registry_fuehrt_beide_strategien(config: Config, mnq: Instrument):
    names = [s.name for s in build_strategies(SYMBOL, mnq, tradeable_config(config))]
    assert names == [CHAIN_NAME, OPENING_RANGE_NAME]


def test_beide_strategien_liefern_auch_wirklich_etwas(portfolio: StrategyPortfolio):
    """Waechter gegen leere Wahrheit.

    Eine Registry, in der die zweite Strategie nie ausloest, ist keine
    Registry - dann waeren alle Aussagen unten trivial erfuellt.
    """
    per_strategy = portfolio.stats_per_strategy()
    for name in (CHAIN_NAME, OPENING_RANGE_NAME):
        assert per_strategy[name]["decisions"] > 0, f"{name} hat nie entschieden"
    assert per_strategy[OPENING_RANGE_NAME]["trades"] > 0, (
        "der Opening Range Breakout hat nie gehandelt - die zweite Strategie ist wirkungslos"
    )


def test_die_zweite_strategie_erhoeht_die_frequenz(
    config: Config, mnq: Instrument, series: BarSeries
):
    """Der ganze Zweck des Umbaus: mehr Trades als die Kette allein."""
    tuned = tradeable_config(config)
    only_chain = Config(
        **{
            **tuned.model_dump(),
            "opening_range": OpeningRangeConfig(
                **{**tuned.opening_range.model_dump(), "enabled": False}
            ),
        }
    )

    alone = _run(only_chain, mnq, series)
    together = _run(tuned, mnq, series)

    assert len(together.signals) > len(alone.signals)


def test_doppelte_strategienamen_werden_abgelehnt(config: Config, mnq: Instrument):
    """Der Name ist der Schluessel jeder Auswertung - zweimal derselbe waere stumm."""
    from tradex.strategy.chain import ChainStrategy

    tuned = tradeable_config(config)
    with pytest.raises(ValueError, match="eindeutig"):
        StrategyPortfolio(
            SYMBOL,
            mnq,
            tuned,
            [ChainStrategy(SYMBOL, mnq, tuned), ChainStrategy(SYMBOL, mnq, tuned)],
        )


def test_portfolio_ohne_strategien_wird_abgelehnt(config: Config, mnq: Instrument):
    with pytest.raises(ValueError, match="ohne Strategien"):
        StrategyPortfolio(SYMBOL, mnq, tradeable_config(config), [])


def test_die_eroeffnungsspanne_kann_ihr_mindest_crv_ueberhaupt_erreichen(config: Config):
    """Geometrie-Waechter, gefunden an echten Daten.

    Der Stop liegt auf der Gegenseite der Spanne, das Ziel ist ein Vielfaches
    ihrer Breite:

        CRV = mult * W / (W + Puffer)  <  mult

    Ist `target_range_mult` nicht groesser als `min_rr`, erzeugt die Strategie
    ausnahmslos Vorschlaege, die sofort verworfen werden - beim ersten Lauf
    ueber 2024 waren das 387 Ablehnungen und null Trades. Ein Backtest haette
    dazu brav "kein Edge" gemeldet, obwohl die Regel nie zum Zug kam.
    """
    assert config.opening_range.target_range_mult > config.risk.min_rr

    issues = check_configuration(config, get_instrument("MNQ"))
    assert not [i for i in issues if i.code == "opening_range.rr_unreachable"]


def test_unerreichbares_crv_wird_gemeldet(config: Config):
    """Die Pruefung muss auch wirklich anschlagen, sonst ist sie Dekoration."""
    broken = Config(
        **{
            **config.model_dump(),
            "opening_range": OpeningRangeConfig(
                **{**config.opening_range.model_dump(), "target_range_mult": 1.5}
            ),
            "risk": RiskConfig(**{**config.risk.model_dump(), "min_rr": 2.0}),
        }
    )

    issues = check_configuration(broken, get_instrument("MNQ"))

    problem = next(i for i in issues if i.code == "opening_range.rr_unreachable")
    assert problem.severity == "error"


# --------------------------------------------------------------- Gemeinsames Risiko
def test_die_kennung_ist_kontoweit_eindeutig(portfolio: StrategyPortfolio):
    """`setup_id` zaehlt JE STRATEGIE und kollidiert zwischen ihnen.

    Als Schluessel im Risikobuch waere das eine Verwechslung mit Ansage: die
    eine Position wuerde die andere schliessen. Deshalb `trade_id`.
    """
    ids = [s.trade_id for s in portfolio.signals]
    assert len(ids) == len(set(ids)), "trade_id kommt doppelt vor"

    # Der Beweis, dass es die Kollision wirklich gibt - sonst prueft der Test nichts.
    per_strategy: dict[str, set[int]] = {}
    for signal in portfolio.signals:
        per_strategy.setdefault(signal.strategy, set()).add(signal.setup_id)
    if len(per_strategy) > 1:
        collections = list(per_strategy.values())
        assert collections[0] & collections[1], (
            "keine ueberlappenden setup_id - der Test kann die Kollision nicht zeigen"
        )


def test_beide_strategien_teilen_sich_ein_budget(
    config: Config, mnq: Instrument, series: BarSeries
):
    """Die Obergrenze gilt fuer das KONTO, nicht je Strategie.

    Ohne zentrale Pruefung waere das erlaubte Gesamtrisiko die Summe der
    Einzelbudgets - bei zwei Strategien also das Doppelte des Erlaubten.

    Geprueft wird ueber den Backtester, denn nur dort werden Positionen
    tatsaechlich gebucht und wieder geschlossen. In einem reinen Strategielauf
    bleibt das Risikobuch leer (es gibt ja keine Ausfuehrung) - eine
    Tagesgrenze koennte dort gar nicht greifen.
    """
    strict = _with_risk(
        tradeable_config(config), max_trades_per_day=1, max_open_positions=1
    )
    result = Backtester(SYMBOL, mnq, strict).run(series)

    assert result.trades, "ohne Trades sagt der Test nichts"
    assert len({t.strategy for t in result.trades}) >= 1

    per_day: dict[int, int] = {}
    for trade in result.trades:
        per_day[trade.trading_day] = per_day.get(trade.trading_day, 0) + 1
    assert max(per_day.values()) == 1


def test_positionen_verschiedener_strategien_ueberlappen_nicht(
    config: Config, mnq: Instrument, series: BarSeries
):
    """`max_open_positions` gilt strategieuebergreifend.

    Genau hier wuerde eine getrennte Risikopruefung je Strategie auffallen:
    zwei Strategien saehen jeweils "null offene Positionen" und beide kaemen
    durch.
    """
    strict = _with_risk(
        tradeable_config(config), max_open_positions=1, max_trades_per_day=50
    )
    result = Backtester(SYMBOL, mnq, strict).run(series)

    assert result.trades, "ohne Trades sagt der Test nichts"
    spans = sorted((t.entry_ts, t.exit_ts) for t in result.trades)
    for (_, first_exit), (second_entry, _) in pairwise(spans):
        assert second_entry >= first_exit, "zwei Positionen ueberlappten sich"


def test_risikobudget_gilt_pro_trade_unabhaengig_von_der_strategie(
    portfolio: StrategyPortfolio, config: Config
):
    budget = tradeable_config(config).risk.risk_per_trade_amount
    for signal in portfolio.signals:
        assert signal.risk_amount <= budget + 1e-9, signal


# ------------------------------------------------------------------ Reihenfolge
def test_reihenfolge_ist_reproduzierbar(config: Config, mnq: Instrument):
    """Bei knappen Grenzen entscheidet die Pruefreihenfolge mit.

    Sie darf deshalb nicht davon abhaengen, in welcher Reihenfolge die
    Strategien zufaellig geantwortet haben.
    """
    tuned = _with_risk(tradeable_config(config), max_open_positions=1)
    data = trending_market(60 * 24 * 6)

    first = _run(tuned, mnq, data)
    second = _run(tuned, mnq, data)

    def fingerprint(p: StrategyPortfolio) -> list[tuple]:
        return [(d.ts, d.strategy, d.setup_id, d.decision) for d in p.decisions]

    assert fingerprint(first) == fingerprint(second)


def test_jede_entscheidung_traegt_ihre_strategie(portfolio: StrategyPortfolio):
    """Ohne diese Spalte laesst sich nicht sagen, wer das Ergebnis getragen hat."""
    known = {s.name for s in portfolio.strategies}
    for decision in portfolio.decisions:
        assert decision.strategy in known, decision


# ------------------------------------------------------------------- Abschalten
def test_zuruecksetzen_leert_alles(portfolio: StrategyPortfolio, config: Config, mnq: Instrument):
    """Nach einem Reset muss ein Lauf dasselbe liefern wie beim ersten Mal."""
    tuned = tradeable_config(config)
    data = trending_market(60 * 24 * 4)

    context = MarketContext(SYMBOL, mnq, tuned)
    fresh = build_portfolio(SYMBOL, mnq, tuned)
    for bar in data:
        updates = context.on_base_bar(bar)
        if updates:
            fresh.on_updates(updates, context)
    before = [(d.ts, d.strategy, d.decision) for d in fresh.decisions]

    fresh.reset()
    assert fresh.decisions == []
    assert fresh.signals == []

    context = MarketContext(SYMBOL, mnq, tuned)
    for bar in data:
        updates = context.on_base_bar(bar)
        if updates:
            fresh.on_updates(updates, context)
    assert [(d.ts, d.strategy, d.decision) for d in fresh.decisions] == before


def test_unbekannte_strategie_erklaert_sich(portfolio: StrategyPortfolio):
    with pytest.raises(KeyError, match="Unbekannte Strategie"):
        portfolio.strategy("gibt_es_nicht")


# ------------------------------------------------------- Strategie-Basisvertrag
def test_jede_strategie_muss_zuruecksetzen_koennen():
    """`reset` ist bewusst abstrakt.

    Eine Strategie, die ihren Zustand beim Zuruecksetzen behaelt, liefert nach
    einem Reset andere Ergebnisse als beim ersten Lauf - und macht jede
    Reproduzierbarkeitsaussage zunichte.
    """

    class Vergesslich(Strategy):
        name = "unvollstaendig"

        def on_updates(self, updates, context) -> StrategyOutput:
            return StrategyOutput()

    with pytest.raises(TypeError, match="reset"):
        Vergesslich()  # type: ignore[abstract]
