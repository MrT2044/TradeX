"""Entscheidungsprotokoll (Spec §25).

Das System muss jederzeit beantworten koennen:
    - Warum wurde dieser Trade gemacht?
    - Warum wurde dieser Trade NICHT gemacht?

Beides ist nur beantwortbar, wenn auch die Nicht-Entscheidungen samt ihrem
vollstaendigen Kontext gespeichert werden. Genau das tut diese Schicht.

Zusammen mit jedem Eintrag wird der `config_hash` gespeichert und die zugehoerige
YAML einmalig in `config_snapshots` abgelegt. Ohne das waere ein Eintrag von
letztem Monat nicht mehr interpretierbar, sobald ein Schwellenwert geaendert wurde.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradex.persistence.db import connect
from tradex.persistence.models import (
    DataGapRecord,
    DecisionRecord,
    Reason,
    SystemEvent,
)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def config_fingerprint(config_path: Path) -> tuple[str, str]:
    """(hash, inhalt) der Konfigurationsdatei.

    Der Hash geht in jeden Decision-Log-Eintrag; der Inhalt wird einmalig
    gespeichert. Zeilenenden werden normalisiert, damit derselbe Inhalt unter
    Windows und Linux denselben Hash ergibt.
    """
    content = config_path.read_text(encoding="utf-8").replace("\r\n", "\n")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    return digest, content


class DecisionLog:
    """Schreibender Zugriff auf `decision_log`, `system_events` und `data_gaps`.

    Haelt eine offene Verbindung. Nicht threadsafe - pro Thread eine Instanz,
    oder Aufrufe serialisieren.
    """

    def __init__(self, database: Path) -> None:
        self._conn = connect(database)
        self._known_config_hashes: set[str] = set()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> DecisionLog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------ Config-Hash
    def register_config(self, config_path: Path) -> str:
        """Config-Snapshot ablegen (idempotent) und Hash zurueckgeben."""
        digest, content = config_fingerprint(config_path)
        if digest in self._known_config_hashes:
            return digest
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO config_snapshots (config_hash, ts_utc, content) "
                "VALUES (?, ?, ?)",
                (digest, utc_now_iso(), content),
            )
        self._known_config_hashes.add(digest)
        return digest

    # -------------------------------------------------------------- Schreiben
    def record(self, record: DecisionRecord) -> int:
        with self._conn:
            cursor = self._conn.execute(
                """
                INSERT INTO decision_log (
                    ts_utc, bar_ts, symbol, timeframe, decision,
                    htf_bias, liquidity_sweep, displacement, fvg, retracement, mss,
                    news_risk, rr, ai_score, risk_pct,
                    reasons, context, config_hash, strategy_version
                ) VALUES (?,?,?,?,?, ?,?,?,?,?,?, ?,?,?,?, ?,?,?,?)
                """,
                (
                    record.ts_utc,
                    record.bar_ts,
                    record.symbol,
                    record.timeframe,
                    record.decision,
                    record.htf_bias,
                    _as_int(record.liquidity_sweep),
                    _as_int(record.displacement),
                    _as_int(record.fvg),
                    _as_int(record.retracement),
                    _as_int(record.mss),
                    record.news_risk,
                    record.rr,
                    record.ai_score,
                    record.risk_pct,
                    json.dumps([r.to_dict() for r in record.reasons], separators=(",", ":")),
                    json.dumps(record.context, separators=(",", ":")) if record.context else None,
                    record.config_hash,
                    record.strategy_version,
                ),
            )
        return int(cursor.lastrowid or 0)

    def record_many(self, records: Sequence[DecisionRecord]) -> int:
        """Batch-Insert - im Backtest laufen hunderttausende Eintraege durch."""
        rows = [
            (
                r.ts_utc,
                r.bar_ts,
                r.symbol,
                r.timeframe,
                r.decision,
                r.htf_bias,
                _as_int(r.liquidity_sweep),
                _as_int(r.displacement),
                _as_int(r.fvg),
                _as_int(r.retracement),
                _as_int(r.mss),
                r.news_risk,
                r.rr,
                r.ai_score,
                r.risk_pct,
                json.dumps([x.to_dict() for x in r.reasons], separators=(",", ":")),
                json.dumps(r.context, separators=(",", ":")) if r.context else None,
                r.config_hash,
                r.strategy_version,
            )
            for r in records
        ]
        with self._conn:
            self._conn.executemany(
                """
                INSERT INTO decision_log (
                    ts_utc, bar_ts, symbol, timeframe, decision,
                    htf_bias, liquidity_sweep, displacement, fvg, retracement, mss,
                    news_risk, rr, ai_score, risk_pct,
                    reasons, context, config_hash, strategy_version
                ) VALUES (?,?,?,?,?, ?,?,?,?,?,?, ?,?,?,?, ?,?,?,?)
                """,
                rows,
            )
        return len(rows)

    def event(self, event: SystemEvent) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO system_events (ts_utc, level, category, message, payload) "
                "VALUES (?,?,?,?,?)",
                (
                    event.ts_utc,
                    event.level,
                    event.category,
                    event.message,
                    json.dumps(event.payload, separators=(",", ":")) if event.payload else None,
                ),
            )

    def record_gap(self, gap: DataGapRecord) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO data_gaps "
                "(symbol, timeframe, gap_start_ts, gap_end_ts, missing_bars, detected_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    gap.symbol,
                    gap.timeframe,
                    gap.gap_start_ts,
                    gap.gap_end_ts,
                    gap.missing_bars,
                    gap.detected_at,
                ),
            )

    # ------------------------------------------------------------------ Lesen
    def recent(self, limit: int = 100, symbol: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM decision_log"
        params: list[Any] = []
        if symbol:
            sql += " WHERE symbol = ?"
            params.append(symbol)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [_row_to_dict(row) for row in self._conn.execute(sql, params)]

    def gaps(self, symbol: str, timeframe: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM data_gaps WHERE symbol = ? AND timeframe = ? ORDER BY gap_start_ts",
            (symbol, timeframe),
        )
        return [dict(row) for row in rows]

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM decision_log").fetchone()
        return int(row["n"])


def _as_int(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["reasons"] = json.loads(data["reasons"]) if data.get("reasons") else []
    if data.get("context"):
        data["context"] = json.loads(data["context"])
    for key in ("liquidity_sweep", "displacement", "fvg", "retracement", "mss"):
        if data.get(key) is not None:
            data[key] = bool(data[key])
    return data


def make_reason(code: str, ok: bool, **params: Any) -> Reason:
    return Reason(code=code, ok=ok, params=params)
