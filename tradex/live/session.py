"""Die Handelssitzung: das Programm handelt selbst (Phase 5, Spec Paragraph 24).

Was hier NICHT steht - und warum das der wichtigste Teil ist
------------------------------------------------------------
Es gibt in diesem Modul keine Analyse, keine Regel, keine Fuellogik und keine
Positionsgroesse. Alles davon kommt unveraendert aus `SymbolBook` - genau der
Klasse, die auch den Backtest fuehrt.

Das ist Architektur-Invariante 3, eine Ebene hoeher: Backtest und Papertrading
sind nicht "zwei Wege, die dasselbe tun sollen", sondern derselbe Weg mit
verschiedenen Bar-Quellen. Der Backtest zieht die Bars aus einer Datei, die
Sitzung aus einem Feed. Ein Papertrade entsteht deshalb aus demselben Code, der
im Backtest den simulierten Trade erzeugt hat - mitsamt Schlupf, Gebuehren und
der pessimistischen Annahme "Stop zuerst".

Waere die Ausfuehrung hier neu geschrieben, waere die Frage "handelt es so, wie
der Backtest sagt?" nicht mehr beantwortbar. Sie waere zu einer Behauptung
geworden.

Was diese Klasse tatsaechlich beitraegt
---------------------------------------
Vier Dinge, die ein Backtest nicht braucht:

    1. Bars kommen einzeln und unvorhersehbar statt als fertige Reihe
    2. Der Feed kann ausfallen - und Stille ist von einem ruhigen Markt nicht
       zu unterscheiden, solange niemand die Uhr befragt
    3. Ergebnisse muessen sofort haltbar gemacht werden, nicht am Ende
    4. Es braucht einen Not-Aus, der sofort greift

Der Not-Aus (Spec Paragraph 24)
-------------------------------
Angehalten wird ueber die Risk Engine, nicht dadurch, dass keine Bars mehr
eingespeist werden. Der Unterschied ist entscheidend: eine angehaltene Sitzung
verarbeitet Bars WEITER und fuehrt offene Positionen zu Ende - sie nimmt nur
keine neuen auf. Wer stattdessen die Bars abklemmt, laesst offene Positionen
ohne Stopueberwachung zurueck. Genau das waere die gefaehrlichste Reaktion auf
eine Stoerung.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from tradex.backtest.execution import SimulatedTrade
from tradex.backtest.runner import BACKTEST_VERSION, SymbolBook
from tradex.config import Config
from tradex.domain.bars import Bar
from tradex.domain.instruments import Instrument
from tradex.live.feed import BarMessage, FeedMessage, HeartbeatMessage, StatusMessage
from tradex.logging_setup import get_logger
from tradex.risk.ledger import RiskLedger
from tradex.strategy.registry import load_news_calendar

log = get_logger(__name__)

Clock = Callable[[], int]
"""Wanduhr in Epoch-Nanosekunden. Injizierbar, damit Tests die Zeit fuehren
koennen - eine Sitzung, deren Ueberwachung sich nur in Echtzeit pruefen laesst,
wird nicht geprueft."""

#: Gruende, aus denen eine Sitzung keine neuen Positionen mehr aufnimmt.
HALT_FEED_STALE = "feed_stale"
HALT_FEED_DISCONNECTED = "feed_disconnected"
HALT_MANUAL = "manual"
HALT_FEED_FINISHED = "feed_finished"
HALT_NOT_CONNECTED = "not_connected"
"""Ausgangszustand jeder Sitzung. Sie beginnt angehalten und nimmt erst
Positionen auf, wenn der Feed sich gemeldet hat - andernfalls koennte sie auf
Bars handeln, ohne dass je eine Verbindung bestaetigt wurde."""


@dataclass(slots=True)
class SessionStatus:
    """Was die Sitzung gerade tut - fuer Anzeige und Protokoll."""

    running: bool
    feed: str
    symbols: tuple[str, ...]
    mode: str
    started_ts: int
    last_bar_ts: int
    """Zeitstempel der letzten verarbeiteten BAR (Marktzeit)."""
    last_message_ts: int
    """Wanduhr der letzten Nachricht irgendeiner Art - Grundlage der
    Stille-Ueberwachung. Getrennt von `last_bar_ts`, weil ein ruhiger Markt
    keine Bars, aber Lebenszeichen liefert."""
    connected: bool
    halted_reason: str
    bars_seen: int
    signals: int
    trades_closed: int
    open_positions: int
    start_equity: float
    realized_pnl: float
    day_pnl: float
    trading_day: int

    @property
    def equity(self) -> float:
        return self.start_equity + self.realized_pnl

    @property
    def accepts_entries(self) -> bool:
        return self.running and not self.halted_reason

    @property
    def seconds_since_message(self) -> float:
        return 0.0


@dataclass(slots=True)
class _BookState:
    """Buchfuehrung je Symbol, die der Backtest nicht braucht."""

    book: SymbolBook
    index: int = 0
    published: int = 0
    """Wie viele Trades dieses Buchs bereits weitergereicht wurden. Der Backtest
    holt sie am Ende alle auf einmal ab; live muessen sie einzeln heraus,
    sobald sie entstehen."""
    last_bar: Bar | None = None


@dataclass(slots=True)
class SessionConfig:
    """Betriebsparameter der Sitzung - keine Handelsregeln."""

    max_silence_seconds: float = 15.0
    """Ohne Nachricht laenger als das gilt der Feed als verloren (Spec
    Paragraph 24, gleicher Wert wie die Bridge-Spezifikation)."""
    resume_after_silence: bool = True
    """Ob eine wiederkehrende Verbindung den Not-Aus selbst zuruecknimmt.
    Beim Wiedergabe-Feed sinnvoll, im Echtbetrieb eine bewusste Entscheidung."""
    trade_sink: Callable[[SimulatedTrade], None] | None = None
    """Wohin fertige Trades sofort gehen. Ohne Senke bleiben sie nur im
    Speicher - fuer Tests richtig, fuer den Betrieb nicht."""


class TradingSession:
    """Fuehrt Strategien gegen einen Live-Feed. Ein Konto, mehrere Symbole."""

    def __init__(
        self,
        instruments: dict[str, Instrument],
        config: Config,
        feed_name: str,
        session_config: SessionConfig | None = None,
        clock: Clock = time.time_ns,
    ) -> None:
        if not instruments:
            raise ValueError("Eine Sitzung ohne Instrumente kann nichts handeln")

        self.config = config
        self.params = session_config or SessionConfig()
        self.feed_name = feed_name
        self.clock = clock

        # EIN Risikobuch und EIN Nachrichtenkalender fuer alle Symbole -
        # dieselbe Begruendung wie im Backtest: die Grenzen gelten dem Konto.
        self.ledger = RiskLedger()
        self.news = load_news_calendar(config)
        self.books = {
            name.upper(): _BookState(
                SymbolBook(name, item, config, self.ledger, news=self.news)
            )
            for name, item in instruments.items()
        }

        self.started_ts = clock()
        self.last_bar_ts = 0
        self.last_message_ts = self.started_ts
        self.connected = False
        self.halted_reason = ""
        self.running = True
        self.bars_seen = 0
        self.trades: list[SimulatedTrade] = []
        self.events: list[tuple[int, str, str]] = []
        """(ts, art, text) - Verbindungswechsel und Not-Aus, fuer die Anzeige."""

        # Sicher beginnen: erst wenn der Feed sich meldet, entstehen Positionen.
        # Die Umkehrung waere gefaehrlicher, als sie klingt - eine Sitzung, die
        # Bars aus einer unbestaetigten Quelle handelt, faellt nicht auf.
        self.halt(HALT_NOT_CONNECTED)

    # --------------------------------------------------------------- Nachrichten
    def on_message(self, message: FeedMessage) -> list[SimulatedTrade]:
        """Eine Feed-Nachricht verarbeiten. Liefert neu geschlossene Trades."""
        self.last_message_ts = self.clock()

        if isinstance(message, BarMessage):
            return self.on_bar(message.symbol, message.bar)
        if isinstance(message, HeartbeatMessage):
            # Nichts zu tun - der Zeitstempel oben IST die Wirkung.
            return []
        if isinstance(message, StatusMessage):
            self._on_status(message)
            return []
        raise TypeError(f"Unbekannte Feed-Nachricht: {type(message)!r}")

    def on_messages(self, messages: Iterable[FeedMessage]) -> list[SimulatedTrade]:
        closed: list[SimulatedTrade] = []
        for message in messages:
            closed.extend(self.on_message(message))
        return closed

    def _on_status(self, message: StatusMessage) -> None:
        was = self.connected
        self.connected = message.connected
        if was == message.connected:
            return
        self._note("feed", f"{'verbunden' if message.connected else 'getrennt'}: {message.detail}")
        if not message.connected:
            self.halt(HALT_FEED_DISCONNECTED)
            return
        # Die erste Verbindung hebt den Anfangszustand IMMER auf - sonst liefe
        # eine frisch gestartete Sitzung nie los. Nach einem Abriss entscheidet
        # dagegen die Einstellung: im Echtbetrieb soll ein Wiederanlauf eine
        # Entscheidung sein und kein Automatismus.
        if self.halted_reason == HALT_NOT_CONNECTED or self.params.resume_after_silence:
            self.resume()

    # ---------------------------------------------------------------------- Bar
    def on_bar(self, symbol: str, bar: Bar) -> list[SimulatedTrade]:
        """Eine geschlossene Bar verarbeiten - derselbe Weg wie im Backtest."""
        key = symbol.upper()
        state = self.books.get(key)
        if state is None:
            # Ein Symbol, fuer das keine Strategie laeuft, wird nicht heimlich
            # mitanalysiert - es waere im Protokoll unsichtbar.
            log.warning("bar_fuer_unbekanntes_symbol", symbol=key)
            return []

        if state.last_bar is not None and bar.ts <= state.last_bar.ts:
            # Doppelte oder rueckwaerts laufende Bars kommen bei einem
            # Neustart des Feeds vor. Sie zweimal zu analysieren wuerde
            # Detektoren Zustaende sehen lassen, die es nie gab.
            log.warning("bar_nicht_aufsteigend", symbol=key, ts=bar.ts, letzte=state.last_bar.ts)
            return []

        state.book.on_bar(bar, state.index)
        state.index += 1
        state.last_bar = bar
        self.last_bar_ts = max(self.last_bar_ts, bar.ts)
        self.bars_seen += 1

        return self._collect(state)

    def _collect(self, state: _BookState) -> list[SimulatedTrade]:
        """Was das Buch auf dieser Bar abgeschlossen hat, herausreichen."""
        fresh = state.book.trades[state.published :]
        state.published = len(state.book.trades)
        for trade in fresh:
            self.trades.append(trade)
            log.info(
                "paper_trade_geschlossen",
                symbol=trade.symbol,
                strategy=trade.strategy,
                side=trade.side,
                r=round(trade.r_multiple, 3),
                pnl=round(trade.pnl, 2),
                grund=trade.exit_reason.value,
            )
            if self.params.trade_sink is not None:
                self.params.trade_sink(trade)
        return list(fresh)

    # ------------------------------------------------------------------ Not-Aus
    def check_liveness(self) -> None:
        """Stille pruefen. Muss regelmaessig aufgerufen werden, auch ohne Bars.

        Ohne diese Pruefung waere ein abgerissener Feed nicht von einem ruhigen
        Markt zu unterscheiden - die Sitzung wuerde weiterhandeln, sobald der
        Feed mit alten Kursen zurueckkommt.
        """
        silence = (self.clock() - self.last_message_ts) / 1e9
        if silence > self.params.max_silence_seconds:
            self.halt(HALT_FEED_STALE)

    def halt(self, reason: str) -> None:
        """Keine neuen Positionen mehr. Offene laufen weiter zu ihrem Stop."""
        if self.halted_reason == reason:
            return
        self.halted_reason = reason
        for state in self.books.values():
            state.book.strategy.risk.halt_reason = reason
        self._note("halt", reason)
        log.warning("sitzung_angehalten", grund=reason, offene_positionen=self.ledger.open_count)

    def resume(self) -> None:
        if not self.halted_reason:
            return
        self._note("resume", f"nach {self.halted_reason}")
        log.info("sitzung_fortgesetzt", vorher=self.halted_reason)
        self.halted_reason = ""
        for state in self.books.values():
            state.book.strategy.risk.halt_reason = ""

    def stop(self) -> None:
        """Sitzung beenden. Offene Positionen bleiben offen - bewusst.

        Sie automatisch zu schliessen waere eine Handelsentscheidung, die
        niemand getroffen hat. Beim Papertrading kostet das nichts; als
        Gewohnheit fuer den Echtbetrieb waere es fatal.
        """
        self.running = False
        self.halt(HALT_MANUAL)

    def _note(self, kind: str, text: str) -> None:
        self.events.append((self.clock(), kind, text))

    # ------------------------------------------------------------------ Auskunft
    @property
    def open_positions(self) -> int:
        return self.ledger.open_count

    @property
    def realized_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    def status(self) -> SessionStatus:
        trading_day = max((t.trading_day for t in self.trades), default=0)
        return SessionStatus(
            running=self.running,
            feed=self.feed_name,
            symbols=tuple(sorted(self.books)),
            mode=self.config.execution.mode.value,
            started_ts=self.started_ts,
            last_bar_ts=self.last_bar_ts,
            last_message_ts=self.last_message_ts,
            connected=self.connected,
            halted_reason=self.halted_reason,
            bars_seen=self.bars_seen,
            signals=sum(len(s.book.strategy.signals) for s in self.books.values()),
            trades_closed=len(self.trades),
            open_positions=self.open_positions,
            start_equity=self.config.risk.account_size,
            realized_pnl=self.realized_pnl,
            day_pnl=sum(t.pnl for t in self.trades if t.trading_day == trading_day),
            trading_day=trading_day,
        )

    @property
    def backtest_version(self) -> str:
        """Welche Ausfuehrungsannahmen gelten - dieselben wie im Backtest.

        Steht in jedem gespeicherten Lauf. Waeren es andere, waeren Papertrades
        und Backtest nicht vergleichbar, und der ganze Aufbau haette seinen
        Zweck verfehlt.
        """
        return BACKTEST_VERSION


@dataclass(slots=True)
class SessionSummary:
    """Kurzfassung am Ende - was die Sitzung erreicht hat."""

    status: SessionStatus
    trades: tuple[SimulatedTrade, ...] = field(default_factory=tuple)
