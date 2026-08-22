"""Kennzahlen ueber eine Menge simulierter Trades (Spec §19).

Reine Funktionen ueber `SimulatedTrade`-Listen - kein Zugriff auf Engine,
Config oder Datenbank. Dadurch sind alle Kennzahlen gegen von Hand gerechnete
Beispiele pruefbar, und dieselben Funktionen lassen sich spaeter unveraendert
auf Paper- und Live-Trades anwenden.

Warum in R gerechnet wird
-------------------------
Ein Ergebnis in USD haengt an Kontogroesse, Stopweite und Stueckzahl - drei
Groessen, die mit der Regel nichts zu tun haben. Das R-Vielfache (Ergebnis
geteilt durch das eingegangene Risiko) misst nur die Regel. Der Erwartungswert
in R ist deshalb die eigentliche Antwort auf "hat das einen Edge?"; die
USD-Zahlen daneben beantworten "traegt das ein Konto dieser Groesse?".
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from tradex.backtest.execution import SimulatedTrade
from tradex.domain.enums import ExitReason


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """Kontostand nach einem abgeschlossenen Trade."""

    ts: int
    trade_number: int
    equity: float
    drawdown: float
    """Abstand zum bisherigen Hoechststand, als positive Zahl."""


@dataclass(frozen=True, slots=True)
class Metrics:
    """Auswertung einer Trade-Menge.

    `profit_factor` und `payoff_ratio` sind None, wenn es keinen Verlusttrade
    gibt: "unendlich gut" ist keine Kennzahl, sondern eine zu kleine Stichprobe.
    """

    trades: int
    wins: int
    losses: int
    scratches: int
    win_rate: float

    gross_profit: float
    gross_loss: float
    commission: float
    net_pnl: float
    profit_factor: float | None

    expectancy_r: float
    expectancy_usd: float
    avg_win_r: float
    avg_loss_r: float
    payoff_ratio: float | None
    best_r: float
    worst_r: float
    stdev_r: float
    sqn: float | None
    """System Quality Number = Erwartungswert / Streuung * sqrt(Trades).
    Setzt den Edge ins Verhaeltnis zu seiner Schwankung UND zur Stichprobe -
    ein hoher Erwartungswert aus acht Trades bekommt so keine gute Note."""

    max_drawdown_usd: float
    max_drawdown_pct: float
    max_drawdown_r: float
    max_consecutive_wins: int
    max_consecutive_losses: int

    avg_bars_held: float
    avg_mae_r: float
    avg_mfe_r: float
    avg_planned_rr: float

    start_equity: float
    final_equity: float
    return_pct: float

    first_ts: int
    last_ts: int
    unresolved: int
    """Trades, die nur das Datenende beendet hat - sie sagen nichts ueber die Regel."""

    @property
    def is_empty(self) -> bool:
        return self.trades == 0


EMPTY = Metrics(
    trades=0,
    wins=0,
    losses=0,
    scratches=0,
    win_rate=0.0,
    gross_profit=0.0,
    gross_loss=0.0,
    commission=0.0,
    net_pnl=0.0,
    profit_factor=None,
    expectancy_r=0.0,
    expectancy_usd=0.0,
    avg_win_r=0.0,
    avg_loss_r=0.0,
    payoff_ratio=None,
    best_r=0.0,
    worst_r=0.0,
    stdev_r=0.0,
    sqn=None,
    max_drawdown_usd=0.0,
    max_drawdown_pct=0.0,
    max_drawdown_r=0.0,
    max_consecutive_wins=0,
    max_consecutive_losses=0,
    avg_bars_held=0.0,
    avg_mae_r=0.0,
    avg_mfe_r=0.0,
    avg_planned_rr=0.0,
    start_equity=0.0,
    final_equity=0.0,
    return_pct=0.0,
    first_ts=0,
    last_ts=0,
    unresolved=0,
)


def equity_curve(trades: Sequence[SimulatedTrade], start_equity: float) -> tuple[EquityPoint, ...]:
    """Kontoverlauf ueber die Trades, in Ausstiegsreihenfolge.

    Der Startpunkt gehoert dazu: ohne ihn faengt jede Auswertung erst nach dem
    ersten Trade an und unterschlaegt einen Verlust gleich zu Beginn.
    """
    points = [EquityPoint(ts=trades[0].entry_ts if trades else 0, trade_number=0, equity=start_equity, drawdown=0.0)]
    equity = start_equity
    peak = start_equity
    for number, trade in enumerate(sorted(trades, key=lambda t: t.exit_ts), start=1):
        equity += trade.pnl
        peak = max(peak, equity)
        points.append(
            EquityPoint(
                ts=trade.exit_ts,
                trade_number=number,
                equity=equity,
                drawdown=peak - equity,
            )
        )
    return tuple(points)


def _drawdown(trades: Sequence[SimulatedTrade], start_equity: float) -> tuple[float, float, float]:
    """(USD, Prozent, R) des groessten Rueckgangs vom jeweiligen Hoechststand."""
    equity = start_equity
    peak = start_equity
    worst_usd = 0.0
    worst_pct = 0.0

    r_equity = 0.0
    r_peak = 0.0
    worst_r = 0.0

    for trade in sorted(trades, key=lambda t: t.exit_ts):
        equity += trade.pnl
        peak = max(peak, equity)
        drop = peak - equity
        if drop > worst_usd:
            worst_usd = drop
            worst_pct = 100.0 * drop / peak if peak > 0 else 0.0

        r_equity += trade.r_multiple
        r_peak = max(r_peak, r_equity)
        worst_r = max(worst_r, r_peak - r_equity)

    return worst_usd, worst_pct, worst_r


def _streaks(trades: Sequence[SimulatedTrade]) -> tuple[int, int]:
    best_win = best_loss = run_win = run_loss = 0
    for trade in sorted(trades, key=lambda t: t.exit_ts):
        if trade.is_win:
            run_win += 1
            run_loss = 0
        elif trade.is_loss:
            run_loss += 1
            run_win = 0
        else:
            run_win = run_loss = 0
        best_win = max(best_win, run_win)
        best_loss = max(best_loss, run_loss)
    return best_win, best_loss


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def summarize(trades: Sequence[SimulatedTrade], start_equity: float) -> Metrics:
    """Alle Kennzahlen einer Trade-Menge."""
    if not trades:
        return _empty_with(start_equity)

    ordered = sorted(trades, key=lambda t: t.exit_ts)
    r_values = [t.r_multiple for t in ordered]
    wins = [t for t in ordered if t.is_win]
    losses = [t for t in ordered if t.is_loss]
    scratches = len(ordered) - len(wins) - len(losses)

    gross_profit = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)  # positiv gefuehrt
    net = sum(t.pnl for t in ordered)

    mean_r = _mean(r_values)
    if len(r_values) > 1:
        variance = sum((r - mean_r) ** 2 for r in r_values) / (len(r_values) - 1)
        stdev = math.sqrt(variance)
    else:
        stdev = 0.0

    avg_win_r = _mean([t.r_multiple for t in wins])
    avg_loss_r = _mean([t.r_multiple for t in losses])
    dd_usd, dd_pct, dd_r = _drawdown(ordered, start_equity)
    streak_wins, streak_losses = _streaks(ordered)

    return Metrics(
        trades=len(ordered),
        wins=len(wins),
        losses=len(losses),
        scratches=scratches,
        win_rate=100.0 * len(wins) / len(ordered),
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        commission=sum(t.commission for t in ordered),
        net_pnl=net,
        profit_factor=(gross_profit / gross_loss) if gross_loss > 0 else None,
        expectancy_r=mean_r,
        expectancy_usd=net / len(ordered),
        avg_win_r=avg_win_r,
        avg_loss_r=avg_loss_r,
        payoff_ratio=(avg_win_r / abs(avg_loss_r)) if avg_loss_r < 0 else None,
        best_r=max(r_values),
        worst_r=min(r_values),
        stdev_r=stdev,
        sqn=(mean_r / stdev * math.sqrt(len(r_values))) if stdev > 0 else None,
        max_drawdown_usd=dd_usd,
        max_drawdown_pct=dd_pct,
        max_drawdown_r=dd_r,
        max_consecutive_wins=streak_wins,
        max_consecutive_losses=streak_losses,
        avg_bars_held=_mean([float(t.bars_held) for t in ordered]),
        avg_mae_r=_mean([t.mae_r for t in ordered]),
        avg_mfe_r=_mean([t.mfe_r for t in ordered]),
        avg_planned_rr=_mean([t.planned_rr for t in ordered]),
        start_equity=start_equity,
        final_equity=start_equity + net,
        return_pct=100.0 * net / start_equity if start_equity > 0 else 0.0,
        first_ts=min(t.entry_ts for t in ordered),
        last_ts=max(t.exit_ts for t in ordered),
        unresolved=sum(1 for t in ordered if not t.is_resolved),
    )


def _empty_with(start_equity: float) -> Metrics:
    return Metrics(
        **{
            **{field: getattr(EMPTY, field) for field in EMPTY.__slots__},
            "start_equity": start_equity,
            "final_equity": start_equity,
        }
    )


def breakdown(
    trades: Sequence[SimulatedTrade],
    key: Callable[[SimulatedTrade], str],
    start_equity: float,
) -> dict[str, Metrics]:
    """Kennzahlen je Gruppe - etwa je Session oder je Handelsrichtung.

    Jede Gruppe startet rechnerisch beim vollen Kontostand. Das ist Absicht:
    verglichen werden sollen die Gruppen untereinander, nicht ihr Beitrag zu
    einem gemeinsamen Verlauf. Ein Drawdown je Gruppe waere sonst davon
    abhaengig, wie viele Trades der anderen Gruppen dazwischen lagen.
    """
    groups: dict[str, list[SimulatedTrade]] = {}
    for trade in trades:
        groups.setdefault(key(trade), []).append(trade)
    return {
        name: summarize(items, start_equity)
        for name, items in sorted(groups.items(), key=lambda kv: kv[0])
    }


def exit_counts(trades: Iterable[SimulatedTrade]) -> dict[str, int]:
    counts = {reason.value: 0 for reason in ExitReason}
    for trade in trades:
        counts[trade.exit_reason.value] += 1
    return counts


def split_by_time(
    trades: Sequence[SimulatedTrade], out_of_sample_fraction: float
) -> tuple[tuple[SimulatedTrade, ...], tuple[SimulatedTrade, ...]]:
    """Zeitraum in einen vorderen und einen hinteren Abschnitt teilen.

    Kein echtes Out-of-Sample-Verfahren - dafuer muesste zwischen den beiden
    Abschnitten etwas angepasst worden sein. Es beantwortet die davor liegende,
    billigere Frage: Sind beide Haelften ueberhaupt aehnlich? Ein Ergebnis, das
    nur in einer der beiden entsteht, stammt eher aus dem Zeitraum als aus der
    Regel.
    """
    if not trades or out_of_sample_fraction <= 0:
        return tuple(trades), ()
    ordered = sorted(trades, key=lambda t: t.entry_ts)
    cut = int(len(ordered) * (1.0 - out_of_sample_fraction))
    cut = min(max(cut, 1), len(ordered) - 1) if len(ordered) > 1 else len(ordered)
    return tuple(ordered[:cut]), tuple(ordered[cut:])
