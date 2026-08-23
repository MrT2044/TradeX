"""SQLite-Schema und Migrationen.

Aufgabenteilung der beiden Speicher (bewusst getrennt):

    SQLite  - transaktionaler Zustand: Entscheidungen, Systemereignisse,
              Config-Snapshots, Strategie-Versionen, Datenluecken.
    Parquet - Bars. Millionen Zeilen, spaltenweise gescannt, nie in SQLite.

Migrationen sind eine schlichte, aufsteigende Liste von SQL-Schritten. Bei einer
Einzelplatz-Desktop-App ist Alembic Overkill; wichtig ist nur, dass jede
Schema-Aenderung reproduzierbar und einmalig angewendet wird.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from tradex.logging_setup import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Migrationen. NIE eine bestehende Migration aendern - immer eine neue anhaengen.
# ---------------------------------------------------------------------------
MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        -- Systemereignisse: Verbindungen, Datenfeed-Status, Fehler (Spec §24).
        CREATE TABLE system_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc      TEXT    NOT NULL,
            level       TEXT    NOT NULL,
            category    TEXT    NOT NULL,
            message     TEXT    NOT NULL,
            payload     TEXT
        );
        CREATE INDEX ix_system_events_ts ON system_events (ts_utc);
        CREATE INDEX ix_system_events_category ON system_events (category, ts_utc);

        -- Entscheidungsprotokoll (Spec §25).
        -- Enthaelt AUSDRUECKLICH auch NO-TRADE-Entscheidungen: die Frage
        -- "warum wurde dieser Trade NICHT gemacht?" ist genauso wichtig wie
        -- die Umkehrung und laesst sich nur aus vollstaendigen Daten beantworten.
        CREATE TABLE decision_log (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc            TEXT    NOT NULL,   -- Zeitpunkt der Auswertung
            bar_ts            INTEGER NOT NULL,   -- ausloesende Bar (Epoch-ns UTC)
            symbol            TEXT    NOT NULL,
            timeframe         TEXT    NOT NULL,
            decision          TEXT    NOT NULL,   -- ANALYSIS | LONG | SHORT | NO_TRADE
            -- Pflichtbedingungen der Strategie (Spec §7-§9). NULL = in dieser
            -- Phase noch nicht ausgewertet, 0 = geprueft und nicht erfuellt.
            htf_bias          TEXT,
            liquidity_sweep   INTEGER,
            displacement      INTEGER,
            fvg               INTEGER,
            retracement       INTEGER,
            mss               INTEGER,
            news_risk         TEXT,
            rr                REAL,
            ai_score          REAL,
            risk_pct          REAL,
            -- Strukturierte Begruendung: Liste von Reason-Codes + Parametern.
            reasons           TEXT    NOT NULL DEFAULT '[]',
            -- Vollstaendiger MarketContext-Snapshot zum Zeitpunkt der Entscheidung.
            context           TEXT,
            -- Reproduzierbarkeit: welche Konfiguration und welche Strategieversion
            -- galten, als diese Entscheidung fiel.
            config_hash       TEXT    NOT NULL,
            strategy_version  TEXT    NOT NULL DEFAULT 'phase2-analysis'
        );
        CREATE INDEX ix_decision_log_bar ON decision_log (symbol, timeframe, bar_ts);
        CREATE INDEX ix_decision_log_ts ON decision_log (ts_utc);
        CREATE INDEX ix_decision_log_decision ON decision_log (decision, ts_utc);

        -- Config-Snapshots: zu jedem config_hash die exakte YAML.
        -- Ohne das ist ein alter Decision-Log-Eintrag nicht interpretierbar.
        CREATE TABLE config_snapshots (
            config_hash TEXT PRIMARY KEY,
            ts_utc      TEXT NOT NULL,
            content     TEXT NOT NULL
        );

        -- Strategie-Versionierung (Spec §21).
        CREATE TABLE strategy_versions (
            version      TEXT PRIMARY KEY,
            created_at   TEXT NOT NULL,
            description  TEXT NOT NULL DEFAULT '',
            config_hash  TEXT NOT NULL,
            approved_by  TEXT NOT NULL DEFAULT '',
            backtest_ref TEXT NOT NULL DEFAULT '',
            is_active    INTEGER NOT NULL DEFAULT 0
        );

        -- Erkannte Luecken in den Marktdaten (Spec §24).
        CREATE TABLE data_gaps (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol        TEXT    NOT NULL,
            timeframe     TEXT    NOT NULL,
            gap_start_ts  INTEGER NOT NULL,
            gap_end_ts    INTEGER NOT NULL,
            missing_bars  INTEGER NOT NULL,
            detected_at   TEXT    NOT NULL,
            UNIQUE (symbol, timeframe, gap_start_ts, gap_end_ts)
        );
        CREATE INDEX ix_data_gaps_symbol ON data_gaps (symbol, timeframe, gap_start_ts);
        """,
    ),
    (
        2,
        """
        -- Backtest-Laeufe (Spec §19, §21).
        --
        -- Ein Lauf ist nur dann etwas wert, wenn spaeter noch feststellbar ist,
        -- WORAUF er sich bezog: welche Konfiguration, welche Strategieversion,
        -- welche Ausfuehrungsannahmen, welcher Zeitraum. Deshalb steht all das
        -- in der Zeile und nicht nur das Ergebnis. `strategy_versions.backtest_ref`
        -- zeigt auf `backtest_runs.id`.
        CREATE TABLE backtest_runs (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc           TEXT    NOT NULL,
            symbol           TEXT    NOT NULL,
            base_timeframe   TEXT    NOT NULL,
            first_ts         INTEGER NOT NULL,
            last_ts          INTEGER NOT NULL,
            bars             INTEGER NOT NULL,
            config_hash      TEXT    NOT NULL,
            strategy_version TEXT    NOT NULL,
            backtest_version TEXT    NOT NULL,
            -- Kennzahlen als Spalten, damit Laeufe ohne JSON-Auswertung
            -- vergleichbar sind.
            trades           INTEGER NOT NULL,
            wins             INTEGER NOT NULL,
            losses           INTEGER NOT NULL,
            net_pnl          REAL    NOT NULL,
            expectancy_r     REAL    NOT NULL,
            profit_factor    REAL,
            max_drawdown_pct REAL    NOT NULL,
            -- Der vollstaendige Bericht inklusive Annahmen und Warnungen.
            report           TEXT    NOT NULL,
            notes            TEXT    NOT NULL DEFAULT ''
        );
        CREATE INDEX ix_backtest_runs_symbol ON backtest_runs (symbol, ts_utc);

        -- Einzeltrades eines Laufs. Getrennt gespeichert, weil die
        -- interessanten Fragen ("verlieren Shorts in der Asia-Session?")
        -- Abfragen sind und keine Textsuche im JSON-Bericht.
        CREATE TABLE backtest_trades (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id         INTEGER NOT NULL REFERENCES backtest_runs (id) ON DELETE CASCADE,
            setup_id       INTEGER NOT NULL,
            direction      TEXT    NOT NULL,
            session        TEXT    NOT NULL,
            trading_day    INTEGER NOT NULL,
            entry_ts       INTEGER NOT NULL,
            exit_ts        INTEGER NOT NULL,
            entry_price    REAL    NOT NULL,
            exit_price     REAL    NOT NULL,
            stop           REAL    NOT NULL,
            target         REAL    NOT NULL,
            quantity       INTEGER NOT NULL,
            exit_reason    TEXT    NOT NULL,
            bars_held      INTEGER NOT NULL,
            planned_rr     REAL    NOT NULL,
            risk_amount    REAL    NOT NULL,
            pnl            REAL    NOT NULL,
            commission     REAL    NOT NULL,
            r_multiple     REAL    NOT NULL,
            mae_r          REAL    NOT NULL,
            mfe_r          REAL    NOT NULL,
            stop_anchor    TEXT    NOT NULL,
            target_source  TEXT    NOT NULL,
            htf_bias       TEXT    NOT NULL
        );
        CREATE INDEX ix_backtest_trades_run ON backtest_trades (run_id, entry_ts);
        """,
    ),
    (
        3,
        """
        -- Papertrading-Sitzungen (Phase 5).
        --
        -- Getrennt von backtest_runs, obwohl beide Trades derselben Bauart
        -- enthalten: ein Backtest ist ein abgeschlossener Lauf ueber einen
        -- bekannten Zeitraum, eine Sitzung laeuft und kann abbrechen. In eine
        -- Tabelle gezwungen muesste jede Auswertung erst klaeren, was sie
        -- gerade vor sich hat.
        CREATE TABLE sessions (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            started_utc      TEXT    NOT NULL,
            ended_utc        TEXT,               -- NULL = laeuft noch oder abgestuerzt
            mode             TEXT    NOT NULL,   -- paper_manual | paper_auto | ...
            feed             TEXT    NOT NULL,   -- replay | nt8 | ...
            symbols          TEXT    NOT NULL,
            config_hash      TEXT    NOT NULL,
            strategy_version TEXT    NOT NULL,
            backtest_version TEXT    NOT NULL,
            start_equity     REAL    NOT NULL,
            notes            TEXT    NOT NULL DEFAULT ''
        );
        CREATE INDEX ix_sessions_started ON sessions (started_utc);

        -- Trades einer Sitzung. Bewusst dieselben Spalten wie backtest_trades
        -- plus `session_id`: beide entstehen aus demselben SimulatedTrade, und
        -- nur so lassen sich Papertrades und Backtest direkt vergleichen -
        -- die eigentliche Pruefung von "Backtest = Live".
        CREATE TABLE session_trades (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id     INTEGER NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
            trade_id       INTEGER NOT NULL,
            setup_id       INTEGER NOT NULL,
            symbol         TEXT    NOT NULL,
            strategy       TEXT    NOT NULL,
            direction      TEXT    NOT NULL,
            session_name   TEXT    NOT NULL,
            trading_day    INTEGER NOT NULL,
            signal_ts      INTEGER NOT NULL,
            entry_ts       INTEGER NOT NULL,
            exit_ts        INTEGER NOT NULL,
            entry_price    REAL    NOT NULL,
            exit_price     REAL    NOT NULL,
            stop           REAL    NOT NULL,
            target         REAL    NOT NULL,
            quantity       INTEGER NOT NULL,
            exit_reason    TEXT    NOT NULL,
            bars_held      INTEGER NOT NULL,
            planned_rr     REAL    NOT NULL,
            risk_amount    REAL    NOT NULL,
            pnl            REAL    NOT NULL,
            commission     REAL    NOT NULL,
            r_multiple     REAL    NOT NULL,
            mae_r          REAL    NOT NULL,
            mfe_r          REAL    NOT NULL,
            stop_anchor    TEXT    NOT NULL,
            target_source  TEXT    NOT NULL,
            htf_bias       TEXT    NOT NULL
        );
        CREATE INDEX ix_session_trades_session ON session_trades (session_id, entry_ts);
        """,
    ),
    (
        4,
        """
        -- Orders, die tatsaechlich an einen Broker gegangen sind (Spec §24).
        --
        -- Getrennt von `session_trades`: dort steht, was aus einem Signal
        -- geworden ist, hier steht, was der BROKER daraus gemacht hat. Die
        -- Differenz zwischen beiden ist die eigentliche Information - ohne sie
        -- liesse sich nach einem Ausfall nicht sagen, ob eine Position
        -- existiert oder nur geplant war.
        --
        -- `order_key` traegt den Duplikatschutz und ist deshalb UNIQUE: er
        -- ueberlebt Prozessneustarts, die interne trade_id nicht (das
        -- Risikobuch liegt im Speicher und faengt wieder bei 1 an).
        CREATE TABLE broker_orders (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            order_key      TEXT    NOT NULL UNIQUE,
            session_id     INTEGER,
            broker         TEXT    NOT NULL,
            account        TEXT    NOT NULL,
            trading_mode   TEXT    NOT NULL,
            symbol         TEXT    NOT NULL,
            strategy       TEXT    NOT NULL,
            signal_id      INTEGER NOT NULL,
            side           TEXT    NOT NULL,
            quantity       INTEGER NOT NULL,
            order_type     TEXT    NOT NULL,
            order_id       INTEGER NOT NULL DEFAULT 0,
            parent_order_id INTEGER NOT NULL DEFAULT 0,
            stop_order_id  INTEGER NOT NULL DEFAULT 0,
            target_order_id INTEGER NOT NULL DEFAULT 0,
            state          TEXT    NOT NULL,
            filled_quantity INTEGER NOT NULL DEFAULT 0,
            avg_fill_price REAL    NOT NULL DEFAULT 0,
            planned_entry  REAL    NOT NULL DEFAULT 0,
            stop_loss      REAL    NOT NULL DEFAULT 0,
            take_profit    REAL    NOT NULL DEFAULT 0,
            reason         TEXT    NOT NULL DEFAULT '',
            error          TEXT    NOT NULL DEFAULT '',
            submitted_utc  TEXT    NOT NULL,
            updated_utc    TEXT    NOT NULL
        );
        CREATE INDEX ix_broker_orders_session ON broker_orders (session_id, submitted_utc);
        CREATE INDEX ix_broker_orders_state ON broker_orders (state, updated_utc);
        """,
    ),
]

CURRENT_SCHEMA_VERSION = MIGRATIONS[-1][0]


def connect(
    database: Path, *, read_only: bool = False, check_same_thread: bool = True
) -> sqlite3.Connection:
    """Verbindung mit den fuer diese App richtigen PRAGMAs oeffnen.

    `check_same_thread=False` wird nur dort gesetzt, wo eine Verbindung
    absichtlich ueber Threadgrenzen hinweg benutzt wird - FastAPI fuehrt
    synchrone Endpunkte in einem Threadpool aus. Die Serialisierung uebernimmt
    dann ein Lock auf Aufruferseite (siehe `DecisionLog`); ohne das waere es
    schlicht ein abgeschaltetes Sicherheitsnetz.
    """
    database.parent.mkdir(parents=True, exist_ok=True)
    if read_only:
        conn = sqlite3.connect(
            f"file:{database}?mode=ro", uri=True, check_same_thread=check_same_thread
        )
    else:
        conn = sqlite3.connect(database, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _schema_version(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return int(row["v"]) if row and row["v"] is not None else 0


def migrate(conn: sqlite3.Connection) -> int:
    """Ausstehende Migrationen anwenden. Liefert die erreichte Schema-Version."""
    current = _schema_version(conn)
    applied = 0
    for version, sql in MIGRATIONS:
        if version <= current:
            continue
        with conn:
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        applied += 1
        log.info("migration_applied", version=version)
    if applied:
        log.info("schema_migrated", from_version=current, to_version=CURRENT_SCHEMA_VERSION)
    return CURRENT_SCHEMA_VERSION


def init_database(database: Path) -> None:
    """Datenbank anlegen bzw. auf den aktuellen Stand migrieren."""
    with connect(database) as conn:
        migrate(conn)


@contextmanager
def session(database: Path) -> Iterator[sqlite3.Connection]:
    """Kontextmanager mit automatischem Commit/Rollback und Close."""
    conn = connect(database)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
