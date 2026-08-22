"""Papertrading: das Programm handelt selbst (Phase 5).

    python scripts/run_paper.py --symbol MNQ_PROXY --speed 3600
    python scripts/run_paper.py --symbol MNQ_PROXY,MES_PROXY --from 2026-01-01
    python scripts/run_paper.py --symbol MNQ_PROXY --feed nt8

Was hier passiert - und was nicht
---------------------------------
Es entstehen echte Entscheidungen und vollstaendig durchsimulierte Trades, mit
Schlupf, Gebuehren und der pessimistischen Annahme "Stop zuerst". Es fliesst
KEIN Geld: es gibt keine Order-Anbindung an einen Broker, und
`execution.live_trading_enabled` bleibt aus (Spec Paragraph 24).

Die Trades entstehen in derselben Klasse wie im Backtest (`SymbolBook`). Das
ist der eigentliche Zweck dieses Betriebs: Er beantwortet die Frage, ob die
Regel unter laufenden Bedingungen dasselbe tut wie in der Auswertung - nicht
weil es versprochen wurde, sondern weil es derselbe Code ist.

Die Wiedergabe ist kein Ersatz fuer echte Daten
-----------------------------------------------
`--feed replay` spielt den lokalen Bestand ab. Das prueft die Mechanik des
Betriebs - Ausfallerkennung, Not-Aus, Persistenz - deterministisch und
kostenlos, und genau das sieht `bridge_nt8/README.md` als ersten Schritt vor.
Was es NICHT prueft, ist das Verhalten am lebenden Markt. Jede gespeicherte
Sitzung traegt deshalb ihren Feed-Namen.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tradex.backtest import metrics as M
from tradex.backtest.execution import SimulatedTrade
from tradex.config import get_config, get_instrument, resolved_config_path
from tradex.data.store import BarStore
from tradex.domain.bars import from_ns, to_ns
from tradex.live.nt8_feed import DEFAULT_HOST, DEFAULT_PORT, NinjaTraderFeed
from tradex.live.replay_feed import ReplayFeed
from tradex.live.runner import SessionRunner
from tradex.live.session import SessionConfig, TradingSession
from tradex.live.store import SessionStore
from tradex.logging_setup import setup_logging
from tradex.persistence.db import init_database
from tradex.persistence.decision_log import DecisionLog
from tradex.service import STRATEGY_VERSION


def _parse_date(raw: str | None) -> int | None:
    if not raw:
        return None
    return to_ns(datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC))


def _print_trade(trade: SimulatedTrade) -> None:
    zeichen = "+" if trade.pnl >= 0 else "-"
    print(
        f"  {zeichen} {from_ns(trade.exit_ts):%Y-%m-%d %H:%M}  {trade.symbol:<10}"
        f"{trade.strategy:<15}{trade.side:<6}"
        f"{trade.r_multiple:>7.2f} R  {trade.pnl:>9.2f} USD   {trade.exit_reason.value}"
    )


class _Heartbeat:
    """Eine Statuszeile, die sich selbst ueberschreibt.

    Ohne sichtbares Lebenszeichen ist eine laufende Sitzung von einer
    haengenden nicht zu unterscheiden - und genau dieser Unterschied ist der
    Grund, warum es die Ausfallerkennung ueberhaupt gibt.
    """

    def __init__(self, total_bars: int) -> None:
        self.total = total_bars
        self.last = -1.0

    def __call__(self, session: TradingSession) -> None:
        status = session.status()
        if self.total:
            anteil = 100.0 * status.bars_seen / self.total
            if anteil - self.last < 0.1 and status.bars_seen:
                return
            self.last = anteil
            fortschritt = f"{status.bars_seen:>8,}/{self.total:,} Bars ({anteil:5.1f} %)"
        else:
            # Echtzeit: es gibt kein "wie weit" - nur "seit wann still".
            still = (session.clock() - status.last_message_ts) / 1e9
            fortschritt = f"{status.bars_seen:>8,} Bars, still seit {still:4.0f} s"

        zustand = status.halted_reason or ("laeuft" if status.connected else "wartet")
        sys.stderr.write(
            f"\r  {fortschritt}  {zustand:<18} Trades {status.trades_closed:>3}  "
            f"offen {status.open_positions}  Konto {status.equity:>11,.2f} USD "
        )
        sys.stderr.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="MNQ_PROXY", help="eines oder mehrere, Komma-getrennt")
    parser.add_argument("--feed", default="replay", help="replay | nt8")
    parser.add_argument(
        "--speed",
        type=float,
        default=3600.0,
        help="Beschleunigung der Wiedergabe (1 = Echtzeit, 0 = so schnell wie moeglich)",
    )
    parser.add_argument("--from", dest="start", help="Startdatum YYYY-MM-DD (nur Wiedergabe)")
    parser.add_argument("--to", dest="end", help="Enddatum YYYY-MM-DD (nur Wiedergabe)")
    parser.add_argument("--host", default=DEFAULT_HOST, help="NinjaTrader-Bridge (nur nt8)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port der Bridge (nur nt8)")
    parser.add_argument("--max-bars", type=int, default=0, help="nach so vielen Bars aufhoeren")
    parser.add_argument("--notes", default="", help="Notiz zur gespeicherten Sitzung")
    parser.add_argument("--no-save", action="store_true", help="nicht in der Datenbank festhalten")
    args = parser.parse_args()

    # Zeilenweise ausgeben statt gepuffert. Wird die Ausgabe in eine Datei
    # umgeleitet - der Normalfall fuer einen Lauf ueber Stunden -, sammelt
    # Python sonst kiloweise Text und schreibt ihn erst am Ende. Man saehe
    # einer laufenden Sitzung stundenlang beim Nichtstun zu.
    sys.stdout.reconfigure(line_buffering=True)

    config = get_config()
    setup_logging("WARNING", config.path(config.data.log_dir))

    if config.execution.live_trading_enabled:
        # Sollte nicht vorkommen - aber wenn doch, ist das Schweigen darueber
        # der Fehler, nicht die Einstellung.
        print("HINWEIS: execution.live_trading_enabled ist AN.")
        print("Dieses Skript handelt trotzdem nur simuliert - es kennt keinen Broker.")

    symbols = [s.strip().upper() for s in args.symbol.split(",") if s.strip()]
    instruments = {name: get_instrument(name) for name in symbols}

    if args.feed == "nt8":
        feed: ReplayFeed | NinjaTraderFeed = NinjaTraderFeed(
            tuple(symbols), config.data.base_timeframe, host=args.host, port=args.port
        )
        total_bars = 0
    elif args.feed == "replay":
        store = BarStore(config.path(config.data.parquet_dir))
        series_by_symbol = {}
        for name in symbols:
            series = store.read(
                name, config.data.base_timeframe, _parse_date(args.start), _parse_date(args.end)
            )
            if len(series) == 0:
                print(f"Keine {config.data.base_timeframe.value}-Daten fuer {name}.")
                return 1
            series_by_symbol[name] = series
        feed = ReplayFeed(
            series_by_symbol, speed=args.speed, base_seconds=config.data.base_timeframe.seconds
        )
        total_bars = feed.total_bars
    else:
        print(f"Unbekannter Feed '{args.feed}'. Vorhanden: replay, nt8.")
        return 1

    sessions: SessionStore | None = None
    if not args.no_save:
        database = config.path(config.data.database)
        init_database(database)
        with DecisionLog(database) as log:
            config_hash = log.register_config(resolved_config_path())
        sessions = SessionStore(database)
        offen = sessions.unfinished()
        if offen:
            # Eine Sitzung ohne Ende ist entweder noch aktiv oder abgestuerzt.
            # Beides sollte man wissen, bevor man eine neue startet.
            print(f"HINWEIS: {len(offen)} fruehere Sitzung(en) ohne sauberes Ende in der Datenbank.")

    session = TradingSession(
        instruments,
        config,
        feed_name=feed.name,
        session_config=SessionConfig(
            # Bei beschleunigter Wiedergabe kaeme die Stille-Ueberwachung sonst
            # nie zum Zug; bei Echtzeit ist es der Wert aus der Bridge-Spec.
            max_silence_seconds=15.0,
            trade_sink=sessions.record_trade if sessions else None,
        ),
    )

    if sessions is not None:
        sessions.start(
            mode=config.execution.mode.value,
            feed=feed.name,
            symbols=tuple(symbols),
            config_hash=config_hash,
            strategy_version=STRATEGY_VERSION,
            backtest_version=session.backtest_version,
            start_equity=config.risk.account_size,
            notes=args.notes,
        )

    print("=" * 78)
    print(f"  PAPERTRADING  {'+'.join(symbols)}  -  Feed: {feed.name}")
    print("=" * 78)
    if total_bars:
        print(f"  {total_bars:,} Bars, Beschleunigung {args.speed:g}x")
    else:
        print(f"  Echtzeit ueber {args.host}:{args.port} - laeuft, bis Strg-C kommt")
    print(f"  Konto {config.risk.account_size:,.0f} USD, Risiko {config.risk.risk_per_trade_pct} % je Trade")
    print(f"  Strategien: {', '.join(s.name for s in next(iter(session.books.values())).book.strategy.strategies)}")
    if sessions is not None:
        print(f"  Sitzung id={sessions.session_id} wird laufend gespeichert")
    print("  Strg-C beendet geordnet.")
    print()

    runner = SessionRunner(session, feed, on_trade=_print_trade, on_tick=_Heartbeat(total_bars))
    runner.install_signal_handler()
    result = runner.run(max_bars=args.max_bars)
    sys.stderr.write("\r" + " " * 110 + "\r")

    if sessions is not None:
        sessions.finish()
        sessions.close()

    _summary(session, result, config)
    # Wie beim Backtest: keine Trades ist kein Erfolg, sondern ein Befund.
    return 0 if result.trades else 2


def _summary(session: TradingSession, result, config) -> None:
    status = session.status()
    print()
    print("  Ergebnis der Sitzung")
    print("  " + "-" * 74)
    print(f"  verarbeitete Bars      {status.bars_seen:>12,}")
    print(f"  Signale                {status.signals:>12,}")
    print(f"  geschlossene Trades    {status.trades_closed:>12,}")
    print(f"  noch offen             {status.open_positions:>12,}")
    print(f"  beendet durch          {result.stopped_by:>12}")
    print()

    if result.trades:
        m = M.summarize(list(result.trades), config.risk.account_size)
        print(f"  Erwartungswert         {m.expectancy_r:>10.3f} R")
        print(f"  Trefferquote           {m.win_rate:>11.1f} %")
        print(f"  Nettoergebnis          {m.net_pnl:>+11.2f} USD")
        print(f"  Konto                  {m.start_equity:,.0f} -> {m.final_equity:,.2f} USD")
    else:
        print("  Kein Trade zustande gekommen. Das ist ein Befund, kein Fehler -")
        print("  bei diesen Regeln und diesem Zeitraum kann es richtig sein.")

    if session.events:
        print()
        print("  Betriebsereignisse")
        print("  " + "-" * 74)
        for ts, art, text in session.events[-10:]:
            print(f"  {from_ns(ts):%H:%M:%S}  {art:<8} {text}")


if __name__ == "__main__":
    raise SystemExit(main())
