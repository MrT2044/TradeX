"""Wiedergabe-Feed: echte Bars aus dem Bestand, in Echtzeit oder schneller.

Wozu er da ist
--------------
Er ist der erste Schritt, den `bridge_nt8/README.md` fuer Phase 5 vorsieht -
und zwar aus einem guten Grund: er ist deterministisch und kostenlos. Wer die
Sitzungsmechanik gegen einen Live-Feed entwickelt, kann einen Fehler nicht
zweimal ausloesen und weiss nie, ob der Markt oder der Code schuld war.

Was er ist und was nicht
------------------------
Er ist ein vollwertiger Feed: dieselben Nachrichten, dieselben Lebenszeichen,
dieselbe Zusicherung "nur geschlossene Bars". Die Sitzung merkt keinen
Unterschied - das ist der Sinn.

Er ist KEIN Ersatz fuer echte Daten. Die Bars sind historisch; was er prueft,
ist die Mechanik des Betriebs, nicht die Regel am lebenden Markt. Deshalb
meldet er sich in `status()` selbst als Wiedergabe, und die Sitzung schreibt
das in jeden gespeicherten Lauf.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from queue import Empty, Queue

from tradex.domain.bars import Bar, BarSeries
from tradex.live.feed import BarMessage, FeedMessage, HeartbeatMessage, StatusMessage

#: Abstand der Lebenszeichen. Entspricht der Bridge-Spezifikation (alle 5 s),
#: damit die Wiedergabe die Ueberwachung genauso auf die Probe stellt wie ein
#: echter Feed.
HEARTBEAT_SECONDS = 5.0


class ReplayFeed:
    """Spielt gespeicherte Bars als Live-Feed ab.

    `speed` ist der Beschleunigungsfaktor gegen die echte Zeit: 1.0 gibt eine
    Minutenbar je Minute aus, 60.0 eine je Sekunde, 0.0 so schnell wie
    moeglich. Der Faktor aendert NUR die Wanduhr zwischen zwei Bars - nie die
    Zeitstempel der Bars selbst, sonst waere die Analyse eine andere.
    """

    name = "replay"

    def __init__(
        self,
        series_by_symbol: dict[str, BarSeries],
        speed: float = 60.0,
        base_seconds: int = 60,
    ) -> None:
        if not series_by_symbol or all(len(s) == 0 for s in series_by_symbol.values()):
            raise ValueError("Wiedergabe ohne Bars")
        self.speed = max(speed, 0.0)
        self.base_seconds = base_seconds
        self._queue: Queue[FeedMessage] = Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._finished = threading.Event()
        # Streng chronologisch verschraenkt - dieselbe Regel wie im Backtest:
        # liefe Symbol fuer Symbol durch, saehe das gemeinsame Risikobuch beim
        # zweiten bereits alle Ergebnisse des ersten.
        self._plan: list[tuple[int, str, Bar]] = sorted(
            (bar.ts, symbol, bar)
            for symbol, series in series_by_symbol.items()
            for bar in series
        )

    # ------------------------------------------------------------------- Lauf
    def start(self) -> None:
        if self._thread is not None:
            return
        self._queue.put(StatusMessage(ts=time.time_ns(), connected=True, detail="Wiedergabe"))
        self._thread = threading.Thread(target=self._run, name="replay-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5.0)

    def _run(self) -> None:
        delay = (self.base_seconds / self.speed) if self.speed > 0 else 0.0
        last_beat = time.monotonic()
        for _, symbol, bar in self._plan:
            if self._stop.is_set():
                break
            if delay:
                target = time.monotonic() + delay
                while not self._stop.is_set():
                    # Der Rest wird EINMAL berechnet und dann benutzt. Vorher
                    # standen Pruefung und `sleep` je fuer sich: zwischen
                    # beiden konnte die Uhr ueber `target` laufen, und
                    # `time.sleep` wirft bei einer negativen Dauer. Der
                    # Wiedergabefaden starb daran lautlos - die Sitzung bekam
                    # keine Bars mehr und sah dabei aus wie ein ruhiger Markt.
                    rest = target - time.monotonic()
                    if rest <= 0:
                        break
                    # In Scheiben warten, damit stop() sofort greift und nicht
                    # erst nach der naechsten Bar.
                    time.sleep(min(0.05, rest))
            self._queue.put(BarMessage(symbol=symbol, bar=bar, received_ts=time.time_ns()))

            now = time.monotonic()
            if now - last_beat >= HEARTBEAT_SECONDS:
                last_beat = now
                self._queue.put(HeartbeatMessage(ts=time.time_ns()))

        self._finished.set()
        self._queue.put(
            StatusMessage(ts=time.time_ns(), connected=False, detail="Wiedergabe beendet")
        )

    # ---------------------------------------------------------------- Abholen
    def messages(self, timeout: float) -> Iterator[FeedMessage]:
        try:
            yield self._queue.get(timeout=timeout)
        except Empty:
            return
        # Alles, was inzwischen noch dazugekommen ist, in einem Rutsch - sonst
        # haengt der Durchsatz an der Schleifenfrequenz der Sitzung.
        while True:
            try:
                yield self._queue.get_nowait()
            except Empty:
                return

    @property
    def is_finished(self) -> bool:
        return self._finished.is_set() and self._queue.empty()

    @property
    def total_bars(self) -> int:
        return len(self._plan)
