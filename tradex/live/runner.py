"""Die Schleife, die den Betrieb am Laufen haelt.

Bewusst getrennt von `TradingSession`: die Sitzung ist eine reine
Zustandsmaschine ohne Nebenlaeufigkeit und ohne Warten - deshalb laesst sie
sich Nachricht fuer Nachricht pruefen. Hier steht alles, was mit Zeit und
Abbruch zu tun hat.

Die Schleife hat genau eine nicht offensichtliche Pflicht: **sie muss auch dann
etwas tun, wenn nichts passiert.** Ein Feed, der schweigt, sieht von innen aus
wie ein ruhiger Markt. Nur der regelmaessige Blick auf die Uhr unterscheidet
beides - deshalb laeuft `check_liveness()` in jedem Durchgang, nicht nur beim
Eintreffen einer Nachricht.
"""

from __future__ import annotations

import signal
from collections.abc import Callable
from dataclasses import dataclass

from tradex.backtest.execution import SimulatedTrade
from tradex.live.feed import LiveFeed
from tradex.live.session import HALT_FEED_FINISHED, TradingSession
from tradex.logging_setup import get_logger

log = get_logger(__name__)

#: Wie lange je Durchgang auf Nachrichten gewartet wird. Kurz genug, dass die
#: Stille-Ueberwachung und ein Abbruch zeitnah greifen.
POLL_SECONDS = 0.5


@dataclass(slots=True)
class RunResult:
    bars: int
    trades: tuple[SimulatedTrade, ...]
    stopped_by: str


class SessionRunner:
    """Verbindet Feed und Sitzung und laeuft, bis jemand aufhoert."""

    def __init__(
        self,
        session: TradingSession,
        feed: LiveFeed,
        on_trade: Callable[[SimulatedTrade], None] | None = None,
        on_tick: Callable[[TradingSession], None] | None = None,
    ) -> None:
        self.session = session
        self.feed = feed
        self.on_trade = on_trade
        self.on_tick = on_tick
        self._interrupted = False

    def request_stop(self) -> None:
        self._interrupted = True

    def install_signal_handler(self) -> None:
        """Strg-C beendet geordnet statt mitten in einer Bar.

        Ein harter Abbruch waehrend `on_bar` liesse einen halb verarbeiteten
        Zustand zurueck: Position im Risikobuch gebucht, Trade nicht
        geschrieben. Der Handler setzt nur ein Flag; beendet wird zwischen
        zwei Durchgaengen.
        """
        def handler(signum: int, frame: object) -> None:
            log.info("abbruch_angefordert", signal=signum)
            self.request_stop()

        signal.signal(signal.SIGINT, handler)

    def run(self, max_bars: int = 0) -> RunResult:
        """Laeuft, bis der Feed endet, `max_bars` erreicht ist oder Strg-C kommt."""
        self.feed.start()
        stopped_by = "unbekannt"
        try:
            while True:
                for message in self.feed.messages(POLL_SECONDS):
                    for trade in self.session.on_message(message):
                        if self.on_trade is not None:
                            self.on_trade(trade)
                    # Die Grenze wird MITTEN im Stapel geprueft, nicht danach:
                    # ein schneller Feed liefert Tausende Bars auf einmal, und
                    # "hoere nach 10 Bars auf" waere sonst folgenlos.
                    if max_bars and self.session.bars_seen >= max_bars:
                        stopped_by = "max_bars"
                        break

                # Broker-Rueckmeldungen kommen unabhaengig von Bars herein:
                # ein Stop loest aus, wenn er ausloest. Steht kein Broker
                # dahinter, ist der Aufruf folgenlos.
                for trade in self.session.pump_broker():
                    if self.on_trade is not None:
                        self.on_trade(trade)

                # Auch ohne Nachricht: die Uhr ist der einzige Zeuge dafuer,
                # dass der Feed noch lebt.
                self.session.check_liveness()
                if self.on_tick is not None:
                    self.on_tick(self.session)

                if stopped_by == "max_bars":
                    break
                if self._interrupted:
                    stopped_by = "abbruch"
                    break
                if self.feed.is_finished:
                    stopped_by = "feed_ende"
                    self.session.halt(HALT_FEED_FINISHED)
                    break
        finally:
            self.feed.stop()
            self.session.running = False

        return RunResult(
            bars=self.session.bars_seen,
            trades=tuple(self.session.trades),
            stopped_by=stopped_by,
        )
