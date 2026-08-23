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
from tradex.live.store import SessionStore
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
        self.sessions = SessionManager(config, get_instruments())
        self.session_store = SessionStore(self.database)

        self.providers = ProviderRegistry()
        self.providers.register(ReplayProvider(self.store))
        self._states: dict[str, SymbolState] = {}

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
        return self.state(symbol).context.snapshot(max_items)

    def bars(self, symbol: str, timeframe: Timeframe, limit: int | None = None) -> BarSeries:
        return self.state(symbol).context.series(timeframe)

    def forming(self, symbol: str, timeframe: Timeframe) -> Bar | None:
        return self.state(symbol).context.forming(timeframe)

    # ------------------------------------------------------------------ Strategie
    def strategy(self, symbol: str) -> StrategyPortfolio:
        return self.state(symbol).strategy

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
