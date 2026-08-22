"""Kennzahlen gegen von Hand gerechnete Beispiele (Spec §19).

Die Trades werden hier direkt gebaut, nicht simuliert: eine Kennzahl muss sich
gegen eine Zahl pruefen lassen, die man selbst ausgerechnet hat. Wuerde man sie
gegen die Ausgabe des Simulators pruefen, waere der Test nur eine Wiederholung
der Implementierung.
"""

from __future__ import annotations

import pytest

from tradex.backtest import metrics as M
from tradex.backtest.execution import SimulatedTrade
from tradex.domain.enums import Direction, ExitReason

START = 10_000.0


def trade(
    pnl: float,
    *,
    setup_id: int = 1,
    risk: float = 100.0,
    exit_ts: int = 0,
    session: str = "ny_am",
    direction: Direction = Direction.BULLISH,
    reason: ExitReason = ExitReason.TARGET,
    bars_held: int = 10,
    mae: float = 5.0,
    mfe: float = 20.0,
) -> SimulatedTrade:
    """Ein Trade mit frei gewaehltem Ergebnis. 1R = 100 USD, sofern nicht anders gesagt."""
    return SimulatedTrade(
        setup_id=setup_id,
        symbol="MNQ",
        direction=direction,
        quantity=1,
        planned_entry=21000.0,
        stop=20990.0,
        target=21020.0,
        planned_rr=2.0,
        planned_stop_ticks=40.0,
        stop_anchor="retracement",
        target_source="liquidity",
        htf_bias="bullish",
        session=session,
        trading_day=20250303,
        signal_ts=exit_ts,
        entry_ts=exit_ts,
        entry_index=0,
        entry_price=21000.0,
        exit_ts=exit_ts,
        exit_index=bars_held,
        exit_price=21000.0 + pnl / 2.0,
        exit_reason=reason,
        bars_held=bars_held,
        risk_points=risk / 2.0,
        risk_amount=risk,
        gross_pnl=pnl,
        commission=0.0,
        pnl=pnl,
        r_multiple=pnl / risk,
        mae_points=mae,
        mfe_points=mfe,
    )


def sequence(*pnls: float) -> list[SimulatedTrade]:
    """Trades in zeitlicher Reihenfolge - ts steigt mit dem Index."""
    return [trade(pnl, setup_id=i + 1, exit_ts=(i + 1) * 60_000_000_000) for i, pnl in enumerate(pnls)]


# ------------------------------------------------------------------ Grundwerte
def test_leere_menge_liefert_keine_erfundenen_zahlen():
    """Ohne Trades gibt es kein Ergebnis - und keinen Profitfaktor von 0."""
    result = M.summarize([], START)

    assert result.trades == 0
    assert result.profit_factor is None
    assert result.sqn is None
    assert result.final_equity == START


def test_kennzahlen_stimmen_gegen_die_handrechnung():
    """3 Gewinner (+200/+200/+100), 2 Verlierer (-100/-100), 1R = 100 USD."""
    trades = sequence(200.0, -100.0, 200.0, 100.0, -100.0)

    result = M.summarize(trades, START)

    assert result.trades == 5
    assert result.wins == 3
    assert result.losses == 2
    assert result.win_rate == pytest.approx(60.0)
    assert result.net_pnl == pytest.approx(300.0)
    assert result.gross_profit == pytest.approx(500.0)
    assert result.gross_loss == pytest.approx(200.0)
    assert result.profit_factor == pytest.approx(2.5)
    # Erwartungswert = 300 USD / 5 Trades = 60 USD = 0.6R
    assert result.expectancy_usd == pytest.approx(60.0)
    assert result.expectancy_r == pytest.approx(0.6)
    assert result.avg_win_r == pytest.approx(5.0 / 3.0)
    assert result.avg_loss_r == pytest.approx(-1.0)
    assert result.payoff_ratio == pytest.approx(5.0 / 3.0)
    assert result.best_r == pytest.approx(2.0)
    assert result.worst_r == pytest.approx(-1.0)
    assert result.final_equity == pytest.approx(START + 300.0)
    assert result.return_pct == pytest.approx(3.0)


def test_ohne_verlusttrade_gibt_es_keinen_profitfaktor():
    """"Unendlich gut" ist keine Kennzahl, sondern eine zu kleine Stichprobe."""
    result = M.summarize(sequence(100.0, 200.0), START)

    assert result.profit_factor is None
    assert result.payoff_ratio is None


def test_nullergebnis_zaehlt_weder_als_gewinn_noch_als_verlust():
    result = M.summarize(sequence(100.0, 0.0, -100.0), START)

    assert (result.wins, result.losses, result.scratches) == (1, 1, 1)


# ------------------------------------------------------------------ Rueckgang
def test_maximaler_rueckgang_wird_vom_hoechststand_gemessen():
    """+300 (Hoch 10300), dann -100/-200/-100 -> tiefster Stand 9900.

    Der Rueckgang betraegt 400 USD, nicht 100: gemessen wird gegen den
    Hoechststand, nicht gegen den Startwert.
    """
    trades = sequence(300.0, -100.0, -200.0, -100.0, 50.0)

    result = M.summarize(trades, START)

    assert result.max_drawdown_usd == pytest.approx(400.0)
    assert result.max_drawdown_pct == pytest.approx(100.0 * 400.0 / 10_300.0)
    assert result.max_drawdown_r == pytest.approx(4.0)
    assert result.max_consecutive_losses == 3
    assert result.max_consecutive_wins == 1


def test_equity_kurve_beginnt_beim_startkapital():
    """Ohne Startpunkt unterschlaegt jede Auswertung einen Verlust gleich zu Beginn."""
    curve = M.equity_curve(sequence(-100.0, 200.0), START)

    assert len(curve) == 3
    assert curve[0].equity == pytest.approx(START)
    assert curve[0].trade_number == 0
    assert curve[1].equity == pytest.approx(9_900.0)
    assert curve[1].drawdown == pytest.approx(100.0)
    assert curve[2].equity == pytest.approx(10_100.0)
    assert curve[2].drawdown == pytest.approx(0.0)


def test_reihenfolge_wird_nach_ausstiegszeit_hergestellt():
    """Sonst haengt der Rueckgang davon ab, wie die Liste sortiert ankam."""
    trades = sequence(300.0, -400.0, 200.0)

    forward = M.summarize(trades, START)
    shuffled = M.summarize(list(reversed(trades)), START)

    assert forward.max_drawdown_usd == pytest.approx(shuffled.max_drawdown_usd)
    assert forward.net_pnl == pytest.approx(shuffled.net_pnl)


# -------------------------------------------------------------- Aufschluesselung
def test_aufschluesselung_trennt_die_gruppen_sauber():
    trades = [
        trade(200.0, setup_id=1, exit_ts=1, session="london"),
        trade(-100.0, setup_id=2, exit_ts=2, session="ny_am"),
        trade(-100.0, setup_id=3, exit_ts=3, session="ny_am"),
    ]

    table = M.breakdown(trades, lambda t: t.session, START)

    assert set(table) == {"london", "ny_am"}
    assert table["london"].trades == 1
    assert table["london"].net_pnl == pytest.approx(200.0)
    assert table["ny_am"].trades == 2
    assert table["ny_am"].expectancy_r == pytest.approx(-1.0)


def test_ausstiegsarten_werden_vollstaendig_gezaehlt():
    """Auch die Arten mit null Treffern - eine fehlende Zeile liest sich wie 'kommt nicht vor'."""
    trades = [
        trade(200.0, setup_id=1, exit_ts=1, reason=ExitReason.TARGET),
        trade(-100.0, setup_id=2, exit_ts=2, reason=ExitReason.STOP),
    ]

    counts = M.exit_counts(trades)

    assert counts == {"stop": 1, "target": 1, "time": 0, "end_of_data": 0}


def test_unaufgeloeste_trades_werden_ausgewiesen():
    trades = [
        trade(200.0, setup_id=1, exit_ts=1),
        trade(50.0, setup_id=2, exit_ts=2, reason=ExitReason.END_OF_DATA),
    ]

    assert M.summarize(trades, START).unresolved == 1


# --------------------------------------------------------------- Zeitraumteilung
def test_zeitraum_wird_nach_anteil_geteilt():
    trades = sequence(1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0)

    first, second = M.split_by_time(trades, 0.3)

    assert len(first) == 7
    assert len(second) == 3
    assert [t.pnl for t in second] == [8.0, 9.0, 10.0]


def test_teilung_laesst_nie_einen_abschnitt_leer():
    """Bei sehr wenigen Trades wuerde ein Anteil sonst auf 0 abrunden."""
    first, second = M.split_by_time(sequence(1.0, 2.0), 0.1)

    assert len(first) == 1
    assert len(second) == 1


def test_ohne_anteil_bleibt_alles_im_ersten_abschnitt():
    first, second = M.split_by_time(sequence(1.0, 2.0), 0.0)

    assert len(first) == 2
    assert second == ()
