"""Laufender Betrieb: das Programm handelt selbst (Phase 5).

    feed.py         was ein Live-Feed liefern darf - nur GESCHLOSSENE Bars
    replay_feed.py  gespeicherte Bars als Feed, deterministisch und kostenlos
    session.py      die Zustandsmaschine: Bars rein, Papertrades raus
    runner.py       die Schleife drumherum - Zeit, Stille, Abbruch
    store.py        Sitzungen und Trades sofort haltbar machen

Der entscheidende Punkt dieses Pakets ist, was NICHT darin steht: keine
Analyse, keine Regel, keine Fuellogik, keine Positionsgroesse. All das kommt
aus `SymbolBook` - derselben Klasse, die den Backtest fuehrt. Backtest und
Papertrading sind damit nicht zwei Wege, die dasselbe tun sollen, sondern
derselbe Weg mit verschiedenen Bar-Quellen (Architektur-Invariante 3).

Live-Trading mit echtem Geld ist hier ausdruecklich NICHT enthalten. Es gibt
keine Order-Anbindung an einen Broker; jeder Trade entsteht in der
Ausfuehrungssimulation. Der Schritt zu echtem Geld ist Phase 8/9 und braucht
eine zweite ausdrueckliche Bestaetigung (Spec Paragraph 24).
"""

from __future__ import annotations

from tradex.live.feed import BarMessage, FeedMessage, HeartbeatMessage, LiveFeed, StatusMessage
from tradex.live.replay_feed import ReplayFeed
from tradex.live.runner import RunResult, SessionRunner
from tradex.live.session import SessionConfig, SessionStatus, TradingSession
from tradex.live.store import SessionStore

__all__ = [
    "BarMessage",
    "FeedMessage",
    "HeartbeatMessage",
    "LiveFeed",
    "ReplayFeed",
    "RunResult",
    "SessionConfig",
    "SessionRunner",
    "SessionStatus",
    "SessionStore",
    "StatusMessage",
    "TradingSession",
]
