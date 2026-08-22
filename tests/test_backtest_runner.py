"""Der Backtest-Lauf als Ganzes (Spec §19, §29).

Der wichtigste Test dieser Datei ist `test_backtest_faellt_dieselben_
entscheidungen_wie_die_strategie`. Er ist die technische Fassung von Spec §29
("Backtest ≡ Live"): der Backtest darf die Regeln nicht anders auslegen als der
Live-Pfad, er darf ihnen nur die Ausfuehrung hinzufuegen. Faellt dieser Test,
ist jede Backtest-Aussage wertlos, egal wie gut sie aussieht.

Danach folgen Eigenschaften, die IMMER gelten muessen - unabhaengig davon,
welche Daten hineinlaufen.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from tests.conftest import tradeable_config, trending_market
from tradex.analysis.context import MarketContext
from tradex.backtest.report import build
from tradex.backtest.runner import Backtester
from tradex.config import BacktestConfig, Config, RiskConfig
from tradex.domain.bars import BarSeries
from tradex.domain.enums import Direction, ExitReason, Timeframe
from tradex.domain.instruments import Instrument
from tradex.strategy.registry import build_portfolio

SYMBOL = "MNQ"
DAYS = 60 * 24 * 12


def _with_backtest(config: Config, **overrides: object) -> Config:
    return Config(
        **{
            **config.model_dump(),
            "backtest": BacktestConfig(**{**config.backtest.model_dump(), **overrides}),
        }
    )


def _without_risk_feedback(config: Config) -> Config:
    """Risikogrenzen aus - dann haengt keine Entscheidung am Ergebnis der vorigen.

    Nur so sind Backtest und blanker Strategielauf ueberhaupt vergleichbar:
    der Backtest fuellt das Risikobuch, der blanke Lauf nicht.
    """
    return Config(
        **{
            **config.model_dump(),
            "risk": RiskConfig(**{**config.risk.model_dump(), "enabled": False}),
        }
    )


@pytest.fixture(scope="module")
def series() -> BarSeries:
    return trending_market(DAYS)


@pytest.fixture(scope="module")
def tuned(config: Config) -> Config:
    return tradeable_config(config)


@pytest.fixture(scope="module")
def result(tuned: Config, mnq: Instrument, series: BarSeries):
    return Backtester(SYMBOL, mnq, tuned).run(series)


# --------------------------------------------------- Invariante 3: ein Pfad
def test_backtest_faellt_dieselben_entscheidungen_wie_die_strategie(
    tuned: Config, mnq: Instrument, series: BarSeries
):
    """Spec §29: der Backtest legt die Regeln nicht neu aus.

    Verglichen wird gegen den blanken Strategielauf ohne jede Simulation. Bis
    auf die Ausfuehrung muss beides Zeichen fuer Zeichen dasselbe ergeben.
    """
    plain = _without_risk_feedback(tuned)

    context = MarketContext(SYMBOL, mnq, plain)
    engine = build_portfolio(SYMBOL, mnq, plain)
    for bar in series:
        updates = context.on_base_bar(bar)
        if updates:
            engine.on_updates(updates, context)

    backtest = Backtester(SYMBOL, mnq, plain).run(series)

    def fingerprint(decisions) -> list[tuple]:
        return [
            (d.ts, d.setup_id, d.decision, d.stage, d.blocking_reason) for d in decisions
        ]

    assert fingerprint(engine.decisions) == fingerprint(backtest.decisions)
    # Jedes Signal des blanken Laufs wird im Backtest gefuellt, verfaellt am
    # Datenende oder wird als veraltet verworfen - verschwinden darf keines.
    assert len(engine.signals) == len(backtest.trades) + backtest.unfilled + backtest.stale
    # Abgeglichen wird ueber die kontoweite Kennung, nicht ueber die Position
    # in der Liste: Trades werden in AUSSTIEGS-Reihenfolge gesammelt, Signale
    # in Signalreihenfolge. Mit mehreren Strategien faellt beides auseinander.
    planned = {s.trade_id: s.entry for s in engine.signals}
    for trade in backtest.trades:
        assert planned[trade.trade_id] == trade.planned_entry, trade


def test_es_entstehen_ueberhaupt_trades(result):
    """Waechter gegen leere Wahrheit: ohne Trades sind alle Aussagen unten trivial."""
    assert result.signals > 0, "die Pflichtkette wurde nie vollstaendig"
    assert len(result.trades) > 0, "kein Signal wurde je gefuellt"


def test_zwei_laeufe_liefern_identische_trades(tuned: Config, mnq: Instrument, series: BarSeries):
    """Ohne Determinismus waere jede Backtest-Aussage nicht nachpruefbar."""

    def fingerprint(res) -> list[tuple]:
        return [
            (t.setup_id, t.entry_ts, t.entry_price, t.exit_ts, t.exit_price, t.exit_reason)
            for t in res.trades
        ]

    first = Backtester(SYMBOL, mnq, tuned).run(series)
    second = Backtester(SYMBOL, mnq, tuned).run(series)

    assert fingerprint(first) == fingerprint(second)
    assert first.net_pnl == second.net_pnl


# ------------------------------------------------------------- Kein Look-ahead
def test_einstieg_liegt_immer_nach_dem_signal(result):
    """Die Order kann fruehestens auf der Bar NACH ihrem Signal gefuellt werden.

    Das ist der Unterschied zwischen einem Backtest und einer Zeitmaschine.
    """
    for trade in result.trades:
        assert trade.entry_ts > trade.signal_ts, trade


def test_ausstieg_liegt_nie_vor_dem_einstieg(result):
    for trade in result.trades:
        assert trade.exit_ts >= trade.entry_ts, trade
        assert trade.bars_held >= 0, trade


# ------------------------------------------------------------- Ausfuehrung
def test_stopausstiege_sind_nie_besser_als_der_stop(result):
    """Ein Stop, der guenstiger fuellt als sein Kurs, ist ein geschoenter Backtest."""
    for trade in result.trades:
        if trade.exit_reason is not ExitReason.STOP:
            continue
        if trade.direction is Direction.BULLISH:
            assert trade.exit_price <= trade.stop, trade
        else:
            assert trade.exit_price >= trade.stop, trade


def test_zielausstiege_erreichen_das_ziel(result):
    for trade in result.trades:
        if trade.exit_reason is not ExitReason.TARGET:
            continue
        if trade.direction is Direction.BULLISH:
            assert trade.exit_price >= trade.target, trade
        else:
            assert trade.exit_price <= trade.target, trade


def test_jeder_trade_hat_ein_eingegangenes_risiko(result):
    """Ohne 1R gibt es kein R-Vielfaches - und damit keine vergleichbare Statistik."""
    for trade in result.trades:
        assert trade.risk_points > 0, trade
        assert trade.risk_amount > 0, trade
        assert trade.quantity >= 1, trade


def test_gebuehren_werden_immer_abgezogen(result, tuned: Config):
    expected = tuned.backtest.commission_per_contract
    for trade in result.trades:
        assert trade.commission == pytest.approx(expected * trade.quantity)
        assert trade.pnl == pytest.approx(trade.gross_pnl - trade.commission)


def test_pessimistische_annahme_ist_nie_besser_als_die_optimistische(
    tuned: Config, mnq: Instrument, series: BarSeries
):
    """`stop_first` darf pro Trade nie mehr einbringen als `target_first`.

    Waere es anders, wuerde die als "vorsichtig" bezeichnete Einstellung das
    Ergebnis beschoenigen - also genau das Gegenteil dessen tun, wofuer sie da ist.
    """
    plain = _without_risk_feedback(tuned)
    pessimistic = Backtester(SYMBOL, mnq, _with_backtest(plain)).run(series)
    optimistic = Backtester(
        SYMBOL, mnq, _with_backtest(plain, same_bar_resolution="target_first")
    ).run(series)

    # Ueber trade_id, nicht setup_id: letztere zaehlt je Strategie und
    # kollidiert zwischen ihnen.
    by_id = {t.trade_id: t for t in optimistic.trades}
    assert by_id, "ohne Trades sagt der Vergleich nichts"
    for trade in pessimistic.trades:
        counterpart = by_id[trade.trade_id]
        assert trade.pnl <= counterpart.pnl + 1e-9, trade


def test_kosten_verschlechtern_das_ergebnis(tuned: Config, mnq: Instrument, series: BarSeries):
    plain = _without_risk_feedback(tuned)
    with_costs = Backtester(SYMBOL, mnq, _with_backtest(plain)).run(series)
    free = Backtester(
        SYMBOL,
        mnq,
        _with_backtest(
            plain, commission_per_contract=0.0, entry_slippage_ticks=0.0, stop_slippage_ticks=0.0
        ),
    ).run(series)

    assert with_costs.net_pnl < free.net_pnl


def test_zeitstop_wird_eingehalten(tuned: Config, mnq: Instrument, series: BarSeries):
    """Ohne Zeitstop kann eine Position ueber Tage offen bleiben - das passt zu
    keinem Intraday-Modell und macht die Haltedauer-Statistik unbrauchbar."""
    limit = 5
    limited = Backtester(SYMBOL, mnq, _with_backtest(tuned, max_holding_bars=limit)).run(series)

    assert limited.trades, "ohne Trades sagt der Test nichts"
    for trade in limited.trades:
        assert trade.bars_held <= limit, trade
    assert any(t.exit_reason is ExitReason.TIME for t in limited.trades), (
        "kein einziger Trade lief in den Zeitstop - er ist damit wirkungslos"
    )


# ------------------------------------------------------------- Datenluecken
def test_signal_ueber_einer_datenluecke_wird_verworfen(
    tuned: Config, mnq: Instrument, series: BarSeries
):
    """Gefunden an echten Daten: Juneteenth 2023.

    Der Handel endete um 11:58 CT und lief erst um 17:00 CT weiter. Die
    Signalbar von 11:58 gilt erst als geschlossen, wenn die naechste Bar
    eintrifft - also fuenf Stunden spaeter. Ohne diese Regel wurde daraus ein
    Einstieg um 17:01 auf Grundlage eines Kursverlaufs vom Vormittag: ein
    Trade, den es nie gab, mit -1,50 R im Ergebnis.

    Hier nachgestellt, indem aus einer Serie mit Trades ein Block
    herausgeschnitten wird. Geprueft wird die Eigenschaft, nicht der Einzelfall:
    zwischen Signal und Fuellung darf nie mehr Zeit liegen als erlaubt.
    """
    interval_ns = tuned.data.base_timeframe.seconds * 1_000_000_000
    grace_ns = tuned.backtest.max_signal_age_bars * interval_ns

    # Jede zehnte Stunde faellt aus - das erzeugt reichlich Luecken mitten im
    # Geschehen, ohne die Kursreihe selbst zu veraendern.
    with_gaps = BarSeries()
    for i, bar in enumerate(series):
        if (i // 60) % 10 == 9:
            continue
        with_gaps.append_bar(bar)

    result = Backtester(SYMBOL, mnq, tuned).run(with_gaps)

    assert result.signals > 0, "ohne Signale sagt der Test nichts"
    for trade in result.trades:
        # Erlaubt ist die Dauer der Signalbar plus die Frist danach. Die
        # Signalbar-Dauer gehoert dazu, weil der Timestamp ihre EROEFFNUNG
        # bezeichnet - eine 5m-Bar ist beim Schliessen schon 5 Minuten alt.
        allowed = Timeframe.parse(trade.timeframe).seconds * 1_000_000_000 + grace_ns
        age = trade.entry_ts - trade.signal_ts
        assert age <= allowed, (
            f"{trade.strategy} #{trade.setup_id} wurde {age / 60e9:.0f} Minuten nach "
            f"dem Signal gefuellt, erlaubt sind {allowed / 60e9:.0f}"
        )


def test_verworfene_signale_belegen_keinen_risikoplatz(
    tuned: Config, mnq: Instrument, series: BarSeries
):
    """Eine nie gefuellte Order darf nicht als genommener Trade zaehlen.

    Sonst wuerde ein Feiertag das Tageslimit aufbrauchen, ohne dass je eine
    Position bestand.
    """
    trimmed = BarSeries()
    for i, bar in enumerate(series):
        if (i // 60) % 10 == 9:
            continue
        trimmed.append_bar(bar)

    backtester = Backtester(SYMBOL, mnq, tuned)
    result = backtester.run(trimmed)

    assert backtester.ledger.open_count == 0
    assert len(result.trades) + result.unfilled + result.stale == result.signals


# --------------------------------------------------------------- Risikobuch
def test_das_risikobuch_begrenzt_die_trades_pro_tag(
    config: Config, mnq: Instrument, series: BarSeries
):
    """Genau das kann der blanke Strategielauf NICHT: er kennt keine Ergebnisse.

    Erst wenn der Backtest seine simulierten Positionen ins selbe Buch schreibt,
    wirken Tagesgrenzen ueberhaupt - sonst waere jede Aussage ueber das
    Risikoverhalten aus dem Backtest wertlos.
    """
    tuned = tradeable_config(config)
    strict = Config(
        **{
            **tuned.model_dump(),
            "risk": RiskConfig(
                **{**tuned.risk.model_dump(), "max_trades_per_day": 1, "max_open_positions": 1}
            ),
        }
    )

    result = Backtester(SYMBOL, mnq, strict).run(series)

    per_day: dict[int, int] = {}
    for trade in result.trades:
        per_day[trade.trading_day] = per_day.get(trade.trading_day, 0) + 1
    assert per_day, "ohne Trades sagt der Test nichts"
    assert max(per_day.values()) == 1


def test_nur_eine_position_gleichzeitig(config: Config, mnq: Instrument, series: BarSeries):
    """Zwischen Signal und Fuellung liegt eine Bar - auch in diesem Fenster
    darf keine zweite Position dieselbe freie Stelle belegen."""
    tuned = tradeable_config(config)
    strict = Config(
        **{
            **tuned.model_dump(),
            "risk": RiskConfig(
                **{**tuned.risk.model_dump(), "max_open_positions": 1, "max_trades_per_day": 50}
            ),
        }
    )

    result = Backtester(SYMBOL, mnq, strict).run(series)

    assert result.trades, "ohne Trades sagt der Test nichts"
    spans = sorted((t.entry_ts, t.exit_ts) for t in result.trades)
    for (_, first_exit), (second_entry, _) in pairwise(spans):
        assert second_entry >= first_exit, "zwei Positionen ueberlappten sich"


# ------------------------------------------------------------------- Bericht
def test_bericht_warnt_bei_zu_kleiner_stichprobe(result, tuned: Config):
    report = build(result, tuned)

    assert report.min_trades == tuned.backtest.min_trades_for_significance
    if report.overall.trades < report.min_trades:
        assert not report.is_significant
        assert any(w.startswith("Zu wenige Trades") for w in report.warnings)


def test_bericht_haelt_die_annahmen_fest(result, tuned: Config):
    """Ein Ergebnis ohne seine Ausfuehrungsannahmen ist nicht interpretierbar."""
    report = build(result, tuned)

    assert report.assumptions["entry_fill"] == tuned.backtest.entry_fill
    assert report.assumptions["commission_per_contract"] == tuned.backtest.commission_per_contract
    assert report.assumptions["stop_anchor"] == tuned.stops.anchor


def test_bericht_bleibt_auch_bei_vielen_trades_klein(result, mnq: Instrument, config: Config):
    small = Config(
        **{
            **tradeable_config(config).model_dump(),
            "backtest": BacktestConfig(**{**config.backtest.model_dump(), "max_report_trades": 3}),
        }
    )
    report = build(result, small)

    assert len(report.trades) <= 3
    assert len(report.equity) <= 3
    assert report.trades_total == len(result.trades)


def test_summe_der_gruppen_ergibt_das_ganze(result, tuned: Config):
    """Eine Aufschluesselung, die Trades verliert, fuehrt in die Irre."""
    report = build(result, tuned)

    for table in (report.by_session, report.by_direction, report.by_exit):
        assert sum(m.trades for m in table.values()) == report.overall.trades
        assert sum(m.net_pnl for m in table.values()) == pytest.approx(report.overall.net_pnl)
