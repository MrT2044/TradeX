"""Historie aus NinjaTrader holen, ohne eine Handelssitzung zu starten.

Warum das ueberhaupt getrennt gehoert
--------------------------------------
Fuer die echten Kontrakte (MNQ, NQ) liegt lokal nichts; ihre Bars kommen
live. Der Chart blieb damit leer, bis jemand eine Sitzung startete - und eine
Sitzung zu starten heisst, den Handel scharfzuschalten. Zwischen "ich will
sehen, wo der Kurs steht" und "ab jetzt darf gehandelt werden" liegt aber
alles, und wer nur schauen will, sollte dafuer nicht handeln muessen.

Dieses Modul holt Bars und schreibt sie in den Parquet-Speicher. Es faellt
keine Entscheidung, es kennt keine Strategie, und es baut keinen Broker: der
`TradingSession` kommt hier nicht vor. Analysiert wird die Historie danach auf
dem ganz normalen Weg ueber `/api/load` - es gibt keinen zweiten Analysepfad
(Invariante 3).

Warum es den Feed benutzt und nicht selbst spricht
---------------------------------------------------
`NinjaTraderFeed` kann das Protokoll bereits, inklusive Wiederverbinden,
kaputten Zeilen und der Uebersetzung Kontrakt -> Wurzelsymbol. Ein zweiter
Socket-Client waere eine zweite Stelle, an der dieselben Fallen erneut
auftauchen - und die zweite ist immer die, die keiner pflegt.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from tradex.domain.bars import BarSeries
from tradex.domain.enums import Timeframe
from tradex.live.feed import BarMessage, StatusMessage
from tradex.live.nt8_feed import DEFAULT_HOST, DEFAULT_PORT, NinjaTraderFeed
from tradex.logging_setup import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class HistoryResult:
    """Was der Abruf gebracht hat - auch im Misserfolg aussagefaehig."""

    symbol: str
    bars: int
    first_ts: int
    last_ts: int
    complete: bool
    """True, wenn `history_end` kam. False heisst: die Zeit lief ab, und die
    Bars unten sind moeglicherweise nur ein Anfang. Der Unterschied gehoert in
    die Anzeige - ein halber Datenbestand, der wie ein ganzer aussieht, ist
    schlimmer als gar keiner."""
    detail: str = ""


def fetch_history(
    symbol: str,
    timeframe: Timeframe,
    *,
    days: int,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    contract: str = "",
    timeout_seconds: float = 60.0,
) -> tuple[BarSeries, HistoryResult]:
    """Bars der letzten `days` Tage abholen. Schreibt nichts - das tut der Aufrufer.

    Der Feed meldet `connected=True` erst NACH `history_end` (siehe
    `nt8_feed._request_history`). Genau dieses Signal ist hier die
    Abbruchbedingung: es kommt exakt dann, wenn die Historie durch ist.
    """
    symbol = symbol.upper()
    feed = NinjaTraderFeed(
        (symbol,),
        timeframe,
        host=host,
        port=port,
        contracts={symbol: contract} if contract else None,
        history_days=days,
        history_timeout_seconds=timeout_seconds,
    )

    series = BarSeries()
    complete = False
    detail = ""
    feed.start()
    try:
        ende = time.monotonic() + timeout_seconds
        while time.monotonic() < ende:
            for message in feed.messages(0.25):
                if isinstance(message, BarMessage) and message.symbol == symbol:
                    series.append_bar(message.bar)
                elif isinstance(message, StatusMessage):
                    if not message.connected:
                        # Ohne Gegenstelle hat Warten keinen Zweck. Der Feed
                        # versuchte es endlos weiter; hier ist das falsch, denn
                        # der Nutzer wartet auf eine Antwort.
                        detail = message.detail
                        return series, _result(symbol, series, False, detail or "keine Verbindung")
                    complete = True
                    detail = message.detail
            if complete:
                break
    finally:
        feed.stop()

    if not complete:
        detail = detail or f"keine Antwort innerhalb von {timeout_seconds:.0f} s"
    log.info(
        "nt8_historie_abgeholt",
        symbol=symbol,
        bars=len(series),
        vollstaendig=complete,
        detail=detail,
    )
    return series, _result(symbol, series, complete, detail)


def _result(symbol: str, series: BarSeries, complete: bool, detail: str) -> HistoryResult:
    return HistoryResult(
        symbol=symbol,
        bars=len(series),
        first_ts=int(series.ts[0]) if len(series) else 0,
        last_ts=int(series.ts[-1]) if len(series) else 0,
        complete=complete,
        detail=detail,
    )
