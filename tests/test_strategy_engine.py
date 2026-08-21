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

from datetime import timedelta

import numpy as np
import pytest

from tests.conftest import DEFAULT_START
from tradex.analysis.context import MarketContext
from tradex.config import Config, RiskConfig, StopsConfig, TradingWindowsConfig
from tradex.domain.bars import BarSeries, to_ns
from tradex.domain.enums import Direction
from tradex.domain.instruments import Instrument
from tradex.strategy.engine import StrategyEngine
from tradex.strategy.setup import SetupStage

SYMBOL = "MNQ"


def _tradeable_config(config: Config) -> Config:
    """Konfiguration, unter der die Kette auch tatsaechlich zu Trades kommt.

    Die Auslieferungswerte sind bewusst so eng, dass auf den vorliegenden Daten
    fast nichts durchkommt. Fuer den Test des Erfolgsfalls wird das Konto
    vergroessert und der erlaubte Stop an das Budget angeglichen - das sind
    Testbedingungen, keine Strategieaenderung.
    """
    risk = RiskConfig(
        **{
            **config.risk.model_dump(),
            "account_size": 100_000.0,
            "risk_per_trade_pct": 0.5,
            "max_trades_per_day": 50,
            "max_open_positions": 50,
            "min_rr": 1.2,
        }
    )
    stops = StopsConfig(**{**config.stops.model_dump(), "max_stop_ticks": 400})
    # Die synthetischen Daten haben keine Sessionstruktur - der Sessionfilter
    # wuerde hier nur zufaellig aussortieren. Er wird in test_risk.py geprueft.
    windows = TradingWindowsConfig(
        **{**config.trading_windows.model_dump(), "enabled": False}
    )
    return Config(
        **{
            **config.model_dump(),
            "risk": risk,
            "stops": stops,
            "trading_windows": windows,
        }
    )


def _trending_market(minutes: int, seed: int = 3) -> BarSeries:
    """Markt mit klarem Trend, Ruecksetzern und gelegentlichen Impulsen.

    Ein Trend ist noetig, damit der HTF-Bias ueberhaupt eine Richtung annimmt -
    ohne ihn entsteht per Spec §7 Schritt 1 gar kein Kandidat.
    """
    rng = np.random.default_rng(seed)
    series = BarSeries()
    price = 21000.0
    for i in range(minutes):
        # Laengere Aufwaertsphasen mit eingestreuten Ruecksetzern.
        drift = 0.25 if (i // 900) % 3 != 2 else -0.20
        shock = 9.0 if i % 260 == 0 else 1.6
        close = price + drift + float(rng.normal(0, shock))
        high = max(price, close) + abs(float(rng.normal(0, shock * 0.45)))
        low = min(price, close) - abs(float(rng.normal(0, shock * 0.45)))
        series.append(
            to_ns(DEFAULT_START + timedelta(minutes=i)),
            price,
            high,
            low,
            close,
            float(rng.integers(60, 900)),
        )
        price = close
    return series


def _run(config: Config, instrument: Instrument, series: BarSeries) -> StrategyEngine:
    context = MarketContext(SYMBOL, instrument, config)
    engine = StrategyEngine(SYMBOL, instrument, config)
    for bar in series:
        updates = context.on_base_bar(bar)
        if updates:
            engine.on_updates(updates, context)
    return engine


@pytest.fixture(scope="module")
def engine(config: Config, mnq: Instrument) -> StrategyEngine:
    return _run(_tradeable_config(config), mnq, _trending_market(60 * 24 * 12))


# --------------------------------------------------------------- Grundverhalten
def test_kette_wird_ueberhaupt_vollstaendig(engine: StrategyEngine):
    """Absicherung gegen eine Strategie, die strukturell nie ausloest.

    Ohne diesen Test blieben alle Invarianten unten gruen, selbst wenn die
    Engine gar keine Trades erzeugen KANN.
    """
    trades = [d for d in engine.decisions if d.is_trade]
    assert len(engine.decisions) > 0, "keine Entscheidungen getroffen"
    assert len(trades) > 0, "die Pflichtkette wurde nie vollstaendig"
    assert len(engine.signals) == len(trades)


def test_es_gibt_auch_ablehnungen(engine: StrategyEngine):
    """Eine Strategie, die alles annimmt, filtert nichts."""
    rejected = [d for d in engine.decisions if not d.is_trade]
    assert len(rejected) > 0
    assert all(d.blocking_reason for d in rejected), "jede Ablehnung braucht einen Grund"


# ---------------------------------------------------------------- Invarianten
def test_trade_nur_bei_vollstaendiger_checkliste(engine: StrategyEngine):
    """Spec §9: fehlt eine Pflichtbedingung, entsteht kein Trade."""
    for decision in engine.decisions:
        if decision.is_trade:
            assert all(decision.checklist.values()), (
                f"Setup {decision.setup_id} gehandelt trotz fehlend: {decision.missing}"
            )


def test_stop_liegt_immer_auf_der_verlustseite(engine: StrategyEngine):
    """Ein Long-Stop ueber dem Einstieg waere kein Stop, sondern Sofortverlust."""
    for signal in engine.signals:
        if signal.direction is Direction.BULLISH:
            assert signal.stop < signal.entry, signal
        else:
            assert signal.stop > signal.entry, signal


def test_ziel_liegt_immer_in_handelsrichtung(engine: StrategyEngine):
    for signal in engine.signals:
        if signal.direction is Direction.BULLISH:
            assert signal.target > signal.entry, signal
        else:
            assert signal.target < signal.entry, signal


def test_mindest_crv_wird_nie_unterschritten(engine: StrategyEngine, config: Config):
    min_rr = _tradeable_config(config).risk.min_rr
    for signal in engine.signals:
        assert signal.rr >= min_rr, signal


def test_positionsgroesse_und_risiko_bleiben_im_budget(
    engine: StrategyEngine, config: Config
):
    tuned = _tradeable_config(config)
    budget = tuned.risk.risk_per_trade_amount
    for signal in engine.signals:
        assert signal.quantity >= 1, signal
        assert signal.quantity <= tuned.risk.max_position_size, signal
        assert signal.risk_amount <= budget + 1e-9, signal


def test_stopweite_bleibt_in_den_grenzen(engine: StrategyEngine, config: Config):
    stops = _tradeable_config(config).stops
    for signal in engine.signals:
        assert stops.min_stop_ticks <= signal.stop_ticks <= stops.max_stop_ticks, signal


def test_bestaetigung_kommt_nie_vor_dem_retracement(engine: StrategyEngine):
    """Kein Look-ahead: der MSS muss NACH dem Ruecklauf liegen.

    Weil der Aggregator kleine Timeframes vor grossen schliesst, kann die
    Bestaetigung fruehestens auf der naechsten Bar der Einstiegsebene erfolgen.
    """
    for candidate in engine.candidates:
        if candidate.confirmed_index is None:
            continue
        assert candidate.retraced_confirmation_index is not None
        assert candidate.confirmed_index > candidate.retraced_confirmation_index


def test_trades_folgen_dem_htf_bias(engine: StrategyEngine):
    """Spec §7 Schritt 1: gehandelt wird nur in Richtung der grossen Zeitebenen."""
    for decision in engine.decisions:
        if not decision.is_trade:
            continue
        expected = "bullish" if decision.direction is Direction.BULLISH else "bearish"
        assert decision.htf_bias == expected, decision


def test_jede_entscheidung_hat_eine_begruendung(engine: StrategyEngine):
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

    def fingerprint(engine: StrategyEngine) -> list[tuple]:
        return [
            (d.ts, d.setup_id, d.decision, d.stage, d.blocking_reason)
            for d in engine.decisions
        ]

    assert fingerprint(first) == fingerprint(second)
    assert [s.entry for s in first.signals] == [s.entry for s in second.signals]


# ------------------------------------------------------- Ungueltigkeit / Verfall
def test_kandidat_stirbt_wenn_kurs_hinter_den_sweep_faellt(
    engine: StrategyEngine,
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


def test_kandidaten_verfallen(engine: StrategyEngine):
    """Ohne Zeitfenster bliebe jeder Sweep ewig als halbfertiges Setup stehen."""
    expired = [d for d in engine.decisions if d.blocking_reason == "setup.expired"]
    assert expired, "kein Kandidat ist je verfallen - Zeitfenster vermutlich wirkungslos"


def test_offene_kandidaten_bleiben_begrenzt(engine: StrategyEngine, config: Config):
    limit = _tradeable_config(config).strategy.max_active_setups
    assert len(engine.active_candidates()) <= limit


def test_abgeschaltete_strategie_erzeugt_nichts(config: Config, mnq: Instrument):
    from tradex.config import StrategyConfig

    disabled = Config(
        **{
            **config.model_dump(),
            "strategy": StrategyConfig(**{**config.strategy.model_dump(), "enabled": False}),
        }
    )
    engine = _run(disabled, mnq, _trending_market(60 * 24 * 2))
    assert engine.decisions == []
    assert engine.signals == []
