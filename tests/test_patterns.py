"""Musterstatistik: findet sie Echtes, und laesst sie Zufall liegen?

Beides muss geprueft werden. Ein Verfahren, das nie etwas findet, besteht jeden
Test gegen Fehlalarme - und ist nutzlos. Deshalb steht neben jedem Test "Rausch
wird nicht gemeldet" ein Waechter-Test mit einem eingebauten Effekt, der
gefunden werden MUSS.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from tests.conftest import tradeable_config, trending_market
from tradex.backtest import patterns
from tradex.backtest.execution import SimulatedTrade
from tradex.backtest.runner import Backtester
from tradex.config import Config
from tradex.domain.bars import to_ns
from tradex.domain.enums import Direction, ExitReason
from tradex.domain.instruments import Instrument

START = datetime(2025, 1, 6, 15, 30, tzinfo=UTC)  # Montag
ALPHA = 0.05
MIN_TRADES = 30
MIN_OOS = 10


def make_trade(
    number: int,
    r_multiple: float,
    *,
    direction: Direction = Direction.BULLISH,
    symbol: str = "MNQ_PROXY",
    strategy: str = "chain",
    session: str = "RTH",
    htf_bias: str = "BULLISH",
    stop_ticks: float = 40.0,
    planned_rr: float = 2.0,
    minutes: int | None = None,
) -> SimulatedTrade:
    """Ein Trade mit frei waehlbarem Ergebnis - die Kursseite ist Beiwerk.

    Die Musterstatistik rechnet ausschliesslich ueber `r_multiple` und die
    Merkmale davor. Kurse, Stueckzahlen und USD-Betraege muessen deshalb nur
    zueinander passen, nicht zu einem realen Markt.
    """
    offset = timedelta(minutes=minutes if minutes is not None else number * 30)
    moment = START + offset
    trading_day = moment.date().toordinal()
    return SimulatedTrade(
        trade_id=number,
        setup_id=number,
        symbol=symbol,
        strategy=strategy,
        direction=direction,
        quantity=1,
        planned_entry=21_000.0,
        stop=20_990.0,
        target=21_020.0,
        planned_rr=planned_rr,
        planned_stop_ticks=stop_ticks,
        stop_anchor="retracement",
        target_source="liquidity",
        timeframe="5m",
        htf_bias=htf_bias,
        session=session,
        trading_day=trading_day,
        signal_ts=to_ns(moment),
        entry_ts=to_ns(moment),
        entry_index=number,
        entry_price=21_000.0,
        exit_ts=to_ns(moment + timedelta(minutes=20)),
        exit_index=number + 20,
        exit_price=21_000.0 + 10.0 * r_multiple,
        exit_reason=ExitReason.TARGET if r_multiple > 0 else ExitReason.STOP,
        bars_held=20,
        risk_points=10.0,
        risk_amount=20.0,
        gross_pnl=20.0 * r_multiple,
        commission=0.0,
        pnl=20.0 * r_multiple,
        r_multiple=r_multiple,
        mae_points=1.0,
        mfe_points=1.0,
    )


def analyse(trades, fraction: float = 0.3) -> patterns.PatternReport:
    return patterns.analyse(
        trades,
        out_of_sample_fraction=fraction,
        alpha=ALPHA,
        min_trades=MIN_TRADES,
        min_oos_trades=MIN_OOS,
    )


#: Vier Ergebnisse, die sich fast aufheben - Rauschen mit Streuung, aber ohne
#: Effekt. Nicht exakt null: ein Mittelwert von genau 0 waere ein Sonderfall,
#: an dem sich Rechenfehler verstecken koennen.
_NOISE = (1.0, -1.0, 0.6, -0.7)


def noise(count: int, start: int = 0) -> list[SimulatedTrade]:
    """Trades ohne jeden Effekt.

    Entscheidend ist, dass die Merkmale UNABHAENGIG vom Ergebnis wechseln:
    Richtung alle vier, Session alle acht Trades. Wuerde die Richtung im
    Gleichtakt mit dem Ergebnis springen, waere in den "Rauschdaten" ein
    kerngesunder Effekt versteckt - und der Test, der beweisen soll, dass
    nichts gefunden wird, bewiese das Gegenteil.
    """
    trades = []
    for i in range(count):
        trades.append(
            make_trade(
                start + i,
                _NOISE[i % 4],
                direction=Direction.BULLISH if (i // 4) % 2 == 0 else Direction.BEARISH,
                session="RTH" if (i // 8) % 2 == 0 else "ETH",
                minutes=(start + i) * 30,
            )
        )
    return trades


# ------------------------------------------------------- Blick in die Zukunft
class _Blind:
    """Ein Trade, der seine Ergebnisfelder verweigert.

    Damit laesst sich strukturell pruefen, was sonst nur eine Absichtserklaerung
    im Docstring waere: keine Bedingung darf etwas lesen, das erst nach dem
    Einstieg feststand. Eine Bedingung "Trades, die am Ziel schlossen" waere
    hochsignifikant und vollkommen wertlos.
    """

    def __init__(self, trade: SimulatedTrade) -> None:
        self._trade = trade

    def __getattr__(self, name: str):
        if name in patterns.OUTCOME_FIELDS:
            raise AssertionError(f"Bedingung liest das Ergebnisfeld '{name}'")
        return getattr(self._trade, name)


def test_keine_bedingung_liest_ein_ergebnisfeld():
    blind = [_Blind(t) for t in noise(60)]
    for condition in patterns.conditions_for(blind):
        for trade in blind:
            assert isinstance(condition.of(trade), str)


def test_die_gegenliste_deckt_die_ergebnisfelder_wirklich_ab():
    """Waechter: `OUTCOME_FIELDS` muss zu `SimulatedTrade` passen.

    Kaeme ein Ergebnisfeld hinzu, ohne hier eingetragen zu werden, liefe der
    Test darueber ins Leere - er wuerde ein Feld schuetzen, das es nicht gibt,
    und das neue uebersehen.
    """
    existing = set(SimulatedTrade.__slots__) | {"mae_r", "mfe_r", "is_win", "is_loss", "is_resolved"}
    assert existing >= patterns.OUTCOME_FIELDS


# ----------------------------------------------------------------- Waechter
def test_ein_eingebauter_effekt_wird_gefunden():
    """Ohne diesen Test bewiese keiner der anderen etwas.

    Long-Trades gewinnen hier deutlich, Short-Trades verlieren deutlich - und
    zwar durchgehend, auch im hinteren Abschnitt. Findet das Verfahren diesen
    Unterschied nicht, sind alle Aussagen "nichts nachweisbar" wertlos.
    """
    trades = []
    for i in range(240):
        long = i % 2 == 0
        value = (1.2 if i % 4 == 0 else 0.8) if long else (-1.2 if i % 4 == 1 else -0.8)
        trades.append(
            make_trade(i, value, direction=Direction.BULLISH if long else Direction.BEARISH)
        )

    report = analyse(trades)
    assert report.hypotheses > 0, "es wurde ueberhaupt nichts getestet"

    found = {(c.value, c.confirmed) for c in report.cells if c.condition == "side"}
    assert ("LONG", True) in found
    assert ("SHORT", True) in found
    assert report.survivors


def test_reines_rauschen_ueberlebt_die_korrektur_nicht():
    report = analyse(noise(300))
    assert report.hypotheses > 0
    assert not report.survivors
    assert not report.significant
    assert any("Kein Muster" in w for w in report.warnings)


def test_ein_einzelner_zufallstreffer_wird_von_der_korrektur_gedaempft():
    """Eine Gruppe mit schwachem Effekt darf unter vielen Tests nicht durchgehen.

    Der p-Wert der Gruppe liegt fuer sich genommen unter 5 %. Genau so entstehen
    Regeln wie "dienstags long": man sieht den einen Test, nicht die zwanzig
    daneben. Nach Korrektur muss die Gruppe durchfallen.
    """
    # Mittelwert +0,34 R bei einer Streuung von rund 1 R - fuer sich genommen
    # knapp signifikant, gemessen an der Zahl der Tests aber nichts.
    lifted = [
        make_trade(
            1000 + i,
            1.34 if i % 2 else -0.66,
            session="ASIA",
            minutes=i * 30 + 15,
        )
        for i in range(60)
    ]
    report = analyse(noise(300) + lifted)

    cell = next(c for c in report.cells if c.condition == "session" and c.value == "ASIA")
    assert cell.tested
    assert cell.p_value < ALPHA, "der Aufhaenger des Tests: fuer sich genommen signifikant"
    assert cell.q_value > cell.p_value
    assert not cell.significant, "unter der Korrektur darf davon nichts uebrig bleiben"
    assert not cell.confirmed


# ------------------------------------------------------------ Gruppengroessen
def test_zu_kleine_gruppen_werden_gezeigt_aber_nicht_getestet():
    trades = noise(200) + [
        make_trade(2000 + i, 3.0, session="ASIA", minutes=i * 30 + 15) for i in range(5)
    ]
    report = analyse(trades)
    asia = [c for c in report.cells if c.value == "ASIA"]
    assert asia and asia[0].trades <= 5
    assert not asia[0].tested
    assert asia[0].q_value == 1.0
    assert not asia[0].significant, "eine ungetestete Gruppe darf nie signifikant heissen"


def test_ungetestete_gruppen_zaehlen_nicht_in_die_korrektur():
    """Sonst verwaesserten viele Kleinstgruppen jeden echten Fund.

    Zwei Laeufe auf denselben Daten, einmal mit zusaetzlichen Splittergruppen:
    die Zahl der Hypothesen darf sich dadurch nicht erhoehen.
    """
    base = noise(200)
    splinters = [
        make_trade(3000 + i, 0.5, session=f"S{i}", minutes=i * 210 + 15) for i in range(20)
    ]
    assert analyse(base).hypotheses == analyse(base + splinters).hypotheses


# ------------------------------------------------------------- Bedingungsliste
def test_bedingungen_mit_nur_einem_wert_entfallen():
    """Eine Bedingung, die alle Trades in eine Gruppe legt, wiederholt nur das
    Gesamtergebnis - wuerde aber die Korrektur fuer alle anderen verschaerfen."""
    trades = [make_trade(i, 0.1 * (-1) ** i) for i in range(60)]
    names = {c.name for c in patterns.conditions_for(trades)}
    assert "strategy" not in names, "alle Trades stammen von derselben Strategie"
    assert "symbol" not in names
    assert "weekday" in names


def test_wochentag_folgt_dem_handelstag_nicht_dem_kalendertag():
    """Der Handelstag beginnt um 17:00 Boersenzeit.

    `trading_day` traegt diese Verschiebung bereits - wer stattdessen den
    Kalendertag aus `signal_ts` naehme, ordnete die Abendstunden dem falschen
    Wochentag zu.
    """
    trade = make_trade(0, 1.0)
    expected = patterns._WEEKDAYS[date.fromordinal(trade.trading_day).weekday()]
    condition = next(c for c in patterns.conditions_for(noise(60)) if c.name == "weekday")
    assert condition.of(trade) == expected


# --------------------------------------------------------------- Gegenprobe
def test_gruppengrenzen_stammen_nur_aus_dem_vorderen_abschnitt():
    """Sonst waere die Gegenprobe keine.

    Die Drittelgrenzen der Stopweite werden am vorderen Abschnitt bestimmt. Im
    hinteren liegen hier ausschliesslich sehr weite Stops - sie muessen
    vollstaendig in den obersten Eimer fallen, statt einen neuen zu bilden.
    """
    early = [
        make_trade(i, 0.2 * (-1) ** i, stop_ticks=20.0 + (i % 3) * 10.0, minutes=i * 30)
        for i in range(140)
    ]
    late = [
        make_trade(500 + i, 0.2 * (-1) ** i, stop_ticks=400.0, minutes=10_000 + i * 30)
        for i in range(60)
    ]
    report = analyse(early + late, fraction=0.3)

    cells = [c for c in report.cells if c.condition == "stop_ticks"]
    assert cells
    top = max(cells, key=lambda c: c.oos_trades)
    assert top.value.startswith("ueber")
    assert sum(c.oos_trades for c in cells) == report.out_of_sample_trades


def test_gegenprobe_mit_zu_wenigen_trades_gilt_nicht_als_bestanden():
    trades = []
    for i in range(200):
        long = i % 2 == 0
        value = (1.2 if i % 4 == 0 else 0.8) if long else (-1.2 if i % 4 == 1 else -0.8)
        trades.append(
            make_trade(i, value, direction=Direction.BULLISH if long else Direction.BEARISH)
        )
    # Fast kein hinterer Abschnitt: der Effekt bleibt signifikant, die
    # Gegenprobe ist aber nicht mehr aussagefaehig.
    report = patterns.analyse(
        trades,
        out_of_sample_fraction=0.02,
        alpha=ALPHA,
        min_trades=MIN_TRADES,
        min_oos_trades=MIN_OOS,
    )
    assert report.significant
    assert not report.survivors
    assert any("bestaetigt sich" in w for w in report.warnings)


# ------------------------------------------------------------------ Randfaelle
def test_ohne_trades_gibt_es_eine_erklaerung_statt_einer_tabelle():
    report = analyse([])
    assert report.cells == ()
    assert report.hypotheses == 0
    assert report.warnings


def test_bericht_ist_reproduzierbar():
    trades = noise(200)
    first, second = analyse(trades), analyse(list(reversed(trades)))
    assert patterns.render_text(first) == patterns.render_text(second)


def test_ausgabe_nennt_die_zahl_der_tests():
    """Ohne sie ist ein q-Wert nicht einzuordnen."""
    text = patterns.render_text(analyse(noise(200)))
    assert "Tests" in text
    assert "VERGANGENHEIT" in text


@pytest.mark.parametrize("count", [0, 1, 2, 31])
def test_kleine_mengen_stuerzen_nicht_ab(count: int):
    patterns.render_text(analyse(noise(count)))


# ------------------------------------------------------- gegen echte Trades
def test_laeuft_auf_trades_aus_einem_echten_backtest(config: Config, mnq: Instrument):
    """Zusammenspiel mit dem Rest: Konfiguration, Trade-Objekt, Ausgabe.

    Die Tests darueber arbeiten mit gebauten Trades - nachrechenbar, aber
    blind fuer Abweichungen zwischen `config.patterns` und `analyse()` oder
    fuer Merkmalswerte, die es in der Praxis gar nicht gibt.
    """
    tuned = tradeable_config(config)
    result = Backtester("MNQ", mnq, tuned).run(trending_market(60 * 24 * 6))
    assert result.trades, "Waechter: ohne Trades prueft dieser Test nichts"

    report = patterns.analyse(
        result.trades,
        out_of_sample_fraction=tuned.backtest.out_of_sample_fraction,
        alpha=tuned.patterns.alpha,
        min_trades=tuned.patterns.min_trades,
        min_oos_trades=tuned.patterns.min_out_of_sample_trades,
    )
    assert report.in_sample_trades + report.out_of_sample_trades == len(result.trades)
    assert all(cell.trades > 0 for cell in report.cells)
    assert patterns.render_text(report)
