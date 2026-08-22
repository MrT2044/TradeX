"""Papertrading: handelt es wie der Backtest - und haelt es an, wenn es muss?

Der wichtigste Test dieser Datei ist der erste. Faellt er, ist der ganze
Aufbau von Phase 5 hinfaellig: dann waeren Backtest und laufender Betrieb zwei
verschiedene Programme, und keine Backtest-Aussage sagte etwas ueber den
Betrieb aus.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from tests.conftest import tradeable_config, trending_market
from tradex.analysis import reasons as R
from tradex.backtest.runner import Backtester
from tradex.config import Config
from tradex.domain.bars import Bar, BarSeries
from tradex.domain.enums import ExitReason
from tradex.domain.instruments import Instrument
from tradex.live.feed import BarMessage, HeartbeatMessage, StatusMessage
from tradex.live.replay_feed import ReplayFeed
from tradex.live.runner import SessionRunner
from tradex.live.session import (
    HALT_FEED_DISCONNECTED,
    HALT_FEED_STALE,
    HALT_MANUAL,
    HALT_NOT_CONNECTED,
    SessionConfig,
    TradingSession,
)
from tradex.live.store import SessionStore
from tradex.persistence.db import init_database

SYMBOL = "MNQ"
MINUTES = 60 * 24 * 6
SECOND = 1_000_000_000


class Clock:
    """Eine Uhr, die der Test stellt.

    Eine Ausfallerkennung, die sich nur in Echtzeit pruefen laesst, wird nicht
    geprueft - ein Test, der 15 Sekunden schlaeft, fliegt beim ersten
    Aufraeumen raus.
    """

    def __init__(self, start: int = 1_700_000_000 * SECOND) -> None:
        self.now = start

    def __call__(self) -> int:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += int(seconds * SECOND)


@pytest.fixture(scope="module")
def series() -> BarSeries:
    return trending_market(MINUTES)


@pytest.fixture(scope="module")
def tuned(config: Config) -> Config:
    return tradeable_config(config)


def connect(session: TradingSession) -> TradingSession:
    """Jede Sitzung beginnt angehalten - erst der Feed macht sie handlungsfaehig."""
    session.on_message(StatusMessage(ts=session.clock(), connected=True, detail="Test"))
    return session


def feed_all(session: TradingSession, series: BarSeries, symbol: str = SYMBOL) -> None:
    connect(session)
    for bar in series:
        session.on_bar(symbol, bar)


# ------------------------------------------------- Invariante 3, eine Ebene hoeher
def test_papertrading_faellt_dieselben_entscheidungen_wie_der_backtest(
    tuned: Config, mnq: Instrument, series: BarSeries
):
    """Spec Paragraph 29 fuer den laufenden Betrieb.

    Die Sitzung schiebt dieselben Bars durch dieselbe Klasse - herauskommen
    muessen dieselben Trades, Feld fuer Feld. Waere die Ausfuehrung fuer den
    Betrieb neu geschrieben worden, koennte dieser Test nicht bestehen; genau
    deshalb ist er der Grund, sie NICHT neu zu schreiben.
    """
    backtest = Backtester(SYMBOL, mnq, tuned).run(series)
    session = TradingSession({SYMBOL: mnq}, tuned, feed_name="test", clock=Clock())
    feed_all(session, series)

    # Der Backtest schliesst am Datenende zwangsweise, was noch laeuft. Die
    # Sitzung tut das bewusst nicht - solche Trades gehoeren nicht verglichen.
    erwartet = [t for t in backtest.trades if t.exit_reason is not ExitReason.END_OF_DATA]

    assert session.trades, "Waechter: ohne Trades prueft dieser Test nichts"
    assert len(session.trades) == len(erwartet)
    assert session.trades == erwartet, "Papertrading weicht vom Backtest ab"


def test_signale_und_ablehnungen_stimmen_ebenfalls_ueberein(
    tuned: Config, mnq: Instrument, series: BarSeries
):
    """Nicht nur die Trades - auch die Entscheidungen dahinter."""
    backtest = Backtester(SYMBOL, mnq, tuned).run(series)
    session = TradingSession({SYMBOL: mnq}, tuned, feed_name="test", clock=Clock())
    feed_all(session, series)

    live = next(iter(session.books.values())).book.strategy
    fingerprint = lambda ds: [(d.ts, d.setup_id, d.decision, d.stage) for d in ds]  # noqa: E731
    assert fingerprint(live.decisions) == fingerprint(backtest.decisions)


# ------------------------------------------------------------------- Not-Aus
def test_eine_frische_sitzung_haelt_still_bis_der_feed_sich_meldet(
    tuned: Config, mnq: Instrument, series: BarSeries
):
    """Sonst handelte sie auf Bars aus einer Quelle, die nie bestaetigt hat."""
    session = TradingSession({SYMBOL: mnq}, tuned, "test", clock=Clock())
    assert session.halted_reason == HALT_NOT_CONNECTED

    for bar in list(series)[:2000]:
        session.on_bar(SYMBOL, bar)
    assert not next(iter(session.books.values())).book.strategy.signals

    connect(session)
    assert not session.halted_reason


def test_stille_haelt_die_sitzung_an(tuned: Config, mnq: Instrument):
    clock = Clock()
    session = connect(
        TradingSession(
            {SYMBOL: mnq}, tuned, "test", SessionConfig(max_silence_seconds=15.0), clock
        )
    )
    session.check_liveness()
    assert not session.halted_reason

    clock.advance(14.0)
    session.check_liveness()
    assert not session.halted_reason, "unter der Grenze darf nichts passieren"

    clock.advance(2.0)
    session.check_liveness()
    assert session.halted_reason == HALT_FEED_STALE


def test_eine_nachricht_setzt_die_stille_zurueck(tuned: Config, mnq: Instrument):
    clock = Clock()
    session = connect(
        TradingSession(
            {SYMBOL: mnq}, tuned, "test", SessionConfig(max_silence_seconds=15.0), clock
        )
    )
    clock.advance(14.0)
    session.on_message(HeartbeatMessage(ts=clock()))
    clock.advance(14.0)
    session.check_liveness()

    assert not session.halted_reason, "der Herzschlag ist genau dafuer da"


def test_angehalten_entstehen_keine_neuen_positionen(
    tuned: Config, mnq: Instrument, series: BarSeries
):
    session = connect(TradingSession({SYMBOL: mnq}, tuned, "test", clock=Clock()))
    session.halt(HALT_MANUAL)
    for bar in series:
        session.on_bar(SYMBOL, bar)

    assert not session.trades
    portfolio = next(iter(session.books.values())).book.strategy
    assert portfolio.decisions, "es muss ueberhaupt Entscheidungen gegeben haben"
    assert not portfolio.signals, "im Not-Aus darf kein Signal entstehen"
    codes = {r.code for d in portfolio.decisions for r in d.reasons}
    assert R.SYSTEM_HALTED in codes


def test_offene_position_laeuft_im_not_aus_weiter_zu_ihrem_stop(
    tuned: Config, mnq: Instrument, series: BarSeries
):
    """Die gefaehrlichste denkbare Reaktion auf eine Stoerung waere, die Bars
    abzuklemmen: dann liefe eine offene Position ohne Stopueberwachung weiter.

    Deshalb haelt die Sitzung ueber die Risk Engine an und verarbeitet Bars
    unveraendert weiter.
    """
    session = connect(TradingSession({SYMBOL: mnq}, tuned, "test", clock=Clock()))

    # So weit laufen lassen, bis eine Position offen ist.
    offen_bei = -1
    for index, bar in enumerate(series):
        session.on_bar(SYMBOL, bar)
        if session.open_positions:
            offen_bei = index
            break
    assert offen_bei >= 0, "Waechter: ohne offene Position prueft der Test nichts"

    vorher = len(session.trades)
    session.halt(HALT_FEED_STALE)
    for bar in list(series)[offen_bei + 1 :]:
        session.on_bar(SYMBOL, bar)

    assert len(session.trades) > vorher, "die offene Position muss beendet worden sein"
    assert session.open_positions == 0


def test_verbindungsverlust_und_rueckkehr(tuned: Config, mnq: Instrument):
    clock = Clock()
    session = TradingSession(
        {SYMBOL: mnq}, tuned, "test", SessionConfig(resume_after_silence=True), clock
    )
    session.on_message(StatusMessage(ts=clock(), connected=True, detail="da"))
    assert session.connected and not session.halted_reason

    session.on_message(StatusMessage(ts=clock(), connected=False, detail="weg"))
    assert session.halted_reason == HALT_FEED_DISCONNECTED

    session.on_message(StatusMessage(ts=clock(), connected=True, detail="wieder da"))
    assert session.connected and not session.halted_reason
    assert [art for _, art, _ in session.events] == [
        "halt", "feed", "resume", "feed", "halt", "feed", "resume",
    ]


def test_ohne_automatische_rueckkehr_bleibt_es_angehalten(tuned: Config, mnq: Instrument):
    """Im Echtbetrieb soll ein Wiederanlauf eine Entscheidung sein, kein Automatismus.

    Die ERSTE Verbindung hebt den Anfangszustand trotzdem auf - sonst liefe
    eine frisch gestartete Sitzung nie los.
    """
    clock = Clock()
    session = TradingSession(
        {SYMBOL: mnq}, tuned, "test", SessionConfig(resume_after_silence=False), clock
    )
    session.on_message(StatusMessage(ts=clock(), connected=True, detail="erste Verbindung"))
    assert not session.halted_reason

    session.on_message(StatusMessage(ts=clock(), connected=False, detail="weg"))
    session.on_message(StatusMessage(ts=clock(), connected=True, detail="wieder da"))
    assert session.halted_reason == HALT_FEED_DISCONNECTED


# ------------------------------------------------------------- Bar-Hygiene
def test_bars_eines_unbekannten_symbols_werden_nicht_heimlich_analysiert(
    tuned: Config, mnq: Instrument, series: BarSeries
):
    session = TradingSession({SYMBOL: mnq}, tuned, "test", clock=Clock())
    session.on_bar("ES", series[0])
    assert session.bars_seen == 0


def test_wiederholte_oder_rueckwaerts_laufende_bars_werden_verworfen(
    tuned: Config, mnq: Instrument, series: BarSeries
):
    """Nach einem Neustart des Feeds kommen Bars manchmal doppelt.

    Zweimal analysiert saehen die Detektoren einen Kursverlauf, den es nie
    gab - und die Sitzung waere nicht mehr mit dem Backtest vergleichbar.
    """
    session = TradingSession({SYMBOL: mnq}, tuned, "test", clock=Clock())
    session.on_bar(SYMBOL, series[0])
    session.on_bar(SYMBOL, series[1])

    session.on_bar(SYMBOL, series[1])
    session.on_bar(SYMBOL, series[0])
    assert session.bars_seen == 2


def test_status_zeigt_den_kontostand(tuned: Config, mnq: Instrument, series: BarSeries):
    session = TradingSession({SYMBOL: mnq}, tuned, "test", clock=Clock())
    feed_all(session, series)
    status = session.status()

    assert status.bars_seen == len(series)
    assert status.trades_closed == len(session.trades)
    assert status.equity == pytest.approx(status.start_equity + session.realized_pnl)


# ------------------------------------------------------------ Wiedergabe-Feed
def test_wiedergabe_verschraenkt_mehrere_symbole_chronologisch():
    """Sonst saehe das gemeinsame Risikobuch beim zweiten Symbol bereits alle
    Ergebnisse des ersten - dieselbe Falle wie im Backtest."""
    from tests.conftest import make_series

    a = make_series([(1.0, 2.0, 0.5, 1.5)] * 4, step_minutes=2)
    b = make_series([(1.0, 2.0, 0.5, 1.5)] * 4, step_minutes=2)
    feed = ReplayFeed({"A": a, "B": b}, speed=0.0)

    reihenfolge = [(ts, symbol) for ts, symbol, _ in feed._plan]
    assert reihenfolge == sorted(reihenfolge)
    assert feed.total_bars == 8


def test_wiedergabe_liefert_alle_bars_und_endet(tuned: Config, mnq: Instrument):
    from tests.conftest import make_series

    series = make_series([(21000.0, 21002.0, 20998.0, 21001.0)] * 25)
    feed = ReplayFeed({SYMBOL: series}, speed=0.0)
    session = TradingSession({SYMBOL: mnq}, tuned, feed.name, clock=Clock())
    result = SessionRunner(session, feed).run()

    assert result.bars == len(series)
    assert result.stopped_by == "feed_ende"
    assert feed.is_finished


def test_max_bars_beendet_den_lauf(tuned: Config, mnq: Instrument):
    from tests.conftest import make_series

    series = make_series([(21000.0, 21002.0, 20998.0, 21001.0)] * 50)
    feed = ReplayFeed({SYMBOL: series}, speed=0.0)
    session = TradingSession({SYMBOL: mnq}, tuned, feed.name, clock=Clock())
    result = SessionRunner(session, feed).run(max_bars=10)

    assert result.stopped_by == "max_bars"
    assert result.bars >= 10


def test_wiedergabe_ohne_bars_wird_abgelehnt():
    with pytest.raises(ValueError, match="ohne Bars"):
        ReplayFeed({}, speed=0.0)


def test_feed_liefert_nur_geschlossene_bars(tuned: Config, mnq: Instrument):
    """Invariante 1 an der Aussenkante.

    Geprueft wird, dass jede Bar, die die Sitzung erreicht, aus der Quelle
    stammt - der Feed erfindet keine laufende Bar dazu.
    """
    from tests.conftest import make_series

    series = make_series([(21000.0, 21002.0, 20998.0, 21001.0)] * 12)
    feed = ReplayFeed({SYMBOL: series}, speed=0.0)
    feed.start()
    gesehen: list[Bar] = []
    for _ in range(30):
        for message in feed.messages(0.2):
            if isinstance(message, BarMessage):
                gesehen.append(message.bar)
        if feed.is_finished:
            break
    feed.stop()

    assert [b.ts for b in gesehen] == [b.ts for b in series]


# ------------------------------------------------------------------ Speicher
def test_sitzung_wird_sofort_haltbar_gemacht(tmp_path: Path, tuned: Config, mnq: Instrument,
                                             series: BarSeries):
    """Nicht am Ende, sondern beim Schliessen jedes Trades.

    Eine Sitzung endet oft anders als geplant. Was dann nur im Speicher stand,
    ist genau in dem Fall weg, in dem man am ehesten wissen will, was war.
    """
    database = tmp_path / "tradex.db"
    init_database(database)
    with SessionStore(database) as store:
        session_id = store.start(
            mode="paper_manual",
            feed="test",
            symbols=(SYMBOL,),
            config_hash="abc",
            strategy_version="v1",
            backtest_version="bt1",
            start_equity=100_000.0,
        )
        assert store.unfinished(), "eine laufende Sitzung hat kein Ende"

        session = TradingSession(
            {SYMBOL: mnq}, tuned, "test",
            SessionConfig(trade_sink=store.record_trade), Clock(),
        )
        feed_all(session, series)

        gespeichert = store.trades(session_id)
        assert len(gespeichert) == len(session.trades)
        assert gespeichert[0]["r_multiple"] == pytest.approx(session.trades[0].r_multiple)
        assert gespeichert[0]["strategy"] == session.trades[0].strategy

        store.finish()
        assert not store.unfinished()
        assert store.sessions()[0]["trades"] == len(session.trades)


def test_trade_ohne_sitzung_wird_abgelehnt(tmp_path: Path, tuned: Config, mnq: Instrument,
                                           series: BarSeries):
    database = tmp_path / "tradex.db"
    init_database(database)
    session = TradingSession({SYMBOL: mnq}, tuned, "test", clock=Clock())
    feed_all(session, series)
    with SessionStore(database) as store, pytest.raises(RuntimeError, match="heimatlos"):
        store.record_trade(session.trades[0])


def test_zwei_sitzungen_teilen_kein_risikobuch(tuned: Config, mnq: Instrument, series: BarSeries):
    """Waechter gegen einen Zustand, der zwischen Laeufen haengen bleibt."""
    erste = TradingSession({SYMBOL: mnq}, tuned, "test", clock=Clock())
    feed_all(erste, series)
    zweite = TradingSession({SYMBOL: mnq}, tuned, "test", clock=Clock())

    assert zweite.open_positions == 0
    assert zweite.realized_pnl == 0.0
    assert zweite.ledger is not erste.ledger


def test_mehrere_instrumente_teilen_ein_konto(tuned: Config, mnq: Instrument, series: BarSeries):
    session = TradingSession({SYMBOL: mnq, "MES": mnq}, tuned, "test", clock=Clock())
    buecher = list(session.books.values())
    assert buecher[0].book.ledger is buecher[1].book.ledger

    for bar in series:
        session.on_bar(SYMBOL, bar)
        session.on_bar("MES", replace(bar))
    # Kontoweit eindeutige Nummern - sonst kollidieren die Instrumente im Buch.
    nummern = [t.trade_id for t in session.trades]
    assert len(nummern) == len(set(nummern))
