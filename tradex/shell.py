"""Windows-Desktop-Huelle.

Startet die Engine (uvicorn) in einem Hintergrund-Thread und oeffnet ein
natives Fenster darauf. Genutzt wird die in Windows 10/11 bereits vorhandene
Edge-WebView2-Komponente - es muss also weder ein Browser noch Node zur Laufzeit
installiert sein.

Warum ein Fenster auf einem lokalen Webserver statt einer klassischen
Desktop-Oberflaeche: die Trennung von Engine und Anzeige (Spec §27) wird damit
technisch erzwungen statt nur vereinbart. Dieselbe Engine laesst sich spaeter
unveraendert headless im Backtest oder als Dienst betreiben.

Aufruf:
    python -m tradex.shell           Fenster oeffnen
    python -m tradex.shell --server  nur die Engine, ohne Fenster
"""

from __future__ import annotations

import argparse
import socket
import threading
import time
from pathlib import Path

import uvicorn

from tradex.config import get_config
from tradex.logging_setup import get_logger, setup_logging

log = get_logger(__name__)

UI_DIST = Path(__file__).resolve().parent.parent / "ui" / "dist"
STARTUP_TIMEOUT_SECONDS = 20.0


class _Server(uvicorn.Server):
    """uvicorn-Server ohne eigene Signalbehandlung.

    Im Thread-Betrieb darf uvicorn keine Signalhandler installieren - das geht
    ausserhalb des Hauptthreads nicht und wuerde den Start abbrechen.
    """

    def install_signal_handlers(self) -> None:
        return None


def _wait_until_ready(host: str, port: int, timeout: float = STARTUP_TIMEOUT_SECONDS) -> bool:
    """Warten, bis der Port annimmt. Verhindert ein Fenster mit Verbindungsfehler."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.4)
            if probe.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.15)
    return False


def _check_ui_built() -> bool:
    if (UI_DIST / "index.html").is_file():
        return True
    print()
    print("  Die Oberflaeche ist noch nicht gebaut.")
    print("  Im Ordner ui/ ausfuehren:")
    print()
    print("      npm install")
    print("      npm run build")
    print()
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", action="store_true", help="nur die Engine, ohne Fenster")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--reload", action="store_true", help="Autoreload (nur mit --server)")
    args = parser.parse_args()

    config = get_config()
    host = args.host or config.app.host
    port = args.port or config.app.port
    setup_logging(config.app.log_level, config.path(config.data.log_dir))

    if args.server:
        uvicorn.run(
            "tradex.api.server:app",
            host=host,
            port=port,
            reload=args.reload,
            log_config=None,
        )
        return 0

    if not _check_ui_built():
        return 1

    try:
        import webview
    except ImportError:
        print("Paket 'pywebview' fehlt. Installation:  pip install pywebview")
        return 1

    from tradex.api.server import create_app

    server = _Server(
        uvicorn.Config(create_app(config), host=host, port=port, log_config=None)
    )
    thread = threading.Thread(target=server.run, name="tradex-engine", daemon=True)
    thread.start()

    if not _wait_until_ready(host, port):
        print(f"Engine ist nicht innerhalb von {STARTUP_TIMEOUT_SECONDS:.0f}s gestartet.")
        return 1

    log.info("shell_opening", url=f"http://{host}:{port}")
    webview.create_window(
        "TradeX - Nasdaq-100 Analyse",
        f"http://{host}:{port}",
        width=1680,
        height=1000,
        min_size=(1100, 700),
        background_color="#0d1117",
    )
    # Blockiert, bis der Nutzer das Fenster schliesst.
    webview.start()

    server.should_exit = True
    thread.join(timeout=5)
    log.info("shell_closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
