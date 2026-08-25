"""Marktbeobachtung: Kurse ansehen, ohne zu handeln.

Bisher gab es Livedaten nur ueber eine `TradingSession` - und eine Sitzung zu
starten heisst, dass ab dann echte Orders entstehen koennen. Wer nur sehen
wollte, wo der Kurs steht, musste dafuer den Handel scharfschalten. Das ist
die falsche Kopplung: Zusehen und Handeln sind verschiedene Dinge.

Was hier NICHT passiert
------------------------
Kein Risikobuch, kein Broker, kein Executor. Diese Klasse fuettert Bars in
denselben `MarketContext`, den auch Wiedergabe und Backtest benutzen
(Invariante 3) - mehr nicht. Aus einer Beobachtung kann strukturell keine
Order werden, weil es hier nichts gibt, das eine senden koennte.

Warum sie den Feed nicht selbst ausliest, sondern einen Faden dafuer hat
------------------------------------------------------------------------
`NinjaTraderFeed` liefert Nachrichten in eine Queue; jemand muss sie abholen.
Im Betrieb ist das der Sitzungsfaden. Ohne Sitzung braucht es einen eigenen -
sonst laeuft die Queue voll und der Chart bleibt stehen, waehrend Daten
ankommen.

Nur EINE Verbindung zur Bridge
-------------------------------
Startet eine Handelssitzung, wird die Beobachtung beendet: zwei Clients am
selben AddOn sind kein Fehler, den man beim Zusehen bemerkt, und der Betrieb
hat Vorrang. Die Sitzung fuehrt danach ihren eigenen Zustand, und der Chart
folgt ihr automatisch (`TradexService.chart_context`).
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from tradex.domain.bars import Bar
from tradex.domain.enums import Timeframe
from tradex.domain.instruments import Instrument
from tradex.live.feed import BarMessage, StatusMessage
from tradex.live.nt8_feed import DEFAULT_HOST, DEFAULT_PORT, NinjaTraderFeed
from tradex.logging_setup import get_logger

log = get_logger(__name__)

#: Wie lange auf Nachrichten gewartet wird, bevor die Abbruchbedingung erneut
#: geprueft wird. Kurz genug, dass ein Stopp nicht haengt.
_POLL_SECONDS = 0.25


class MarketWatch:
    """Ein Symbol live mitlesen - ohne Handel, ohne Risiko."""

    def __init__(
        self,
        symbol: str,
        instrument: Instrument,
        timeframe: Timeframe,
        on_bar: Callable[[str, Bar], None],
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        history_days: int = 0,
        history_timeout_seconds: float = 30.0,
    ) -> None:
        self.symbol = symbol.upper()
        self.instrument = instrument
        self._on_bar = on_bar
        self._feed = NinjaTraderFeed(
            (self.symbol,),
            timeframe,
            host=host,
            port=port,
            contracts={self.symbol: instrument.nt8_symbol} if instrument.nt8_symbol else None,
            history_days=history_days,
            history_timeout_seconds=history_timeout_seconds,
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.connected = False
        self.bars_seen = 0
        self.detail = ""

    # ------------------------------------------------------------------- Lauf
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._feed.start()
        self._thread = threading.Thread(target=self._run, name="marktbeobachtung", daemon=True)
        self._thread.start()
        log.info("beobachtung_gestartet", symbol=self.symbol)

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        self._feed.stop()
        if thread is not None:
            thread.join(timeout=5.0)
        self.connected = False
        log.info("beobachtung_beendet", symbol=self.symbol, bars=self.bars_seen)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def last_price(self) -> float:
        """Zuletzt gehandelter Kurs aus den Ticks - nur zur Anzeige."""
        return self._feed.last_price.get(self.symbol, 0.0)

    @property
    def last_tick_ts(self) -> int:
        """Wanduhr des letzten Ticks - 0, wenn noch keiner kam."""
        return self._feed.last_tick_ts.get(self.symbol, 0)

    @property
    def ticks_seen(self) -> int:
        return self._feed.ticks_seen

    def live_bar(self) -> Bar | None:
        """Die laufende Kerze aus Ticks - nur zur Anzeige, nie zur Analyse.

        Sie geht NICHT durch `_on_bar` und damit nicht in den `MarketContext`.
        Der Weg dorthin fuehrt ausschliesslich ueber geschlossene Bars des
        AddOns (Invariante 1).
        """
        return self._feed.live_bar(self.symbol)

    # -------------------------------------------------------------- Lesefaden
    def _run(self) -> None:
        while not self._stop.is_set():
            for message in self._feed.messages(_POLL_SECONDS):
                if isinstance(message, BarMessage):
                    if message.symbol != self.symbol:
                        continue
                    self.bars_seen += 1
                    try:
                        self._on_bar(message.symbol, message.bar)
                    except Exception as fehler:
                        # Ein Fehler in der Analyse darf die Beobachtung nicht
                        # beenden: sonst steht der Chart still und man sieht
                        # nur, dass "nichts mehr kommt" - der schlechteste
                        # aller Zustaende, weil er nach Marktruhe aussieht.
                        log.warning(
                            "beobachtung_analysefehler", symbol=self.symbol, fehler=str(fehler)
                        )
                elif isinstance(message, StatusMessage):
                    self.connected = message.connected
                    self.detail = message.detail
