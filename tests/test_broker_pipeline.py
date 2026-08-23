"""Die Orderkette vom Signal bis zum PnL - und vor allem ihre Fehlerfaelle.

Was hier geprueft wird, ist nicht "funktioniert der Gutfall". Der Gutfall ist
der einfache Teil. Geprueft wird die Menge der Zustaende, in denen ein
Handelsprogramm Geld verliert, ohne dass jemand es merkt:

    - eine gesendete Order wird fuer eine Position gehalten
    - eine abgelehnte Order blockiert den Platz im Risikobuch bis Tagesende
    - eine Teilfuellung wird als volle Position gefuehrt
    - dasselbe Signal geht zweimal hinaus
    - nach einem Neustart kollidieren die Schluessel zweier Sitzungen
    - bei mehreren Instrumenten verschluckt ein Buch die Fills des anderen

Alle diese Faelle sind hier auf Bestellung herstellbar (`tests/fake_broker.py`)
und deshalb ueberhaupt pruefbar - an einem echten IB Gateway waeren sie es
nicht.
"""

from __future__ import annotations

import pytest

from tests.conftest import tradeable_config
from tests.fake_broker import FakeBroker
from tradex.backtest.execution import ExecutionOrder
from tradex.broker.base import BrokerNotConnected
from tradex.broker.executor import BrokerExecutor
from tradex.broker.journal import TradeJournal
from tradex.broker.manager import OrderManager, build_order_key
from tradex.broker.types import ROLE_STOP, ROLE_TARGET
from tradex.config import Config
from tradex.domain.bars import Bar
from tradex.domain.enums import Direction, ExitReason
from tradex.domain.instruments import Instrument
from tradex.risk.ledger import OpenPosition, RiskLedger
from tradex.strategy.signal import TradeSignal

T0 = 1_700_000_000
"""Fester Zeitpunkt in Sekunden. Die Uhr wird injiziert - ein Test, der auf
die echte Wanduhr angewiesen ist, prueft das Datenalter nicht, sondern die
Laufzeit der Testmaschine."""

BAR_TS = T0 * 1_000_000_000
TRADING_DAY = 20251101


class Uhr:
    """Injizierbare Wanduhr in Sekunden."""

    def __init__(self, jetzt: float = T0 + 61.0) -> None:
        self.jetzt = jetzt

    def __call__(self) -> float:
        return self.jetzt


def _bar(price: float = 21000.0) -> Bar:
    return Bar(ts=BAR_TS, open=price, high=price + 5, low=price - 5, close=price, volume=100.0)


def _signal(trade_id: int, symbol: str = "MNQ", quantity: int = 2) -> TradeSignal:
    return TradeSignal(
        trade_id=trade_id,
        setup_id=trade_id,
        symbol=symbol,
        direction=Direction.BULLISH,
        entry=21000.0,
        stop=20980.0,
        target=21040.0,
        stop_ticks=80.0,
        target_points=40.0,
        rr=2.0,
        quantity=quantity,
        risk_amount=80.0,
        reward_amount=160.0,
        entry_ts=BAR_TS,
        entry_index=1,
        stop_anchor="test",
        target_source="test",
    )


def _order(signal: TradeSignal, instrument: Instrument) -> ExecutionOrder:
    return ExecutionOrder(
        signal=signal,
        instrument=instrument,
        session="ny_am",
        trading_day=TRADING_DAY,
        htf_bias="bullish",
        signal_index=1,
    )


def _buche(ledger: RiskLedger, signal: TradeSignal) -> None:
    """Was `SymbolBook` vor der Ausfuehrung tut: Platz reservieren.

    Ohne diese Buchung waere `_abandon()` folgenlos und die Tests wuerden
    genau das nicht pruefen, worum es geht - dass ein gescheiterter Versuch
    den Platz wieder freigibt.
    """
    ledger.open_position(
        OpenPosition(
            setup_id=signal.trade_id,
            direction=signal.direction,
            entry_ts=signal.entry_ts,
            entry_price=signal.entry,
            stop=signal.stop,
            target=signal.target,
            quantity=signal.quantity,
            risk_amount=signal.risk_amount,
        ),
        TRADING_DAY,
    )


@pytest.fixture
def welt(config: Config, mnq: Instrument):
    """Ein Konto, ein Broker, ein Instrument - wie im Betrieb verdrahtet."""

    def bauen(symbols: tuple[str, ...] = ("MNQ",), session_id: int = 7, **broker_args):
        cfg = tradeable_config(config)
        broker = FakeBroker(**broker_args)
        ledger = RiskLedger()
        uhr = Uhr()
        journal = TradeJournal(broker=broker.name, trading_mode="paper_auto")
        orders = OrderManager(broker, cfg.broker, journal, None, session_id)
        orders.bind_account(broker.get_account_info())
        executors = {
            symbol: BrokerExecutor(
                symbol, mnq, cfg, ledger, orders, broker, session_id=session_id, clock=uhr
            )
            for symbol in symbols
        }
        return broker, ledger, orders, executors, uhr

    return bauen


# --------------------------------------------------------------------- Gutfall
def test_die_ganze_kette_vom_signal_bis_zum_pnl(welt):
    broker, ledger, _, executors, _ = welt()
    executor = executors["MNQ"]
    signal = _signal(1)
    _buche(ledger, signal)

    executor.place(_order(signal, executor.instrument), _bar(), index=1)
    schluessel = build_order_key(7, 1)

    # Gesendet ist nicht gefuellt: bis der Broker meldet, gibt es keine Position.
    assert len(broker.requests) == 1
    assert executor.open_count == 0

    broker.fill(schluessel, price=21001.0, commission=1.4)
    executor.poll()
    assert executor.open_count == 1

    # Der Einstieg im Risikobuch wird auf den ECHTEN Fuellkurs nachgezogen.
    assert ledger.position(1).entry_price == pytest.approx(21001.0)

    broker.fill(schluessel, role=ROLE_TARGET, price=21040.0, commission=1.4)
    update = executor.poll()

    assert len(update.closed) == 1
    trade = update.closed[0]
    assert trade.exit_reason is ExitReason.TARGET
    assert trade.entry_price == pytest.approx(21001.0)
    assert trade.exit_price == pytest.approx(21040.0)
    # 39 Punkte * 2 Kontrakte * 2 USD/Punkt - 2,80 USD Gebuehren
    assert trade.gross_pnl == pytest.approx(156.0)
    assert trade.commission == pytest.approx(2.8)
    assert trade.pnl == pytest.approx(153.2)
    assert executor.open_count == 0


def test_der_stop_liefert_den_ausstiegsgrund_stop(welt):
    broker, ledger, _, executors, _ = welt()
    executor = executors["MNQ"]
    signal = _signal(1)
    _buche(ledger, signal)
    executor.place(_order(signal, executor.instrument), _bar(), index=1)
    schluessel = build_order_key(7, 1)

    broker.fill(schluessel, price=21000.0)
    executor.poll()
    broker.fill(schluessel, role=ROLE_STOP, price=20980.0)
    update = executor.poll()

    assert update.closed[0].exit_reason is ExitReason.STOP
    assert update.closed[0].pnl < 0


# ------------------------------------------------------------------ Fehlerfaelle
def test_abgelehnte_order_gibt_den_platz_im_risikobuch_wieder_frei(welt):
    """Sonst blockiert ein Phantom den einzigen Platz bis Tagesende."""
    broker, ledger, _, executors, _ = welt()
    executor = executors["MNQ"]
    signal = _signal(1)
    _buche(ledger, signal)
    executor.place(_order(signal, executor.instrument), _bar(), index=1)
    assert ledger.open_count == 1

    broker.reject(build_order_key(7, 1), message="201: Order rejected")
    update = executor.poll()

    assert ledger.open_count == 0
    assert update.unfilled == 1
    assert executor.open_count == 0


def test_teilfuellung_fuehrt_zur_tatsaechlich_gefuellten_groesse(welt):
    """Zwei Kontrakte bestellt, einer bekommen - die Position ist EINER.

    Die gefaehrliche Variante waere, weiter mit zwei zu rechnen: der Stop
    laege dann auf der doppelten Menge, und das Risiko waere in Wahrheit ein
    anderes als das berechnete.
    """
    broker, ledger, _, executors, _ = welt()
    executor = executors["MNQ"]
    signal = _signal(1, quantity=2)
    _buche(ledger, signal)
    executor.place(_order(signal, executor.instrument), _bar(), index=1)
    schluessel = build_order_key(7, 1)

    broker.partial_fill(schluessel, quantity=1, price=21000.0)
    executor.poll()

    assert executor.open_count == 1
    assert ledger.position(1).quantity == 1

    broker.fill(schluessel, role=ROLE_TARGET, price=21040.0)
    trade = executor.poll().closed[0]
    assert trade.quantity == 1
    # 40 Punkte * 1 Kontrakt * 2 USD/Punkt
    assert trade.gross_pnl == pytest.approx(80.0)


def test_dasselbe_signal_zweimal_ergibt_genau_eine_order(welt):
    broker, ledger, _, executors, _ = welt()
    executor = executors["MNQ"]
    signal = _signal(1)
    _buche(ledger, signal)

    executor.place(_order(signal, executor.instrument), _bar(), index=1)
    executor.place(_order(signal, executor.instrument), _bar(), index=2)

    assert len(broker.requests) == 1


def test_ohne_verbindung_entsteht_keine_order_und_nichts_wird_nachgesendet(welt):
    """Eine spaeter nachgesendete Order handelt auf einer Lage, die es nicht
    mehr gibt. Deshalb: verwerfen, nicht sammeln."""
    broker, ledger, _, executors, _ = welt()
    executor = executors["MNQ"]
    broker.fail_next_with = BrokerNotConnected("Testabriss")

    signal = _signal(1)
    _buche(ledger, signal)
    executor.place(_order(signal, executor.instrument), _bar(), index=1)

    assert broker.requests == []
    assert ledger.open_count == 0

    # Verbindung ist wieder da - trotzdem wird nichts nachgeholt.
    broker.connect()
    assert executor.poll().closed == []
    assert broker.requests == []


def test_verbindungsverlust_sperrt_neue_orders(welt):
    broker, ledger, orders, executors, _ = welt()
    executor = executors["MNQ"]
    broker.drop_connection()
    orders.drain()  # der Abriss wird beim Abholen bemerkt

    assert orders.blocked_reason
    assert not orders.is_ready

    signal = _signal(1)
    _buche(ledger, signal)
    executor.place(_order(signal, executor.instrument), _bar(), index=1)

    assert broker.requests == []
    assert ledger.open_count == 0


def test_veraltete_bar_erzeugt_keine_order(welt):
    """Ein Signal aus einer Datenluecke darf nicht nachtraeglich ausgefuehrt
    werden, sobald der Feed zurueckkommt."""
    broker, ledger, _, executors, uhr = welt()
    executor = executors["MNQ"]
    uhr.jetzt = T0 + 3600.0  # eine Stunde nach Bar-Schluss

    signal = _signal(1)
    _buche(ledger, signal)
    executor.place(_order(signal, executor.instrument), _bar(), index=1)

    assert broker.requests == []
    assert ledger.open_count == 0


def test_gesperrter_kontrakt_erzeugt_keine_order(welt):
    broker, ledger, _, executors, _ = welt(tradeable=False)
    executor = executors["MNQ"]
    signal = _signal(1)
    _buche(ledger, signal)
    executor.place(_order(signal, executor.instrument), _bar(), index=1)

    assert broker.requests == []
    assert ledger.open_count == 0


def test_ratenlimit_haelt_eine_signalflut_auf(welt, config: Config):
    broker, ledger, _, executors, _ = welt()
    executor = executors["MNQ"]
    grenze = config.broker.max_orders_per_minute

    for nummer in range(grenze + 3):
        signal = _signal(nummer + 1)
        _buche(ledger, signal)
        executor.place(_order(signal, executor.instrument), _bar(), index=nummer)

    assert len(broker.requests) == grenze


# --------------------------------------------------------------------- Neustart
def test_nach_einem_neustart_kollidieren_die_schluessel_nicht():
    """`RiskLedger` zaehlt im Speicher und beginnt nach einem Neustart wieder
    bei 1. Ohne Sitzungskennung im Schluessel waere das neue Signal 1 ein
    Duplikat des alten - und ginge nie hinaus."""
    assert build_order_key(7, 1) != build_order_key(8, 1)
    assert build_order_key(None, 1) != build_order_key(1, 1)


def test_ein_neustart_sendet_bekannte_schluessel_nicht_erneut(config: Config, tmp_path):
    """Der Duplikatschutz muss den Prozess ueberleben - im Speicher allein
    waere er ein Gedaechtnis auf Zeit."""
    from tradex.broker.store import BrokerOrderStore
    from tradex.broker.types import OrderRequest, OrderSide
    from tradex.persistence.db import init_database

    datenbank = tmp_path / "tradex.db"
    init_database(datenbank)
    cfg = tradeable_config(config)

    anfrage = OrderRequest(
        order_key=build_order_key(7, 1),
        symbol="MNQ",
        side=OrderSide.BUY,
        quantity=1,
        stop_loss=20980.0,
        take_profit=21040.0,
    )

    broker = FakeBroker()
    store = BrokerOrderStore(datenbank)
    journal = TradeJournal(broker=broker.name, trading_mode="paper_auto")
    orders = OrderManager(broker, cfg.broker, journal, store, 7)
    orders.bind_account(broker.get_account_info())
    orders.submit(anfrage, now=T0)
    assert len(broker.requests) == 1
    store.close()

    # Neustart: neuer Manager, neuer Speicher, dieselbe Datenbank.
    store2 = BrokerOrderStore(datenbank)
    orders2 = OrderManager(broker, cfg.broker, journal, store2, 7)
    orders2.bind_account(broker.get_account_info())
    assert orders2.submit(anfrage, now=T0) is None
    assert len(broker.requests) == 1
    store2.close()


# ------------------------------------------------------- Mehrere Instrumente
def test_fills_mehrerer_instrumente_gehen_nicht_verloren(welt):
    """Der Manager gehoert dem Konto, die Executoren je einem Instrument.

    Die Broker-Queue gibt jedes Ereignis nur EINMAL heraus. Holte jeder
    Executor blind alles ab und verwuerfe das Fremde, verschluckte der erste
    Aufrufer die Fuellungen des anderen - bei einem Symbol faellt das nie auf.
    """
    broker, ledger, _, executors, _ = welt(symbols=("MNQ", "MES"))
    ersteres, zweites = executors["MNQ"], executors["MES"]

    signal_a = _signal(1, symbol="MNQ")
    signal_b = _signal(2, symbol="MES")
    _buche(ledger, signal_a)
    _buche(ledger, signal_b)
    ersteres.place(_order(signal_a, ersteres.instrument), _bar(), index=1)
    zweites.place(_order(signal_b, zweites.instrument), _bar(), index=1)

    broker.fill(build_order_key(7, 1), price=21000.0)
    broker.fill(build_order_key(7, 2), price=21000.0)

    # Das ERSTE Buch holt ab - und darf dem zweiten nichts wegnehmen.
    ersteres.poll()
    zweites.poll()

    assert ersteres.open_count == 1
    assert zweites.open_count == 1
