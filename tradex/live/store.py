"""Sitzungen und ihre Trades dauerhaft festhalten (Phase 5, Spec Paragraph 21).

Warum sofort geschrieben wird und nicht am Ende
-----------------------------------------------
Ein Backtest darf seine Ergebnisse am Schluss ablegen: er endet planmaessig.
Eine Sitzung endet oft anders - Stromausfall, Absturz, geschlossener Deckel.
Was dann nur im Speicher stand, ist weg, und zwar genau in dem Fall, in dem man
am ehesten wissen will, was passiert ist.

Deshalb bekommt jede Sitzung ihre Zeile beim Start, und jeder Trade wird in dem
Moment geschrieben, in dem er schliesst. Eine Sitzung ohne `ended_utc` ist eine
laufende oder eine abgestuerzte - der Unterschied ergibt sich aus dem
Zeitstempel, nicht aus einem Feld, das niemand mehr setzen konnte.

Die Spalten sind dieselben wie in `backtest_trades`. Das ist Absicht: nur so
laesst sich die eigentliche Frage von Phase 5 beantworten - handelt der
Papertrading-Betrieb so, wie der Backtest es vorhergesagt hat?
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

from tradex.backtest.execution import SimulatedTrade
from tradex.persistence.db import connect
from tradex.persistence.decision_log import utc_now_iso


class SessionStore:
    """Schreibender und lesender Zugriff auf `sessions` / `session_trades`."""

    def __init__(self, database: Path) -> None:
        self._conn = connect(database, check_same_thread=False)
        self._lock = threading.Lock()
        self.session_id = 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> SessionStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -------------------------------------------------------------- Schreiben
    def start(
        self,
        *,
        mode: str,
        feed: str,
        symbols: tuple[str, ...],
        config_hash: str,
        strategy_version: str,
        backtest_version: str,
        start_equity: float,
        notes: str = "",
    ) -> int:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT INTO sessions (
                    started_utc, ended_utc, mode, feed, symbols, config_hash,
                    strategy_version, backtest_version, start_equity, notes
                ) VALUES (?, NULL, ?,?,?,?, ?,?,?,?)
                """,
                (
                    utc_now_iso(),
                    mode,
                    feed,
                    ",".join(symbols),
                    config_hash,
                    strategy_version,
                    backtest_version,
                    start_equity,
                    notes,
                ),
            )
        self.session_id = int(cursor.lastrowid or 0)
        return self.session_id

    def record_trade(self, trade: SimulatedTrade) -> None:
        """Einen geschlossenen Trade sofort festhalten."""
        if not self.session_id:
            raise RuntimeError("Erst start() aufrufen - ein Trade ohne Sitzung ist heimatlos")
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO session_trades (
                    session_id, trade_id, setup_id, symbol, strategy, direction,
                    session_name, trading_day, signal_ts, entry_ts, exit_ts,
                    entry_price, exit_price, stop, target, quantity, exit_reason,
                    bars_held, planned_rr, risk_amount, pnl, commission,
                    r_multiple, mae_r, mfe_r, stop_anchor, target_source, htf_bias
                ) VALUES (?,?,?,?,?,?, ?,?,?,?,?, ?,?,?,?,?,?, ?,?,?,?,?, ?,?,?,?,?,?)
                """,
                (
                    self.session_id,
                    trade.trade_id,
                    trade.setup_id,
                    trade.symbol,
                    trade.strategy,
                    trade.direction.value,
                    trade.session,
                    trade.trading_day,
                    trade.signal_ts,
                    trade.entry_ts,
                    trade.exit_ts,
                    trade.entry_price,
                    trade.exit_price,
                    trade.stop,
                    trade.target,
                    trade.quantity,
                    trade.exit_reason.value,
                    trade.bars_held,
                    trade.planned_rr,
                    trade.risk_amount,
                    trade.pnl,
                    trade.commission,
                    trade.r_multiple,
                    trade.mae_r,
                    trade.mfe_r,
                    trade.stop_anchor,
                    trade.target_source,
                    trade.htf_bias,
                ),
            )

    def finish(self) -> None:
        """Sitzung als planmaessig beendet kennzeichnen."""
        if not self.session_id:
            return
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE sessions SET ended_utc = ? WHERE id = ?",
                (utc_now_iso(), self.session_id),
            )

    # ------------------------------------------------------------------ Lesen
    def sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT s.*,
                       (SELECT COUNT(*) FROM session_trades t WHERE t.session_id = s.id) AS trades,
                       (SELECT COALESCE(SUM(pnl), 0) FROM session_trades t
                        WHERE t.session_id = s.id) AS net_pnl
                FROM sessions s ORDER BY s.id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def trades(self, session_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows: list[sqlite3.Row] = self._conn.execute(
                "SELECT * FROM session_trades WHERE session_id = ? ORDER BY entry_ts",
                (session_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def unfinished(self) -> list[dict[str, Any]]:
        """Sitzungen ohne Ende - laufende oder abgestuerzte.

        Beim Start eines neuen Laufs ist das die interessanteste Abfrage
        ueberhaupt: stand hier etwas offen, ist der letzte Betrieb nicht saeuber
        beendet worden.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sessions WHERE ended_utc IS NULL ORDER BY id DESC"
            ).fetchall()
        return [dict(row) for row in rows]
