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
