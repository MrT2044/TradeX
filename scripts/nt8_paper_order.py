"""Eine echte Order auf NinjaTraders Simulationskonto - der Nachweis fuer A8.

    .\\.venv\\Scripts\\python.exe scripts\\nt8_paper_order.py --yes-send-paper-order

Dieses Skript SENDET tatsaechlich. Zwei Bestaetigungen stehen davor, und beide
schuetzen vor verschiedenen Fehlern: `--yes-send-paper-order` verhindert, dass
ein versehentlicher Aufruf handelt; die Eingabe `JA` verhindert, dass eine aus
der Befehlsgeschichte zurueckgeholte Zeile es tut.

**Es gibt keinen Schalter an der Sicherheitskette vorbei.** Ohne
`--yes-send-paper-order` verbindet das Skript nur, zeigt das Konto und
beendet sich - das ist zugleich der Verbindungstest.

Was hier NICHT passiert
-----------------------
Keine Strategie, kein Signal, kein Risikobuch. Dieses Skript prueft den
ORDERWEG, nicht die Handelsregeln. Die Menge steht auf der Kommandozeile und
kommt nicht aus einer Positionsgroessenrechnung.

Aufraeumen ist Pflicht
----------------------
Der `finally`-Block storniert und stellt glatt - auch nach einer Ausnahme und
auch nach Strg+C. Eine offene Position auf einem unbeaufsichtigten Konto ist
der einzige Ausgang, den dieses Skript nicht hinterlassen darf.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import socket
import sys
import time

from tradex.broker.base import BrokerError
from tradex.broker.env import read_env
from tradex.broker.guard import check_configuration
from tradex.broker.nt8.adapter import NinjaTraderBroker
from tradex.broker.types import OrderRequest, OrderSide
from tradex.config import get_config, get_instruments
from tradex.logging_setup import setup_logging

TRENNER = "=" * 74


def _argumente() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Eine echte Order auf Sim101 senden.")
    parser.add_argument("--symbol", default="MNQ", help="Instrument (Default MNQ)")
    parser.add_argument("--quantity", type=int, default=1, help="Kontrakte (Default 1)")
    parser.add_argument("--side", choices=["BUY", "SELL"], default="BUY")
    parser.add_argument(
        "--stop",
        type=float,
        default=0.0,
        help="Stopkurs (absolut). Ohne Angabe geht die Order ohne Klammer hinaus. "
        "Den aktuellen Kurs zeigt scripts/nt8_tick_probe.py.",
    )
    parser.add_argument("--target", type=float, default=0.0, help="Zielkurs (absolut)")
    parser.add_argument(
        "--timeout", type=float, default=30.0, help="Wie lange auf Rueckmeldungen gewartet wird"
    )
    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="Position NICHT glattstellen. Nur bewusst benutzen.",
    )
    parser.add_argument(
        "--yes-send-paper-order",
        action="store_true",
        help="Ohne diesen Schalter wird nichts gesendet.",
    )
    return parser.parse_args()


class _Marktdaten:
    """Haelt waehrend des Tests ein Marktdaten-Abonnement offen.

    NinjaTraders Simulationsmotor fuellt nur, solange fuer das Instrument
    Echtzeitdaten anliegen; sonst lehnt er mit "There is no market data
    available to drive the simulation engine" ab. Genau das ist beim ersten
    Versuch passiert: der Orderweg baut nur die Broker-Verbindung auf, und die
    abonniert bewusst keine Kursdaten.

    Im Betrieb stellt sich die Frage nicht - dort laeuft der Feed und hat
    laengst abonniert. Sie stellt sich nur fuer dieses eigenstaendige Skript,
    und deshalb steht die Loesung hier und nicht im Adapter: eine
    Orderanbindung, die nebenbei Kursdaten bestellt, waere eine Vermischung
    zweier Wege, die dieses Projekt getrennt haelt.
    """

    def __init__(self, host: str, port: int, kontrakt: str, timeframe: str) -> None:
        self._host, self._port = host, port
        self._kontrakt, self._timeframe = kontrakt, timeframe
        self._sock: socket.socket | None = None

    def oeffnen(self) -> None:
        try:
            self._sock = socket.create_connection((self._host, self._port), timeout=5.0)
            befehl = {
                "type": "subscribe",
                "symbol": self._kontrakt,
                "timeframe": self._timeframe,
            }
            self._sock.sendall((json.dumps(befehl) + "\n").encode("utf-8"))
            print(f"  Marktdaten        abonniert ({self._kontrakt})")
        except OSError as fehler:
            # Kein Abbruch: vielleicht laeuft TradeX daneben und hat bereits
            # abonniert. Verschweigen waere aber falsch - ohne Daten lehnt der
            # Simulationsmotor jede Order ab, und man suchte den Grund im
            # Orderweg.
            print(f"  Marktdaten        FEHLGESCHLAGEN ({fehler})")

    def schliessen(self) -> None:
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None


def _ereignisse_zeigen(broker: NinjaTraderBroker, dauer: float) -> None:
    """Rueckmeldungen mitschreiben - der eigentliche Gegenstand des Tests.

    Ein erfolgreicher Sendeaufruf sagt nichts. Ob eine Position entstanden
    ist, sagen ausschliesslich `order_update` und `execution`.
    """
    ende = time.monotonic() + dauer
    while time.monotonic() < ende:
        for ereignis in broker.drain_events():
            kennung = ereignis.order_key or "-"
            if ereignis.kind == "fill":
                p = ereignis.payload
                print(
                    f"    fill        {kennung:<24} {p.get('quantity')} @ "
                    f"{p.get('price')}  Gebuehr {p.get('commission')}"
                )
            elif ereignis.kind == "order":
                zustand = ereignis.state.value if ereignis.state else "?"
                print(f"    order       {kennung:<24} {zustand}")
            elif ereignis.kind == "error":
                p = ereignis.payload
                print(f"    ABGELEHNT   {kennung:<24} {p.get('code')}: {p.get('detail')}")
            elif ereignis.kind == "position":
                p = ereignis.payload
                print(f"    position    {p.get('symbol'):<24} {p.get('quantity')}")
            elif ereignis.kind == "connection":
                print("    VERBINDUNG VERLOREN")
        time.sleep(0.25)


def main() -> int:
    args = _argumente()
    config = get_config()
    setup_logging("WARNING", config.path(config.data.log_dir))

    print(TRENNER)
    print("  ECHTE ORDER AUF NINJATRADER - dieses Skript sendet tatsaechlich")
    print(TRENNER)

    # --- Sicherheitskette: kein Weg vorbei ----------------------------------
    env = read_env(config.execution, config.broker)
    gate = check_configuration(config.execution, config.broker, env)
    print(f"  Modus             {config.execution.mode.value}")
    print(f"  live_trading      {config.execution.live_trading_enabled}")
    print(f"  broker.enabled    {config.broker.enabled}")
    print(f"  provider          {config.broker.provider}")
    for reason in gate.reasons:
        marke = "ok  " if reason.ok else "SPERRT"
        print(f"    {marke}  {reason.code}  {reason.params}")

    if not gate.approved:
        print()
        print(f"  ABBRUCH: die Sicherheitskette sperrt ({gate.blocking_code}).")
        return 1

    if config.broker.provider != "nt8":
        print(f"\n  ABBRUCH: broker.provider steht auf {config.broker.provider!r}, nicht 'nt8'.")
        return 1

    if config.execution.live_trading_enabled:
        # Doppelt geprueft, weil die Folge hier nicht rueckgaengig zu machen ist.
        print("\n  ABBRUCH: live_trading_enabled ist an. Dieses Skript ist nur fuer Paper.")
        return 1

    symbol = args.symbol.upper()
    instrumente = get_instruments()
    if symbol not in instrumente or not instrumente[symbol].nt8_symbol:
        print(f"\n  ABBRUCH: {symbol} hat kein nt8_symbol in config/instruments.yaml.")
        print("  NinjaTraders Datenanbieter will den Kontrakt, nicht das Wurzelsymbol.")
        return 1

    mit_klammer = args.stop > 0 or args.target > 0
    print()
    print(f"  Symbol            {symbol}  ({instrumente[symbol].nt8_symbol})")
    print(f"  Richtung          {args.side}")
    print(f"  Kontrakte         {args.quantity}")
    print(
        "  Ordertyp          MARKET"
        + (f", Stop {args.stop} / Ziel {args.target}" if mit_klammer else ", ohne Klammer")
    )
    print("  Danach            " + ("BLEIBT OFFEN (--keep-open)" if args.keep_open
                                    else "wird sofort glattgestellt"))

    broker = NinjaTraderBroker(
        host=config.broker.nt8.host,
        port=config.broker.nt8.port,
        account=config.broker.nt8.account,
        # Ohne Bestaetigung wird verbunden, aber es gibt keinen Sendeweg.
        allow_orders=args.yes_send_paper_order,
        tradeable_symbols=(symbol,),
        allowed_accounts=config.broker.nt8.allowed_accounts,
        contracts={symbol: instrumente[symbol].nt8_symbol},
        connect_timeout_seconds=config.broker.nt8.connect_timeout_seconds,
    )

    print()
    print(f"  Verbinde mit {config.broker.nt8.host}:{config.broker.nt8.port} ...")
    try:
        broker.connect()
    except BrokerError as fehler:
        print(f"\n  FEHLGESCHLAGEN: {fehler}")
        print("  Laeuft NinjaTrader, und ist TradeXBridge.cs uebersetzt (F5)?")
        return 1

    # Das Abonnement umschliesst ALLES, auch das Aufraeumen: das Glattstellen
    # ist selbst eine Order und braucht dieselben Daten wie der Einstieg.
    marktdaten = _Marktdaten(
        config.broker.nt8.host,
        config.broker.nt8.port,
        instrumente[symbol].nt8_symbol,
        config.data.base_timeframe.value,
    )

    marktdaten.oeffnen()

    gesendet = False
    try:
        konto = broker.get_account_info()
        print(f"  Konto             {konto.account}")
        print(f"  Simulation        {konto.is_paper}  ({konto.paper_evidence})")
        print(f"  Kontostand        {konto.net_liquidation:,.2f} {konto.currency}")

        handelbar, begruendung = broker.can_trade(symbol)
        if not handelbar:
            print(f"\n  ABBRUCH: {begruendung}")
            return 1

        if not args.yes_send_paper_order:
            print()
            print("  Es wurde NICHTS gesendet: --yes-send-paper-order fehlt.")
            print("  Bis hierher ist das der Verbindungs- und Kontotest.")
            return 2

        print()
        antwort = input("  Wirklich senden? Tippe JA (grossgeschrieben): ").strip()
        if antwort != "JA":
            print("  Abgebrochen - es wurde nichts gesendet.")
            return 2

        request = OrderRequest(
            # Eindeutig ueber Prozessneustarts hinweg - das AddOn lehnt einen
            # zweimal benutzten Schluessel ab, auch wenn die erste Order
            # laengst geschlossen ist.
            order_key=f"manueller-test-{time.time_ns()}",
            symbol=symbol,
            side=OrderSide(args.side),
            quantity=args.quantity,
            stop_loss=args.stop,
            take_profit=args.target,
            signal_id=0,
            strategy="manueller_test",
        )

        print()
        print(f"  Sende {request.order_key} ...")
        gesendet = True
        order = broker.place_market_order(request)
        print(f"  Gesendet          #{order.order_id}  {order.state.value}")
        print()
        print(f"  Rueckmeldungen (max {args.timeout:.0f} s):")
        _ereignisse_zeigen(broker, args.timeout)

        offene = broker.get_open_orders()
        positionen = [p for p in broker.get_positions() if p.quantity != 0]
        print()
        print(f"  Offene Orders     {len(offene)}")
        for offen in offene:
            print(f"    #{offen.order_id:<4} {offen.symbol:<8} {offen.state.value}")
        print(f"  Positionen        {len(positionen)}")
        for position in positionen:
            print(f"    {position.symbol:<8} {position.quantity:>4} @ {position.avg_price:,.2f}")

        if not positionen and not offene:
            print()
            print("  Keine Fuellung und keine offene Order. Das ist KEIN Fehler, wenn der")
            print("  Markt geschlossen ist - MNQ pausiert taeglich 16:00-17:00 CT und")
            print("  ruht von Freitag 16:00 CT bis Sonntag 17:00 CT.")
    finally:
        if gesendet and not args.keep_open:
            print()
            print("  Raeume auf: stornieren, dann glattstellen ...")
            try:
                # Reihenfolge wie im AddOn: erst Storno. Andersherum loeste
                # eine noch stehende Klammerorder auf der glattgestellten
                # Position eine Gegenposition aus.
                broker.cancel_all_orders()
                broker.close_position(symbol)
                _ereignisse_zeigen(broker, 15.0)
                rest = [p for p in broker.get_positions() if p.quantity != 0]
                if rest:
                    print()
                    print("  ACHTUNG: es ist noch eine Position offen:")
                    for position in rest:
                        print(f"    {position.symbol:<8} {position.quantity:>4}")
                    print("  In NinjaTrader von Hand schliessen (Control Center -> Positions).")
                else:
                    print("  Konto ist glatt.")
            except BrokerError as fehler:
                print(f"  AUFRAEUMEN FEHLGESCHLAGEN: {fehler}")
                print("  Positionen und Orders in NinjaTrader von Hand pruefen.")
        # Erst nach dem Aufraeumen: das Glattstellen ist selbst eine Order und
        # braucht dieselben Marktdaten wie der Einstieg.
        marktdaten.schliessen()
        broker.disconnect()
        print()
        print("  Verbindung getrennt.")

    print(TRENNER)
    return 0


if __name__ == "__main__":
    sys.exit(main())
