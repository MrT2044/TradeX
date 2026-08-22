"""Backtest-Bericht: Kennzahlen buendeln, einordnen und ausgeben (Spec §19).

Der Bericht ist bewusst mehr als eine Zahlentabelle. Er beantwortet drei Fragen
in dieser Reihenfolge:

    1. Darf man diese Zahlen ueberhaupt lesen?  -> `warnings`
    2. Was ist herausgekommen?                  -> `overall`
    3. Woher kam es?                            -> `breakdowns`, `rejections`

Punkt 1 steht zuerst, weil er am haeufigsten uebersehen wird. Ein
Erwartungswert aus zwoelf Trades auf synthetischen Daten sieht genauso aus wie
einer aus zwoelfhundert - deshalb sagt der Bericht ausdruecklich dazu, was er
nicht belegen kann.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

from tradex.backtest import metrics as M
from tradex.backtest.execution import SimulatedTrade
from tradex.backtest.runner import BACKTEST_VERSION, BacktestResult
from tradex.config import Config
from tradex.domain.bars import from_ns

#: Ab dieser Abweichung im Erwartungswert (in R) zwischen erster und zweiter
#: Haelfte des Zeitraums weist der Bericht ausdruecklich darauf hin.
SPLIT_DIVERGENCE_R = 0.3


@dataclass(frozen=True, slots=True)
class BacktestReport:
    """Vollstaendiges Ergebnis eines Laufs, fertig zum Anzeigen oder Speichern."""

    symbol: str
    instrument_name: str
    base_timeframe: str
    bars: int
    first_ts: int
    last_ts: int
    backtest_version: str

    overall: M.Metrics
    in_sample: M.Metrics
    out_of_sample: M.Metrics

    equity: tuple[M.EquityPoint, ...]
    trades: tuple[SimulatedTrade, ...]
    trades_total: int

    by_strategy: dict[str, M.Metrics]
    """Die wichtigste Aufschluesselung, sobald mehrere Strategien laufen:
    welche hat das Ergebnis getragen, welche nur Gebuehren produziert?"""
    by_symbol: dict[str, M.Metrics]
    """Bei mehreren Instrumenten: traegt die Regel ueberall oder nur auf einem?"""
    by_session: dict[str, M.Metrics]
    by_direction: dict[str, M.Metrics]
    by_exit: dict[str, M.Metrics]
    by_stop_anchor: dict[str, M.Metrics]
    by_target_source: dict[str, M.Metrics]

    exit_counts: dict[str, int]
    rejections: dict[str, int]
    signals: int
    unfilled: int
    stale: int

    assumptions: dict[str, float | str | int]
    warnings: tuple[str, ...]
    min_trades: int
    """Ab wie vielen Trades die Kennzahlen als belastbar gelten."""

    @property
    def is_significant(self) -> bool:
        """Genug Trades, dass die Kennzahlen ueberhaupt etwas behaupten koennen."""
        return self.overall.trades >= self.min_trades


def build(result: BacktestResult, config: Config) -> BacktestReport:
    """Aus dem Rohergebnis eines Laufs den fertigen Bericht bauen."""
    params = config.backtest
    trades = result.trades
    start_equity = result.start_equity

    overall = M.summarize(trades, start_equity)
    first_part, second_part = M.split_by_time(trades, params.out_of_sample_fraction)
    in_sample = M.summarize(first_part, start_equity)
    out_of_sample = M.summarize(second_part, start_equity)

    limit = params.max_report_trades
    return BacktestReport(
        symbol=result.symbol,
        instrument_name=result.instrument.name,
        base_timeframe=result.base_timeframe,
        bars=result.bars,
        first_ts=result.first_ts,
        last_ts=result.last_ts,
        backtest_version=BACKTEST_VERSION,
        overall=overall,
        in_sample=in_sample,
        out_of_sample=out_of_sample,
        equity=_thin(M.equity_curve(trades, start_equity), limit),
        trades=tuple(trades[-limit:]),
        trades_total=len(trades),
        by_strategy=M.breakdown(trades, lambda t: t.strategy, start_equity),
        by_symbol=M.breakdown(trades, lambda t: t.symbol, start_equity),
        by_session=M.breakdown(trades, lambda t: t.session, start_equity),
        by_direction=M.breakdown(trades, lambda t: t.side, start_equity),
        by_exit=M.breakdown(trades, lambda t: t.exit_reason.value, start_equity),
        by_stop_anchor=M.breakdown(trades, lambda t: t.stop_anchor, start_equity),
        by_target_source=M.breakdown(trades, lambda t: t.target_source, start_equity),
        exit_counts=M.exit_counts(trades),
        rejections=dict(result.rejections),
        signals=result.signals,
        unfilled=result.unfilled,
        stale=result.stale,
        assumptions={
            "entry_fill": params.entry_fill,
            "same_bar_resolution": params.same_bar_resolution,
            "entry_slippage_ticks": params.entry_slippage_ticks,
            "stop_slippage_ticks": params.stop_slippage_ticks,
            "commission_per_contract": params.commission_per_contract,
            "max_holding_bars": params.max_holding_bars,
            "max_signal_age_bars": params.max_signal_age_bars,
            "account_size": config.risk.account_size,
            "risk_per_trade_pct": config.risk.risk_per_trade_pct,
            "min_rr": config.risk.min_rr,
            "stop_anchor": config.stops.anchor,
        },
        warnings=_warnings(result, overall, in_sample, out_of_sample, config),
        min_trades=params.min_trades_for_significance,
    )


def _thin(points: tuple[M.EquityPoint, ...], limit: int) -> tuple[M.EquityPoint, ...]:
    """Equity-Kurve auf `limit` Punkte ausduennen, Anfang und Ende behalten."""
    if len(points) <= limit:
        return points
    step = len(points) / limit
    picked = [points[int(i * step)] for i in range(limit - 1)]
    picked.append(points[-1])
    return tuple(picked)


def _warnings(
    result: BacktestResult,
    overall: M.Metrics,
    first_half: M.Metrics,
    second_half: M.Metrics,
    config: Config,
) -> tuple[str, ...]:
    """Was der Leser wissen muss, BEVOR er die Zahlen deutet."""
    messages: list[str] = []
    minimum = config.backtest.min_trades_for_significance

    if overall.trades == 0:
        messages.append(
            "Kein einziger Trade zustande gekommen. Die Kennzahlen sagen nichts "
            "ueber die Regel aus - erst muss geklaert werden, woran die Kette scheitert "
            "(siehe Ablehnungsgruende)."
        )
    elif overall.trades < minimum:
        messages.append(
            f"Zu wenige Trades fuer eine belastbare Aussage: {overall.trades} von "
            f"mindestens {minimum}. Jede Kennzahl unten ist Zufall in Reichweite."
        )

    if result.symbol.endswith("_DEMO"):
        messages.append(
            "SYNTHETISCHE DEMODATEN - kein Marktverhalten. Das Ergebnis prueft die "
            "Mechanik des Backtests, nicht die Strategie."
        )
    if result.symbol.endswith("_PROXY"):
        messages.append(
            "Index-CFD statt MNQ-Future: keine Rolls, Volumen ist eine "
            "Aktivitaetskennzahl, Basis fehlt. Fuer die Frage nach dem Edge brauchbar, "
            "fuer die Freigabe von Echtgeld nicht."
        )

    if overall.unresolved:
        messages.append(
            f"{overall.unresolved} Trade(s) liefen beim Datenende noch - ihr Ergebnis "
            "ist ein Artefakt des Zeitfensters, nicht der Regel."
        )
    if result.unfilled:
        messages.append(
            f"{result.unfilled} Signal(e) wurden nie gefuellt, weil die Daten davor endeten."
        )
    if result.stale:
        messages.append(
            f"{result.stale} Signal(e) lagen ueber einer Datenluecke (Feiertag, "
            "Handelsunterbrechung) und wurden verworfen statt zu einem Kurs Stunden "
            "spaeter gefuellt."
        )

    if first_half.trades and second_half.trades:
        gap = abs(first_half.expectancy_r - second_half.expectancy_r)
        if gap > SPLIT_DIVERGENCE_R:
            messages.append(
                f"Erste und zweite Haelfte des Zeitraums weichen deutlich ab "
                f"({first_half.expectancy_r:+.2f} R gegen {second_half.expectancy_r:+.2f} R). "
                "Das Ergebnis haengt eher am Zeitraum als an der Regel."
            )

    if config.news.enabled and result.news_missing:
        share = 100.0 * result.news_missing / max(len(result.decisions), 1)
        messages.append(
            f"Nachrichtenfilter ist eingeschaltet, aber fuer {share:.0f} % der "
            f"Entscheidungen ({result.news_missing:,}) lagen keine Termine vor. Dieser Lauf "
            "wurde weitgehend OHNE Filter gerechnet - er ist mit einem Live-Betrieb MIT "
            "Filter nicht vergleichbar. Erst Termine nachladen (scripts/fetch_news.py)."
        )

    if config.backtest.entry_fill == "signal_close":
        messages.append(
            "entry_fill=signal_close: gefuellt wird zum Schlusskurs der Signalbar. "
            "Das ist real nicht moeglich und beschoenigt jedes Ergebnis."
        )
    return tuple(messages)


# --------------------------------------------------------------------- Ausgabe
def to_dict(report: BacktestReport) -> dict:
    """JSON-taugliche Fassung - Grundlage fuer Regressionsvergleiche."""
    return {
        "symbol": report.symbol,
        "instrument": report.instrument_name,
        "base_timeframe": report.base_timeframe,
        "bars": report.bars,
        "first_ts": report.first_ts,
        "last_ts": report.last_ts,
        "backtest_version": report.backtest_version,
        "assumptions": report.assumptions,
        "warnings": list(report.warnings),
        "signals": report.signals,
        "unfilled": report.unfilled,
        "stale": report.stale,
        "trades_total": report.trades_total,
        "overall": asdict(report.overall),
        "in_sample": asdict(report.in_sample),
        "out_of_sample": asdict(report.out_of_sample),
        "by_strategy": {k: asdict(v) for k, v in report.by_strategy.items()},
        "by_symbol": {k: asdict(v) for k, v in report.by_symbol.items()},
        "by_session": {k: asdict(v) for k, v in report.by_session.items()},
        "by_direction": {k: asdict(v) for k, v in report.by_direction.items()},
        "by_exit": {k: asdict(v) for k, v in report.by_exit.items()},
        "by_stop_anchor": {k: asdict(v) for k, v in report.by_stop_anchor.items()},
        "by_target_source": {k: asdict(v) for k, v in report.by_target_source.items()},
        "exit_counts": report.exit_counts,
        "rejections": report.rejections,
        "equity": [asdict(p) for p in report.equity],
        "trades": [asdict(t) | {"exit_reason": t.exit_reason.value, "direction": t.direction.value} for t in report.trades],
    }


def _ts(ts: int) -> str:
    return f"{from_ns(ts):%Y-%m-%d %H:%M}" if ts else "-"


def _pf(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "-"


def _line(label: str, value: str) -> str:
    return f"  {label:<28} {value:>18}"


def render_text(report: BacktestReport) -> str:
    """Vollstaendiger Bericht fuer die Konsole."""
    m = report.overall
    out: list[str] = []
    out.append("=" * 74)
    out.append(f"  BACKTEST  {report.symbol}  -  {report.instrument_name}")
    out.append("=" * 74)
    out.append(
        f"  {_ts(report.first_ts)} bis {_ts(report.last_ts)}   "
        f"{report.bars:,} Bars ({report.base_timeframe})"
    )
    out.append("")

    if report.warnings:
        out.append("  !! ZUERST LESEN")
        for message in report.warnings:
            out.append(f"   - {message}")
        out.append("")

    out.append("  Ergebnis")
    out.append("  " + "-" * 70)
    out.append(_line("Trades", f"{m.trades:,}"))
    out.append(_line("davon Gewinner", f"{m.wins:,} ({m.win_rate:.1f} %)"))
    out.append(_line("davon Verlierer", f"{m.losses:,}"))
    out.append(_line("Erwartungswert", f"{m.expectancy_r:+.3f} R"))
    out.append(_line("Erwartungswert je Trade", f"{m.expectancy_usd:+,.2f} USD"))
    out.append(_line("Profitfaktor", _pf(m.profit_factor)))
    out.append(_line("Payoff (Gewinn/Verlust)", _pf(m.payoff_ratio)))
    out.append(_line("SQN", _pf(m.sqn)))
    out.append("")
    out.append(_line("Nettoergebnis", f"{m.net_pnl:+,.2f} USD"))
    out.append(_line("davon Gebuehren", f"-{m.commission:,.2f} USD"))
    out.append(_line("Konto", f"{m.start_equity:,.0f} -> {m.final_equity:,.2f} USD"))
    out.append(_line("Rendite", f"{m.return_pct:+.2f} %"))
    out.append(_line("Max. Rueckgang", f"{m.max_drawdown_usd:,.2f} USD ({m.max_drawdown_pct:.2f} %)"))
    out.append(_line("Max. Rueckgang in R", f"{m.max_drawdown_r:.2f} R"))
    out.append(_line("Laengste Verlustserie", f"{m.max_consecutive_losses}"))
    out.append("")
    out.append(_line("Bestes / schlechtestes R", f"{m.best_r:+.2f} / {m.worst_r:+.2f}"))
    out.append(_line("Streuung der R-Werte", f"{m.stdev_r:.2f}"))
    out.append(_line("Haltedauer", f"{m.avg_bars_held:.0f} Bars"))
    out.append(_line("MAE / MFE", f"{m.avg_mae_r:.2f} R / {m.avg_mfe_r:.2f} R"))
    out.append(_line("geplantes CRV", f"{m.avg_planned_rr:.2f}"))
    out.append("")

    out.append("  Zeitraumhaelften (haelt das Ergebnis ueber die Zeit?)")
    out.append("  " + "-" * 70)
    out.append(f"  {'Abschnitt':<14}{'Trades':>8}{'Trefferq.':>12}{'Erwartung':>14}{'Profitfaktor':>16}")
    for label, part in (("erste", report.in_sample), ("zweite", report.out_of_sample)):
        out.append(
            f"  {label:<14}{part.trades:>8,}{part.win_rate:>11.1f} %"
            f"{part.expectancy_r:>13.3f} R{_pf(part.profit_factor):>16}"
        )
    out.append("")

    for title, table in (
        ("Nach Strategie", report.by_strategy),
        # Nur zeigen, wenn es ueberhaupt mehrere gibt - eine einzeilige Tabelle
        # mit dem Gesamtergebnis waere reine Wiederholung.
        ("Nach Instrument", report.by_symbol if len(report.by_symbol) > 1 else {}),
        ("Nach Session", report.by_session),
        ("Nach Richtung", report.by_direction),
        ("Nach Ausstiegsart", report.by_exit),
    ):
        if not table:
            continue
        out.append(f"  {title}")
        out.append("  " + "-" * 70)
        out.append(f"  {'':<14}{'Trades':>8}{'Trefferq.':>12}{'Erwartung':>14}{'Ergebnis':>16}")
        for name, part in table.items():
            out.append(
                f"  {name:<14}{part.trades:>8,}{part.win_rate:>11.1f} %"
                f"{part.expectancy_r:>13.3f} R{part.net_pnl:>15,.0f} $"
            )
        out.append("")

    if report.rejections:
        out.append("  Warum kein Trade zustande kam")
        out.append("  " + "-" * 70)
        for code, count in list(report.rejections.items())[:12]:
            out.append(f"  {count:>8,}x  {code}")
        out.append("")

    out.append("  Annahmen der Ausfuehrung")
    out.append("  " + "-" * 70)
    for key, value in report.assumptions.items():
        out.append(_line(key, str(value)))
    out.append("")
    out.append(
        f"  Signale {report.signals:,}   davon gefuellt {report.trades_total:,}   "
        f"nie gefuellt {report.unfilled:,}   ueber Datenluecke verworfen {report.stale:,}"
    )
    out.append("")
    out.append(f"  erstellt {datetime.now():%Y-%m-%d %H:%M}  ({report.backtest_version})")
    return "\n".join(out)
