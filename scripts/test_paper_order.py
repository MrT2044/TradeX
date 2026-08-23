"""Eine einzelne echte Paper-Order senden - von Hand, mit zwei Bestaetigungen.

    .\\.venv\\Scripts\\python.exe scripts\\test_paper_order.py --yes-send-paper-order

Das ist der Nachweis, den kein Test ersetzen kann: dass eine Order den ganzen
Weg geht - Sicherheitskette, Kontraktaufloesung, `placeOrder`, Rueckmeldung,
Fuellung - und dass danach wieder alles glatt ist.

Warum zwei Bestaetigungen
--------------------------
`--yes-send-paper-order` verhindert, dass ein versehentlicher Aufruf handelt;
die Eingabe `JA` verhindert, dass eine zurueckgeholte Zeile aus der
Kommandozeilen-Historie handelt. Beides zusammen kostet fuenf Sekunden und
schuetzt gegen die zwei Wege, auf denen so ein Skript sonst ungewollt laeuft.

Warum es hier keine Ausnahme von der Sicherheitskette gibt
-----------------------------------------------------------
Sperrt die Kette, bricht dieses Skript ab. Es gibt keinen Schalter dagegen.
Ein "nur zum Testen"-Weg an der Kette vorbei waere genau der Weg, der spaeter
mit echtem Geld benutzt wird - und die Kette waere ab dann Dekoration.

Warum es nicht `test_` im Wurzelverzeichnis heisst
---------------------------------------------------
Wie `test_ibkr_connection.py`: ein blankes `pytest` sammelt `test_*.py` im
Wurzelverzeichnis ein und wuerde bei jedem Testlauf eine Order senden.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tradex.broker.base import BrokerError
from tradex.broker.env import read_env
from tradex.broker.guard import check_configuration
from tradex.broker.types import OrderRequest, OrderSide, OrderState
from tradex.config import get_config, get_instruments
from tradex.logging_setup import setup_logging

TRENNER = "=" * 78


def _argumente() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Eine echte Paper-Order senden.")
    parser.add_argument("--symbol", default="MNQ", help="Instrument (Default MNQ)")
    parser.add_argument("--quantity", type=int, default=1, help="Kontrakte (Default 1)")
    parser.add_argument(
        "--side",
        default="BUY",
        choices=[seite.value for seite in OrderSide],
        help="Richtung (Default BUY)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Sekunden auf die Fuellung warten (Default 60)",
    )
    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="Position NICHT glattstellen. Nur benutzen, wenn man sie von Hand schliesst.",
    )
    parser.add_argument(
        "--yes-send-paper-order",
        action="store_true",
        help="Erste von zwei Bestaetigungen. Ohne sie wird nichts gesendet.",
    )
    return parser.parse_args()


def _ereignisse_zeigen(adapter, dauer: float, bis_endzustand: bool = True) -> None:
    """Rueckmeldungen abholen und ausgeben, bis die Order endgueltig ist.

    Es wird gepollt, nicht blockierend gewartet: `drain_events()` kehrt sofort
    zurueck, weil im Betrieb der Sitzungsfaden ruft - und der darf nie auf den
    Broker warten. Dieses Skript benutzt denselben Weg, damit es dasselbe
    Verhalten prueft wie der Betrieb.
    """
    ende = time.monotonic() + dauer
    while time.monotonic() < ende:
        for event in adapter.drain_events():
            zustand = event.state.value if event.state else ""
            print(
                f"    [{event.kind:<10}] {event.order_key or '-':<24} "
                f"#{event.order_id or 0:<8} {zustand:<17} {event.payload or ''}"
            )
            if bis_endzustand and event.state is OrderState.FILLED:
                return
            if bis_endzustand and event.state in (OrderState.REJECTED, OrderState.CANCELLED):
                return
        time.sleep(0.25)


def main() -> int:
    args = _argumente()
    config = get_config()
    setup_logging("WARNING", config.path(config.data.log_dir))

    print(TRENNER)
    print("  ECHTE PAPER-ORDER - dieses Skript sendet tatsaechlich")
    print(TRENNER)

    # --- Sicherheitskette: kein Weg vorbei ----------------------------------
    env = read_env(config.execution, config.broker)
    gate = check_configuration(config.execution, config.broker, env)
    print(f"  Modus             {config.execution.mode.value}")
    print(f"  live_trading      {config.execution.live_trading_enabled}")
    print(f"  broker.enabled    {config.broker.enabled}")
    for reason in gate.reasons:
        marke = "ok  " if reason.ok else "SPERRT"
        print(f"    {marke}  {reason.code}  {reason.params}")

    if not gate.approved:
        print()
        print(f"  ABBRUCH: die Sicherheitskette sperrt ({gate.blocking_code}).")
        print("  Fuer den Paper-Handel noetig: execution.mode: paper_auto und")
        print("  broker.enabled: true in config/default.yaml.")
        return 1

    if config.execution.live_trading_enabled:
        # Doppelt geprueft, weil die Folge hier nicht rueckgaengig zu machen ist.
        print("\n  ABBRUCH: live_trading_enabled ist an. Dieses Skript ist nur fuer Paper.")
        return 1

    symbol = args.symbol.upper()
    instruments = get_instruments()
    if symbol not in instruments or instruments[symbol].ibkr is None:
        print(f"\n  ABBRUCH: {symbol} hat keinen ibkr-Block in config/instruments.yaml.")
        return 1
    handelbar = {s: i for s, i in instruments.items() if i.ibkr is not None}

    print()
    print(f"  Symbol            {symbol}")
    print(f"  Richtung          {args.side}")
    print(f"  Kontrakte         {args.quantity}")
    print("  Ordertyp          MARKET, ohne Bracket")
    print("  Danach            " + ("BLEIBT OFFEN (--keep-open)" if args.keep_open
                                    else "wird sofort glattgestellt"))

    if not args.yes_send_paper_order:
        print()
        print("  Es wurde NICHTS gesendet: --yes-send-paper-order fehlt.")
        return 2

    print()
    antwort = input("  Wirklich senden? Tippe JA (grossgeschrieben): ").strip()
    if antwort != "JA":
        print("  Abgebrochen - es wurde nichts gesendet.")
        return 2

    try:
        from tradex.broker.ibkr.adapter import IbkrAdapter
    except ImportError as fehler:
        print(f"\n  {fehler}")
        return 1

    adapter = IbkrAdapter(config, handelbar, env=env, allow_orders=True)

    print()
    print(f"  Verbinde mit {config.broker.ibkr.host}:{config.broker.ibkr.paper_port} ...")
    try:
        adapter.connect()
    except BrokerError as fehler:
        print(f"\n  FEHLGESCHLAGEN: {fehler}")
        return 1

    gesendet = False
    try:
        konto = adapter.get_account_info()
        print(f"  Konto             {konto.account}")
        print(f"  Paper bestaetigt  {konto.is_paper}  ({konto.paper_evidence})")

        # Die Allowlist ist der einzige harte Nachweis. Ein Praefixtreffer
        # allein reicht hier nicht: dieses Skript sendet, der Verbindungstest
        # nicht.
        if konto.account not in config.broker.ibkr.allowed_accounts:
            print()
            print("  ABBRUCH: Konto steht nicht in broker.ibkr.allowed_accounts.")
            print(f'  Wenn das das Paper-Konto ist: allowed_accounts: ["{konto.account}"]')
            return 1

        handelbar_ok, begruendung = adapter.can_trade(symbol)
        print(f"  Kontrakt          {begruendung}")
        if not handelbar_ok:
            print("\n  ABBRUCH: Kontrakt nicht eindeutig aufgeloest.")
            return 1

        request = OrderRequest(
            order_key=f"manueller-test-{time.time_ns()}",
            symbol=symbol,
            side=OrderSide(args.side),
            quantity=args.quantity,
            signal_id=0,
            strategy="manueller_test",
        )

        print()
        print(f"  Sende {request.order_key} ...")
        gesendet = True
        order = adapter.place_market_order(request)
        print(f"  Gesendet          #{order.order_id}  {order.state.value}")
        print()
        print(f"  Warte auf Rueckmeldungen (max {args.timeout:.0f} s):")
        _ereignisse_zeigen(adapter, args.timeout)

        offene = adapter.get_open_orders()
        positionen = adapter.get_positions()
        print()
        print(f"  Offene Orders     {len(offene)}")
        for offen in offene:
            print(f"    #{offen.order_id:<8} {offen.symbol:<8} {offen.state.value}")
        print(f"  Positionen        {len(positionen)}")
        for position in positionen:
            print(f"    {position.symbol:<8} {position.quantity:>4} @ {position.avg_price:,.2f}")

        if not positionen and not offene:
            print()
            print("  Keine Fuellung und keine offene Order. Das ist KEIN Fehler, wenn der")
            print("  Markt geschlossen ist - MNQ pausiert taeglich 16:00-17:00 CT und")
            print("  ruht von Freitag 16:00 CT bis Sonntag 17:00 CT.")
    finally:
        # Aufraeumen laeuft auch nach einer Ausnahme oder Strg+C. Eine offene
        # Position auf einem unbeaufsichtigten Konto ist der einzige Ausgang,
        # den dieses Skript nicht hinterlassen darf.
        if gesendet and not args.keep_open:
            print()
            print("  Raeume auf: offene Orders stornieren, Position glattstellen ...")
            try:
                adapter.cancel_all_orders()
                glatt = adapter.close_position(symbol)
                if glatt is not None:
                    print(f"  Glattstellung     #{glatt.order_id} gesendet")
                    _ereignisse_zeigen(adapter, 20.0)
                rest = [p for p in adapter.get_positions() if p.quantity != 0]
                if rest:
                    print()
                    print("  ACHTUNG: es ist noch eine Position offen:")
                    for position in rest:
                        print(f"    {position.symbol:<8} {position.quantity:>4}")
                    print("  Im IB Gateway / in TWS von Hand schliessen.")
                else:
                    print("  Konto ist glatt.")
            except BrokerError as fehler:
                print(f"  AUFRAEUMEN FEHLGESCHLAGEN: {fehler}")
                print("  Positionen und Orders im Gateway von Hand pruefen.")
        adapter.disconnect()
        print()
        print("  Verbindung getrennt.")

    print(TRENNER)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
