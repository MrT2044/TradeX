"""Was kommt tatsaechlich ueber die Bridge? - Messgeraet, keine Sitzung.

Warum es das braucht
--------------------
"Die Ticks gehen nicht" ist keine Diagnose. Es kann daran liegen, dass das
AddOn keine erzeugt, dass es das Instrument nicht aufloest, dass der
Datenanbieter keine Level-1-Daten liefert oder dass der Markt geschlossen ist -
und alle vier sehen von der Oberflaeche aus gleich aus: eine Kerze, die sich
nicht bewegt.

Dieses Skript zaehlt, was ueber die Leitung kommt, aufgeschluesselt nach
Nachrichtenart. Danach ist die Frage beantwortet statt bewertet.

Was es NICHT tut
----------------
Kein Risikobuch, kein Broker, kein Executor, keine Analyse. Es liest einen
Socket und zaehlt. Aus diesem Skript kann strukturell keine Order entstehen.

    .\\.venv\\Scripts\\python.exe scripts\\nt8_tick_probe.py --seconds 30
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from collections import Counter

from tradex.config import get_instrument, load_config
from tradex.live.nt8_feed import DEFAULT_HOST, DEFAULT_PORT

#: Wie lange auf den ersten Byte gewartet wird, bevor der Versuch als
#: gescheitert gilt. Kurz: eine Bridge, die laeuft, meldet sich sofort.
_CONNECT_TIMEOUT = 5.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="MNQ")
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--timeframe", default="", help="Standard: data.base_timeframe aus der Config"
    )
    args = parser.parse_args()

    config = load_config()
    timeframe = args.timeframe or config.data.base_timeframe.value

    # Abonniert wird der KONTRAKT ("MNQ SEP26"), nicht das Wurzelsymbol - genau
    # wie im Betrieb. Ein Probelauf, der etwas anderes abonniert als die
    # Sitzung, misst den falschen Weg.
    try:
        instrument = get_instrument(args.symbol.upper())
        kontrakt = instrument.nt8_symbol or args.symbol.upper()
    except Exception:
        kontrakt = args.symbol.upper()

    print(f"Bridge      : {args.host}:{args.port}")
    print(f"Abonniert   : {kontrakt}  ({timeframe})")
    print(f"Messdauer   : {args.seconds:.0f} s\n")

    try:
        sock = socket.create_connection((args.host, args.port), timeout=_CONNECT_TIMEOUT)
    except OSError as fehler:
        print(f"KEINE VERBINDUNG: {fehler}")
        print("Laeuft NinjaTrader, und ist das AddOn uebersetzt (F5 im NinjaScript-Editor)?")
        return 1

    arten: Counter[str] = Counter()
    preise: list[float] = []
    erste_bar = ""
    letzter_tick = ""
    unbekannt: list[str] = []

    with sock:
        sock.sendall(
            (
                json.dumps({"type": "subscribe", "symbol": kontrakt, "timeframe": timeframe}) + "\n"
            ).encode("utf-8")
        )
        sock.settimeout(1.0)
        ende = time.monotonic() + args.seconds
        puffer = b""
        while time.monotonic() < ende:
            try:
                stueck = sock.recv(65536)
            except TimeoutError:
                continue
            if not stueck:
                print("Gegenstelle hat geschlossen.")
                break
            puffer += stueck
            while b"\n" in puffer:
                zeile, puffer = puffer.split(b"\n", 1)
                if not zeile.strip():
                    continue
                try:
                    nachricht = json.loads(zeile)
                    art = str(nachricht["type"])
                except (ValueError, KeyError):
                    arten["KAPUTT"] += 1
                    continue
                arten[art] += 1
                if art == "tick":
                    preise.append(float(nachricht.get("price", 0.0)))
                    letzter_tick = json.dumps(nachricht)
                elif art == "bar" and not erste_bar:
                    erste_bar = json.dumps(nachricht)
                elif art == "status":
                    print(f"  status: {nachricht.get('detail', '')}")
                elif art not in ("heartbeat", "history_end") and len(unbekannt) < 3:
                    unbekannt.append(json.dumps(nachricht))

    # ------------------------------------------------------------- Befund
    print("\nGezaehlt:")
    for art, zahl in sorted(arten.items()):
        print(f"  {art:<12} {zahl}")
    if not arten:
        print("  (nichts - die Bridge hat geschwiegen)")
    for zeile in unbekannt:
        print(f"  unbekannte Nachricht: {zeile}")
    if erste_bar:
        print(f"\nErste Bar   : {erste_bar}")
    if letzter_tick:
        print(f"Letzter Tick: {letzter_tick}")

    ticks = arten["tick"]
    print()
    if ticks:
        spanne = f"{min(preise):.2f} - {max(preise):.2f}" if preise else "?"
        print(f"BEFUND: {ticks} Ticks, Kursspanne {spanne}.")
        print("Die Produzentenseite liefert. Die laufende Kerze kann sich bewegen.")
        if preise and min(preise) == max(preise):
            print("ABER: der Kurs hat sich nicht veraendert - ruhiger Markt oder ein")
            print("stehender Feed. Laenger messen, um das zu unterscheiden.")
        return 0

    print("BEFUND: KEIN EINZIGER TICK.")
    if arten["heartbeat"]:
        print("Die Bridge lebt (Heartbeats kommen), sendet aber keine Ticks. Moegliche")
        print("Gruende, in der Reihenfolge, in der man sie pruefen sollte:")
        print("  1. Das AddOn ist noch die alte Fassung - F5 im NinjaScript-Editor.")
        print("  2. Der Datenanbieter liefert keine Level-1-Daten fuer diesen Kontrakt")
        print("     (im Output-Tab von NinjaTrader steht dann eine Meldung dazu).")
        print("  3. Der Markt ist geschlossen - dann kommen auch keine Bars.")
    else:
        print("Es kam nicht einmal ein Heartbeat - die Gegenstelle ist nicht die Bridge.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
