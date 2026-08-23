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
from dataclasses import dataclass, field

from tradex.backtest.execution import SimulatedTrade
from tradex.backtest.runner import BACKTEST_VERSION
from tradex.config import Config, resolved_config_path
from tradex.data.store import BarStore
from tradex.domain.instruments import Instrument
from tradex.live.feed import LiveFeed
from tradex.live.nt8_feed import DEFAULT_HOST, DEFAULT_PORT, NinjaTraderFeed
from tradex.live.replay_feed import ReplayFeed
from tradex.live.runner import SessionRunner
from tradex.live.session import HALT_MANUAL, SessionConfig, SessionStatus, TradingSession
from tradex.live.store import SessionStore
from tradex.logging_setup import get_logger
from tradex.persistence.db import init_database
from tradex.persistence.decision_log import DecisionLog

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


def build_session(
    request: SessionRequest,
    config: Config,
    instruments: dict[str, Instrument],
    feed_name: str,
    sink: SessionStore | None,
) -> TradingSession:
    return TradingSession(
        {name: instruments[name] for name in request.symbols},
        config,
        feed_name=feed_name,
        session_config=SessionConfig(
            trade_sink=sink.record_trade if sink is not None else None,
        ),
    )


class SessionManager:
    """Fuehrt hoechstens EINE Sitzung - und macht sie steuerbar.

    Hoechstens eine: zwei gleichzeitige Sitzungen haetten getrennte
    Risikobuecher und damit zusammen das doppelte erlaubte Risiko. Genau der
    Fehler, den `portfolio.py` eine Ebene tiefer verhindert.
    """

    def __init__(self, config: Config, instruments: dict[str, Instrument]) -> None:
        self.config = config
        self.instruments = instruments
        self._lock = threading.Lock()
        self._session: TradingSession | None = None
        self._runner: SessionRunner | None = None
        self._thread: threading.Thread | None = None
        self._store: SessionStore | None = None
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
            session = build_session(request, self.config, self.instruments, feed.name, store)
            runner = SessionRunner(session, feed)

            self._session, self._runner, self._store = session, runner, store
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
        return ManagerState(
            active=self.is_running,
            feed=session.feed_name if session else "",
            symbols=tuple(sorted(session.books)) if session else (),
            session_id=self._store.session_id if self._store else 0,
            status=status,
            stopped_by=self._stopped_by,
            error=self._error,
            warnings=self._warnings(session, status),
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
        return tuple(messages)
