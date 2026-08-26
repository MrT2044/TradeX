"""Die Kontosperre der NinjaTrader-Anbindung (A5).

Der Kern der ganzen Migration: bei IBKR blieb der Paper-Nachweis strukturell
indirekt (Port + `DU`-Praefix + Allowlist), weil die TWS-API kein Feld "dies
ist ein Paper-Konto" kennt. `Account.Provider == Provider.Simulator` ist eine
Eigenschaft des Kontos.

Die Pruefung steht doppelt - im AddOn und hier. Diese Tests decken die
Python-Haelfte; `test_bridge_contract.py` haelt die C#-Haelfte fest.
"""

from __future__ import annotations

from tradex.analysis import reasons as R
from tradex.broker.guard import check_simulated_account, confirm_simulated_account


# ------------------------------------------------------------- Der Nachweis
def test_ein_simulationskonto_wird_bestaetigt():
    info = confirm_simulated_account("Sim101", is_simulation=True, provider="Simulator")

    assert info.is_paper
    assert info.account == "Sim101"
    # Ein Flag ohne Begruendung waere im Nachhinein nicht ueberpruefbar.
    assert "Simulator" in info.paper_evidence


def test_ein_fremdes_konto_wird_abgelehnt():
    info = confirm_simulated_account("Playback101", is_simulation=False, provider="Playback")

    assert not info.is_paper
    assert "Playback" in info.paper_evidence


def test_ohne_konto_gibt_es_keine_freigabe():
    """Fail closed: "nichts gemeldet" ist kein "in Ordnung"."""
    info = confirm_simulated_account("", is_simulation=True)
    assert not info.is_paper
    assert info.paper_evidence == "kein Konto gemeldet"


def test_leerzeichen_sind_kein_konto():
    assert not confirm_simulated_account("   ", is_simulation=True).is_paper


# ------------------------------------------------------------- Die Allowlist
def test_die_allowlist_engt_ein_und_erlaubt_nicht():
    """Der Waechter, auf den es ankommt.

    Die Liste darf ein Simulationskonto ausschliessen, aber NIE eines
    freischalten, das keines ist - sonst waere sie ein Schalter an der
    Kontosperre vorbei, und genau die traegt hier alles.
    """
    info = confirm_simulated_account(
        "Echtgeld1",
        is_simulation=False,
        provider="Rithmic",
        allowed_accounts=("Echtgeld1",),
    )
    assert not info.is_paper, "die Allowlist darf die Simulator-Pruefung nicht aushebeln"


def test_ein_simulationskonto_ausserhalb_der_liste_wird_abgelehnt():
    """`Backtest` ist ebenfalls Provider.Simulator - und hat net_liquidation 0.

    Ohne Liste waere "das erste passende Konto" gehandelt worden. "Welches
    Konto hat der Bot eigentlich gehandelt?" ist keine Frage, die man aus
    Bequemlichkeit offenlaesst.
    """
    info = confirm_simulated_account(
        "Backtest", is_simulation=True, provider="Simulator", allowed_accounts=("Sim101",)
    )
    assert not info.is_paper
    assert "allowed_accounts" in info.paper_evidence


def test_mit_liste_und_treffer_steht_beides_im_nachweis():
    info = confirm_simulated_account(
        "Sim101", is_simulation=True, provider="Simulator", allowed_accounts=("sim101",)
    )
    assert info.is_paper
    assert "Simulator" in info.paper_evidence and "allowlist" in info.paper_evidence


def test_eine_leere_liste_ist_keine_liste():
    """Nicht gesetzt heisst "keine zusaetzliche Einschraenkung", nicht "nichts
    ist erlaubt" - sonst koennte niemand ohne Konfiguration handeln."""
    assert confirm_simulated_account(
        "Sim101", is_simulation=True, provider="Simulator", allowed_accounts=("", "  ")
    ).is_paper


# ----------------------------------------------------------------- Als Reason
def test_die_stufe_traegt_ihren_eigenen_code():
    """Eigener Code statt `BROKER_ACCOUNT_UNCONFIRMED`.

    "Unbestaetigt" heisst, der Nachweis fehlt; "nicht simuliert" heisst, er
    liegt vor und faellt negativ aus. Im Protokoll ist das der Unterschied
    zwischen "wir wissen es nicht" und "wir wissen, dass nicht".
    """
    info = confirm_simulated_account("Sim101", is_simulation=True, provider="Simulator")
    grund = check_simulated_account(info)

    assert grund.code == R.BROKER_ACCOUNT_NOT_SIMULATED
    assert grund.ok
    assert grund.params["account"] == "Sim101"
    assert grund.params["nachweis"]


def test_ohne_kontodaten_sperrt_die_stufe():
    grund = check_simulated_account(None)
    assert grund.code == R.BROKER_ACCOUNT_NOT_SIMULATED
    assert not grund.ok


def test_ein_abgelehntes_konto_sperrt_die_stufe():
    info = confirm_simulated_account("Playback101", is_simulation=False, provider="Playback")
    assert not check_simulated_account(info).ok


# -------------------------------------------------------- Keine Portpruefung
def test_fuer_ninjatrader_gibt_es_keine_portstufe():
    """Der Bridge-Port trennt nicht zwischen Simulation und Echtgeld.

    Eine Portpruefung dort saehe aus wie ein Nachweis und waere keiner. Dieser
    Test haelt fest, dass niemand aus Symmetrie eine einbaut.
    """
    from tradex.broker import guard

    assert not hasattr(guard, "check_bridge_port")
    assert R.BROKER_PORT_NOT_PAPER not in {
        R.BROKER_ACCOUNT_NOT_SIMULATED,
    }, "die beiden Codes duerfen nicht verwechselt werden"
