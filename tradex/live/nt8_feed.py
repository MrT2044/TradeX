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

Ticks laufen NEBEN der Analyse her, nie hinein
-----------------------------------------------
Das AddOn sendet Ticks. Die Engine analysiert aber ausschliesslich geschlossene
Bars (Invariante 1); ein Tick, der irgendwo einfloesse, waere ein Zustand, den
der Backtest nie sieht. Sie landen deshalb **nicht** in der Nachrichten-Queue,
aus der die Sitzung liest, sondern in zwei Feldern daneben:

* `last_price` - der zuletzt gehandelte Kurs je Symbol
* `live_bar()` - die laufende Bar des Basis-Timeframes, aus Ticks gebaut

Die laufende Bar gibt es, weil das AddOn per Definition nur GESCHLOSSENE Bars
schickt: um 14:21:36 ist die letzte davon die von 14:20. Ohne die Tickbar zeigte
der Chart deshalb dauerhaft eine Minute zu wenig - die Kerze, die sich gerade
bildet, kaeme nirgends her. Sie ist reine Anzeige und wird nie weitergereicht;
wer sie in `on_base_bar` gaebe, brauchte den Backtest nicht mehr zu befragen.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from queue import Empty, Queue

from tradex.broker.nt8.protocol import ORDER_MESSAGE_TYPES
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

_NS_PER_SECOND = 1_000_000_000


@dataclass
class _TickBar:
    """Die laufende Bar eines Buckets, aus Ticks aufgebaut - nur zur Anzeige.

    Bewusst hier und nicht in `tradex/analysis/`: was aus Ticks entsteht, darf
    nicht in die Naehe des Analysepfads geraten. Ein Bar-Objekt daraus zu bauen
    ist Absicht - die Anzeige rechnet mit Bars - aber es verlaesst dieses Modul
    nur ueber `live_bar()`, und das liest niemand, der analysiert.
    """

    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def update(self, price: float, size: float) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += size

    def to_bar(self) -> Bar:
        return Bar(
            ts=self.ts,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
        )


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
        history_days: int = 0,
        history_timeout_seconds: float = 30.0,
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

        self.history_days = max(0, history_days)
        self.history_timeout_seconds = history_timeout_seconds

        self._queue: Queue[FeedMessage] = Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._contracts: dict[str, str] = {}
        self.ticks_seen = 0
        self.malformed = 0
        self.order_messages_ignored = 0
        """Orderereignisse, die hier vorbeikamen. Der Feed wertet sie nicht aus -
        dafuer ist `tradex/broker/nt8/` da -, aber sie zu zaehlen zeigt, dass
        beide Wege dieselbe Leitung benutzen."""
        self.reconnects = 0
        self.history_bars = 0
        #: Symbole, deren Historie noch laeuft. Solange hier etwas steht, gilt
        #: die Verbindung nach aussen als NICHT hergestellt - siehe
        #: `_publish_status`.
        self._history_pending: set[str] = set()
        self._history_deadline = 0.0
        self.last_price: dict[str, float] = {}
        """Zuletzt gehandelter Kurs je Symbol, aus den Ticks. AUSSCHLIESSLICH
        zur Anzeige: er geht in keinen Detektor und in keine Entscheidung ein.
        Ohne ihn steht der Chart zwischen zwei Bar-Schluessen eine Minute lang
        still, obwohl sich der Markt bewegt."""
        self.last_tick_ts: dict[str, int] = {}
        """Wanduhr des letzten Ticks je Symbol. Ohne sie liesse sich ein
        stehengebliebener Kurs nicht von einem ruhigen Markt unterscheiden -
        und die Anzeige zeigte stundenlang eine 'laufende' Kerze, die laengst
        keine mehr ist."""
        self._live: dict[str, _TickBar] = {}
        self._live_lock = threading.Lock()

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
                    self._subscribe(sock)
                    # Reihenfolge mit Bedacht: erst Historie anfordern, DANN
                    # den Verbindungszustand melden. Die Sitzung beginnt
                    # angehalten und nimmt erst Positionen auf, wenn sie
                    # "verbunden" gesehen hat - meldeten wir das zuerst,
                    # handelte sie auf drei Tage alten Bars, waehrend die
                    # Historie durchlaeuft. `_request_history` schiebt die
                    # Meldung bis `history_end` auf.
                    self._request_history(sock)
                    if not self._history_pending:
                        self._publish_status(True, f"{self.host}:{self.port}")
                    self._read_loop(sock)
                # Hierher kommt man, wenn die GEGENSTELLE geschlossen hat -
                # ohne Ausnahme, also am Wiederverbindungsdeckel unten vorbei.
                # Bisher ging es danach sofort in den naechsten Versuch: ein
                # AddOn, das jede Verbindung gleich wieder abweist (etwa
                # waehrend NinjaTrader neu uebersetzt), erzeugte damit eine
                # Schleife ohne Pause, die den Rechner belastet und den Grund
                # unter Tausenden Zeilen begraebt.
                if self._stop.is_set():
                    break
                delay = _RECONNECT_DELAYS[min(attempt, len(_RECONNECT_DELAYS) - 1)]
                attempt += 1
                self.reconnects += 1
                self._stop.wait(delay)
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

    def _request_history(self, sock: socket.socket) -> None:
        """Die letzten Tage aus NinjaTraders lokalem Bestand nachfordern.

        Fuer die echten Futures (MNQ, NQ) liegt auf der Platte nichts - deren
        Bars kommen live. Ohne Historie faengt der Chart bei null an und man
        sieht tagelang nicht, wo der laufende Kurs eigentlich steht.

        Die Bars kommen als gewoehnliche `bar`-Nachrichten zurueck und laufen
        durch denselben Weg wie Livebars. Das ist Absicht und kein Zufall: die
        Sitzung soll keinen zweiten Bar-Pfad kennen (Invariante 3).
        """
        self._history_pending = set()
        if self.history_days <= 0:
            return
        bis = time.time_ns()
        von = bis - self.history_days * 86_400 * 1_000_000_000
        for symbol in self.symbols:
            payload = {
                "type": "history",
                "symbol": self.contracts.get(symbol, symbol),
                "timeframe": self.timeframe.value,
                "from": von,
                "to": bis,
            }
            sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            self._history_pending.add(symbol)
        self._history_deadline = time.monotonic() + self.history_timeout_seconds
        log.info("nt8_historie_angefordert", tage=self.history_days, symbole=list(self.symbols))

    def _history_timed_out(self) -> None:
        """Ohne Antwort trotzdem weitermachen - aber es sagen.

        Ein AddOn, das den `history`-Befehl nicht kennt, antwortet nie. Ohne
        Deckel bliebe die Sitzung fuer immer angehalten: sie bekaeme Bars,
        wuerde sie analysieren und nie eine Position aufnehmen - und das saehe
        von aussen aus wie ein Markt ohne Signale.
        """
        offen = sorted(self._history_pending)
        self._history_pending.clear()
        log.warning("nt8_historie_ohne_antwort", offen=offen, sekunden=self.history_timeout_seconds)
        self._publish_status(True, f"{self.host}:{self.port} (ohne Historie)")

    def _read_loop(self, sock: socket.socket) -> None:
        buffer = b""
        while not self._stop.is_set():
            try:
                chunk = sock.recv(_RECEIVE_BYTES)
            except TimeoutError:
                # Kein Datenverkehr ist kein Fehler - ob es einer wird,
                # entscheidet die Stille-Ueberwachung der Sitzung.
                if self._history_pending and time.monotonic() > self._history_deadline:
                    self._history_timed_out()
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
            self._handle_tick(message)
        elif kind == "history_end":
            self._handle_history_end(message)
        elif kind in ORDER_MESSAGE_TYPES:
            # Seit Phase 9 laeuft der Orderweg ueber dieselbe Leitung, und das
            # AddOn sendet Orderereignisse per Broadcast an JEDEN Client. Sie
            # gehen den Feed nichts an - aber sie als kaputt zu zaehlen waere
            # falsch: `malformed` ist der Zeuge dafuer, dass die
            # Rahmenverarbeitung bricht. Ein Zaehler, der bei normalem Betrieb
            # hochlaeuft, taugt fuer keine Diagnose mehr.
            self.order_messages_ignored += 1
        else:
            self.malformed += 1

    def _handle_history_end(self, message: dict[str, object]) -> None:
        gemeldet = str(message.get("symbol", "")).upper()
        symbol = self._back.get(gemeldet, gemeldet)
        self._history_pending.discard(symbol)
        log.info("nt8_historie_fertig", symbol=symbol, bars=self.history_bars)
        if not self._history_pending:
            # Erst jetzt gilt die Verbindung als hergestellt: ab hier sind die
            # Bars aktuell, und die Sitzung darf Positionen aufnehmen.
            self._publish_status(True, f"{self.host}:{self.port}")

    def _handle_tick(self, message: dict[str, object]) -> None:
        """Nur den letzten Kurs merken - nichts davon geht in die Analyse.

        Die Engine wertet ausschliesslich geschlossene Bars aus (Invariante 1).
        Ein Tick, der irgendwo einfloesse, waere ein Zustand, den der Backtest
        nie sieht. Angezeigt werden darf er trotzdem: zwischen zwei
        Minutenschluessen bewegt sich der Markt, und ein Chart, der das
        verschweigt, sieht eingefroren aus.
        """
        self.ticks_seen += 1
        try:
            gemeldet = str(message["symbol"]).upper()
            preis = float(message["price"])  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError):
            self.malformed += 1
            return
        if preis <= 0:
            # Ein Preis von null ist kein Geschaeft, sondern ein Platzhalter.
            # Er wuerde die laufende Kerze bis auf die Nulllinie ziehen.
            self.malformed += 1
            return

        symbol = self._back.get(gemeldet, gemeldet)
        try:
            size = float(message.get("size", 0.0))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            size = 0.0
        # Der Zeitstempel des AddOns sagt, wann gehandelt wurde. Fuer die Frage
        # "ist dieser Kurs noch aktuell" zaehlt aber, wann WIR ihn gesehen
        # haben: eine falsch gestellte Uhr in NinjaTrader liesse eine tote
        # Anzeige sonst frisch aussehen.
        self.last_price[symbol] = preis
        self.last_tick_ts[symbol] = time.time_ns()

        bucket = self._tick_bucket(message)
        with self._live_lock:
            laufend = self._live.get(symbol)
            if laufend is None or laufend.ts != bucket:
                self._live[symbol] = _TickBar(bucket, preis, preis, preis, preis, size)
            else:
                laufend.update(preis, size)

    def _tick_bucket(self, message: dict[str, object]) -> int:
        """Beginn des Buckets, in den dieser Tick faellt - im Raster des Feeds.

        Genommen wird die Marktzeit des Ticks, nicht die Wanduhr: sonst landete
        ein Tick, der eine halbe Sekunde unterwegs war, ueber der Minutengrenze
        im falschen Bucket - und der Chart zeigte zwei Kerzen fuer dieselbe
        Minute. Fehlt der Zeitstempel, ist die Wanduhr die einzige Auskunft,
        die es gibt.
        """
        try:
            ts = int(message["ts"])  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError):
            ts = 0
        if ts <= 0:
            ts = time.time_ns()
        raster = self.timeframe.seconds * _NS_PER_SECOND
        return (ts // raster) * raster

    def live_bar(self, symbol: str) -> Bar | None:
        """Die aus Ticks gebaute laufende Bar - AUSSCHLIESSLICH zur Anzeige.

        Sie wird nie in die Queue gelegt und nie weitergereicht: wer sie
        analysierte, saehe einen Zustand, den der Backtest nicht kennt
        (Invariante 1).
        """
        with self._live_lock:
            laufend = self._live.get(symbol.upper())
            return laufend.to_bar() if laufend is not None else None

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

        if self._history_pending:
            self.history_bars += 1
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
