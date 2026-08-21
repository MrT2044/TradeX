"""Risikoschicht: Positionsgroesse, Tagesgrenzen, Handelsfenster, Konsistenz.

Spec §10, §13, §24.
"""

from __future__ import annotations

import math

import pytest

from tradex.analysis import reasons as R
from tradex.config import Config, RiskConfig, StopsConfig, TargetsConfig, TradingWindowsConfig
from tradex.domain.enums import Direction, SessionName
from tradex.domain.instruments import Instrument
from tradex.risk.consistency import affordable_stop_ticks, check_configuration
from tradex.risk.engine import RiskEngine
from tradex.risk.ledger import OpenPosition, RiskLedger
from tradex.risk.sizing import calculate_position_size

DAY = 739_000
SESSION = SessionName.NY_AM.value
ATR = 10.0


def _risk(config: Config, **overrides) -> RiskConfig:
    return RiskConfig(**{**config.risk.model_dump(), **overrides})


def _config_with(config: Config, **sections) -> Config:
    return Config(**{**config.model_dump(), **sections})


# ------------------------------------------------------------ Positionsgroesse
def test_positionsgroesse_wird_berechnet(config: Config, mnq: Instrument):
    """25 USD Budget, 5 Punkte Stop, 2 USD/Punkt -> 10 USD je Kontrakt -> 2 Stueck."""
    params = _risk(config, account_size=10_000.0, risk_per_trade_pct=0.25)
    size = calculate_position_size(5.0, mnq, params)

    assert size.ok
    assert size.quantity == 2
    assert math.isclose(size.risk_per_contract, 10.0)
    assert math.isclose(size.risk_amount, 20.0)
    assert math.isclose(size.risk_budget, 25.0)


def test_wird_abgerundet_nie_aufgerundet(config: Config, mnq: Instrument):
    """Aufrunden hiesse, das erlaubte Risiko zu ueberschreiten."""
    params = _risk(config, account_size=10_000.0, risk_per_trade_pct=0.25)
    size = calculate_position_size(4.0, mnq, params)  # 8 USD/Kontrakt -> 3.125
    assert size.quantity == 3
    assert size.risk_amount <= size.risk_budget


def test_zu_weiter_stop_ergibt_keinen_trade(config: Config, mnq: Instrument):
    """Der wichtigste Sonderfall: schon ein Kontrakt riskiert zu viel.

    Der Stop wird NICHT enger gesetzt, damit es passt - das waere eine
    stillschweigende Regelaenderung.
    """
    params = _risk(config, account_size=10_000.0, risk_per_trade_pct=0.25)
    size = calculate_position_size(20.0, mnq, params)  # 40 USD > 25 USD Budget

    assert not size.ok
    assert size.quantity == 0
    assert size.rejection == "size_zero"


def test_obergrenze_der_stueckzahl(config: Config, mnq: Instrument):
    params = _risk(config, account_size=1_000_000.0, risk_per_trade_pct=1.0, max_position_size=5)
    size = calculate_position_size(5.0, mnq, params)

    assert size.ok
    assert size.quantity == 5
    assert size.capped


def test_nq_braucht_groesseres_konto_als_mnq(config: Config, instruments: dict[str, Instrument]):
    """Derselbe Stop kostet bei NQ das Zehnfache."""
    params = _risk(config, account_size=10_000.0, risk_per_trade_pct=0.25)
    mnq_size = calculate_position_size(5.0, instruments["MNQ"], params)
    nq_size = calculate_position_size(5.0, instruments["NQ"], params)

    assert mnq_size.ok
    assert not nq_size.ok, "100 USD Risiko je NQ-Kontrakt sprengen ein 25-USD-Budget"


# -------------------------------------------------------------------- Grenzen
def test_tagesverlustlimit_stoppt_weitere_trades(config: Config, mnq: Instrument):
    ledger = RiskLedger()
    engine = RiskEngine(config, mnq, ledger)

    assert engine.evaluate(5.0, SESSION, ATR, DAY).approved

    # Verlust ueber dem Limit buchen (max_daily_loss_pct 1.0 -> 100 USD)
    ledger.open_position(
        OpenPosition(1, Direction.BULLISH, 0, 21000.0, 20995.0, 21015.0, 1, 25.0), DAY
    )
    ledger.close_position(1, 100, -120.0, DAY)

    assessment = engine.evaluate(5.0, SESSION, ATR, DAY)
    assert not assessment.approved
    assert assessment.blocking_reason == R.RISK_DAILY_LOSS_LIMIT


def test_gewinne_zaehlen_nicht_gegen_das_verlustlimit(config: Config, mnq: Instrument):
    ledger = RiskLedger()
    engine = RiskEngine(config, mnq, ledger)
    ledger.open_position(
        OpenPosition(1, Direction.BULLISH, 0, 21000.0, 20995.0, 21015.0, 1, 25.0), DAY
    )
    ledger.close_position(1, 100, +80.0, DAY)

    assert ledger.day(DAY).realized_loss == 0.0
    assert engine.evaluate(5.0, SESSION, ATR, DAY).approved


def test_maximale_trades_pro_tag(config: Config, mnq: Instrument):
    ledger = RiskLedger()
    engine = RiskEngine(config, mnq, ledger)
    for i in range(config.risk.max_trades_per_day):
        ledger.open_position(
            OpenPosition(i, Direction.BULLISH, 0, 21000.0, 20995.0, 21015.0, 1, 25.0), DAY
        )
        ledger.close_position(i, 100, +1.0, DAY)

    assessment = engine.evaluate(5.0, SESSION, ATR, DAY)
    assert not assessment.approved
    assert assessment.blocking_reason == R.RISK_MAX_TRADES


def test_grenze_gilt_je_handelstag(config: Config, mnq: Instrument):
    """Ein neuer Handelstag setzt Zaehler und Verlust zurueck."""
    ledger = RiskLedger()
    engine = RiskEngine(config, mnq, ledger)
    ledger.open_position(
        OpenPosition(1, Direction.BULLISH, 0, 21000.0, 20995.0, 21015.0, 1, 25.0), DAY
    )
    ledger.close_position(1, 100, -120.0, DAY)

    assert not engine.evaluate(5.0, SESSION, ATR, DAY).approved
    assert engine.evaluate(5.0, SESSION, ATR, DAY + 1).approved


def test_maximal_offene_positionen(config: Config, mnq: Instrument):
    ledger = RiskLedger()
    engine = RiskEngine(config, mnq, ledger)
    ledger.open_position(
        OpenPosition(1, Direction.BULLISH, 0, 21000.0, 20995.0, 21015.0, 1, 25.0), DAY
    )

    assessment = engine.evaluate(5.0, SESSION, ATR, DAY)
    assert not assessment.approved
    assert assessment.blocking_reason == R.RISK_MAX_POSITIONS


def test_doppelte_position_zum_selben_setup_wird_abgelehnt(config: Config):
    """Spec §24 Duplicate Order Protection."""
    ledger = RiskLedger()
    position = OpenPosition(1, Direction.BULLISH, 0, 21000.0, 20995.0, 21015.0, 1, 25.0)
    ledger.open_position(position, DAY)
    with pytest.raises(ValueError, match="bereits eine offene Position"):
        ledger.open_position(position, DAY)


# ------------------------------------------------------------ Handelsfenster
def test_session_ausserhalb_der_freigabe(config: Config, mnq: Instrument):
    engine = RiskEngine(config, mnq, RiskLedger())
    assessment = engine.evaluate(5.0, SessionName.ASIA.value, ATR, DAY)

    assert not assessment.approved
    assert assessment.blocking_reason == R.WINDOW_SESSION_BLOCKED


def test_zu_geringe_volatilitaet(config: Config, mnq: Instrument):
    engine = RiskEngine(config, mnq, RiskLedger())
    # min_atr_ticks = 8 -> 2.0 Punkte
    assessment = engine.evaluate(5.0, SESSION, 0.5, DAY)

    assert not assessment.approved
    assert assessment.blocking_reason == R.WINDOW_VOLATILITY_LOW


def test_zu_hohe_volatilitaet(config: Config, mnq: Instrument):
    engine = RiskEngine(config, mnq, RiskLedger())
    assessment = engine.evaluate(5.0, SESSION, 500.0, DAY)

    assert not assessment.approved
    assert assessment.blocking_reason == R.WINDOW_VOLATILITY_HIGH


def test_handelsfenster_abschaltbar(config: Config, mnq: Instrument):
    windows = TradingWindowsConfig(**{**config.trading_windows.model_dump(), "enabled": False})
    engine = RiskEngine(_config_with(config, trading_windows=windows), mnq, RiskLedger())
    assert engine.evaluate(5.0, SessionName.ASIA.value, ATR, DAY).approved


def test_reihenfolge_der_ablehnungsgruende(config: Config, mnq: Instrument):
    """Harte Sperren zuerst: bei erreichtem Tagesverlust ist die Session egal."""
    ledger = RiskLedger()
    engine = RiskEngine(config, mnq, ledger)
    ledger.open_position(
        OpenPosition(1, Direction.BULLISH, 0, 21000.0, 20995.0, 21015.0, 1, 25.0), DAY
    )
    ledger.close_position(1, 100, -120.0, DAY)

    assessment = engine.evaluate(999.0, SessionName.ASIA.value, ATR, DAY)
    assert assessment.blocking_reason == R.RISK_DAILY_LOSS_LIMIT


# ----------------------------------------------------------------- Konsistenz
def test_bezahlbare_stopweite(config: Config, mnq: Instrument):
    """25 USD Budget / 2 USD je Punkt = 12.5 Punkte = 50 Ticks."""
    assert math.isclose(affordable_stop_ticks(config, mnq), 50.0)


def test_widerspruch_max_stop_gegen_budget_wird_gemeldet(config: Config, mnq: Instrument):
    """Genau der Fall, der beim ersten Strategielauf 22 von 24 Setups verwarf.

    Die Konfiguration erlaubt 240 Ticks Stop, bezahlbar sind aber nur 50. Ohne
    diese Meldung sucht man den Fehler an der falschen Stelle.
    """
    issues = check_configuration(config, mnq)
    codes = {i.code for i in issues}
    assert "risk.max_stop_exceeds_budget" in codes


def test_stimmige_konfiguration_meldet_nichts(config: Config, mnq: Instrument):
    stops = StopsConfig(**{**config.stops.model_dump(), "max_stop_ticks": 50})
    tuned = _config_with(config, stops=stops)
    assert check_configuration(tuned, mnq) == []


def test_unbezahlbarer_mindeststop_ist_ein_fehler(config: Config, mnq: Instrument):
    """Wenn selbst der kleinste erlaubte Stop zu teuer ist, kann NIE ein Trade entstehen."""
    risk = _risk(config, account_size=200.0, risk_per_trade_pct=0.25)  # 0.50 USD Budget
    tuned = _config_with(config, risk=risk)

    issues = check_configuration(tuned, mnq)
    assert any(i.code == "risk.stop_unaffordable" and i.severity == "error" for i in issues)


def test_tageslimit_unter_einzeltrade_ist_ein_fehler(config: Config, mnq: Instrument):
    risk = _risk(config, risk_per_trade_pct=2.0, max_daily_loss_pct=1.0)
    tuned = _config_with(config, risk=risk)

    issues = check_configuration(tuned, mnq)
    assert any(i.code == "risk.daily_limit_below_single_trade" for i in issues)


def test_fallback_unter_min_rr_ist_ein_fehler(config: Config, mnq: Instrument):
    targets = TargetsConfig(
        **{**config.targets.model_dump(), "mode": "r_multiple", "fallback_r_multiple": 1.0}
    )
    tuned = _config_with(config, targets=targets)

    issues = check_configuration(tuned, mnq)
    assert any(i.code == "targets.fallback_below_min_rr" for i in issues)


# --------------------------------------------------------------------- Ledger
def test_ledger_rechnet_r_vielfache(config: Config):
    ledger = RiskLedger()
    ledger.open_position(
        OpenPosition(1, Direction.BULLISH, 0, 21000.0, 20995.0, 21015.0, 2, 20.0), DAY
    )
    trade = ledger.close_position(1, 100, 60.0, DAY)

    assert math.isclose(trade.r_multiple, 3.0)
    assert ledger.summary()["trades"] == 1
    assert ledger.summary()["net_pnl"] == 60.0
    assert ledger.open_count == 0
