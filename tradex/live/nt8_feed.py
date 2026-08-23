"""NinjaTrader-8-Feed: echte Marktdaten ueber einen lokalen Socket.

Gegenstelle ist das NinjaScript-AddOn aus `bridge_nt8/`. Das Protokoll steht
dort und nicht hier - dieses Modul ist der Client dazu.

Warum ueberhaupt eine Bridge
----------------------------
Die dokumentierte externe NinjaTrader-Schnittstelle (ATI) kann fuer Marktdaten
nur den zuletzt gehandelten Preis abfragen. Keine Bars, keine Historie. Wer
Bars will, braucht Code INNERHALB von NinjaTrader - daran kaeme auch ein
.NET-Backend nicht vorbei.

Was dieses Modul gegen einen unzuverlaessigen Socket tut
--------------------------------------------------------
Ein lokaler Socket faellt seltener aus als ein Netzwerk, aber er faellt aus:
NinjaTrader wird neu gestartet, das AddOn neu geladen, der Rechner geht in den
Ruhezustand. Drei Vorkehrungen:

1. **Wiederverbinden mit wachsender Wartezeit.** Ein Client, der im
   Millisekundentakt neu verbindet, macht aus einer kurzen Stoerung eine
   dauerhafte.
2. **Kaputte Zeilen ueberspringen, nicht abstuerzen.** Ein halb uebertragener
   Datensatz beim Verbindungsabriss ist normal. Er darf den Lesefaden nicht
   beenden - sonst laeuft die Sitzung weiter, waehrend niemand mehr zuhoert.
3. **Zustandswechsel melden.** Jeder Verbindungsauf- und -abbau geht als
   `StatusMessage` an die Sitzung. Die entscheidet, was daraus folgt - dieses
   Modul faellt keine Handelsentscheidung, auch keine negative.

Ticks werden gelesen und verworfen
----------------------------------
Das AddOn kann Ticks senden. Die Engine analysiert aber ausschliesslich
geschlossene Bars (Invariante 1); ein Tick, der irgendwo einfliesst, waere ein
Zustand, den der Backtest nie sieht. Sie zu zaehlen ist trotzdem nuetzlich: die
Zahl zeigt, ob der Feed lebt, wenn gerade keine Bar schliesst.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from queue import Empty, Queue

from tradex.domain.bars import Bar
from tradex.domain.enums import Timeframe
from tradex.live.feed import BarMessage, FeedMessage, HeartbeatMessage, StatusMessage
from tradex.logging_setup import get_logger

log = get_logger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 39473
"""NICHT 36973: das ist NinjaTraders eigener ATI-Port (an einer laufenden
Installation 8.1.8.2 nachgemessen). Die Bridge kaeme dort nie hoch, und ein
Client wuerde sich stattdessen mit der Order-Schnittstelle unterhalten."""

#: Wartezeiten zwischen Verbindungsversuchen, in Sekunden. Waechst, damit aus
#: einer Stoerung keine Dauerlast wird; deckelt, damit ein Wiederanlauf nicht
#: minutenlang auf sich warten laesst.
_RECONNECT_DELAYS = (1.0, 2.0, 5.0, 10.0, 15.0)

_SOCKET_TIMEOUT = 2.0
_RECEIVE_BYTES = 65536


class NinjaTraderFeed:
    """Liest geschlossene Bars aus dem NinjaScript-AddOn."""

    name = "nt8"

    def __init__(
        self,
        symbols: tuple[str, ...],
        timeframe: Timeframe = Timeframe.M1,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        contracts: dict[str, str] | None = None,
    ) -> None:
        if not symbols:
            raise ValueError("Ein Feed ohne Symbole abonniert nichts")
        self.symbols = tuple(s.upper() for s in symbols)
        self.timeframe = timeframe
        # NinjaTrader kennt Kontrakte ("MNQ SEP26"), TradeX rechnet mit dem
        # Wurzelsymbol ("MNQ"). Die Uebersetzung gehoert hierher: der Feed ist
        # der Adapter. Wuerde die Sitzung Bars unter dem Kontraktnamen sehen,
        # faende sie kein Buch dafuer - und der Betrieb liefe leer, ohne dass
        # irgendetwas nach einem Fehler aussaehe.
        self.contracts = {k.upper(): v for k, v in (contracts or {}).items()}
        self._back = {v.upper(): k.upper() for k, v in self.contracts.items()}
        self.host = host
        self.port = port

        self._queue: Queue[FeedMessage] = Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._contracts: dict[str, str] = {}
        self.ticks_seen = 0
        self.malformed = 0
        self.reconnects = 0

    # ------------------------------------------------------------------- Lauf
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="nt8-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5.0)

    @property
    def is_finished(self) -> bool:
        """Ein Marktfeed endet nie - er wird getrennt.

        Der Unterschied ist keine Wortklauberei: das Ende einer Wiedergabe ist
        ein regulaerer Abschluss, ein stiller Marktfeed ein Alarm. Wuerde hier
        True geliefert, beendete die Schleife den Betrieb bei jeder Stoerung
        statt es erneut zu versuchen.
        """
        return False

    # ------------------------------------------------------------- Lesefaden
    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                with socket.create_connection((self.host, self.port), timeout=_SOCKET_TIMEOUT) as sock:
                    attempt = 0
                    self._publish_status(True, f"{self.host}:{self.port}")
                    self._subscribe(sock)
                    self._read_loop(sock)
            except OSError as error:
                self._publish_status(False, str(error))
                if self._stop.is_set():
                    break
                delay = _RECONNECT_DELAYS[min(attempt, len(_RECONNECT_DELAYS) - 1)]
                attempt += 1
                self.reconnects += 1
                log.warning("nt8_verbindung_fehlt", fehler=str(error), naechster_versuch_s=delay)
                self._stop.wait(delay)

    def _subscribe(self, sock: socket.socket) -> None:
        for symbol in self.symbols:
            payload = {
                "type": "subscribe",
                "symbol": self.contracts.get(symbol, symbol),
                "timeframe": self.timeframe.value,
            }
            sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))

    def _read_loop(self, sock: socket.socket) -> None:
        buffer = b""
        while not self._stop.is_set():
            try:
                chunk = sock.recv(_RECEIVE_BYTES)
            except TimeoutError:
                # Kein Datenverkehr ist kein Fehler - ob es einer wird,
                # entscheidet die Stille-Ueberwachung der Sitzung.
                continue
            if not chunk:
                self._publish_status(False, "Gegenstelle hat geschlossen")
                return
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                self._handle_line(line)

    def _handle_line(self, raw: bytes) -> None:
        line = raw.strip()
        if not line:
            return
        try:
            message = json.loads(line.decode("utf-8"))
            kind = str(message["type"])
        except (ValueError, KeyError, UnicodeDecodeError):
            # Beim Verbindungsabriss bleibt regelmaessig eine halbe Zeile
            # zurueck. Daran darf der Lesefaden nicht sterben.
            self.malformed += 1
            return

        if kind == "bar":
            self._handle_bar(message)
        elif kind == "heartbeat":
            self._queue.put(HeartbeatMessage(ts=int(message.get("ts", time.time_ns()))))
        elif kind == "status":
            self._publish_status(
                bool(message.get("connected", False)), str(message.get("detail", ""))
            )
        elif kind == "tick":
            self.ticks_seen += 1
        else:
            self.malformed += 1

    def _handle_bar(self, message: dict[str, object]) -> None:
        try:
            gemeldet = str(message["symbol"]).upper()
            symbol = self._back.get(gemeldet, gemeldet)
            timeframe = str(message.get("timeframe", self.timeframe.value))
            bar = Bar(
                ts=int(message["ts"]),  # type: ignore[arg-type]
                open=float(message["open"]),  # type: ignore[arg-type]
                high=float(message["high"]),  # type: ignore[arg-type]
                low=float(message["low"]),  # type: ignore[arg-type]
                close=float(message["close"]),  # type: ignore[arg-type]
                volume=float(message.get("volume", 0.0)),  # type: ignore[arg-type]
                roll_boundary=self._is_roll(symbol, message.get("contract")),
            )
        except (KeyError, TypeError, ValueError):
            self.malformed += 1
            return

        if timeframe != self.timeframe.value:
            # Ein Abonnement, das wir nicht bestellt haben. Es stillschweigend
            # zu analysieren waere schlimmer als es zu verwerfen: die Sitzung
            # mischte zwei Zeitebenen in einen Detektorzustand.
            self.malformed += 1
            return

        self._queue.put(BarMessage(symbol=symbol, bar=bar, received_ts=time.time_ns()))

    def _is_roll(self, symbol: str, contract: object) -> bool:
        """Kontraktwechsel erkennen.

        Der Preissprung an der Kontraktnaht sieht aus wie eine riesige
        Imbalance mit starkem Impuls und ist doch nur ein Buchungsartefakt.
        Die erste Bar eines neuen Kontrakts wird deshalb markiert - genau wie
        im historischen Bestand (`tradex/data/rolls.py`).

        Der ERSTE gesehene Kontrakt ist keine Grenze: dass ein Feed irgendwo
        anfaengt, ist kein Roll.
        """
        if contract is None:
            return False
        name = str(contract)
        vorher = self._contracts.get(symbol)
        self._contracts[symbol] = name
        if vorher is None or vorher == name:
            return False
        log.info("nt8_kontraktwechsel", symbol=symbol, von=vorher, nach=name)
        return True

    def _publish_status(self, connected: bool, detail: str) -> None:
        self._queue.put(StatusMessage(ts=time.time_ns(), connected=connected, detail=detail))

    # ---------------------------------------------------------------- Abholen
    def messages(self, timeout: float) -> Iterator[FeedMessage]:
        try:
            yield self._queue.get(timeout=timeout)
        except Empty:
            return
        while True:
            try:
                yield self._queue.get_nowait()
            except Empty:
                return
