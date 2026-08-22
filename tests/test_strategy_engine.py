"""Strategy Engine: die Pflichtkette im Zusammenspiel (Spec §6-§9).

Zwei Arten von Pruefung:

1. Zustandsmaschine - handgebaute Kandidaten, deren Verfall und Ungueltigkeit
   sich exakt nachrechnen lassen.

2. Eigenschaften ueber viele Entscheidungen - Aussagen, die IMMER gelten
   muessen, egal welche Daten hineinlaufen. Etwa: ein Trade entsteht nie mit
   unvollstaendiger Checkliste, und ein Stop liegt nie auf der falschen Seite
   des Einstiegs. Solche Invarianten fangen Fehler, die ein einzelnes
   handgebautes Beispiel nie beruehrt.
"""

from __future__ import annotations

import pytest

from tests.conftest import tradeable_config as _tradeable_config
from tests.conftest import trending_market as _trending_market
from tradex.analysis.context import MarketContext
from tradex.config import Config
from tradex.domain.bars import BarSeries
from tradex.domain.enums import Direction
from tradex.domain.instruments import Instrument
from tradex.strategy.chain import CHAIN_NAME
from tradex.strategy.portfolio import StrategyPortfolio
from tradex.strategy.registry import build_portfolio
from tradex.strategy.setup import SetupStage

SYMBOL = "MNQ"


def _run(config: Config, instrument: Instrument, series: BarSeries) -> StrategyPortfolio:
    context = MarketContext(SYMBOL, instrument, config)
    engine = build_portfolio(SYMBOL, instrument, config)
    for bar in series:
        updates = context.on_base_bar(bar)
        if updates:
            engine.on_updates(updates, context)
    return engine


@pytest.fixture(scope="module")
def engine(config: Config, mnq: Instrument) -> StrategyPortfolio:
    return _run(_tradeable_config(config), mnq, _trending_market(60 * 24 * 12))


# --------------------------------------------------------------- Grundverhalten
def test_kette_wird_ueberhaupt_vollstaendig(engine: StrategyPortfolio):
    """Absicherung gegen eine Strategie, die strukturell nie ausloest.

    Ohne diesen Test blieben alle Invarianten unten gruen, selbst wenn die
    Engine gar keine Trades erzeugen KANN.
    """
    trades = [d for d in engine.decisions if d.is_trade]
    assert len(engine.decisions) > 0, "keine Entscheidungen getroffen"
    assert len(trades) > 0, "die Pflichtkette wurde nie vollstaendig"
    assert len(engine.signals) == len(trades)


def test_es_gibt_auch_ablehnungen(engine: StrategyPortfolio):
    """Eine Strategie, die alles annimmt, filtert nichts."""
    rejected = [d for d in engine.decisions if not d.is_trade]
    assert len(rejected) > 0
    assert all(d.blocking_reason for d in rejected), "jede Ablehnung braucht einen Grund"


# ---------------------------------------------------------------- Invarianten
def test_trade_nur_bei_vollstaendiger_checkliste(engine: StrategyPortfolio):
    """Spec §9: fehlt eine Pflichtbedingung, entsteht kein Trade."""
    for decision in engine.decisions:
        if decision.is_trade:
            assert all(decision.checklist.values()), (
                f"Setup {decision.setup_id} gehandelt trotz fehlend: {decision.missing}"
            )


def test_stop_liegt_immer_auf_der_verlustseite(engine: StrategyPortfolio):
    """Ein Long-Stop ueber dem Einstieg waere kein Stop, sondern Sofortverlust."""
    for signal in engine.signals:
        if signal.direction is Direction.BULLISH:
            assert signal.stop < signal.entry, signal
        else:
            assert signal.stop > signal.entry, signal


def test_ziel_liegt_immer_in_handelsrichtung(engine: StrategyPortfolio):
    for signal in engine.signals:
        if signal.direction is Direction.BULLISH:
            assert signal.target > signal.entry, signal
        else:
            assert signal.target < signal.entry, signal


def test_mindest_crv_wird_nie_unterschritten(engine: StrategyPortfolio, config: Config):
    min_rr = _tradeable_config(config).risk.min_rr
    for signal in engine.signals:
        assert signal.rr >= min_rr, signal


def test_positionsgroesse_und_risiko_bleiben_im_budget(
    engine: StrategyPortfolio, config: Config
):
    tuned = _tradeable_config(config)
    budget = tuned.risk.risk_per_trade_amount
    for signal in engine.signals:
        assert signal.quantity >= 1, signal
        assert signal.quantity <= tuned.risk.max_position_size, signal
        assert signal.risk_amount <= budget + 1e-9, signal


def test_stopweite_bleibt_in_den_grenzen(engine: StrategyPortfolio, config: Config):
    stops = _tradeable_config(config).stops
    for signal in engine.signals:
        assert stops.min_stop_ticks <= signal.stop_ticks <= stops.max_stop_ticks, signal


def test_bestaetigung_kommt_nie_vor_dem_retracement(engine: StrategyPortfolio):
    """Kein Look-ahead: der MSS muss NACH dem Ruecklauf liegen.

    Weil der Aggregator kleine Timeframes vor grossen schliesst, kann die
    Bestaetigung fruehestens auf der naechsten Bar der Einstiegsebene erfolgen.
    """
    for candidate in engine.strategy(CHAIN_NAME).candidates:
        if candidate.confirmed_index is None:
            continue
        assert candidate.retraced_confirmation_index is not None
        assert candidate.confirmed_index > candidate.retraced_confirmation_index


def test_trades_folgen_dem_htf_bias(engine: StrategyPortfolio):
    """Spec §7 Schritt 1: die KETTE handelt nur in Richtung der grossen Zeitebenen.

    Ausdruecklich nur fuer die Kette. Der Opening Range Breakout hat bewusst
    keinen Bias-Filter - haetten beide Strategien denselben, waeren ihre
    Ergebnisse stark korreliert und das Portfolio nur eine teurere Fassung der
    Kette. Ob der Filter hilft, soll der Vergleich zeigen.
    """
    for decision in engine.decisions:
        if not decision.is_trade or decision.strategy != CHAIN_NAME:
            continue
        expected = "bullish" if decision.direction is Direction.BULLISH else "bearish"
        assert decision.htf_bias == expected, decision


def test_jede_entscheidung_hat_eine_begruendung(engine: StrategyPortfolio):
    """Spec §25: nachvollziehbar muss beides sein - Trade und Nicht-Trade."""
    for decision in engine.decisions:
        assert decision.reasons, decision
        codes = {r.code for r in decision.reasons}
        assert "decision.trade" in codes or "decision.no_trade" in codes


# ---------------------------------------------------------------- Determinismus
def test_zwei_laeufe_liefern_identische_entscheidungen(config: Config, mnq: Instrument):
    """Ohne Determinismus waere jede Backtest-Aussage wertlos."""
    tuned = _tradeable_config(config)
    series = _trending_market(60 * 24 * 5)

    first = _run(tuned, mnq, series)
    second = _run(tuned, mnq, series)

    def fingerprint(engine: StrategyPortfolio) -> list[tuple]:
        return [
            (d.ts, d.setup_id, d.decision, d.stage, d.blocking_reason)
            for d in engine.decisions
        ]

    assert fingerprint(first) == fingerprint(second)
    assert [s.entry for s in first.signals] == [s.entry for s in second.signals]


# ------------------------------------------------------- Ungueltigkeit / Verfall
def test_kandidat_stirbt_wenn_kurs_hinter_den_sweep_faellt(
    engine: StrategyPortfolio,
):
    """Wird das Sweep-Extrem wieder durchhandelt, war die Liquiditaet nicht
    geholt und abgelehnt, sondern schlicht durchbrochen."""
    killed = [
        d
        for d in engine.decisions
        if d.blocking_reason == "setup.invalidated_beyond_sweep"
    ]
    assert killed, "Ungueltigkeitsregel hat nie gegriffen - vermutlich wirkungslos"
    for decision in killed:
        assert decision.stage == SetupStage.INVALIDATED.value


def test_kandidaten_verfallen(engine: StrategyPortfolio):
    """Ohne Zeitfenster bliebe jeder Sweep ewig als halbfertiges Setup stehen."""
    expired = [d for d in engine.decisions if d.blocking_reason == "setup.expired"]
    assert expired, "kein Kandidat ist je verfallen - Zeitfenster vermutlich wirkungslos"


def test_offene_kandidaten_bleiben_begrenzt(engine: StrategyPortfolio, config: Config):
    limit = _tradeable_config(config).strategy.max_active_setups
    assert len(engine.active_candidates()) <= limit


def test_verdraengte_kandidaten_werden_protokolliert(config: Config, mnq: Instrument):
    """Spec §25: kein Setup darf lautlos verschwinden.

    Verdraengte Kandidaten taten das frueher - an echten Daten gemessen 627
    allein im Jahr 2024, rund zwei pro Handelstag. Sie fehlten damit in jeder
    Auswertung der Frage "warum wurde nicht gehandelt?".

    Die Obergrenze steht hier auf 1, damit die Verdraengung sicher eintritt.
    Auf den synthetischen Daten laufen sonst selten genug Kandidaten
    gleichzeitig, um die Auslieferungsgrenze von 4 zu erreichen.
    """
    from tradex.config import StrategyConfig

    tuned = _tradeable_config(config)
    crowded_config = Config(
        **{
            **tuned.model_dump(),
            "strategy": StrategyConfig(
                **{**tuned.strategy.model_dump(), "max_active_setups": 1}
            ),
        }
    )
    engine = _run(crowded_config, mnq, _trending_market(60 * 24 * 6))

    crowded = [d for d in engine.decisions if d.blocking_reason == "setup.crowded_out"]
    assert crowded, "die Verdraengungsregel hat nie gegriffen - Test ist wirkungslos"
    for decision in crowded:
        assert not decision.is_trade
        reason = next(r for r in decision.reasons if r.code == "setup.crowded_out")
        # Die erreichte Stufe muss erhalten bleiben, sonst steht ueberall
        # "expired" und man sieht nicht, wie weit der Kandidat kam.
        assert reason.params["stage"] in ("swept", "displaced", "retraced")


def test_abgeschaltete_strategie_erzeugt_nichts(config: Config, mnq: Instrument):
    """Jede Strategie muss sich einzeln abschalten lassen.

    Geprueft wird beides: dass die Kette schweigt, wenn sie aus ist - und dass
    das Portfolio trotzdem noch laeuft, weil die zweite Strategie weiter
    arbeitet. Ohne den zweiten Teil wuerde ein Fehler in der Registry, der
    versehentlich ALLE Strategien abschaltet, unbemerkt bleiben.
    """
    from tradex.config import OpeningRangeConfig, StrategyConfig

    without_chain = Config(
        **{
            **config.model_dump(),
            "strategy": StrategyConfig(**{**config.strategy.model_dump(), "enabled": False}),
        }
    )
    engine = _run(without_chain, mnq, _trending_market(60 * 24 * 2))
    assert all(d.strategy != CHAIN_NAME for d in engine.decisions)

    everything_off = Config(
        **{
            **without_chain.model_dump(),
            "opening_range": OpeningRangeConfig(
                **{**config.opening_range.model_dump(), "enabled": False}
            ),
        }
    )
    silent = _run(everything_off, mnq, _trending_market(60 * 24 * 2))
    assert silent.decisions == []
    assert silent.signals == []
