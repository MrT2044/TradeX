"""Anwendungsschicht zwischen Engine und API.

Haelt den Laufzeitzustand (geladene Instrumente, MarketContext je Symbol,
Entscheidungsprotokoll) und stellt genau die Operationen bereit, die das UI
braucht. Die Analyse selbst passiert ausschliesslich in `MarketContext` - hier
wird nur orchestriert.

Replay-Cursor
-------------
Die Basis-Serie wird vollstaendig geladen, aber nicht zwingend vollstaendig in
die Analyse gegeben. Der Cursor sagt, bis wohin gefuettert wurde. Damit laesst
sich Bar fuer Bar vorwaerts gehen und beobachten, wann welcher Detektor
anspringt - genau das, wofuer Phase 2 gedacht ist: die Schwellenwerte visuell
gegen echte Kursverlaeufe pruefen, bevor eine Strategie darauf aufbaut.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from tradex.analysis.context import ContextSnapshot, MarketContext, TimeframeUpdate
from tradex.backtest.report import BacktestReport
from tradex.backtest.report import build as build_report
from tradex.backtest.runner import run_backtest
from tradex.backtest.store import BacktestStore
from tradex.config import Config, get_instrument, get_instruments, resolved_config_path
from tradex.data.integrity import IntegrityReport, check
from tradex.data.provider import MarketDataProvider, ProviderRegistry
from tradex.data.replay_provider import ReplayProvider
from tradex.data.sessions import SessionCalendar
from tradex.data.store import BarStore, Coverage
from tradex.domain.bars import Bar, BarSeries
from tradex.domain.enums import Timeframe, TradingMode
from tradex.domain.instruments import Instrument
from tradex.live.manager import SessionManager
from tradex.live.nt8_feed import DEFAULT_HOST, DEFAULT_PORT
from tradex.live.nt8_history import HistoryResult, fetch_history
from tradex.live.store import SessionStore
from tradex.live.watch import MarketWatch
from tradex.logging_setup import get_logger
from tradex.persistence.db import init_database
from tradex.persistence.decision_log import DecisionLog, utc_now_iso
from tradex.persistence.models import DecisionRecord
from tradex.risk.consistency import ConsistencyIssue, check_configuration
from tradex.strategy.portfolio import StrategyPortfolio
from tradex.strategy.registry import build_portfolio
from tradex.strategy.setup import SetupCandidate
from tradex.strategy.signal import StrategyDecision, TradeSignal

log = get_logger(__name__)

#: Obergrenze fuer einen einzelnen Ladevorgang. Schuetzt das UI davor, versehentlich
#: mehrere Jahre 1m-Daten in eine einzige Antwort zu ziehen.
MAX_LOAD_BARS = 400_000

#: Kennung der aktuell aktiven Regelfassung (Spec §21). Sie steht in jedem
#: Protokolleintrag, damit spaeter nachvollziehbar bleibt, nach welchen Regeln
#: eine Entscheidung fiel.
STRATEGY_VERSION = "phase3-strategy-v1"

#: Obergrenze fuer einen Backtest aus dem UI heraus. Der Lauf blockiert die
#: Antwort; ueber mehrere Jahre 1m-Daten waere das Fenster minutenlang taub.
#: Fuer lange Zeitraeume gibt es `scripts/run_backtest.py`.
MAX_BACKTEST_BARS = 1_500_000


@dataclass(frozen=True, slots=True)
class MarketStatus:
    """Marktzustand zur Wanduhrzeit - unabhaengig davon, was gerade ankommt."""

    symbol: str
    server_ts: int
    """Wanduhr des Servers. Gehoert dazu, weil der Betrachter sonst nicht
    unterscheiden kann, ob der Markt zu ist oder die Anzeige steht."""
    session: str
    is_open: bool
    is_rth: bool
    timezone: str
    """Boersenzeitzone des Instruments - die Zone, in der diese Aussage gilt."""


@dataclass
class SymbolState:
    """Laufzeitzustand eines geladenen Instruments."""

    symbol: str
    instrument: Instrument
    context: MarketContext
    strategy: StrategyPortfolio
    base: BarSeries
    cursor: int = 0
    """Anzahl bereits in die Analyse gegebener Basis-Bars."""
    integrity: IntegrityReport | None = None
    last_updates: list[TimeframeUpdate] = field(default_factory=list)
    last_decisions: list[StrategyDecision] = field(default_factory=list)

    @property
    def exhausted(self) -> bool:
        return self.cursor >= len(self.base)

    @property
    def progress(self) -> float:
        return self.cursor / len(self.base) if len(self.base) else 1.0


class TradexService:
    """Zentrale Anwendungsschicht. Eine Instanz pro laufender Anwendung."""

    def __init__(self, config: Config, config_path: Path | None = None) -> None:
        self.config = config
        # Nicht fest default.yaml annehmen: laeuft der Dienst unter
        # TRADEX_CONFIG, muss der gespeicherte Hash zu DIESER Datei gehoeren.
        self.config_path = config_path or resolved_config_path()
        self.store = BarStore(config.path(config.data.parquet_dir))
        self.database = config.path(config.data.database)
        init_database(self.database)
        self.decision_log = DecisionLog(self.database)
        self.backtest_store = BacktestStore(self.database)
        self.config_hash = self.decision_log.register_config(self.config_path)
        self._backtests: dict[str, BacktestReport] = {}
        # Der laufende Betrieb (Phase 7). Bewusst hier und nicht im
        # API-Modul: dieselbe Engine soll sich headless genauso betreiben
        # lassen wie hinter der Oberflaeche.
        self.sessions = SessionManager(config, get_instruments(), events=self.decision_log)
        self.session_store = SessionStore(self.database)

        self.providers = ProviderRegistry()
        self.providers.register(ReplayProvider(self.store))
        self._states: dict[str, SymbolState] = {}
        #: Laufende Marktbeobachtung (Chart ohne Handel). Hoechstens eine -
        #: es gibt nur eine Verbindung zur Bridge.
        self._watch: MarketWatch | None = None

        log.info(
            "service_started",
            mode=config.execution.mode.value,
            config_hash=self.config_hash,
            database=str(self.database),
        )

    def close(self) -> None:
        # Zuerst den Betrieb anhalten, dann die Datenbanken schliessen: eine
        # noch laufende Sitzung wuerde sonst in eine geschlossene Verbindung
        # schreiben, und der letzte Trade waere genau der, der fehlt.
        self.stop_watch()
        if self.sessions.is_running:
            self.sessions.stop()
        self.session_store.close()
        self.decision_log.close()
        self.backtest_store.close()

    def session_runs(self, limit: int = 20) -> list[dict[str, object]]:
        """Frueher gelaufene Sitzungen aus dem Archiv."""
        return self.session_store.sessions(limit)

    # -------------------------------------------------------------- Stammdaten
    def instruments(self) -> dict[str, Instrument]:
        return get_instruments()

    def coverage(self) -> list[Coverage]:
        """Was tatsaechlich lokal vorliegt - Grundlage der Symbolauswahl im UI."""
        result = []
        for symbol in self.store.symbols():
            for timeframe in self.store.timeframes(symbol):
                item = self.store.coverage(symbol, timeframe)
                if item:
                    result.append(item)
        return result

    def provider(self, name: str) -> MarketDataProvider:
        return self.providers.get(name)

    # ------------------------------------------------------------------ Laden
    def load(
        self,
        symbol: str,
        start_ts: int | None = None,
        end_ts: int | None = None,
        max_bars: int = MAX_LOAD_BARS,
        feed_all: bool = True,
    ) -> SymbolState:
        """Basis-Bars laden und optional sofort komplett analysieren.

        `feed_all=False` laedt nur, ohne zu analysieren - der Ausgangspunkt fuer
        einen schrittweisen Replay.
        """
        symbol = symbol.upper()
        instrument = get_instrument(symbol)
        base_timeframe = self.config.data.base_timeframe

        series = self.store.read(symbol, base_timeframe, start_ts, end_ts, limit=max_bars)
        if len(series) == 0:
            raise LookupError(
                f"Keine {base_timeframe.value}-Daten fuer {symbol} im lokalen Speicher. "
                "Zuerst importieren: scripts/fetch_databento.py oder scripts/generate_demo_data.py"
            )

        report = check(
            series,
            symbol,
            base_timeframe,
            SessionCalendar(instrument),
            self.config.data.min_gap_bars,
        )
        if not report.is_clean:
            log.warning("data_integrity", summary=report.summary())
            for gap in report.gaps:
                log.warning("data_gap", symbol=symbol, gap=str(gap))

        issues = check_configuration(self.config, instrument)
        for issue in issues:
            log.warning("config_inconsistent", code=issue.code, message=issue.message)

        state = SymbolState(
            symbol=symbol,
            instrument=instrument,
            context=MarketContext(symbol, instrument, self.config),
            strategy=build_portfolio(symbol, instrument, self.config),
            base=series,
            integrity=report,
        )
        self._states[symbol] = state

        log.info(
            "symbol_loaded",
            symbol=symbol,
            bars=len(series),
            clean=report.is_clean,
            gaps=len(report.gaps),
        )
        if feed_all:
            self.step(symbol, len(series))
        return state

    def state(self, symbol: str) -> SymbolState:
        try:
            return self._states[symbol.upper()]
        except KeyError as exc:
            raise LookupError(f"{symbol} ist nicht geladen. Zuerst /api/load aufrufen.") from exc

    def is_loaded(self, symbol: str) -> bool:
        return symbol.upper() in self._states

    def loaded_symbols(self) -> list[str]:
        return sorted(self._states)

    # ------------------------------------------------------------------ Replay
    def step(self, symbol: str, count: int = 1) -> list[TimeframeUpdate]:
        """`count` weitere Basis-Bars analysieren.

        Das ist derselbe Aufruf, den spaeter der Live-Feed macht - es gibt keinen
        gesonderten Replay-Pfad (Architektur-Invariante 3).
        """
        state = self.state(symbol)
        updates: list[TimeframeUpdate] = []
        decisions: list[StrategyDecision] = []
        end = min(state.cursor + max(count, 0), len(state.base))
        for i in range(state.cursor, end):
            step_updates = state.context.on_base_bar(state.base[i])
            if not step_updates:
                continue
            updates.extend(step_updates)
            decisions.extend(state.strategy.on_updates(step_updates, state.context))
        state.cursor = end
        state.last_updates = updates
        state.last_decisions = decisions
        if decisions:
            self._persist_decisions(decisions)
        return updates

    # --------------------------------------------------------- Beobachtung
    def start_watch(self, symbol: str, host: str = "", port: int = 0) -> MarketWatch:
        """Ein Symbol live mitlesen, ohne zu handeln.

        Wird ein anderes Symbol beobachtet, wird gewechselt: es gibt nur eine
        Verbindung zur Bridge. Laeuft eine Handelssitzung, wird abgelehnt -
        die fuehrt ihren eigenen Zustand, und der Chart folgt ihr ohnehin.
        """
        symbol = symbol.upper()
        if self.sessions.is_running:
            raise LookupError(
                "Es laeuft eine Handelssitzung - deren Daten zeigt der Chart bereits."
            )
        if self._watch is not None:
            if self._watch.symbol == symbol and self._watch.is_running:
                return self._watch
            self.stop_watch()

        instrument = get_instrument(symbol)
        if not instrument.nt8_symbol:
            raise LookupError(f"Fuer {symbol} ist kein nt8_symbol hinterlegt.")

        # Ein Zustand muss existieren, bevor Bars hineinlaufen koennen. Fuer die
        # echten Kontrakte gibt es keinen gespeicherten Bestand, also wird hier
        # ein leerer angelegt - `load()` verlangt Dateien und waere dafuer der
        # falsche Weg.
        if symbol not in self._states:
            self._states[symbol] = SymbolState(
                symbol=symbol,
                instrument=instrument,
                context=MarketContext(symbol, instrument, self.config),
                strategy=build_portfolio(symbol, instrument, self.config),
                base=BarSeries(),
            )

        watch = MarketWatch(
            symbol,
            instrument,
            self.config.data.base_timeframe,
            self._on_watch_bar,
            host=host or DEFAULT_HOST,
            port=port or DEFAULT_PORT,
            history_days=self.config.live.nt8_history_days,
            history_timeout_seconds=self.config.live.nt8_history_timeout_seconds,
        )
        self._watch = watch
        watch.start()
        return watch

    def stop_watch(self) -> None:
        watch, self._watch = self._watch, None
        if watch is not None:
            watch.stop()

    @property
    def watch(self) -> MarketWatch | None:
        return self._watch

    def _on_watch_bar(self, symbol: str, bar: Bar) -> None:
        """Eine Live-Bar analysieren - derselbe Pfad wie Wiedergabe und Backtest.

        Laeuft im Beobachtungsfaden. Geschrieben wird nur in den Zustand dieses
        Symbols; gelesen wird von der API ohne Sperre, aus demselben Grund wie
        bei `SessionManager.state()` - eine Sperre koppelte die Anzeige an den
        Datenfaden.
        """
        state = self._states.get(symbol)
        if state is None:
            return
        updates = state.context.on_base_bar(bar)
        if not updates:
            return
        decisions = state.strategy.on_updates(updates, state.context)
        state.last_updates = updates
        state.last_decisions = decisions
        if decisions:
            self._persist_decisions(decisions)

    def _persist_decisions(self, decisions: list[StrategyDecision]) -> None:
        """Strategieentscheidungen ins Protokoll schreiben (Spec §25).

        Auch die Ablehnungen - genau sie beantworten spaeter die Frage, warum
        ein Trade NICHT gemacht wurde. Als Batch, weil bei einem Durchlauf ueber
        Monate schnell Zehntausende Eintraege zusammenkommen.
        """
        records = [
            DecisionRecord(
                ts_utc=utc_now_iso(),
                bar_ts=decision.ts,
                symbol=decision.symbol,
                timeframe=decision.timeframe,
                decision=decision.decision,
                config_hash=self.config_hash,
                strategy_version=STRATEGY_VERSION,
                htf_bias=decision.htf_bias,
                liquidity_sweep=decision.checklist.get("sweep"),
                displacement=decision.checklist.get("displacement"),
                fvg=decision.checklist.get("fvg"),
                retracement=decision.checklist.get("retracement"),
                mss=decision.checklist.get("mss"),
                rr=decision.signal.rr if decision.signal else None,
                risk_pct=(
                    self.config.risk.risk_per_trade_pct if decision.signal else None
                ),
                reasons=decision.reasons,
                context={"setup_id": decision.setup_id, "stage": decision.stage},
            )
            for decision in decisions
        ]
        self.decision_log.record_many(records)

    def reset(self, symbol: str) -> SymbolState:
        """Analyse zuruecksetzen, Daten behalten."""
        state = self.state(symbol)
        state.context = MarketContext(state.symbol, state.instrument, self.config)
        state.strategy = build_portfolio(state.symbol, state.instrument, self.config)
        state.cursor = 0
        state.last_updates = []
        state.last_decisions = []
        return state

    # ------------------------------------------------------------------ Abfragen
    def snapshot(self, symbol: str, max_items: int = 50) -> ContextSnapshot:
        return self.chart_context(symbol).snapshot(max_items)

    # -------------------------------------------------------------- Historie
    def import_nt8_history(
        self, symbol: str, days: int = 0, host: str = "", port: int = 0
    ) -> HistoryResult:
        """Historie aus NinjaTrader nachladen und ablegen.

        Bewusst OHNE Sitzung: eine Sitzung zu starten heisst, den Handel
        scharfzuschalten, und wer nur sehen will wo der Kurs steht, sollte
        dafuer nicht handeln muessen.

        Laeuft eine Sitzung, wird abgelehnt statt eine zweite Verbindung zur
        Bridge aufzumachen. Zwei Clients am selben AddOn sind kein Fehler, den
        man beim Zusehen bemerkt - aber der Betrieb ist wichtiger als die
        Bequemlichkeit, und im Zweifel hat er Vorrang.
        """
        if self.sessions.is_running:
            raise LookupError(
                "Waehrend einer laufenden Sitzung wird keine Historie nachgeladen - "
                "die Sitzung holt sie beim Start selbst."
            )
        symbol = symbol.upper()
        instrument = get_instruments().get(symbol)
        if instrument is None:
            raise LookupError(f"Unbekanntes Instrument {symbol}")
        if not instrument.nt8_symbol:
            raise LookupError(
                f"Fuer {symbol} ist kein nt8_symbol hinterlegt - NinjaTrader kennt es nicht."
            )

        series, result = fetch_history(
            symbol,
            self.config.data.base_timeframe,
            days=days or self.config.live.nt8_history_days,
            host=host or DEFAULT_HOST,
            port=port or DEFAULT_PORT,
            contract=instrument.nt8_symbol,
            timeout_seconds=self.config.live.nt8_history_timeout_seconds,
        )
        if len(series):
            # `write` ersetzt nach Zeitstempel und ist idempotent - zweimal
            # abholen aendert nichts, und ein abgebrochener Abruf laesst sich
            # einfach wiederholen.
            self.store.write(symbol, self.config.data.base_timeframe, series)
        return result

    def chart_context(self, symbol: str) -> MarketContext:
        """Woher der Chart seine Bars nimmt.

        Laeuft eine Sitzung fuer dieses Symbol, gilt DEREN Analysezustand: das
        ist der Betrieb, und wer zusieht, will sehen, was gerade passiert - und
        nicht, wo die Wiedergabe stehengeblieben ist. Sonst der geladene
        Wiedergabe-Zustand.

        Die Auswahl steht bewusst hier und nicht in der API-Schicht: sonst
        muesste jeder Endpunkt sie einzeln treffen, und der erste, der es
        vergisst, zeigt im Betrieb alte Kurse an.
        """
        # Nur eine LAUFENDE Sitzung hat Vorrang. Der `SessionManager` haelt
        # seine beendete Sitzung absichtlich fest; ohne diese Bedingung blieb
        # der Chart nach jedem Stopp auf dem eingefrorenen Endstand stehen,
        # auch wenn die Beobachtung laengst wieder Bars lieferte.
        live = self.sessions.context(symbol) if self.sessions.is_running else None
        return live if live is not None else self.state(symbol).context

    def is_live(self, symbol: str) -> bool:
        """Kommt der Chart gerade aus dem LAUFENDEN Betrieb?

        `is_running` ist noetig, weil der `SessionManager` seine Sitzung beim
        Stopp absichtlich stehen laesst - sonst meldete diese Auskunft nach
        jeder beendeten Sitzung bis zum Programmende "live".
        """
        return self.sessions.is_running and self.sessions.context(symbol) is not None

    def bars(self, symbol: str, timeframe: Timeframe, limit: int | None = None) -> BarSeries:
        return self.chart_context(symbol).series(timeframe)

    def forming(self, symbol: str, timeframe: Timeframe) -> Bar | None:
        return self.chart_context(symbol).forming(timeframe)

    # -------------------------------------------------------- Marktzustand
    def market_status(self, symbol: str) -> MarketStatus:
        """Ist der Markt JETZT offen - nach Uhr und Handelskalender.

        Die Anzeige las das bisher aus `snapshot.session`, also aus der
        Session der zuletzt ANALYSIERTEN Bar. Fuer eine Wiedergabe ist das
        richtig, im Betrieb ist es falsch: liegt der geladene Bestand ein paar
        Tage zurueck, meldet die Kopfzeile "geschlossen", waehrend nebenan
        Ticks hereinlaufen. Zwei Quellen fuer dieselbe Frage - und die
        auffaelligere von beiden war die falsche.

        Bewusst NICHT aus ankommenden Daten abgeleitet. Historie, verzoegerte
        Kurse und ein Simulationsfeed kommen auch bei geschlossener Boerse an;
        wer daraus auf "offen" schliesst, baut sich eine Anzeige, die genau
        dann luegt, wenn es darauf ankommt. Der Kalender weiss es, die
        Datenlage nicht.
        """
        symbol = symbol.upper()
        try:
            instrument = get_instrument(symbol)
        except LookupError:
            instrument = get_instrument(self.config.data.default_symbol)
        now = time.time_ns()
        info = SessionCalendar(instrument).info_at(now)
        return MarketStatus(
            symbol=symbol,
            server_ts=now,
            session=info.session.value,
            is_open=info.is_open,
            is_rth=info.is_rth,
            timezone=instrument.exchange_timezone,
        )

    # ------------------------------------------------------- Laufende Kerze
    def _tick_source(self, symbol: str) -> tuple[Bar | None, int]:
        """(laufende Tickbar, Wanduhr des letzten Ticks) - Sitzung vor Beobachtung.

        Dieselbe Rangfolge wie `chart_context`, und aus demselben Grund: Kurse
        und Kerzen muessen aus derselben Quelle kommen. Zwei Quellen
        nebeneinander faellt niemandem auf und ist genau deshalb gefaehrlich.
        """
        symbol = symbol.upper()
        # `is_running` gehoert dazu: `SessionManager` raeumt `_session` und
        # `_feed` beim Stopp NICHT weg (der Abschlussbericht braucht sie noch).
        # Ohne diese Bedingung zeigte die Kerze nach dem Beenden einer Sitzung
        # weiter auf deren toten Feed - der Kurs stand still, waehrend die
        # inzwischen wieder laufende Beobachtung daneben Ticks bekam. Ein
        # eingefrorener Kurs sieht aus wie ein ruhiger Markt.
        if self.sessions.is_running and self.sessions.context(symbol) is not None:
            return self.sessions.live_bar(symbol), self.sessions.last_tick_ts(symbol)
        watch = self._watch
        if watch is not None and watch.is_running and watch.symbol == symbol:
            return watch.live_bar(), watch.last_tick_ts
        return None, 0

    def last_price(self, symbol: str) -> float:
        """Zuletzt gehandelter Kurs - nur zur Anzeige."""
        bar, _ = self._tick_source(symbol)
        return bar.close if bar is not None else 0.0

    def display_bar(self, symbol: str, timeframe: Timeframe) -> Bar | None:
        """Die Kerze, die sich GERADE bildet - gezeichnet, nie analysiert.

        Warum es sie ueberhaupt braucht: das NinjaTrader-AddOn sendet
        ausschliesslich geschlossene Bars (Invariante 1). Um 14:21:36 ist die
        letzte davon die von 14:20, und `forming` fuer eine hoehere Zeitebene
        besteht nur aus solchen geschlossenen Minuten. Die Minute, die gerade
        laeuft, kennt die Engine also gar nicht - der Chart hing genau eine
        Minute hinterher, und ein Tickkurs bewegte die falsche Kerze.

        Zusammengesetzt wird sie aus zwei Teilen, und beide sind schon da:
        `forming` liefert Eroeffnung, Hoch und Tief der bereits geschlossenen
        Minuten dieses Buckets, die Tickbar den Rest bis jetzt. Der Bucket
        kommt aus demselben Raster wie die Analyse (`context.bucket_start`) -
        eine eigene Rechnung waere eine zweite Wahrheit.

        None heisst: es gibt nichts Laufendes zu zeigen. Dann bleibt es bei
        `forming`, also beim bisherigen Verhalten.
        """
        live, tick_ts = self._tick_source(symbol)
        if live is None:
            return None
        alter = (time.time_ns() - tick_ts) / 1e9 if tick_ts else float("inf")
        if alter > self.config.live.display_tick_max_age_seconds:
            # Veraltet. Eine Kerze stehenzulassen, die wie eine laufende
            # aussieht, waere schlimmer als gar keine.
            return None

        context = self.chart_context(symbol)
        bucket = context.bucket_start(live.ts, timeframe)
        series = context.series(timeframe)
        if len(series) and bucket <= int(series.ts[-1]):
            # Der Tick gehoert in einen Bucket, der bereits geschlossen und
            # analysiert ist. Ihn zu zeichnen erzeugte eine zweite Kerze mit
            # demselben Zeitstempel.
            return None

        forming = context.forming(timeframe)
        if forming is not None and forming.ts > bucket:
            # Der Tick ist aelter als der Zwischenstand. Kommt bei einem
            # Neustart des Feeds vor; eine Kerze davor zu zeichnen liefe der
            # Zeit entgegen.
            return None
        if forming is None or forming.ts != bucket:
            # Kein Zwischenstand fuer diesen Bucket - entweder ist er gerade
            # erst angebrochen, oder `forming` haengt noch im vorigen (genau
            # der Fall, der auf 1m immer eintritt).
            return Bar(
                ts=bucket,
                open=live.open,
                high=live.high,
                low=live.low,
                close=live.close,
                volume=live.volume,
            )
        return Bar(
            ts=bucket,
            open=forming.open,
            high=max(forming.high, live.high),
            low=min(forming.low, live.low),
            close=live.close,
            volume=forming.volume + live.volume,
        )

    # ------------------------------------------------------------------ Strategie
    def strategy(self, symbol: str) -> StrategyPortfolio:
        """Wie `chart_context`: der laufende Betrieb geht vor.

        Sonst zeigten Chart und Strategieanzeige NEBENEINANDER zwei
        verschiedene Zustaende - die Kurse aus der Sitzung, die Entscheidungen
        aus einer Wiedergabe, die vielleicht Wochen woanders steht. Das faellt
        nicht auf und ist genau deshalb gefaehrlich.
        """
        live = self.sessions.strategy(symbol) if self.sessions.is_running else None
        return live if live is not None else self.state(symbol).strategy

    def decisions(self, symbol: str, limit: int = 50) -> list[StrategyDecision]:
        return self.state(symbol).strategy.recent_decisions(limit)

    def active_setups(self, symbol: str) -> list[SetupCandidate]:
        return self.state(symbol).strategy.active_candidates()

    def signals(self, symbol: str, limit: int = 50) -> list[TradeSignal]:
        return self.state(symbol).strategy.signals[-limit:]

    # ------------------------------------------------------------- Backtest
    def backtest(
        self,
        symbol: str,
        start_ts: int | None = None,
        end_ts: int | None = None,
        max_bars: int = MAX_BACKTEST_BARS,
        save: bool = True,
    ) -> BacktestReport:
        """Einen Backtest ueber den lokalen Datenbestand rechnen (Spec §19).

        Bewusst UNABHAENGIG vom Replay-Zustand: der Lauf baut einen eigenen
        `MarketContext` auf und laesst den geladenen Zustand des Symbols
        unberuehrt. Sonst haette es einen Unterschied gemacht, wie weit man vor
        dem Start des Backtests im Chart vorgespult hatte - und das Ergebnis
        waere nicht reproduzierbar.

        Die Einzelentscheidungen gehen NICHT ins Entscheidungsprotokoll: ueber
        mehrere Jahre kaemen Millionen Zeilen zusammen und wuerden die
        Live-Historie unbrauchbar machen. Festgehalten wird der Lauf als Ganzes
        (`backtest_runs`) samt seinen Trades - mit `config_hash`, ohne den zwei
        Ergebnisse nicht vergleichbar waeren, sondern nur zwei Zahlen.
        """
        symbol = symbol.upper()
        instrument = get_instrument(symbol)
        base_timeframe = self.config.data.base_timeframe

        series = self.store.read(symbol, base_timeframe, start_ts, end_ts, limit=max_bars)
        if len(series) == 0:
            raise LookupError(
                f"Keine {base_timeframe.value}-Daten fuer {symbol} im lokalen Speicher."
            )

        result = run_backtest(symbol, instrument, self.config, series)
        report = build_report(result, self.config)
        self._backtests[symbol] = report

        if save:
            run_id = self.backtest_store.record(report, self.config_hash, STRATEGY_VERSION)
            log.info(
                "backtest_recorded",
                run_id=run_id,
                symbol=symbol,
                trades=report.overall.trades,
                expectancy_r=round(report.overall.expectancy_r, 3),
            )
        return report

    def last_backtest(self, symbol: str) -> BacktestReport:
        try:
            return self._backtests[symbol.upper()]
        except KeyError as exc:
            raise LookupError(
                f"Fuer {symbol.upper()} wurde in dieser Sitzung noch kein Backtest gerechnet."
            ) from exc

    def backtest_runs(self, symbol: str | None = None, limit: int = 20) -> list[dict[str, object]]:
        return self.backtest_store.runs(limit=limit, symbol=symbol)

    # ------------------------------------------------------------- Protokoll
    def record_analysis(self, symbol: str) -> int:
        """Aktuellen Analysezustand ins Entscheidungsprotokoll schreiben.

        In Phase 1+2 gibt es keine Trades; protokolliert wird `decision=ANALYSIS`.
        Ab Phase 3 kommen an derselben Stelle LONG / SHORT / NO_TRADE hinein -
        das Schema ist dafuer bereits ausgelegt (Spec §25).
        """
        state = self.state(symbol)
        snapshot = state.context.snapshot()
        entry_tf = self.config.timeframes.entry[0]
        entry = snapshot.timeframe(entry_tf)

        return self.decision_log.record(
            DecisionRecord(
                ts_utc=utc_now_iso(),
                bar_ts=snapshot.last_ts,
                symbol=symbol.upper(),
                timeframe=entry_tf.value,
                decision="ANALYSIS",
                config_hash=self.config_hash,
                htf_bias=snapshot.bias.bias.value,
                liquidity_sweep=bool(entry.recent_sweeps) if entry else None,
                displacement=bool(entry.last_displacement) if entry else None,
                fvg=bool(entry.active_fvgs) if entry else None,
                mss=bool(entry.last_mss) if entry else None,
                reasons=snapshot.bias.reasons,
            )
        )

    # ---------------------------------------------------------------- Zustand
    def mode(self) -> TradingMode:
        return self.config.execution.mode

    def consistency_issues(self) -> list[ConsistencyIssue]:
        """Widersprueche zwischen Risiko-, Stop- und Instrumenteinstellungen.

        Ohne diese Pruefung koennte das System stundenlang laufen und jedes
        Setup mit "Positionsgroesse 0" verwerfen, ohne dass erkennbar waere,
        dass die Konfiguration das gar nicht zulaesst.
        """
        collected: list[ConsistencyIssue] = []
        seen: set[str] = set()
        instruments = [s.instrument for s in self._states.values()] or [
            get_instrument(self.config.data.default_symbol)
        ]
        for instrument in instruments:
            for issue in check_configuration(self.config, instrument):
                key = f"{instrument.symbol}:{issue.code}"
                if key not in seen:
                    seen.add(key)
                    collected.append(issue)
        return collected

    def warnings(self) -> list[str]:
        """Betriebshinweise fuer das Dashboard (Spec §22, §24)."""
        messages: list[str] = []
        if self.config.execution.mode is TradingMode.ANALYSIS_ONLY:
            messages.append(
                "Analysemodus: Signale werden berechnet und protokolliert, "
                "aber keine Orders erzeugt. Ausfuehrung kommt ab Phase 5."
            )
        messages.extend(issue.message for issue in self.consistency_issues())
        for state in self._states.values():
            if state.instrument.symbol.endswith("_DEMO"):
                messages.append(
                    f"{state.symbol}: SYNTHETISCHE DEMODATEN - keine echten Marktdaten. "
                    "Erkenntnisse daraus haben keinerlei Aussagekraft."
                )
            if state.integrity and not state.integrity.is_clean:
                messages.append(f"Datenqualitaet: {state.integrity.summary()}")
        return messages
