"""Musterstatistik: bedingte Verteilungen statt Mustersuche.

Der Bericht in `report.py` zeigt Aufschluesselungen - nach Session, nach
Richtung, nach Strategie. Sie laden zu genau dem Fehler ein, den dieses Projekt
nicht machen will: "RTH bringt +0,4 R, also handeln wir nur noch RTH". Zwoelf
Trades reichen fuer diese Zahl, und bei acht Aufschluesselungen ist die beste
davon fast garantiert Zufall.

Dieses Modul beantwortet dieselbe Frage mit dem Verfahren, das sie beantworten
kann:

    1. Jede Untergruppe wird gegen "Erwartungswert null" getestet.
    2. Alle Tests zusammen bekommen eine Mehrfachtest-Korrektur (FDR).
    3. Getestet wird nur auf dem VORDEREN Teil des Zeitraums. Der hintere Teil
       bleibt unangetastet und dient als Gegenprobe.

Was das Verfahren NICHT ist
---------------------------
Es ist keine Mustersuche. Es durchsucht die Historie nicht nach der besten
Kombination, sondern prueft eine feste, vorher festgelegte Liste von
Bedingungen. Der Unterschied ist die Zahl der Hypothesen: eine Suche ueber
Schwellenwerte probiert Tausende und findet garantiert etwas, hier sind es
zwanzig und die Korrektur kennt ihre Anzahl.

Es ist auch kein Freibrief. Ein Muster, das hier ueberlebt, ist eine Hypothese
fuer den naechsten Zeitraum - kein Befund. Die Bestaetigung auf dem hinteren
Abschnitt ist eine schwache Gegenprobe, weil dort typischerweise nur wenige
Trades je Gruppe liegen; sie kann ein Muster widerlegen, aber keines beweisen.

Warum nur Merkmale von VOR dem Einstieg
---------------------------------------
`exit_reason`, `bars_held`, `mae_r` und `pnl` sind Ergebnisse, keine
Bedingungen. "Trades, die am Ziel schlossen, laufen besser" ist wahr und
wertlos, weil man die Bedingung beim Einstieg nicht kennt. Zulaessig ist nur,
was zum Zeitpunkt der Entscheidung feststand - `OUTCOME_FIELDS` haelt die
Gegenliste, ein Test erzwingt sie.
"""

from __future__ import annotations

import textwrap
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date

from tradex.backtest import metrics as M
from tradex.backtest.execution import SimulatedTrade
from tradex.backtest.significance import benjamini_hochberg, mean_test
from tradex.domain.bars import from_ns

#: Felder von `SimulatedTrade`, die erst NACH dem Einstieg feststehen. Eine
#: Bedingung, die eines davon liest, waere ein Blick in die Zukunft.
OUTCOME_FIELDS = frozenset(
    {
        "exit_ts",
        "exit_index",
        "exit_price",
        "exit_reason",
        "bars_held",
        "gross_pnl",
        "pnl",
        "r_multiple",
        "mae_points",
        "mfe_points",
        "mae_r",
        "mfe_r",
        "is_win",
        "is_loss",
        "is_resolved",
    }
)

_WEEKDAYS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")


@dataclass(frozen=True, slots=True)
class Condition:
    """Eine Bedingung, unter der die Trade-Menge aufgeteilt wird."""

    name: str
    """Technischer Schluessel, etwa `session`."""
    label: str
    """Beschriftung fuer den Bericht."""
    of: Callable[[SimulatedTrade], str]
    """Merkmalswert eines Trades. Darf nur lesen, was beim Einstieg feststand."""


@dataclass(frozen=True, slots=True)
class Cell:
    """Eine Untergruppe: alle Trades mit einem bestimmten Merkmalswert."""

    condition: str
    label: str
    value: str

    trades: int
    mean_r: float
    stdev_r: float
    t_stat: float
    p_value: float
    q_value: float
    """p-Wert nach Mehrfachtest-Korrektur. 1.0, wenn nicht getestet wurde."""
    ci_low: float
    ci_high: float
    tested: bool
    """False, wenn die Gruppe zu klein war. Solche Zeilen zaehlen NICHT als
    Hypothese - sonst wuerde die Korrektur durch Rauschen verwaessert."""

    oos_trades: int
    oos_mean_r: float
    oos_tested: bool
    """Der hintere Abschnitt hat genug Trades, dass sein Vorzeichen etwas
    bedeutet. Darunter ist es ein Muenzwurf, und die Gegenprobe gilt weder als
    bestanden noch als gescheitert."""

    significant: bool
    """q unter dem Niveau: im vorderen Abschnitt nicht durch Zufall erklaerbar."""
    confirmed: bool
    """Zusaetzlich im hinteren Abschnitt mit gleichem Vorzeichen."""


@dataclass(frozen=True, slots=True)
class PatternReport:
    """Ergebnis der Musterstatistik ueber eine Trade-Menge."""

    cells: tuple[Cell, ...]
    hypotheses: int
    """Anzahl tatsaechlich durchgefuehrter Tests - die Zahl, gegen die
    korrigiert wurde. Steht im Bericht, weil sie das Ergebnis mitbestimmt."""
    alpha: float
    min_trades: int
    min_oos_trades: int
    in_sample_trades: int
    in_sample_mean_r: float
    """Erwartungswert ueber ALLE Trades des vorderen Abschnitts.

    Steht im Bericht als Einordnung: eine Gruppe mit +0,3 R heisst etwas
    anderes, wenn das Ganze schon +0,3 R bringt. Getestet wird trotzdem gegen
    null - "traegt diese Gruppe?" ist die Frage, an der ein Filter haengt."""
    out_of_sample_trades: int
    warnings: tuple[str, ...]

    @property
    def survivors(self) -> tuple[Cell, ...]:
        """Muster, die Korrektur UND Gegenprobe ueberstanden haben."""
        return tuple(c for c in self.cells if c.confirmed)

    @property
    def significant(self) -> tuple[Cell, ...]:
        return tuple(c for c in self.cells if c.significant)


# ------------------------------------------------------------------ Bedingungen
def _weekday(trade: SimulatedTrade) -> str:
    """Wochentag des Handelstags - nicht des Kalendertags.

    Der Handelstag beginnt um 17:00 Boersenzeit; eine Bar von Sonntag 23:00 UTC
    gehoert bereits zum Montag. `trading_day` traegt diese Verschiebung schon,
    der Kalendertag aus `signal_ts` waere hier schlicht falsch.
    """
    return _WEEKDAYS[date.fromordinal(trade.trading_day).weekday()]


def _utc_hour(trade: SimulatedTrade) -> str:
    """Einstiegsstunde in UTC.

    Bewusst UTC und nicht Boersenzeit: dieses Modul rechnet nur ueber Trades und
    kennt kein Instrument. Boersenzeit hiesse, hier eine Zeitzone zu erraten -
    lieber eine Beschriftung, die stimmt, als eine, die bequem ist.
    """
    return f"{from_ns(trade.signal_ts):%H} UTC"


def _terciles(values: Sequence[float]) -> tuple[float, float]:
    """Die beiden Drittelgrenzen einer Zahlenreihe."""
    ordered = sorted(values)
    if not ordered:
        return 0.0, 0.0
    last = len(ordered) - 1
    return ordered[last // 3], ordered[(2 * last) // 3]


def _bucketed(
    name: str,
    label: str,
    of: Callable[[SimulatedTrade], float],
    trades: Sequence[SimulatedTrade],
    unit: str,
) -> Condition:
    """Eine stetige Groesse in Drittel schneiden.

    Die Grenzen stammen AUSSCHLIESSLICH aus dem vorderen Abschnitt und werden
    danach unveraendert auf den hinteren angewendet. Wuerde man sie auf allen
    Trades bestimmen, flossen Daten des hinteren Abschnitts in die Definition
    der Gruppen ein - die Gegenprobe waere dann keine mehr.
    """
    low, high = _terciles([of(t) for t in trades])

    def bucket(trade: SimulatedTrade) -> str:
        value = of(trade)
        if value <= low:
            return f"bis {low:.1f}{unit}"
        if value <= high:
            return f"{low:.1f}-{high:.1f}{unit}"
        return f"ueber {high:.1f}{unit}"

    return Condition(name=name, label=label, of=bucket)


def conditions_for(trades: Sequence[SimulatedTrade]) -> tuple[Condition, ...]:
    """Die feste Liste der Bedingungen, an `trades` kalibriert.

    Bedingungen mit nur einem vorkommenden Wert fallen heraus: sie wiederholen
    das Gesamtergebnis und wuerden die Zahl der Hypothesen erhoehen, ohne eine
    Frage zu stellen.
    """
    candidates = (
        Condition("strategy", "Strategie", lambda t: t.strategy),
        Condition("symbol", "Instrument", lambda t: t.symbol),
        Condition("side", "Richtung", lambda t: t.side),
        Condition("session", "Session", lambda t: t.session),
        Condition("htf_bias", "HTF-Bias", lambda t: t.htf_bias or "ohne"),
        Condition("weekday", "Wochentag", _weekday),
        Condition("hour", "Einstiegsstunde", _utc_hour),
        Condition("timeframe", "Zeitebene", lambda t: t.timeframe),
        Condition("stop_anchor", "Stop-Anker", lambda t: t.stop_anchor),
        Condition("target_source", "Zielquelle", lambda t: t.target_source),
        _bucketed("stop_ticks", "Stopweite", lambda t: t.planned_stop_ticks, trades, " Ticks"),
        _bucketed("planned_rr", "geplantes CRV", lambda t: t.planned_rr, trades, ""),
    )
    return tuple(c for c in candidates if len({c.of(t) for t in trades}) > 1)


# --------------------------------------------------------------------- Analyse
def _group(
    trades: Sequence[SimulatedTrade], condition: Condition
) -> dict[str, list[SimulatedTrade]]:
    groups: dict[str, list[SimulatedTrade]] = {}
    for trade in trades:
        groups.setdefault(condition.of(trade), []).append(trade)
    return groups


def analyse(
    trades: Sequence[SimulatedTrade],
    *,
    out_of_sample_fraction: float,
    alpha: float,
    min_trades: int,
    min_oos_trades: int,
) -> PatternReport:
    """Bedingte Verteilungen mit Korrektur und Gegenprobe."""
    in_sample, out_of_sample = M.split_by_time(trades, out_of_sample_fraction)
    if not in_sample:
        return PatternReport(
            cells=(),
            hypotheses=0,
            alpha=alpha,
            min_trades=min_trades,
            min_oos_trades=min_oos_trades,
            in_sample_trades=0,
            in_sample_mean_r=0.0,
            out_of_sample_trades=0,
            warnings=("Keine Trades - nichts zu untersuchen.",),
        )

    conditions = conditions_for(in_sample)
    raw: list[tuple[Cell, float | None]] = []

    for condition in conditions:
        groups = _group(in_sample, condition)
        later = _group(out_of_sample, condition)
        for value, members in sorted(groups.items()):
            r_values = [t.r_multiple for t in members]
            oos = [t.r_multiple for t in later.get(value, ())]
            oos_mean = sum(oos) / len(oos) if oos else 0.0
            tested = len(members) >= min_trades
            result = mean_test(r_values, alpha)
            raw.append(
                (
                    Cell(
                        condition=condition.name,
                        label=condition.label,
                        value=value,
                        trades=len(members),
                        mean_r=result.mean,
                        stdev_r=result.stdev,
                        t_stat=result.t_stat,
                        p_value=result.p_value if tested else 1.0,
                        q_value=1.0,
                        ci_low=result.ci_low,
                        ci_high=result.ci_high,
                        tested=tested,
                        oos_trades=len(oos),
                        oos_mean_r=oos_mean,
                        oos_tested=len(oos) >= min_oos_trades,
                        significant=False,
                        confirmed=False,
                    ),
                    result.p_value if tested else None,
                )
            )

    tested_p = [p for _, p in raw if p is not None]
    q_by_index = dict(
        zip(
            [i for i, (_, p) in enumerate(raw) if p is not None],
            benjamini_hochberg(tested_p),
            strict=True,
        )
    )

    cells: list[Cell] = []
    for index, (cell, _) in enumerate(raw):
        q = q_by_index.get(index, 1.0)
        significant = cell.tested and q < alpha
        confirmed = (
            significant and cell.oos_tested and (cell.oos_mean_r > 0) == (cell.mean_r > 0)
        )
        cells.append(
            Cell(
                **{
                    **{field: getattr(cell, field) for field in Cell.__slots__},
                    "q_value": q,
                    "significant": significant,
                    "confirmed": confirmed,
                }
            )
        )

    cells.sort(key=lambda c: (c.q_value, -c.trades))
    return PatternReport(
        cells=tuple(cells),
        hypotheses=len(tested_p),
        alpha=alpha,
        min_trades=min_trades,
        min_oos_trades=min_oos_trades,
        in_sample_trades=len(in_sample),
        in_sample_mean_r=sum(t.r_multiple for t in in_sample) / len(in_sample),
        out_of_sample_trades=len(out_of_sample),
        warnings=_warnings(cells, len(tested_p), len(in_sample), min_trades, alpha),
    )


def _warnings(
    cells: Sequence[Cell], hypotheses: int, in_sample: int, min_trades: int, alpha: float
) -> tuple[str, ...]:
    messages: list[str] = [
        "Diese Auswertung sucht in der VERGANGENHEIT. Was hier uebrig bleibt, ist "
        "eine Hypothese fuer den naechsten Zeitraum, kein Befund."
    ]
    if hypotheses == 0:
        messages.append(
            f"Keine einzige Gruppe erreicht {min_trades} Trades - es wurde nichts "
            "getestet. Die Tabelle unten ist eine Aufzaehlung, keine Statistik."
        )
        return tuple(messages)

    survivors = [c for c in cells if c.confirmed]
    significant = [c for c in cells if c.significant]
    if not significant:
        messages.append(
            f"Kein Muster ueberlebt die Korrektur ueber {hypotheses} Tests. Das ist "
            "das erwartete Ergebnis, wenn es keinen bedingten Edge gibt - und der "
            "Grund, warum die Korrektur ueberhaupt gerechnet wird: ohne sie waere "
            f"bei alpha={alpha:.2f} rein zufaellig etwa {hypotheses * alpha:.1f} mal "
            "'signifikant' herausgekommen."
        )
    elif not survivors:
        messages.append(
            f"{len(significant)} Muster halten der Korrektur stand, aber keines "
            "bestaetigt sich im hinteren Abschnitt. Das spricht fuer den Zeitraum "
            "und gegen die Regel."
        )
    if in_sample < min_trades * 3:
        messages.append(
            f"Nur {in_sample} Trades im vorderen Abschnitt. Bei dieser Groesse kann "
            "die Statistik fast nur 'nichts nachweisbar' herausbekommen - ein "
            "leeres Ergebnis ist hier kein Beleg fuer Abwesenheit."
        )
    return tuple(messages)


# --------------------------------------------------------------------- Ausgabe
def render_text(report: PatternReport) -> str:
    """Musterstatistik fuer die Konsole."""
    out: list[str] = []
    out.append("=" * 95)
    out.append("  MUSTERSTATISTIK  -  bedingte Verteilungen mit Mehrfachtest-Korrektur")
    out.append("=" * 95)
    out.append(
        f"  vorderer Abschnitt {report.in_sample_trades:,} Trades   "
        f"hinterer (Gegenprobe) {report.out_of_sample_trades:,}   "
        f"Tests {report.hypotheses}   Niveau {report.alpha:.2f}"
    )
    out.append(
        f"  Erwartungswert ueber alles: {report.in_sample_mean_r:+.3f} R. Getestet wird jede "
        "Gruppe gegen NULL ('traegt sie?'),\n  nicht gegen diesen Wert ('ist sie besser als "
        "der Rest?') - das waere eine andere Frage."
    )
    out.append("")
    for message in report.warnings:
        # Umbrechen statt ueberlaufen zu lassen: die Hinweise sind der Teil,
        # den man lesen soll, bevor man die Tabelle liest.
        out.extend(
            textwrap.wrap(message, width=91, initial_indent="  !! ", subsequent_indent="     ")
        )
    out.append("")

    out.append(
        f"  {'Bedingung':<16}{'Auspraegung':<18}{'n':>5}{'Erwartung':>12}"
        f"{'95-%-Band':>18}{'q':>8}{'Gegenprobe':>16}"
    )
    out.append("  " + "-" * 93)
    for cell in report.cells:
        q = f"{cell.q_value:.3f}" if cell.tested else "-"
        check = (
            f"{cell.oos_mean_r:+.2f} R ({cell.oos_trades})"
            if cell.oos_tested
            else f"zu klein ({cell.oos_trades})"
        )
        mark = " *" if cell.confirmed else ("  " if not cell.significant else " ?")
        out.append(
            f"  {cell.label:<16}{cell.value:<18}{cell.trades:>5}"
            f"{cell.mean_r:>10.3f} R"
            f"{cell.ci_low:>10.2f}..{cell.ci_high:>6.2f}"
            f"{q:>8}{check:>14}{mark}"
        )
    out.append("")
    out.append("  * = Korrektur bestanden und im hinteren Abschnitt gleiches Vorzeichen")
    out.append("  ? = Korrektur bestanden, Gegenprobe nicht bestanden oder zu klein")
    out.append(
        "  Gruppen unter der Mindestgroesse werden gezeigt, aber nicht getestet -"
    )
    out.append("  sie zaehlen deshalb auch nicht in die Korrektur hinein.")
    return "\n".join(out)
