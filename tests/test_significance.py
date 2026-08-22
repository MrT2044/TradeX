"""Statistik gegen Tabellenwerte, nicht gegen sich selbst.

Die Funktionen in `tradex/backtest/significance.py` sind von Hand
implementiert, weil `scipy` fuer drei Formeln eine zu grosse Abhaengigkeit
waere. Damit das vertretbar bleibt, werden sie hier gegen unabhaengig bekannte
Werte geprueft: geschlossene Sonderfaelle der Betafunktion und Zahlen aus der
t-Tabelle.
"""

from __future__ import annotations

import math

import pytest

from tradex.backtest.significance import (
    benjamini_hochberg,
    mean_test,
    regularized_beta,
    t_critical,
    t_p_value,
)


# ------------------------------------------------------- unvollstaendige Beta
@pytest.mark.parametrize("x", [0.05, 0.25, 0.5, 0.75, 0.95])
@pytest.mark.parametrize("a", [0.5, 1.0, 2.0, 7.5])
def test_beta_gegen_geschlossene_sonderfaelle(a: float, x: float):
    """I_x(a,1) = x^a und I_x(1,b) = 1-(1-x)^b - beide exakt bekannt."""
    assert regularized_beta(a, 1.0, x) == pytest.approx(x**a, abs=1e-12)
    assert regularized_beta(1.0, a, x) == pytest.approx(1.0 - (1.0 - x) ** a, abs=1e-12)


def test_beta_ist_symmetrisch():
    """I_x(a,b) = 1 - I_(1-x)(b,a) - die Identitaet hinter der Fallunterscheidung."""
    for a, b, x in ((2.0, 3.0, 0.3), (7.0, 0.5, 0.8), (0.5, 0.5, 0.61)):
        assert regularized_beta(a, b, x) == pytest.approx(
            1.0 - regularized_beta(b, a, 1.0 - x), abs=1e-12
        )


# --------------------------------------------------------------- t-Verteilung
@pytest.mark.parametrize(
    ("df", "t_value", "p_expected"),
    [
        # Werte aus der t-Tabelle: zweiseitig 5 %.
        (1, 12.706, 0.05),
        (10, 2.228, 0.05),
        (30, 2.042, 0.05),
        (120, 1.980, 0.05),
        # zweiseitig 1 %
        (10, 3.169, 0.01),
        (30, 2.750, 0.01),
    ],
)
def test_p_wert_gegen_t_tabelle(df: int, t_value: float, p_expected: float):
    assert t_p_value(t_value, df) == pytest.approx(p_expected, abs=5e-4)


def test_p_wert_bei_vielen_freiheitsgraden_naehert_sich_der_normalverteilung():
    """Fuer df -> unendlich geht die t- in die Standardnormalverteilung ueber."""
    assert t_p_value(1.959964, 100_000) == pytest.approx(0.05, abs=1e-4)


def test_p_wert_ist_zweiseitig_und_symmetrisch():
    assert t_p_value(2.0, 25) == pytest.approx(t_p_value(-2.0, 25))


def test_kritischer_wert_passt_zum_p_wert():
    """Beide Richtungen muessen dieselbe Grenze beschreiben.

    Sonst koennte das Vertrauensband null ausschliessen, waehrend der Test
    daneben nicht ablehnt - der Bericht widerspraeche sich selbst.
    """
    for df in (2, 9, 29, 150):
        critical = t_critical(df, 0.05)
        assert t_p_value(critical, df) == pytest.approx(0.05, abs=1e-6)
    assert t_critical(10, 0.05) == pytest.approx(2.228, abs=1e-3)


# ------------------------------------------------------------------ Mittelwert
def test_mittelwerttest_von_hand_nachgerechnet():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    result = mean_test(values)

    assert result.mean == pytest.approx(3.0)
    assert result.stdev == pytest.approx(math.sqrt(2.5))
    assert result.standard_error == pytest.approx(math.sqrt(2.5 / 5))
    assert result.t_stat == pytest.approx(3.0 / math.sqrt(0.5))
    assert not result.includes_zero


def test_mittelwerttest_erkennt_rauschen():
    """Eine symmetrische Reihe um null darf nicht signifikant sein."""
    values = [1.0, -1.0] * 20
    result = mean_test(values)
    assert result.mean == pytest.approx(0.0)
    assert result.p_value == 1.0
    assert result.includes_zero


def test_mittelwerttest_haelt_zu_kleine_stichproben_aus():
    assert mean_test([]).n == 0
    assert mean_test([2.0]).mean == 2.0
    assert mean_test([2.0]).p_value == 1.0
    # Ohne Streuung ist der t-Wert nicht definiert - das darf nicht zu einem
    # "unendlich signifikanten" Ergebnis fuehren.
    assert mean_test([2.0, 2.0, 2.0]).p_value == 1.0


def test_vertrauensband_und_test_sagen_dasselbe():
    """Band schliesst null aus <=> p < alpha. Das ist keine Zusatzannahme,
    sondern dieselbe Aussage - und genau deshalb pruefbar."""
    for values in ([0.4] * 8 + [-0.2] * 4, [1.5, -1.0, 0.3, 0.2, -0.9, 2.2], [0.1] * 40):
        result = mean_test(values, alpha=0.05)
        assert result.includes_zero == (result.p_value >= 0.05)


# --------------------------------------------------------- Mehrfachtestkorrektur
def test_benjamini_hochberg_von_hand():
    p_values = [0.01, 0.02, 0.03, 0.04, 0.05]
    q = benjamini_hochberg(p_values)
    # q_i = min ueber alle j>=i von p_j * n / j
    assert q == pytest.approx((0.05, 0.05, 0.05, 0.05, 0.05))


def test_benjamini_hochberg_ist_monoton():
    p_values = [0.001, 0.9, 0.02, 0.4, 0.03]
    q = benjamini_hochberg(p_values)
    paired = sorted(zip(p_values, q, strict=True))
    assert [value for _, value in paired] == sorted(value for _, value in paired)
    assert all(a <= b + 1e-12 for a, b in zip(p_values, q, strict=True)), "q darf nie unter p liegen"


def test_benjamini_hochberg_entschaerft_den_einzelfund():
    """Ein p-Wert von 0,04 unter zwanzig Tests ist kein Fund mehr.

    Genau dieser Fall ist der Grund fuer die Korrektur: bei zwanzig Tests und
    alpha=5 % ist EIN Treffer die Erwartung, auch wenn nichts da ist.
    """
    q = benjamini_hochberg([0.04] + [0.5] * 19)
    assert q[0] > 0.05


def test_benjamini_hochberg_laesst_starke_funde_durch():
    q = benjamini_hochberg([0.0001] + [0.5] * 19)
    assert q[0] < 0.01


def test_benjamini_hochberg_ohne_werte():
    assert benjamini_hochberg([]) == ()
