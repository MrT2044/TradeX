"""Backtest-Laeufe dauerhaft festhalten (Spec §19, §21).

Warum ueberhaupt gespeichert wird
---------------------------------
Ein Backtest, der nur auf der Konsole erscheint, beantwortet die wichtigste
Frage nicht: *Ist es besser geworden?* Dafuer braucht es den Lauf von vorher -
mitsamt der Konfiguration, unter der er entstand. Ohne `config_hash` und
`strategy_version` waeren zwei Ergebnisse nicht vergleichbar, sondern nur zwei
Zahlen.

`strategy_versions.backtest_ref` (Spec §21) zeigt auf `backtest_runs.id`. Erst
dadurch bekommt die Aussage "Version X wurde freigegeben" eine ueberpruefbare
Grundlage.

Warum der Speicher hier liegt und nicht in `tradex/persistence/`
----------------------------------------------------------------
Das Schema steht dort (`db.py`, Migration 2), der Zugriff hier: die
Persistenzschicht darf nichts ueber Backtests wissen, sonst zeigt eine untere
Schicht auf eine obere. Umgekehrt ist die Abhaengigkeit unproblematisch.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from tradex.backtest.report import BacktestReport, to_dict
from tradex.persistence.db import connect
from tradex.persistence.decision_log import utc_now_iso


class BacktestStore:
    """Schreibender und lesender Zugriff auf `backtest_runs` / `backtest_trades`.

    Wie `DecisionLog` mit Lock: FastAPI fuehrt synchrone Endpunkte im
    Threadpool aus, die Verbindung wandert also zwangslaeufig zwischen Threads.
    """

    def __init__(self, database: Path) -> None:
        self._conn = connect(database, check_same_thread=False)
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> BacktestStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -------------------------------------------------------------- Schreiben
    def record(
        self,
        report: BacktestReport,
        config_hash: str,
        strategy_version: str,
        notes: str = "",
    ) -> int:
        """Lauf samt Einzeltrades speichern. Liefert die Lauf-ID."""
        m = report.overall
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT INTO backtest_runs (
                    ts_utc, symbol, base_timeframe, first_ts, last_ts, bars,
                    config_hash, strategy_version, backtest_version,
                    trades, wins, losses, net_pnl, expectancy_r, profit_factor,
                    max_drawdown_pct, report, notes
                ) VALUES (?,?,?,?,?,?, ?,?,?, ?,?,?,?,?,?, ?,?,?)
                """,
                (
                    utc_now_iso(),
                    report.symbol,
                    report.base_timeframe,
                    report.first_ts,
                    report.last_ts,
                    report.bars,
                    config_hash,
                    strategy_version,
                    report.backtest_version,
                    m.trades,
                    m.wins,
                    m.losses,
                    m.net_pnl,
                    m.expectancy_r,
                    m.profit_factor,
                    m.max_drawdown_pct,
                    json.dumps(to_dict(report), separators=(",", ":")),
                    notes,
                ),
            )
            run_id = int(cursor.lastrowid or 0)

            self._conn.executemany(
                """
                INSERT INTO backtest_trades (
                    run_id, setup_id, direction, session, trading_day,
                    entry_ts, exit_ts, entry_price, exit_price, stop, target,
                    quantity, exit_reason, bars_held, planned_rr, risk_amount,
                    pnl, commission, r_multiple, mae_r, mfe_r,
                    stop_anchor, target_source, htf_bias
                ) VALUES (?,?,?,?,?, ?,?,?,?,?,?, ?,?,?,?,?, ?,?,?,?,?, ?,?,?)
                """,
                [
                    (
                        run_id,
                        t.setup_id,
                        t.direction.value,
                        t.session,
                        t.trading_day,
                        t.entry_ts,
                        t.exit_ts,
                        t.entry_price,
                        t.exit_price,
                        t.stop,
                        t.target,
                        t.quantity,
                        t.exit_reason.value,
                        t.bars_held,
                        t.planned_rr,
                        t.risk_amount,
                        t.pnl,
                        t.commission,
                        t.r_multiple,
                        t.mae_r,
                        t.mfe_r,
                        t.stop_anchor,
                        t.target_source,
                        t.htf_bias,
                    )
                    for t in report.trades
                ],
            )
        return run_id

    # ------------------------------------------------------------------ Lesen
    def runs(self, limit: int = 20, symbol: str | None = None) -> list[dict[str, Any]]:
        """Laufuebersicht ohne den vollstaendigen Bericht - der ist zu gross."""
        sql = (
            "SELECT id, ts_utc, symbol, base_timeframe, first_ts, last_ts, bars, "
            "config_hash, strategy_version, backtest_version, trades, wins, losses, "
            "net_pnl, expectancy_r, profit_factor, max_drawdown_pct, notes "
            "FROM backtest_runs"
        )
        params: list[Any] = []
        if symbol:
            sql += " WHERE symbol = ?"
            params.append(symbol.upper())
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def report(self, run_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT report FROM backtest_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return json.loads(row["report"]) if row else None

    def trades(self, run_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows: list[sqlite3.Row] = self._conn.execute(
                "SELECT * FROM backtest_trades WHERE run_id = ? ORDER BY entry_ts", (run_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM backtest_runs").fetchone()
        return int(row["n"])
