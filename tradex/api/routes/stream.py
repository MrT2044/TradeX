"""Livedaten ohne Dauerabfragen (Server-Sent Events).

Warum SSE und nicht WebSockets
------------------------------
Der Datenfluss geht ausschliesslich in eine Richtung: der Server meldet, das
Dashboard schaut zu. Steuerbefehle laufen weiter ueber die vorhandenen
POST-Endpunkte, wo sie zusammen mit ihrer Berechtigungspruefung stehen. Ein
bidirektionaler Kanal waere dafuer nicht nur unnoetig, sondern eine zweite
Stelle, an der Befehle ins System gelangen koennten - und die muesste dieselbe
Pruefung noch einmal fuehren.

SSE bringt ausserdem mit, was hier zaehlt: es laeuft ueber gewoehnliches HTTP
(also durch jeden Reverse Proxy), und der Browser verbindet nach einem Abriss
von selbst neu. Genau das ist der Normalfall auf einem Handy, das kurz das
Netz wechselt.

Warum trotzdem gepollt wird - nur serverseitig
-----------------------------------------------
Die Sitzung kennt keinen Beobachter, dem sie etwas zurufen koennte. Der
Zustand wird deshalb hier im Takt abgefragt und nur dann gesendet, wenn er
sich geaendert hat. Der Unterschied zum bisherigen Verfahren ist nicht die
Abfrage, sondern wo sie stattfindet: statt einer HTTP-Anfrage je Sekunde je
Betrachter laeuft eine Schleife im Server.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from tradex.api.schemas import BarDto, SessionStatusDto
from tradex.api.state import get_service
from tradex.domain.enums import Timeframe
from tradex.logging_setup import get_logger

router = APIRouter(tags=["stream"])

log = get_logger(__name__)

#: Wie oft der Zustand geprueft wird. Schnell genug, dass ein Not-Aus sichtbar
#: wird, bevor jemand ein zweites Mal drueckt.
POLL_SECONDS = 1.0

#: Ohne Aenderung wird trotzdem gesendet, damit die Leitung nicht als tot gilt.
#: Proxies und Mobilfunknetze schliessen stille Verbindungen.
#:
#: Bewusst ein BENANNTES Ereignis und keine Kommentarzeile (`: ping`): ein
#: Kommentar haelt zwar die Verbindung offen, loest im Browser aber keinen
#: Listener aus. Der Betrachter koennte dann nicht unterscheiden, ob nichts
#: passiert oder ob die Leitung tot ist - und wuerde nach kurzer Zeit
#: faelschlich melden, seine Zahlen seien veraltet.
HEARTBEAT_SECONDS = 10.0


#: Takt des Kursstroms. Vier Mal je Sekunde reichte nicht - so sah die Anzeige
#: neben NinjaTrader aus, als stuende sie still. Zehn Mal je Sekunde ist die
#: Grenze dessen, was ein Auge als fluessig liest, und kostet nichts: gesendet
#: wird nur bei Aenderung, und die Nutzlast ist eine Zeile.
TICK_POLL_SECONDS = 0.1

#: Wie oft der Kursstrom ohne Aenderung ein Lebenszeichen schickt. Kuerzer als
#: beim Zustandsstrom, weil ein ruhiger Markt hier der Normalfall ist.
TICK_HEARTBEAT_SECONDS = 5.0


def _payload(dto: SessionStatusDto) -> str:
    return dto.model_dump_json()


async def _events(request: Request) -> AsyncIterator[bytes]:
    letzte = ""
    seit_heartbeat = 0.0

    while True:
        if await request.is_disconnected():
            return

        try:
            aktuell = _payload(SessionStatusDto.of(get_service().sessions.state()))
        except Exception as fehler:  # der Stream darf nicht am Zustand sterben
            # Ein Fehler beim Erheben des Zustands ist selbst eine Meldung -
            # die Leitung stillschweigend zu beenden waere das Gegenteil von
            # dem, wofuer diese Ansicht da ist.
            log.warning("stream_zustand_fehlgeschlagen", fehler=str(fehler))
            yield b"event: error\ndata: " + json.dumps({"message": str(fehler)}).encode() + b"\n\n"
            await asyncio.sleep(POLL_SECONDS)
            continue

        if aktuell != letzte:
            letzte = aktuell
            seit_heartbeat = 0.0
            yield b"event: session\ndata: " + aktuell.encode("utf-8") + b"\n\n"
        else:
            seit_heartbeat += POLL_SECONDS
            if seit_heartbeat >= HEARTBEAT_SECONDS:
                seit_heartbeat = 0.0
                yield b'event: heartbeat\ndata: {"ok":true}\n\n'

        await asyncio.sleep(POLL_SECONDS)


def tick_payload(symbol: str, timeframe: Timeframe) -> str:
    """Laufender Kurs und laufende Kerze - sonst nichts.

    Bewusst NICHT ueber `/stream`: der traegt den gesamten Betriebszustand mit
    sich und prueft einmal je Sekunde. Fuer eine Kursanzeige ist das zu schwer
    und zu grob. Und bewusst nicht andersherum - Kurse in den Zustandsstrom zu
    legen hiesse, jede Sekunde den ganzen Betriebszustand zu senden, weil sich
    eine Zahl darin geaendert hat.

    Die laufende Kerze kommt fertig aus dem Service. Sie hier oder gar im
    Browser zusammenzusetzen hiesse, das Bucket-Raster ein zweites Mal zu
    rechnen - und am Rand des Rasters saehe man zwei Kerzen fuer dieselbe
    Minute.
    """
    service = get_service()
    try:
        bar = service.display_bar(symbol, timeframe)
        preis = service.last_price(symbol)
    except LookupError:
        # Symbol nicht geladen: kein Fehler, sondern "es gibt nichts zu
        # zeigen". Der Strom bleibt offen - das Symbol kann gleich da sein.
        bar, preis = None, 0.0
    return json.dumps(
        {
            "symbol": symbol.upper(),
            "timeframe": timeframe.value,
            "price": preis,
            "bar": BarDto.of(bar).model_dump() if bar is not None else None,
        }
    )


async def _ticks(request: Request, symbol: str, timeframe: Timeframe) -> AsyncIterator[bytes]:
    letzte = ""
    seit_heartbeat = 0.0

    while True:
        if await request.is_disconnected():
            return
        try:
            aktuell = tick_payload(symbol, timeframe)
        except Exception as fehler:  # der Strom darf nicht am Zustand sterben
            log.warning("tickstrom_fehlgeschlagen", fehler=str(fehler))
            yield b"event: error\ndata: " + json.dumps({"message": str(fehler)}).encode() + b"\n\n"
            await asyncio.sleep(TICK_POLL_SECONDS)
            continue

        if aktuell != letzte:
            # Zusammenfassung statt Vollstaendigkeit: die Anzeige braucht den
            # NEUESTEN Kurs, nicht jeden einzelnen. Wer zwischen zwei Takten
            # drei Ticks verpasst, sieht trotzdem das Richtige - wer den
            # neuesten verpasst, nicht.
            letzte = aktuell
            seit_heartbeat = 0.0
            yield b"event: tick\ndata: " + aktuell.encode("utf-8") + b"\n\n"
        else:
            seit_heartbeat += TICK_POLL_SECONDS
            if seit_heartbeat >= TICK_HEARTBEAT_SECONDS:
                seit_heartbeat = 0.0
                yield b'event: heartbeat\ndata: {"ok":true}\n\n'

        await asyncio.sleep(TICK_POLL_SECONDS)


@router.get("/ticks")
async def ticks(request: Request, symbol: str, timeframe: str = "5m") -> StreamingResponse:
    """Kursstrom fuer den Chart. Keine Sitzungsdaten, keine Steuerbefehle."""
    try:
        tf = Timeframe.parse(timeframe)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return StreamingResponse(
        _ticks(request, symbol, tf),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/stream")
async def stream(request: Request) -> StreamingResponse:
    """Laufender Zustandsstrom fuer das Dashboard."""
    return StreamingResponse(
        _events(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            # Ohne das puffert nginx den Strom und die Anzeige steht - der
            # klassische Fehler bei SSE hinter einem Reverse Proxy.
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
