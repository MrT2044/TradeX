"""Verbindung zu IB Gateway pruefen - ohne eine einzige Order.

    .\\.venv\\Scripts\\python.exe scripts\\test_ibkr_connection.py

Der erste Lauf beantwortet die Fragen, die man vor allem anderen beantwortet
haben will: Steht die Verbindung? Welches Konto ist das? Ist es nachweislich
ein Paper-Konto? Und laesst sich der Kontrakt eindeutig aufloesen?

Warum dieses Skript keine Order senden KANN
--------------------------------------------
Der Adapter wird mit `allow_orders=False` gebaut. Das ist kein Versprechen im
Text dieses Skripts, sondern ein Zweig, den es nicht gibt: `place_market_order`
wirft, bevor irgendetwas hinausgeht. Ein Skript, das nur verspricht, nichts zu
senden, waere genau so viel wert wie sein letzter Umbau.

Warum es nicht `test_` im Wurzelverzeichnis heisst
---------------------------------------------------
Es liegt in `scripts/`, weil ein blankes `pytest` Dateien namens `test_*.py`
im Wurzelverzeichnis einsammelt. Es wuerde dann bei jedem Testlauf versuchen,
IB Gateway zu verbinden.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tradex.broker.base import BrokerError
from tradex.broker.env import read_env
from tradex.broker.guard import check_configuration
from tradex.config import get_config, get_instruments
from tradex.logging_setup import setup_logging

TRENNER = "=" * 78


def main() -> int:
    config = get_config()
    setup_logging("WARNING", config.path(config.data.log_dir))

    print(TRENNER)
    print("  IBKR-Verbindungstest - es wird KEINE Order gesendet")
    print(TRENNER)

    # --- Vor dem Verbindungsaufbau: darf ueberhaupt gehandelt werden? -------
    env = read_env(config.execution, config.broker)
    gate = check_configuration(config.execution, config.broker, env)
    print(f"  Modus             {config.execution.mode.value}")
    print(f"  live_trading      {config.execution.live_trading_enabled}")
    print(f"  broker.enabled    {config.broker.enabled}")
    for reason in gate.reasons:
        marke = "ok  " if reason.ok else "SPERRT"
        print(f"    {marke}  {reason.code}  {reason.params}")

    if not gate.approved:
        # Kein Abbruch: die Verbindung laesst sich trotzdem pruefen, und genau
        # dafuer ist dieses Skript da. Nur handeln duerfte diese Konfiguration
        # nicht - und das steht hier ausdruecklich. Der Adapter laesst den
        # Socket zu, weil er ohne Orderrecht gebaut ist.
        print()
        print(f"  HINWEIS: In dieser Konfiguration entstuenden KEINE Orders ({gate.blocking_code}).")
        print("  Die Verbindung wird trotzdem geprueft - dafuer ist dieser Test da.")
        print("  Fuer den spaeteren Handelsbetrieb noetig: execution.mode: paper_auto")
        print("  und broker.enabled: true in config/default.yaml.")

    instruments = get_instruments()
    handelbar = {s: i for s, i in instruments.items() if i.ibkr is not None}
    if not handelbar:
        print("\n  Kein Instrument hat einen ibkr-Block in config/instruments.yaml.")
        return 1

    # --- Verbinden ----------------------------------------------------------
    print()
    print(f"  Verbinde mit {config.broker.ibkr.host}:{config.broker.ibkr.paper_port} "
          f"(Client-ID {config.broker.ibkr.client_id}) ...")

    try:
        from tradex.broker.ibkr.adapter import IbkrAdapter
    except ImportError as fehler:
        print(f"\n  {fehler}")
        return 1

    # allow_orders=False: dieser Adapter hat keinen Sendeweg.
    adapter = IbkrAdapter(config, handelbar, env=env, allow_orders=False)

    try:
        adapter.connect()
    except BrokerError as fehler:
        print(f"\n  FEHLGESCHLAGEN: {fehler}")
        print()
        # Die Ursache haengt davon ab, WO es gescheitert ist. Pauschal auf das
        # Gateway zu zeigen, obwohl die Konfiguration gesperrt hat, schickt
        # einen in die falsche Richtung - genau das ist beim Bauen passiert.
        if "gesperrt" in str(fehler):
            print("  Das war die Sicherheitskette, nicht die Verbindung.")
            print("  Siehe die Stufen oben - dort steht, welche blockiert hat.")
        else:
            print("  Haeufige Ursachen:")
            print("    - IB Gateway laeuft nicht oder ist nicht angemeldet")
            print("    - API Settings: 'Enable ActiveX and Socket Clients' ist aus")
            print(f"    - Socket Port ist nicht {config.broker.ibkr.paper_port}")
            print("    - 'Trusted IPs' enthaelt 127.0.0.1 nicht")
        return 1

    try:
        konto = adapter.get_account_info()
        print()
        print(f"  Konto             {konto.account}")
        print(f"  Paper bestaetigt  {konto.is_paper}  ({konto.paper_evidence})")
        print(f"  Kontoart          {konto.account_type or '(nicht gemeldet)'}")
        print(f"  Waehrung          {konto.currency}")
        print(f"  NetLiquidation    {konto.net_liquidation:,.2f}")
        print(f"  AvailableFunds    {konto.available_funds:,.2f}")

        print()
        print("  Kontrakte")
        for aufloesung in adapter.registry.all():
            print(f"    {aufloesung.describe()}")

        positionen = adapter.get_positions()
        print()
        print(f"  Positionen        {len(positionen)}")
        for position in positionen:
            print(f"    {position.symbol:<10} {position.quantity:>6}  @ {position.avg_price:,.2f}")

        orders = adapter.get_open_orders()
        print(f"  Offene Orders     {len(orders)}")
        for order in orders:
            print(
                f"    #{order.order_id:<8} {order.symbol:<10} {order.side.value:<5} "
                f"{order.quantity:>4}  {order.kind.value:<7} {order.state.value}"
                f"  ref={order.order_key or '(fremd)'}"
            )

        print()
        print(f"  Live Trading      {'AKTIV' if config.execution.live_trading_enabled else 'deaktiviert'}")
        print(f"  Orderrecht        {'ja' if adapter.allow_orders else 'nein (allow_orders=False)'}")

        if not konto.is_paper:
            print()
            print("  ACHTUNG: Dieses Konto ist NICHT als Paper-Konto belegt.")
            print("  Die Kontonummer oben in broker.ibkr.allowed_accounts eintragen,")
            print("  wenn es tatsaechlich das Paper-Konto ist.")
            return 1

        if not config.broker.ibkr.allowed_accounts:
            print()
            print("  EMPFEHLUNG: broker.ibkr.allowed_accounts ist leer - erkannt wurde")
            print("  das Konto ueber das Praefix. Trag es ein, das ist der einzige")
            print("  harte Nachweis:")
            print(f'      allowed_accounts: ["{konto.account}"]')
    finally:
        adapter.disconnect()
        print()
        print("  Verbindung getrennt.")

    print(TRENNER)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
