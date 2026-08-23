"""Sperrfristen nach einem Trade (Spec Paragraph 24).

Der Punkt, auf den es ankommt: die Frist laeuft nach MARKTZEIT, nicht nach der
Wanduhr. Mit der Wanduhr gerechnet vergingen im Backtest zwischen zwei Bars
Mikrosekunden - die Sperre griffe dort nie und im Echtbetrieb immer, und damit
waeren Backtest und Live nicht mehr dieselbe Aussage (Invariante 3).
"""

from __future__ import annotations

from tests.conftest import tradeable_config
from tradex.analysis import reasons as R
from tradex.config import Config, RiskConfig
from tradex.domain.enums import Direction
from tradex.domain.instruments import Instrument
from tradex.risk.engine import RiskEngine
from tradex.risk.ledger import OpenPosition, RiskLedger

TAG = 20251103
T0 = 1_700_000_000_000_000_000
"""Marktzeit in Nanosekunden."""

MINUTE = 60_000_000_000


def _config(config: Config, **cooldowns: float) -> Config:
    basis = tradeable_config(config)
    risk = RiskConfig(**{**basis.risk.model_dump(), **cooldowns})
    return Config(**{**basis.model_dump(), "risk": risk})


def _ledger_mit_abschluss(pnl: float, exit_ts: int) -> RiskLedger:
    ledger = RiskLedger()
    ledger.open_position(
        OpenPosition(
            setup_id=1,
            direction=Direction.BULLISH,
            entry_ts=exit_ts - 10 * MINUTE,
            entry_price=21000.0,
            stop=20980.0,
            target=21040.0,
            quantity=1,
            risk_amount=40.0,
        ),
        TAG,
    )
    ledger.close_position(1, exit_ts=exit_ts, pnl=pnl, trading_day=TAG)
    return ledger


def _codes(engine: RiskEngine, ts: int) -> set[str]:
    return {r.code for r in engine.check_limits(TAG, ts=ts) if not r.ok}


def test_ohne_konfiguration_sperrt_nichts(config: Config, mnq: Instrument):
    """Auslieferungszustand: beide Fristen 0, also wirkungslos.

    Waechter gegen die stille Verhaltensaenderung - eine Sperrfrist ist eine
    Strategieaenderung und wird hier nicht per Default eingefuehrt.
    """
    ledger = _ledger_mit_abschluss(pnl=-40.0, exit_ts=T0)
    engine = RiskEngine(tradeable_config(config), mnq, ledger)
    assert _codes(engine, T0 + MINUTE) == set()


def test_nach_einem_trade_wird_die_frist_eingehalten(config: Config, mnq: Instrument):
    ledger = _ledger_mit_abschluss(pnl=+80.0, exit_ts=T0)
    engine = RiskEngine(_config(config, cooldown_minutes_after_trade=15.0), mnq, ledger)

    assert R.RISK_COOLDOWN_AFTER_TRADE in _codes(engine, T0 + 5 * MINUTE)
    assert R.RISK_COOLDOWN_AFTER_TRADE in _codes(engine, T0 + 14 * MINUTE)
    # Genau auf der Grenze ist die Frist abgelaufen.
    assert _codes(engine, T0 + 15 * MINUTE) == set()


def test_nach_einem_verlust_gilt_die_laengere_frist(config: Config, mnq: Instrument):
    """Nach einem Stop ist die Marktlage haeufig genau die, die ihn ausgeloest
    hat - der naechste Vorschlag entsteht dann aus demselben Geschehen."""
    ledger = _ledger_mit_abschluss(pnl=-40.0, exit_ts=T0)
    engine = RiskEngine(
        _config(config, cooldown_minutes_after_trade=5.0, cooldown_minutes_after_loss=30.0),
        mnq,
        ledger,
    )

    # Die kurze Frist ist um, die lange nicht.
    codes = _codes(engine, T0 + 10 * MINUTE)
    assert codes == {R.RISK_COOLDOWN_AFTER_LOSS}
    assert _codes(engine, T0 + 30 * MINUTE) == set()


def test_ein_gewinn_loest_die_verlustfrist_nicht_aus(config: Config, mnq: Instrument):
    ledger = _ledger_mit_abschluss(pnl=+80.0, exit_ts=T0)
    engine = RiskEngine(_config(config, cooldown_minutes_after_loss=30.0), mnq, ledger)
    assert _codes(engine, T0 + MINUTE) == set()


def test_ohne_zeitangabe_wird_nicht_gesperrt(config: Config, mnq: Instrument):
    """`ts=0` heisst "Zeit unbekannt". Die Sperre ist eine Zusatzbedingung,
    keine Sicherheitsgrenze: ihr Ausfall erlaubt keinen verbotenen Trade."""
    ledger = _ledger_mit_abschluss(pnl=-40.0, exit_ts=T0)
    engine = RiskEngine(_config(config, cooldown_minutes_after_loss=30.0), mnq, ledger)
    assert _codes(engine, 0) == set()


def test_ohne_abgeschlossenen_trade_sperrt_nichts(config: Config, mnq: Instrument):
    engine = RiskEngine(_config(config, cooldown_minutes_after_trade=15.0), mnq, RiskLedger())
    assert _codes(engine, T0) == set()


def test_die_frist_wirkt_im_gesamturteil(config: Config, mnq: Instrument):
    """Nicht nur in `check_limits` - `evaluate()` muss sie durchreichen."""
    ledger = _ledger_mit_abschluss(pnl=-40.0, exit_ts=T0)
    engine = RiskEngine(_config(config, cooldown_minutes_after_loss=30.0), mnq, ledger)

    urteil = engine.evaluate(
        stop_distance_points=20.0,
        session="ny_am",
        atr=5.0,
        trading_day=TAG,
        ts=T0 + 5 * MINUTE,
    )
    assert not urteil.approved
    assert urteil.blocking_reason == R.RISK_COOLDOWN_AFTER_LOSS
