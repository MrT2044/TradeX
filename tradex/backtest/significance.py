"""Signifikanz: wie wahrscheinlich ist dieses Ergebnis reiner Zufall?

Reine Mathematik ueber Zahlenreihen - kein Bezug zu Trades, Config oder Engine.
Dadurch laesst sich jede Funktion gegen Werte aus einer Tabelle pruefen statt
gegen sich selbst.

Warum das hier von Hand steht
-----------------------------
`scipy` wuerde `ttest_1samp` und `false_discovery_control` mitbringen, ist aber
eine 60-MB-Abhaengigkeit fuer drei Funktionen. Die Verteilungsfunktion der
t-Verteilung ist die regularisierte unvollstaendige Betafunktion; die steht in
jedem Formelwerk und ist in vierzig Zeilen zu haben. Tests stellen sie gegen
Werte aus der t-Tabelle.

Warum Mehrfachtest-Korrektur
----------------------------
Wer zwanzig Untergruppen einer Trade-Menge auf "Erwartungswert ungleich null"
prueft, findet bei alpha = 5 % im Schnitt EINE signifikante, auch wenn gar kein
Effekt existiert. Genau so entstehen Handelsregeln wie "dienstags long". Die
Korrektur nach Benjamini-Hochberg rechnet diese Erwartung heraus: sie
kontrolliert den Anteil falscher Funde unter den Funden (FDR), nicht die
Wahrscheinlichkeit eines einzelnen Fehlalarms.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

#: Abbruchkriterien der Kettenbruchentwicklung. Beide sind numerische
#: Konstanten des Verfahrens, keine fachlichen Schwellenwerte - sie gehoeren
#: deshalb NICHT in die Config.
_MAX_ITERATIONS = 300
_EPSILON = 3.0e-16
_TINY = 1.0e-300


def _betacf(a: float, b: float, x: float) -> float:
    """Kettenbruch der unvollstaendigen Betafunktion (Verfahren von Lentz)."""
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _TINY:
        d = _TINY
    d = 1.0 / d
    h = d
    for m in range(1, _MAX_ITERATIONS + 1):
        m2 = 2 * m
        # gerader Schritt
        num = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + num * d
        if abs(d) < _TINY:
            d = _TINY
        c = 1.0 + num / c
        if abs(c) < _TINY:
            c = _TINY
        d = 1.0 / d
        h *= d * c
        # ungerader Schritt
        num = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + num * d
        if abs(d) < _TINY:
            d = _TINY
        c = 1.0 + num / c
        if abs(c) < _TINY:
            c = _TINY
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _EPSILON:
            return h
    return h


def regularized_beta(a: float, b: float, x: float) -> float:
    """I_x(a, b) - die regularisierte unvollstaendige Betafunktion."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log1p(-x)
    )
    # Der Kettenbruch konvergiert nur auf einer Seite schnell; jenseits davon
    # wird die Symmetrie I_x(a,b) = 1 - I_(1-x)(b,a) benutzt.
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def t_p_value(t_stat: float, df: int) -> float:
    """Zweiseitiger p-Wert der t-Verteilung."""
    if df <= 0:
        return 1.0
    if t_stat == 0.0:
        return 1.0
    return regularized_beta(df / 2.0, 0.5, df / (df + t_stat * t_stat))


def t_critical(df: int, alpha: float = 0.05) -> float:
    """Der t-Wert, ab dem zweiseitig auf `alpha` Niveau abgelehnt wird.

    Ueber Bisektion aus `t_p_value`, statt als zweite Naeherung: so kann das
    Konfidenzintervall gar nicht zu einem anderen Ergebnis kommen als der Test
    daneben.
    """
    if df <= 0:
        return float("inf")
    low, high = 0.0, 1000.0
    for _ in range(200):
        mid = 0.5 * (low + high)
        if t_p_value(mid, df) > alpha:
            low = mid
        else:
            high = mid
    return 0.5 * (low + high)


@dataclass(frozen=True, slots=True)
class MeanTest:
    """Test der Hypothese "der Mittelwert ist null" fuer eine Stichprobe."""

    n: int
    mean: float
    stdev: float
    standard_error: float
    t_stat: float
    p_value: float
    ci_low: float
    ci_high: float

    @property
    def includes_zero(self) -> bool:
        """Das Vertrauensband schliesst null ein - der Effekt kann null sein."""
        return self.ci_low <= 0.0 <= self.ci_high


def _undefined(n: int, mean: float) -> MeanTest:
    """Kein Test moeglich: zu wenige Werte oder gar keine Streuung.

    Das Vertrauensband ist dann UNENDLICH weit, nicht etwa null breit. Ein
    Band der Breite null um den Mittelwert wuerde behaupten, der wahre Wert
    stehe exakt fest - und wuerde null ausschliessen, waehrend der Test
    daneben nicht ablehnt. Der Bericht widerspraeche sich selbst.
    """
    return MeanTest(
        n=n,
        mean=mean,
        stdev=0.0,
        standard_error=0.0,
        t_stat=0.0,
        p_value=1.0,
        ci_low=float("-inf"),
        ci_high=float("inf"),
    )


def mean_test(values: Sequence[float], alpha: float = 0.05) -> MeanTest:
    """Einstichproben-t-Test gegen null, mit Vertrauensband zum Niveau `alpha`.

    Der t-Test setzt keine normalverteilten EINZELWERTE voraus, sondern einen
    naeherungsweise normalverteilten MITTELWERT. R-Verteilungen sind stark
    rechtsschief (viele -1, wenige +3) - fuer den Mittelwert traegt der zentrale
    Grenzwertsatz das trotzdem, ab etwa 30 Werten ordentlich. Darunter ist der
    p-Wert eine Schaetzung mit eigener Unsicherheit; deshalb wird eine
    Mindestanzahl vorausgesetzt, bevor ueberhaupt getestet wird.
    """
    n = len(values)
    if n < 2:
        return _undefined(n, values[0] if n else 0.0)

    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    stdev = math.sqrt(variance)
    error = stdev / math.sqrt(n)
    if error == 0.0:
        return _undefined(n, mean)

    t_stat = mean / error
    half = t_critical(n - 1, alpha) * error
    return MeanTest(
        n=n,
        mean=mean,
        stdev=stdev,
        standard_error=error,
        t_stat=t_stat,
        p_value=t_p_value(t_stat, n - 1),
        ci_low=mean - half,
        ci_high=mean + half,
    )


def benjamini_hochberg(p_values: Sequence[float]) -> tuple[float, ...]:
    """p-Werte in q-Werte umrechnen (FDR-Korrektur), Reihenfolge bleibt erhalten.

    q ist die Rate falscher Funde, die man in Kauf nimmt, wenn man diesen Fund
    und alle staerkeren annimmt. Bonferroni waere strenger, aber bei zwanzig
    Untergruppen so streng, dass auch ein echter Effekt nie durchkaeme.

    Die Monotonie (`min` mit dem naechstgroesseren q) gehoert zum Verfahren:
    ohne sie koennte ein staerkerer p-Wert ein schlechteres q bekommen als ein
    schwaecherer.
    """
    count = len(p_values)
    if count == 0:
        return ()
    order = sorted(range(count), key=lambda i: p_values[i])
    q_values = [1.0] * count
    running = 1.0
    for rank, index in enumerate(reversed(order), start=1):
        position = count - rank + 1
        running = min(running, p_values[index] * count / position)
        q_values[index] = min(1.0, running)
    return tuple(q_values)
