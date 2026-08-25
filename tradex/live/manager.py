"""Eine laufende Sitzung im Serverprozess fuehren (Phase 7, Spec Paragraph 24).

Warum das noetig ist
--------------------
`scripts/run_paper.py` fuehrt eine Sitzung im Vordergrund. Das reicht zum
Messen, aber nicht zum Betreiben: ein Not-Aus, den man nur erreicht, indem man
in das richtige Konsolenfenster klickt und Strg-C drueckt, ist keiner. Spec
Paragraph 24 verlangt einen Kill Switch - erreichbar, sofort wirksam, von
ueberall.

Diese Klasse haelt die Sitzung deshalb in einem Hintergrundfaden des Servers.
Das UI sieht ihren Zustand und kann sie anhalten.

Warum der Not-Aus auch dann wirkt, wenn der Faden haengt
--------------------------------------------------------
`halt()` setzt nur ein Feld in der Risk Engine. Es wartet auf nichts, es
braucht den Sitzungsfaden nicht und kann deshalb nicht daran scheitern, dass
dieser gerade beschaeftigt ist. Wuerde der Not-Aus dem Faden eine Nachricht
schicken und auf Bestaetigung warten, waere er genau dann wirkungslos, wenn man
ihn am dringendsten braucht.

Eine Stelle, an der Sitzungen entstehen
---------------------------------------
`build_feed` und `build_session` stehen hier und werden auch vom Skript
benutzt. Dieselbe Ueberlegung wie bei `strategy/registry.py`: gaebe es zwei
Konstruktionswege, liefe der Serverbetrieb irgendwann mit einer anderen
Zusammenstellung als der Kommandozeilenbetrieb - und niemand wuesste, welche
der beiden die gemessene ist.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from tradex.analysis.context import MarketContext
from tradex.backtest.execution import SimulatedTrade
from tradex.backtest.runner import BACKTEST_VERSION
from tradex.broker.base import BrokerInterface
from tradex.broker.env import read_env
from tradex.broker.executor import BrokerExecutor
from tradex.broker.guard import check_configuration
from tradex.broker.journal import TradeJournal
from tradex.broker.manager import OrderManager
from tradex.broker.store import BrokerOrderStore
from tradex.config import Config, resolved_config_path
from tradex.data.store import BarStore
from tradex.domain.bars import Bar
from tradex.domain.instruments import Instrument
from tradex.live.feed import LiveFeed
from tradex.live.nt8_feed import DEFAULT_HOST, DEFAULT_PORT, NinjaTraderFeed
from tradex.live.replay_feed import ReplayFeed
from tradex.live.runner import SessionRunner
from tradex.live.session import (
    HALT_MANUAL,
    ExecutorFactory,
    SessionConfig,
    SessionStatus,
    TradingSession,
)
from tradex.live.store import SessionStore
from tradex.logging_setup import get_logger
from tradex.persistence.db import init_database
from tradex.persistence.decision_log import DecisionLog, utc_now_iso
from tradex.persistence.models import SystemEvent
from tradex.risk.ledger import RiskLedger
from tradex.strategy.portfolio import StrategyPortfolio

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SessionRequest:
    """Was gestartet werden soll. Reine Daten, keine Objekte."""

    symbols: tuple[str, ...]
    feed: str = "replay"
    speed: float = 3600.0
    start_ts: int | None = None
    end_ts: int | None = None
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    notes: str = ""
    save: bool = True
    max_bars: int = 0


@dataclass(slots=True)
class BrokerState:
    """Zustand der Orderanbindung fuer die Anzeige.

    Enthaelt ausschliesslich Werte, die OHNE Rueckfrage beim Broker
    verfuegbar sind. Ein Statusaufruf der Oberflaeche darf nie auf ein Netz
    warten muessen - sonst steht die Anzeige genau dann, wenn der Broker
    klemmt und man am dringendsten hinsehen will.
    """

    enabled: bool
    provider: str = ""
    connected: bool = False
    account: str = ""
    is_paper: bool = False
    paper_evidence: str = ""
    blocked_reason: str = ""
    open_orders: int = 0
    tradeable_symbols: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ready(self) -> bool:
        """Duerfte gerade eine Order entstehen?"""
        return self.enabled and self.connected and self.is_paper and not self.blocked_reason


@dataclass(slots=True)
class ManagerState:
    """Zustand des Managers - auch dann aussagefaehig, wenn nichts laeuft."""

    active: bool
    feed: str
    symbols: tuple[str, ...]
    session_id: int
    status: SessionStatus | None
    stopped_by: str
    error: str
    warnings: tuple[str, ...] = field(default_factory=tuple)
    broker: BrokerState = field(default_factory=lambda: BrokerState(enabled=False))
    last_prices: dict[str, float] = field(default_factory=dict)
    """Zuletzt gehandelte Kurse je Symbol - nur zur Anzeige, nie fuer eine
    Entscheidung. Leer, wenn der Feed keine Ticks liefert."""


def build_feed(
    request: SessionRequest, config: Config, instruments: dict[str, Instrument]
) -> LiveFeed:
    """Den Feed bauen - die einzige Stelle, an der das passiert."""
    if request.feed == "nt8":
        contracts = {
            name: instruments[name].nt8_symbol
            for name in request.symbols
            if instruments[name].nt8_symbol
        }
        return NinjaTraderFeed(
            request.symbols,
            config.data.base_timeframe,
            host=request.host,
            port=request.port,
            contracts=contracts,
            history_days=config.live.nt8_history_days,
            history_timeout_seconds=config.live.nt8_history_timeout_seconds,
        )
    if request.feed != "replay":
        known = "replay, nt8"
        raise LookupError(f"Unbekannter Feed {request.feed!r}. Bekannt: {known}")

    store = BarStore(config.path(config.data.parquet_dir))
    series_by_symbol = {}
    for name in request.symbols:
        series = store.read(name, config.data.base_timeframe, request.start_ts, request.end_ts)
        if len(series) == 0:
            raise LookupError(f"Keine {config.data.base_timeframe.value}-Daten fuer {name}")
        series_by_symbol[name] = series
    return ReplayFeed(
        series_by_symbol,
        speed=request.speed,
        base_seconds=config.data.base_timeframe.seconds,
    )


@dataclass(slots=True)
class BrokerLink:
    """Die aufgebaute Broker-Anbindung eines Kontos.

    Haelt Adapter, Order-Manager und Protokoll zusammen, damit die Sitzung nur
    ein Objekt kennt - und damit es genau eine Stelle gibt, an der die
    Verbindung wieder abgebaut wird.
    """

    adapter: BrokerInterface
    orders: OrderManager
    journal: TradeJournal
    store: BrokerOrderStore | None
    session_id: int | None
    config: Config
    symbols: tuple[str, ...] = ()
    """Die Symbole dieser Sitzung - fuer die Anzeige, welche davon der Broker
    tatsaechlich handeln kann."""

    def health(self) -> str:
        """"" wenn gehandelt werden darf, sonst der Grund."""
        if not self.adapter.is_connected():
            return "verbindung_verloren"
        return self.orders.blocked_reason

    def state(self) -> BrokerState:
        """Momentaufnahme fuer die Anzeige - ohne Rueckfrage beim Broker."""
        konto = self.orders.account
        return BrokerState(
            enabled=True,
            provider=self.adapter.name,
            connected=self.adapter.is_connected(),
            account=konto.account if konto else "",
            is_paper=bool(konto and konto.is_paper),
            paper_evidence=konto.paper_evidence if konto else "",
            blocked_reason=self.orders.blocked_reason,
            open_orders=len(self.orders.live_orders()),
            tradeable_symbols=tuple(
                symbol for symbol in self.symbols if self.adapter.can_trade(symbol)[0]
            ),
        )

    def executor_factory(self) -> ExecutorFactory:
        def make(symbol: str, instrument: Instrument, ledger: RiskLedger) -> BrokerExecutor:
            return BrokerExecutor(
                symbol,
                instrument,
                self.config,
                ledger,
                self.orders,
                self.adapter,
                session_id=self.session_id,
            )

        return make

    def close(self) -> None:
        """Verbindung abbauen. Offene Positionen bleiben offen - bewusst.

        Ihre Stops und Ziele liegen als echte Orders beim Broker und wirken
        weiter. Sie beim Trennen glattzustellen waere eine Handelsentscheidung,
        die niemand getroffen hat.
        """
        try:
            self.adapter.disconnect()
        finally:
            if self.store is not None:
                self.store.close()


def build_broker(
    request: SessionRequest,
    config: Config,
    instruments: dict[str, Instrument],
    session_id: int | None = None,
    event_sink: Callable[[int, str, str], None] | None = None,
) -> BrokerLink | None:
    """Die Orderanbindung aufbauen. None heisst: es wird weiter simuliert.

    Die gesamte Sicherheitskette laeuft VOR dem Verbindungsaufbau: eine
    Sitzung, die sich anmeldet und danach feststellt, dass sie nicht handeln
    darf, hat bereits einen Zustand beim Broker erzeugt.
    """
    if not config.broker.enabled:
        return None

    env = read_env(config.execution, config.broker)
    gate = check_configuration(config.execution, config.broker, env)
    if not gate.approved:
        # Kein stilles Zurueckfallen auf die Simulation: wer `broker.enabled`
        # setzt, will Orders. Sie klaglos durch Papertrades zu ersetzen waere
        # genau die Verwechslung, die diese ganze Schicht verhindern soll.
        raise LookupError(
            f"broker.enabled ist an, aber die Sicherheitskette sperrt: {gate.blocking_code} "
            f"({gate.blocking_reason.params if gate.blocking_reason else ''})"
        )

    journal = TradeJournal(
        broker=config.broker.provider,
        trading_mode=config.execution.mode.value,
        event_sink=event_sink,
        clock=time.time_ns,
    )
    traded = {name: instruments[name] for name in request.symbols}

    if config.broker.provider == "ibkr":
        # Lazy: `ibapi` kommt aus einem Installer und nicht von PyPI. Der
        # Import gehoert deshalb hinter die Entscheidung, ihn zu brauchen.
        from tradex.broker.ibkr.adapter import IbkrAdapter

        adapter: BrokerInterface = IbkrAdapter(config, traded, journal=journal, env=env)
    else:  # pragma: no cover - `provider` ist per Config auf "ibkr" begrenzt
        raise LookupError(f"Unbekannter Broker {config.broker.provider!r}")

    store: BrokerOrderStore | None = None
    if request.save:
        store = BrokerOrderStore(config.path(config.data.database))

    adapter.connect()
    orders = OrderManager(adapter, config.broker, journal, store, session_id)
    orders.bind_account(adapter.get_account_info())
    return BrokerLink(
        adapter=adapter,
        orders=orders,
        journal=journal,
        store=store,
        session_id=session_id,
        config=config,
        symbols=request.symbols,
    )


def build_session(
    request: SessionRequest,
    config: Config,
    instruments: dict[str, Instrument],
    feed_name: str,
    sink: SessionStore | None,
    events: DecisionLog | None = None,
    broker: BrokerLink | None = None,
) -> TradingSession:
    return TradingSession(
        {name: instruments[name] for name in request.symbols},
        config,
        feed_name=feed_name,
        session_config=SessionConfig(
            trade_sink=sink.record_trade if sink is not None else None,
            event_sink=_event_writer(events, feed_name, request.symbols) if events else None,
            executor_factory=broker.executor_factory() if broker is not None else None,
            broker_health=broker.health if broker is not None else None,
        ),
    )


def _event_writer(
    log_db: DecisionLog, feed_name: str, symbols: tuple[str, ...]
) -> Callable[[int, str, str], None]:
    """Betriebsereignisse in `system_events` schreiben (Spec Paragraph 24).

    Warum in die Datenbank und nicht nur ins Textlog: die Frage "warum stand
    der Betrieb heute Nacht?" beantwortet man am naechsten Morgen, und dann
    ist eine Abfrage mehr wert als eine rotierende Datei. Die Tabelle gibt es
    seit Migration 1 - benutzt hat sie bisher niemand.
    """
    levels = {"halt": "warning", "error": "error"}

    def write(ts: int, kind: str, text: str) -> None:
        try:
            log_db.event(
                SystemEvent(
                    ts_utc=utc_now_iso(),
                    level=levels.get(kind, "info"),
                    category=f"session.{kind}",
                    message=text,
                    payload={"feed": feed_name, "symbols": ",".join(symbols), "ts": ts},
                )
            )
        except Exception:
            # Ein Schreibfehler im Protokoll darf den Betrieb nicht anhalten.
            # Umgekehrt waere es falsch herum: dann brechen Positionen ab,
            # weil eine Datenbank klemmt.
            log.warning("betriebsereignis_nicht_gespeichert", art=kind, text=text)

    return write


class SessionManager:
    """Fuehrt hoechstens EINE Sitzung - und macht sie steuerbar.

    Hoechstens eine: zwei gleichzeitige Sitzungen haetten getrennte
    Risikobuecher und damit zusammen das doppelte erlaubte Risiko. Genau der
    Fehler, den `portfolio.py` eine Ebene tiefer verhindert.
    """

    def __init__(
        self,
        config: Config,
        instruments: dict[str, Instrument],
        events: DecisionLog | None = None,
    ) -> None:
        self.config = config
        self.instruments = instruments
        # EINE Verbindung fuer die Lebensdauer des Managers, von aussen
        # hereingereicht und von aussen geschlossen. Eine je Sitzung waere ein
        # Wettlauf: ein Not-Aus trifft regelmaessig eine Sitzung, die gerade
        # zu Ende gelaufen ist - und schriebe dann in eine geschlossene
        # Verbindung. Genau das ist beim Testen passiert.
        self._events = events
        self._lock = threading.Lock()
        self._session: TradingSession | None = None
        self._feed: LiveFeed | None = None
        self._runner: SessionRunner | None = None
        self._thread: threading.Thread | None = None
        self._store: SessionStore | None = None
        self._broker: BrokerLink | None = None
        self._request: SessionRequest | None = None
        self._stopped_by = ""
        self._error = ""

    # ------------------------------------------------------------------ Start
    def start(self, request: SessionRequest) -> ManagerState:
        with self._lock:
            if self.is_running:
                raise LookupError(
                    "Es laeuft bereits eine Sitzung. Zwei gleichzeitig haetten getrennte "
                    "Risikobuecher und zusammen das doppelte erlaubte Risiko."
                )
            unknown = [s for s in request.symbols if s not in self.instruments]
            if unknown:
                raise LookupError(f"Unbekannte Symbole: {', '.join(unknown)}")
            if not request.symbols:
                raise LookupError("Eine Sitzung ohne Symbole kann nichts handeln")

            feed = build_feed(request, self.config, self.instruments)
            store = self._open_store(request, feed.name)
            # Der Broker wird VOR der Sitzung aufgebaut: schlaegt die
            # Sicherheitskette an, soll gar nichts erst laufen. Die Sitzung
            # danach abzubrechen hiesse, eine bereits verbundene Anmeldung
            # wieder aufloesen zu muessen.
            try:
                broker = build_broker(
                    request,
                    self.config,
                    self.instruments,
                    session_id=store.session_id if store is not None else None,
                    event_sink=_event_writer(self._events, feed.name, request.symbols)
                    if self._events
                    else None,
                )
            except Exception:
                if store is not None:
                    store.finish()
                    store.close()
                raise

            try:
                session = build_session(
                    request, self.config, self.instruments, feed.name, store, self._events, broker
                )
                runner = SessionRunner(session, feed)
            except Exception:
                # Der Broker ist an dieser Stelle bereits VERBUNDEN. Ohne
                # dieses Aufraeumen bliebe die Anmeldung beim Gateway offen,
                # ohne dass irgendetwas sie noch kennt - und der naechste
                # Startversuch liefe gegen eine belegte Client-ID.
                if broker is not None:
                    broker.close()
                if store is not None:
                    store.finish()
                    store.close()
                raise

            self._session, self._runner, self._store = session, runner, store
            self._feed = feed
            self._broker = broker
            self._request, self._stopped_by, self._error = request, "", ""
            self._thread = threading.Thread(
                target=self._run, args=(runner, request.max_bars), name="tradex-session",
                daemon=True,
            )
            self._thread.start()
            log.info("sitzung_gestartet", feed=feed.name, symbole=list(request.symbols))
        return self.state()

    def _open_store(self, request: SessionRequest, feed_name: str) -> SessionStore | None:
        if not request.save:
            return None
        from tradex.service import STRATEGY_VERSION

        database = self.config.path(self.config.data.database)
        init_database(database)
        with DecisionLog(database) as log_db:
            # `resolved_config_path()`, nicht fest default.yaml: laeuft der
            # Server unter TRADEX_CONFIG mit einer Variante, etikettierte ein
            # fester Pfad jede gespeicherte Sitzung falsch.
            config_hash = log_db.register_config(resolved_config_path())
        store = SessionStore(database)
        store.start(
            mode=self.config.execution.mode.value,
            feed=feed_name,
            symbols=request.symbols,
            config_hash=config_hash,
            strategy_version=STRATEGY_VERSION,
            backtest_version=BACKTEST_VERSION,
            start_equity=self.config.risk.account_size,
            notes=request.notes,
        )
        return store

    def _run(self, runner: SessionRunner, max_bars: int) -> None:
        try:
            result = runner.run(max_bars=max_bars)
            self._stopped_by = result.stopped_by
        except Exception as error:  # der Betrieb darf nicht still enden
            self._error = f"{type(error).__name__}: {error}"
            self._stopped_by = "fehler"
            log.exception("sitzung_abgebrochen")
        finally:
            if self._broker is not None:
                # Zuerst der Broker: solange die Verbindung steht, koennen
                # noch Rueckmeldungen kommen, und die sollen in einer noch
                # offenen Datenbank landen.
                self._broker.close()
                self._broker = None
            if self._store is not None:
                self._store.finish()
                self._store.close()
                self._store = None

    # -------------------------------------------------------------- Kill Switch
    def halt(self, reason: str = HALT_MANUAL) -> ManagerState:
        """Not-Aus. Keine neuen Positionen; offene laufen zu ihrem Stop.

        Bewusst KEIN Abbruch des Fadens: eine Sitzung, die keine Bars mehr
        verarbeitet, laesst offene Positionen ohne Stopueberwachung zurueck.
        """
        session = self._session
        if session is None:
            raise LookupError("Es laeuft keine Sitzung")
        session.halt(reason)
        return self.state()

    def resume(self) -> ManagerState:
        session = self._session
        if session is None:
            raise LookupError("Es laeuft keine Sitzung")
        session.resume()
        return self.state()

    def stop(self, timeout: float = 10.0) -> ManagerState:
        """Sitzung beenden. Offene Positionen bleiben offen - bewusst."""
        with self._lock:
            runner, thread = self._runner, self._thread
            if runner is None:
                raise LookupError("Es laeuft keine Sitzung")
            runner.request_stop()
            if self._session is not None:
                self._session.stop()
        if thread is not None:
            thread.join(timeout=timeout)
        return self.state()

    # ---------------------------------------------------------------- Auskunft
    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def session(self) -> TradingSession | None:
        return self._session

    def context(self, symbol: str) -> MarketContext | None:
        """Der Analysezustand des laufenden Betriebs - oder None.

        Damit sieht die Oberflaeche die Bars, die GERADE hereinkommen. Vorher
        gab es dafuer keinen Weg: die Sitzung fuehrt ihre eigenen `SymbolBook`s,
        und der Chart las aus den Wiedergabe-Zustaenden des Service. Ein
        Echtzeit-Chart war damit nicht bloss abgeschaltet, es gab ihn nicht.

        Bewusst ohne Sperre, aus demselben Grund wie `state()`: eine Sperre
        wuerde die Anzeige an den Sitzungsfaden koppeln, und ausgerechnet beim
        Zusehen bliebe sie stehen. Das ist hier auch ungefaehrlich - `BarSeries`
        haelt vorbelegte numpy-Felder und zaehlt erst NACH dem Schreiben hoch,
        und ein Feld, das nebenher waechst, wird ersetzt statt veraendert. Wer
        dazwischen liest, bekommt eine um eine Bar aeltere Fassung, nie eine
        halb geschriebene.
        """
        session = self._session
        if session is None:
            return None
        state = session.books.get(symbol.upper())
        return state.book.context if state is not None else None

    def strategy(self, symbol: str) -> StrategyPortfolio | None:
        """Das Strategiebuch des laufenden Betriebs - oder None.

        Gegenstueck zu `context()`: Kurse und Entscheidungen muessen aus
        derselben Quelle kommen, sonst stehen zwei Zustaende nebeneinander,
        die nicht zusammengehoeren.
        """
        session = self._session
        if session is None:
            return None
        state = session.books.get(symbol.upper())
        return state.book.strategy if state is not None else None

    def last_prices(self) -> dict[str, float]:
        """Zuletzt gehandelte Kurse aus dem Feed - nur zur Anzeige.

        Nicht jeder Feed hat sie (die Wiedergabe kennt keine Ticks), deshalb
        wird gefragt statt vorausgesetzt. Diese Zahlen gehen in keine
        Entscheidung ein; sie fuellen die Luecke zwischen zwei Bar-Schluessen,
        in der ein Chart sonst stillsteht, obwohl sich der Markt bewegt.
        """
        feed = self._feed
        preise = getattr(feed, "last_price", None) if feed is not None else None
        return dict(preise) if isinstance(preise, dict) else {}

    def live_bar(self, symbol: str) -> Bar | None:
        """Die aus Ticks gebaute laufende Kerze - nur zur Anzeige.

        Wie `last_prices()` gefragt statt vorausgesetzt: die Wiedergabe kennt
        keine Ticks. Und wie dort geht nichts davon in eine Entscheidung ein -
        die Sitzung sieht diese Bar nie, sie liest nur aus der Feed-Queue, und
        dort liegt sie nicht.
        """
        feed = self._feed
        holen = getattr(feed, "live_bar", None) if feed is not None else None
        if not callable(holen):
            return None
        bar = holen(symbol)
        return bar if isinstance(bar, Bar) else None

    def last_tick_ts(self, symbol: str) -> int:
        feed = self._feed
        stempel = getattr(feed, "last_tick_ts", None) if feed is not None else None
        return int(stempel.get(symbol.upper(), 0)) if isinstance(stempel, dict) else 0

    def trades(self, limit: int = 100) -> tuple[SimulatedTrade, ...]:
        session = self._session
        return tuple(session.trades[-limit:]) if session else ()

    def state(self) -> ManagerState:
        """Momentaufnahme. Bewusst ohne Sperre.

        Gelesen werden einzelne Zahlen und Zeichenketten; eine davon kann um
        Sekundenbruchteile veraltet sein. Eine Sperre waere hier schaedlich:
        sie wuerde die Anzeige an den Sitzungsfaden koppeln, und ausgerechnet
        beim Beobachten eines haengenden Betriebs bliebe die Anzeige stehen.
        """
        session = self._session
        status = session.status() if session else None
        broker = self._broker
        return ManagerState(
            active=self.is_running,
            feed=session.feed_name if session else "",
            symbols=tuple(sorted(session.books)) if session else (),
            session_id=self._store.session_id if self._store else 0,
            status=status,
            stopped_by=self._stopped_by,
            error=self._error,
            warnings=self._warnings(session, status),
            broker=broker.state()
            if broker is not None
            else BrokerState(enabled=self.config.broker.enabled),
            last_prices=self.last_prices(),
        )

    def _warnings(
        self, session: TradingSession | None, status: SessionStatus | None
    ) -> tuple[str, ...]:
        """Was der Betrachter wissen muss, bevor er den Zahlen glaubt."""
        if session is None or status is None:
            return ()
        messages: list[str] = []

        if status.halted_reason:
            messages.append(
                f"Angehalten ({status.halted_reason}): keine neuen Positionen. "
                "Offene Positionen laufen weiter zu ihrem Stop."
            )
        if not status.connected:
            messages.append("Keine Feed-Verbindung.")

        still = (session.clock() - status.last_message_ts) / 1e9
        if still > session.params.max_silence_seconds / 2:
            messages.append(f"Seit {still:.0f} s keine Nachricht vom Feed.")

        risk = self.config.risk
        verlust = -min(status.day_pnl, 0.0)
        if risk.enabled and verlust > 0.5 * risk.max_daily_loss_amount:
            messages.append(
                f"Tagesverlust {verlust:,.0f} USD - mehr als die Haelfte des Limits "
                f"({risk.max_daily_loss_amount:,.0f} USD)."
            )
        if self.config.news.enabled and (session.news is None or session.news.is_empty):
            messages.append(
                "Nachrichtenfilter ist eingeschaltet, aber es liegen keine Termine vor - "
                "er sperrt nichts (scripts/fetch_news.py)."
            )
        if session.feed_name == "replay":
            messages.append(
                "Wiedergabe-Feed: historische Bars. Prueft den Betrieb, nicht den Markt."
            )
        messages.extend(self._broker_warnings())
        return tuple(messages)

    def _broker_warnings(self) -> list[str]:
        """Was am Brokerzustand den Zahlen widerspricht.

        Der gefaehrlichste Fall steht zuerst: eine Sitzung, die aussieht, als
        wuerde sie handeln, deren Orders aber nirgends ankommen.
        """
        broker = self._broker
        if broker is None:
            if self.config.broker.enabled:
                messages = [
                    "broker.enabled ist gesetzt, aber es besteht keine Anbindung - "
                    "die Trades dieser Sitzung sind simuliert."
                ]
                return messages
            return []

        zustand = broker.state()
        messages: list[str] = []
        if not zustand.connected:
            messages.append("Broker getrennt: es entstehen keine neuen Orders.")
        elif zustand.blocked_reason:
            messages.append(f"Broker gesperrt ({zustand.blocked_reason}): keine neuen Orders.")
        elif not zustand.is_paper:
            messages.append("Konto NICHT als Paper-Konto belegt - Orderweg gesperrt.")
        nicht_handelbar = [s for s in broker.symbols if s not in zustand.tradeable_symbols]
        if nicht_handelbar:
            messages.append(
                f"Beim Broker nicht handelbar: {', '.join(nicht_handelbar)} - "
                "Signale dafuer werden abgelehnt."
            )
        return messages
