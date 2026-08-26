"""Startpruefung: laeuft alles, was TradeX braucht?

Aufgerufen von `start_tradex.bat` - vor dem Start als Vorpruefung, nach dem
Start als Gesundheitspruefung des laufenden Systems.

Warum das ein Python-Skript ist und keine Batch-Logik
------------------------------------------------------
Eine `.bat` kann keinen Socket testen, kein JSON lesen und keine Konfiguration
aufloesen. Alles, was hier geprueft wird, haengt an genau diesen drei Dingen -
in Batch nachgebaut waere es eine zweite, schlechtere Wahrheit ueber den
Zustand des Systems.

Wichtig: dieses Skript **entscheidet nichts und startet nichts**. Es stellt
fest und meldet. Was aus einem roten Befund folgt, entscheidet die `.bat` -
und bei allem, was den Handel betrifft, entscheidet es ohnehin die
Sicherheitskette in `tradex/broker/guard.py`.
"""

from __future__ import annotations

import argparse
import socket
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tradex.config import get_config, get_instruments

OK = "  [ok]   "
WARN = "  [warn] "
FAIL = "  [FEHL] "


@dataclass(slots=True)
class Befund:
    """Ein Pruefergebnis. `kritisch` entscheidet ueber den Rueckgabewert."""

    name: str
    ok: bool
    detail: str
    kritisch: bool = True

    def zeile(self) -> str:
        marke = OK if self.ok else (FAIL if self.kritisch else WARN)
        return f"{marke}{self.name:<26} {self.detail}"


def _port_offen(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(timeout)
            return probe.connect_ex((host, port)) == 0
    except OSError:
        return False


def pruefe_umgebung() -> list[Befund]:
    befunde = [
        Befund(
            "Python",
            sys.version_info >= (3, 12),
            f"{sys.version_info.major}.{sys.version_info.minor} (noetig: 3.12+)",
        )
    ]
    try:
        import tradex  # noqa: F401

        befunde.append(Befund("Paket tradex", True, "importierbar"))
    except ImportError as fehler:  # pragma: no cover - nur bei kaputtem Setup
        befunde.append(Befund("Paket tradex", False, str(fehler)))
    return befunde


def pruefe_daten() -> list[Befund]:
    config = get_config()
    parquet = config.path(config.data.parquet_dir)
    symbole = sorted(p.name.removeprefix("symbol=") for p in parquet.glob("symbol=*"))
    return [
        Befund(
            "Datenbestand",
            bool(symbole),
            ", ".join(symbole) if symbole else f"leer ({parquet})",
            kritisch=False,
        ),
        Befund(
            "Oberflaeche gebaut",
            (config.path(Path("ui/dist")) / "index.html").exists(),
            "ui/dist/index.html",
        ),
    ]


def pruefe_feed() -> list[Befund]:
    """NinjaTrader-Bridge. Nicht kritisch: die Wiedergabe braucht sie nicht."""
    from tradex.live.nt8_feed import DEFAULT_HOST, DEFAULT_PORT

    erreichbar = _port_offen(DEFAULT_HOST, DEFAULT_PORT)
    detail = (
        f"Bridge auf {DEFAULT_HOST}:{DEFAULT_PORT}"
        if erreichbar
        else f"keine Bridge auf Port {DEFAULT_PORT} - NinjaTrader starten, "
        "sonst nur Wiedergabe moeglich"
    )
    return [Befund("Marktdaten (NT8)", erreichbar, detail, kritisch=False)]


def pruefe_broker() -> list[Befund]:
    """Orderanbindung. Aus ist ein gueltiger Zustand, kein Fehler."""
    config = get_config()
    if not config.broker.enabled:
        return [
            Befund(
                "Broker",
                True,
                "aus (broker.enabled: false) - Signale werden simuliert",
                kritisch=False,
            )
        ]

    befunde: list[Befund] = []
    from tradex.broker.env import read_env
    from tradex.broker.guard import check_configuration

    env = read_env(config.execution, config.broker)
    gate = check_configuration(config.execution, config.broker, env)
    befunde.append(
        Befund(
            "Sicherheitskette",
            gate.approved,
            "alle Stufen frei" if gate.approved else f"gesperrt: {gate.blocking_code}",
        )
    )

    # Ab hier haengt es davon ab, WELCHE Anbindung Orders sendet. Die Stufen
    # gegen die jeweils andere zu pruefen waere schlimmer als sie
    # wegzulassen: ein rotes "kein Gateway auf 127.0.0.1:4002" bei einer
    # NinjaTrader-Anbindung bringt einem bei, rote Zeilen zu ignorieren.
    if config.broker.provider == "nt8":
        host, port = config.broker.nt8.host, config.broker.nt8.port
        erreichbar = _port_offen(host, port)
        befunde.append(
            Befund(
                "NinjaTrader-Bridge",
                erreichbar,
                # Dieselbe Leitung wie die Marktdatenstufe weiter oben - dort
                # ist ihr Ausfall ein Hinweis (Wiedergabe geht weiter), hier
                # ein kritischer Befund: `broker.enabled` ist an, und ohne
                # Bridge geht keine Order hinaus. Der Zusatz steht dabei,
                # damit zwei Zeilen zur selben Adresse nicht wie ein Fehler
                # der Anzeige aussehen.
                f"{host}:{port} (dieselbe Bridge wie die Marktdaten)"
                if erreichbar
                else f"keine Bridge auf {host}:{port} - ohne sie geht keine Order hinaus",
            )
        )
        # Der Kontoname steht in der Config, das Urteil faellt das AddOn
        # (`Account.Provider == Provider.Simulator`). Hier laesst sich nur
        # pruefen, dass ueberhaupt eines benannt ist - "das erste passende"
        # waere sonst das Backtest-Konto mit net_liquidation 0.
        konto = config.broker.nt8.account.strip()
        befunde.append(
            Befund(
                "Simulationskonto",
                bool(konto),
                konto if konto else "broker.nt8.account ist leer - Konto wird geraten",
                kritisch=False,
            )
        )
        fehlend = [
            symbol
            for symbol, instrument in get_instruments().items()
            if not instrument.nt8_symbol
        ]
        befunde.append(
            Befund(
                "NT8-Kontrakte",
                len(fehlend) < len(get_instruments()),
                f"ohne nt8_symbol: {', '.join(fehlend)}" if fehlend else "alle hinterlegt",
                kritisch=False,
            )
        )
        return befunde

    host, port = config.broker.ibkr.host, config.broker.ibkr.paper_port
    erreichbar = _port_offen(host, port)
    befunde.append(
        Befund(
            "IB Gateway",
            erreichbar,
            f"{host}:{port}" if erreichbar else f"kein Gateway auf {host}:{port}",
        )
    )

    fehlend = [
        symbol
        for symbol, instrument in get_instruments().items()
        if instrument.ibkr is None or not instrument.ibkr.is_complete
    ]
    befunde.append(
        Befund(
            "IBKR-Kontrakte",
            len(fehlend) < len(get_instruments()),
            f"ohne ibkr-Block: {', '.join(fehlend)}" if fehlend else "alle hinterlegt",
            kritisch=False,
        )
    )
    return befunde


def pruefe_dienst(host: str, port: int) -> list[Befund]:
    """Laeuft die Engine und antwortet sie?"""
    if not _port_offen(host, port):
        return [Befund("Engine", False, f"nichts auf {host}:{port}")]
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=5) as antwort:
            gesund = antwort.status == 200
        return [Befund("Engine", gesund, f"http://{host}:{port} antwortet")]
    except (urllib.error.URLError, TimeoutError) as fehler:
        return [Befund("Engine", False, f"Port offen, aber /api/health schweigt: {fehler}")]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage",
        choices=("pre", "post"),
        default="pre",
        help="pre = vor dem Start, post = laufendes System pruefen",
    )
    args = parser.parse_args()

    config = get_config()
    befunde: list[Befund] = []

    if args.stage == "pre":
        befunde += pruefe_umgebung()
        befunde += pruefe_daten()
        befunde += pruefe_feed()
        befunde += pruefe_broker()
    else:
        befunde += pruefe_dienst(config.app.host, config.app.port)
        befunde += pruefe_feed()
        befunde += pruefe_broker()

    print()
    for befund in befunde:
        print(befund.zeile())
    print()

    kritisch = [b for b in befunde if not b.ok and b.kritisch]
    hinweise = [b for b in befunde if not b.ok and not b.kritisch]
    if hinweise:
        print(f"  {len(hinweise)} Hinweis(e) - der Start laeuft trotzdem weiter.")
    if kritisch:
        print(f"  {len(kritisch)} kritischer Befund - Start abgebrochen.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
