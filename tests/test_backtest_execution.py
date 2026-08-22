"""Ausfuehrungssimulation: die vier Annahmen, an denen ein Backtest luegen kann.

Alle Faelle sind von Hand gerechnet. Genau hier entscheidet sich, ob eine
Backtest-Zahl etwas wert ist: ein Simulator, der guenstiger fuellt als die
Wirklichkeit, produziert widerspruchsfreie und trotzdem wertlose Statistiken.
Deshalb wird jeder Ausstieg gegen einen konkret ausgerechneten Kurs geprueft,
nicht nur gegen "irgendwo im Bereich".
"""

from __future__ import annotations

import pytest

from tests.conftest import make_series
from tradex.backtest.execution import OpenTrade
from tradex.config import BacktestConfig
from tradex.domain.enums import Direction, ExitReason
from tradex.domain.instruments import Instrument
from tradex.strategy.signal import TradeSignal

#: MNQ: 0.25 Punkte je Tick, 2 USD je Punkt. Ein Kontrakt, ein Tick = 0.50 USD.
COMMISSION = 0.74


def params(**overrides: object) -> BacktestConfig:
    base = {
        "entry_fill": "next_bar_open",
        "same_bar_resolution": "stop_first",
        "entry_slippage_ticks": 1.0,
        "stop_slippage_ticks": 1.0,
        "commission_per_contract": COMMISSION,
        "max_holding_bars": 0,
    }
    return BacktestConfig(**{**base, **overrides})


def long_signal(quantity: int = 2) -> TradeSignal:
    """LONG 21000 / Stop 20990 / Ziel 21020 - runde Zahlen zum Nachrechnen."""
    return TradeSignal(
        setup_id=1,
        symbol="MNQ",
        direction=Direction.BULLISH,
        entry=21000.0,
        stop=20990.0,
        target=21020.0,
        stop_ticks=40.0,
        target_points=20.0,
        rr=2.0,
        quantity=quantity,
        risk_amount=40.0,
        reward_amount=80.0,
        entry_ts=0,
        entry_index=0,
        stop_anchor="retracement",
        target_source="liquidity",
    )


def short_signal(quantity: int = 1) -> TradeSignal:
    return TradeSignal(
        setup_id=2,
        symbol="MNQ",
        direction=Direction.BEARISH,
        entry=21000.0,
        stop=21010.0,
        target=20980.0,
        stop_ticks=40.0,
        target_points=20.0,
        rr=2.0,
        quantity=quantity,
        risk_amount=20.0,
        reward_amount=40.0,
        entry_ts=0,
        entry_index=0,
        stop_anchor="retracement",
        target_source="liquidity",
    )


def open_trade(mnq: Instrument, signal: TradeSignal, config: BacktestConfig) -> OpenTrade:
    return OpenTrade(
        signal=signal,
        params=config,
        instrument=mnq,
        session="ny_am",
        trading_day=20250303,
        signal_index=0,
        htf_bias="bullish",
    )


# ------------------------------------------------------------------- Fuellung
def test_einstieg_rutscht_gegen_die_position(mnq: Instrument):
    """Gefuellt wird auf der Eroeffnung der Folgebar - plus Schlupf gegen sich.

    Zum Schlusskurs der Signalbar zu kaufen ist der haeufigste stille Fehler in
    Backtests: dieser Kurs ist bereits Vergangenheit, wenn das Signal entsteht.
    """
    bars = make_series([(21001.0, 21005.0, 21000.0, 21004.0)])
    trade = open_trade(mnq, long_signal(), params())

    trade.fill(bars[0], 0)

    assert trade.entry_price == pytest.approx(21001.25)  # 21001 + 1 Tick
    assert trade.filled


def test_short_rutscht_in_die_andere_richtung(mnq: Instrument):
    bars = make_series([(21001.0, 21005.0, 21000.0, 21004.0)])
    trade = open_trade(mnq, short_signal(), params())

    trade.fill(bars[0], 0)

    assert trade.entry_price == pytest.approx(21000.75)  # 21001 - 1 Tick


def test_signal_close_fuellt_zum_geplanten_kurs(mnq: Instrument):
    """Die ausdruecklich unrealistische Variante - sie existiert nur zum Vergleich."""
    bars = make_series([(21001.0, 21005.0, 21000.0, 21004.0)])
    trade = open_trade(mnq, long_signal(), params(entry_fill="signal_close"))

    trade.fill(bars[0], 0)

    assert trade.entry_price == pytest.approx(21000.25)  # geplanter Einstieg + Schlupf


def test_doppelte_fuellung_ist_ein_fehler(mnq: Instrument):
    bars = make_series([(21001.0, 21005.0, 21000.0, 21004.0)])
    trade = open_trade(mnq, long_signal(), params())
    trade.fill(bars[0], 0)

    with pytest.raises(ValueError, match="bereits gefuellt"):
        trade.fill(bars[0], 0)


# -------------------------------------------------------------------- Ausstieg
def test_ziel_fuellt_ohne_schlupf(mnq: Instrument):
    """Das Ziel ist eine Limit-Order: sie fuellt zum Kurs oder gar nicht."""
    bars = make_series(
        [
            (21001.0, 21005.0, 21000.0, 21004.0),   # Fuellung bei 21001.25
            (21004.0, 21022.0, 21003.0, 21021.0),   # Ziel 21020 erreicht
        ]
    )
    trade = open_trade(mnq, long_signal(quantity=2), params())
    trade.fill(bars[0], 0)
    assert trade.on_bar(bars[0], 0) is None

    done = trade.on_bar(bars[1], 1)

    assert done is not None
    assert done.exit_reason is ExitReason.TARGET
    assert done.exit_price == pytest.approx(21020.0)
    # 18.75 Punkte * 2 USD * 2 Kontrakte = 75 USD, minus 2 * 0.74 Gebuehr
    assert done.gross_pnl == pytest.approx(75.0)
    assert done.commission == pytest.approx(1.48)
    assert done.pnl == pytest.approx(73.52)
    # 1R = 21001.25 - 20990 = 11.25 Punkte = 45 USD
    assert done.risk_amount == pytest.approx(45.0)
    assert done.r_multiple == pytest.approx(73.52 / 45.0)


def test_stop_rutscht(mnq: Instrument):
    """Der Stop ist eine Market-Order. Ein Backtest ohne Schlupf uebertreibt."""
    bars = make_series(
        [
            (21001.0, 21005.0, 21000.0, 21004.0),
            (21004.0, 21004.0, 20988.0, 20989.0),   # Stop 20990 durchhandelt
        ]
    )
    trade = open_trade(mnq, long_signal(quantity=1), params())
    trade.fill(bars[0], 0)
    trade.on_bar(bars[0], 0)

    done = trade.on_bar(bars[1], 1)

    assert done is not None
    assert done.exit_reason is ExitReason.STOP
    assert done.exit_price == pytest.approx(20989.75)  # 20990 - 1 Tick
    # Ein Trade darf durch Schlupf und Gebuehren SCHLECHTER als -1R sein.
    assert done.r_multiple < -1.0


def test_kurssprung_ueber_den_stop_fuellt_am_eroeffnungskurs(mnq: Instrument):
    """Eroeffnet die Bar jenseits des Stops, gibt es den Stopkurs nicht mehr.

    Wer hier trotzdem am Stopkurs fuellt, blendet genau die Verluste aus, die
    ein Konto tatsaechlich gefaehrden.
    """
    bars = make_series(
        [
            (21001.0, 21005.0, 21000.0, 21004.0),
            (20970.0, 20975.0, 20965.0, 20972.0),   # Sprung tief unter den Stop
        ]
    )
    trade = open_trade(mnq, long_signal(quantity=1), params())
    trade.fill(bars[0], 0)
    trade.on_bar(bars[0], 0)

    done = trade.on_bar(bars[1], 1)

    assert done is not None
    assert done.exit_reason is ExitReason.STOP
    assert done.exit_price == pytest.approx(20969.75)  # Eroeffnung - 1 Tick
    assert done.r_multiple < -2.0


def test_stop_und_ziel_in_derselben_bar_nimmt_den_schlechteren_fall(mnq: Instrument):
    """OHLC sagt nicht, was zuerst kam. Voreingestellt gilt der Stop."""
    both = make_series(
        [
            (21001.0, 21005.0, 21000.0, 21004.0),
            (21004.0, 21025.0, 20985.0, 21010.0),   # beruehrt Stop UND Ziel
        ]
    )

    pessimistic = open_trade(mnq, long_signal(quantity=1), params())
    pessimistic.fill(both[0], 0)
    pessimistic.on_bar(both[0], 0)
    stopped = pessimistic.on_bar(both[1], 1)

    optimistic = open_trade(mnq, long_signal(quantity=1), params(same_bar_resolution="target_first"))
    optimistic.fill(both[0], 0)
    optimistic.on_bar(both[0], 0)
    hit_target = optimistic.on_bar(both[1], 1)

    assert stopped is not None and stopped.exit_reason is ExitReason.STOP
    assert hit_target is not None and hit_target.exit_reason is ExitReason.TARGET
    assert stopped.pnl < hit_target.pnl


def test_zeitstop_beendet_die_position(mnq: Instrument):
    """Ohne Zeitstop bleibt eine Position im Seitwaertsmarkt beliebig lange offen."""
    bars = make_series(
        [(21001.0, 21005.0, 21000.0, 21004.0)] + [(21004.0, 21006.0, 21002.0, 21003.0)] * 5
    )
    trade = open_trade(mnq, long_signal(quantity=1), params(max_holding_bars=3))
    trade.fill(bars[0], 0)

    done = None
    for index in range(len(bars)):
        done = trade.on_bar(bars[index], index)
        if done is not None:
            break

    assert done is not None
    assert done.exit_reason is ExitReason.TIME
    assert done.bars_held == 3
    assert done.exit_price == pytest.approx(21002.75)  # Schlusskurs - 1 Tick Schlupf


def test_datenende_ist_kein_regelausstieg(mnq: Instrument):
    bars = make_series([(21001.0, 21005.0, 21000.0, 21004.0)])
    trade = open_trade(mnq, long_signal(quantity=1), params())
    trade.fill(bars[0], 0)

    done = trade.force_close(bars[0], 0, ExitReason.END_OF_DATA)

    assert done.exit_reason is ExitReason.END_OF_DATA
    assert not done.is_resolved


def test_short_wird_symmetrisch_behandelt(mnq: Instrument):
    bars = make_series(
        [
            (21001.0, 21002.0, 20998.0, 20999.0),   # Fuellung bei 21000.75
            (20999.0, 21000.0, 20978.0, 20979.0),   # Ziel 20980 erreicht
        ]
    )
    trade = open_trade(mnq, short_signal(quantity=1), params())
    trade.fill(bars[0], 0)
    assert trade.on_bar(bars[0], 0) is None

    done = trade.on_bar(bars[1], 1)

    assert done is not None
    assert done.exit_reason is ExitReason.TARGET
    assert done.exit_price == pytest.approx(20980.0)
    assert done.gross_pnl == pytest.approx((21000.75 - 20980.0) * 2.0)
    assert done.pnl > 0


# ------------------------------------------------------------------- MAE / MFE
def test_mae_und_mfe_messen_den_verlauf_zwischen_ein_und_ausstieg(mnq: Instrument):
    """Wie weit lief es gegen die Position, bevor sie aufging?

    Die Zahl beantwortet spaeter die Frage, ob ein engerer Stop ueberhaupt
    haltbar gewesen waere - ohne sie ist jede Stop-Diskussion Meinung.
    """
    bars = make_series(
        [
            (21001.0, 21005.0, 20995.0, 21004.0),   # Fuellung 21001.25, Tief 20995
            (21004.0, 21022.0, 21003.0, 21021.0),   # Hoch 21022, Ziel 21020
        ]
    )
    trade = open_trade(mnq, long_signal(quantity=1), params())
    trade.fill(bars[0], 0)
    trade.on_bar(bars[0], 0)

    done = trade.on_bar(bars[1], 1)

    assert done is not None
    assert done.mae_points == pytest.approx(21001.25 - 20995.0)
    assert done.mfe_points == pytest.approx(21022.0 - 21001.25)
    assert done.mae_r == pytest.approx(done.mae_points / done.risk_points)


def test_ausstieg_vor_fuellung_ist_ein_fehler(mnq: Instrument):
    bars = make_series([(21001.0, 21005.0, 21000.0, 21004.0)])
    trade = open_trade(mnq, long_signal(), params())

    with pytest.raises(ValueError, match="noch nicht gefuellt"):
        trade.on_bar(bars[0], 0)


def test_geschlossene_position_nimmt_keine_bars_mehr_an(mnq: Instrument):
    """Sonst wuerde ein Buchungsfehler im Aufrufer denselben Trade mehrfach zaehlen."""
    bars = make_series(
        [
            (21001.0, 21005.0, 21000.0, 21004.0),
            (21004.0, 21022.0, 21003.0, 21021.0),
        ]
    )
    trade = open_trade(mnq, long_signal(quantity=1), params())
    trade.fill(bars[0], 0)
    trade.on_bar(bars[0], 0)
    assert trade.on_bar(bars[1], 1) is not None

    with pytest.raises(ValueError, match="bereits geschlossen"):
        trade.on_bar(bars[1], 1)
