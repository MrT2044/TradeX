"""Mehrere Instrumente an einem Konto (Phase 4b).

Der Punkt ist nicht, dass zwei Symbole doppelt so viele Trades ergeben - das
waere trivial. Es geht um das, was NUR im Zusammenspiel schiefgehen kann:

    - Teilen sich beide wirklich ein Risikobuch?
    - Werden die Bars chronologisch verschraenkt, oder sieht das zweite Symbol
      beim Start bereits die Ergebnisse des ersten?
    - Ist der Einzelfall wirklich derselbe Codepfad wie der Mehrfachfall?

Die letzte Frage ist die wichtigste: gaebe es zwei Pfade, waere Spec §29 eine
Ebene hoeher gebrochen.
"""

from __future__ import annotations

from datetime import timedelta
from itertools import pairwise

import pytest

from tests.conftest import DEFAULT_START, tradeable_config, trending_market
from tradex.backtest.report import build
from tradex.backtest.runner import Backtester, run_multi_backtest
from tradex.config import Config, RiskConfig
from tradex.domain.bars import BarSeries, to_ns
from tradex.domain.instruments import Instrument

A = "MNQ"
B = "NQ"


def _shifted(minutes: int, seed: int, offset: timedelta = timedelta()) -> BarSeries:
    """Zweite Kursreihe mit eigenem Verlauf, aber demselben Zeitraster."""
    source = trending_market(minutes, seed=seed)
    shifted = BarSeries()
    for i, bar in enumerate(source):
        shifted.append(
            to_ns(DEFAULT_START + offset + timedelta(minutes=i)),
            bar.open,
            bar.high,
            bar.low,
            bar.close,
            bar.volume,
        )
    return shifted


@pytest.fixture(scope="module")
def tuned(config: Config) -> Config:
    return tradeable_config(config)


@pytest.fixture(scope="module")
def data() -> dict[str, BarSeries]:
    return {A: trending_market(60 * 24 * 10), B: _shifted(60 * 24 * 10, seed=11)}


@pytest.fixture(scope="module")
def pair(instruments: dict[str, Instrument]) -> dict[str, Instrument]:
    return {A: instruments["MNQ"], B: instruments["NQ"]}


@pytest.fixture(scope="module")
def result(tuned: Config, pair: dict[str, Instrument], data: dict[str, BarSeries]):
    return run_multi_backtest(pair, tuned, data)


# ------------------------------------------------------------------ Ein Pfad
def test_ein_instrument_liefert_dasselbe_wie_vorher(
    tuned: Config, mnq: Instrument, data: dict[str, BarSeries]
):
    """Der Einzelfall MUSS derselbe Codepfad sein wie der Mehrfachfall.

    Gaebe es zwei Wege, koennten sie auseinanderlaufen - und der Backtest
    saegte an dem Ast, auf dem seine eigene Glaubwuerdigkeit sitzt.
    """
    single = Backtester(A, mnq, tuned).run(data[A])
    via_many = run_multi_backtest({A: mnq}, tuned, {A: data[A]})

    def fingerprint(res) -> list[tuple]:
        return [(t.trade_id, t.entry_ts, t.entry_price, t.exit_ts, t.exit_price) for t in res.trades]

    assert fingerprint(single) == fingerprint(via_many)
    assert single.net_pnl == via_many.net_pnl
    assert single.symbols == via_many.symbols == (A,)


# ------------------------------------------------------------- Verschraenkung
def test_bars_werden_chronologisch_verschraenkt(result):
    """Sonst saehe das gemeinsame Risikobuch beim zweiten Symbol die Zukunft.

    Nachweis ueber die Trades: waeren die Symbole nacheinander gelaufen, laege
    das erste vollstaendig vor dem zweiten.
    """
    assert result.is_multi_symbol
    by_symbol = {}
    for trade in result.trades:
        by_symbol.setdefault(trade.symbol, []).append(trade.entry_ts)
    assert len(by_symbol) == 2, "ein Symbol hat nie gehandelt - der Test sagt nichts"

    first, second = (sorted(v) for v in by_symbol.values())
    # Die Zeitraeume muessen sich ueberlappen, nicht aufeinanderfolgen.
    assert first[0] < second[-1] and second[0] < first[-1]


def test_beide_instrumente_liefern_trades(result):
    """Waechter gegen leere Wahrheit."""
    symbols = {t.symbol for t in result.trades}
    assert symbols == {A, B}


def test_mehr_instrumente_ergeben_mehr_trades(
    tuned: Config, pair: dict[str, Instrument], data: dict[str, BarSeries], mnq: Instrument
):
    """Der ganze Zweck: die Stichprobe waechst, ohne dass eine Regel sich aendert."""
    alone = Backtester(A, mnq, tuned).run(data[A])
    together = run_multi_backtest(pair, tuned, data)

    assert len(together.trades) > len(alone.trades)


# --------------------------------------------------------- Gemeinsames Konto
def test_das_risikobuch_gilt_fuer_beide_instrumente(
    config: Config, pair: dict[str, Instrument], data: dict[str, BarSeries]
):
    """`max_open_positions` gilt fuer das KONTO, nicht je Instrument.

    Ohne gemeinsames Buch saehe jedes Symbol "null offene Positionen" und
    beide kaemen durch - das doppelte Risiko bei formal korrekter Rechnung.
    """
    tuned = tradeable_config(config)
    strict = Config(
        **{
            **tuned.model_dump(),
            "risk": RiskConfig(
                **{**tuned.risk.model_dump(), "max_open_positions": 1, "max_trades_per_day": 50}
            ),
        }
    )

    result = run_multi_backtest(pair, strict, data)

    assert result.trades, "ohne Trades sagt der Test nichts"
    spans = sorted((t.entry_ts, t.exit_ts) for t in result.trades)
    for (_, first_exit), (second_entry, _) in pairwise(spans):
        assert second_entry >= first_exit, (
            "zwei Positionen ueberlappten sich - das Risikobuch ist nicht gemeinsam"
        )


def test_die_kennung_bleibt_ueber_instrumente_hinweg_eindeutig(result):
    ids = [t.trade_id for t in result.trades]
    assert len(ids) == len(set(ids))


# ------------------------------------------------------------------- Bericht
def test_bericht_schluesselt_nach_instrument_auf(result, tuned: Config):
    report = build(result, tuned)

    assert set(report.by_symbol) == {A, B}
    assert sum(m.trades for m in report.by_symbol.values()) == report.overall.trades
    assert sum(m.net_pnl for m in report.by_symbol.values()) == pytest.approx(
        report.overall.net_pnl
    )


def test_unbekanntes_symbol_wird_abgelehnt(tuned: Config, mnq: Instrument, data: dict[str, BarSeries]):
    tester = Backtester(A, mnq, tuned)
    with pytest.raises(ValueError, match="Keine Buecher"):
        tester.run_many({B: data[B]})


def test_lauf_ohne_bars_wird_abgelehnt(tuned: Config, mnq: Instrument):
    with pytest.raises(ValueError, match="ohne Bars"):
        Backtester(A, mnq, tuned).run(BarSeries())
