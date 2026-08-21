"""Reason-Codes fuer die Begruendungsanzeige (Spec §23, §25).

Das Backend liefert NIE fertige Saetze, sondern Code + Parameter. Uebersetzt
wird erst im UI (`ui/src/i18n/de.ts`).

Drei Gruende fuer diese Trennung:
    1. Mehrsprachigkeit ohne Aenderung an der Engine
    2. Begruendungen werden testbar - ein Test prueft Codes, keine Formulierungen
    3. Das Entscheidungsprotokoll bleibt maschinell auswertbar: "wie oft scheiterte
       ein Setup an fehlendem MSS?" ist eine Abfrage, keine Textsuche
"""

from __future__ import annotations

from typing import Final

# --- HTF Bias (Spec §7 Schritt 1) -------------------------------------------
HTF_BIAS: Final = "htf.bias"
HTF_STRUCTURE: Final = "htf.structure"
HTF_FVG_BALANCE: Final = "htf.fvg_balance"
HTF_LIQUIDITY_DRAW: Final = "htf.liquidity_draw"

# --- Liquidity Sweep (Spec §7 Schritt 2) ------------------------------------
SWEEP_FOUND: Final = "sweep.found"
SWEEP_MISSING: Final = "sweep.missing"

# --- Displacement (Spec §7 Schritt 3) ---------------------------------------
DISPLACEMENT_FOUND: Final = "displacement.found"
DISPLACEMENT_MISSING: Final = "displacement.missing"
DISPLACEMENT_VOLUME: Final = "displacement.volume_confirmed"

# --- FVG (Spec §7 Schritt 4) ------------------------------------------------
FVG_FOUND: Final = "fvg.found"
FVG_MISSING: Final = "fvg.missing"

# --- Retracement (Spec §7 Schritt 5) ----------------------------------------
RETRACEMENT_FOUND: Final = "retracement.found"
RETRACEMENT_MISSING: Final = "retracement.missing"

# --- Market Structure Shift (Spec §7 Schritt 6) -----------------------------
MSS_FOUND: Final = "mss.found"
MSS_MISSING: Final = "mss.missing"

# --- Datenqualitaet und Betriebszustand (Spec §24) --------------------------
DATA_WARMUP: Final = "data.warmup"
DATA_GAP: Final = "data.gap"
DATA_ROLL: Final = "data.roll_boundary"
MARKET_CLOSED: Final = "market.closed"

#: Alle bekannten Codes - `tests/test_reasons.py` prueft, dass fuer jeden davon
#: eine deutsche Uebersetzung existiert.
ALL_CODES: Final[tuple[str, ...]] = (
    HTF_BIAS,
    HTF_STRUCTURE,
    HTF_FVG_BALANCE,
    HTF_LIQUIDITY_DRAW,
    SWEEP_FOUND,
    SWEEP_MISSING,
    DISPLACEMENT_FOUND,
    DISPLACEMENT_MISSING,
    DISPLACEMENT_VOLUME,
    FVG_FOUND,
    FVG_MISSING,
    RETRACEMENT_FOUND,
    RETRACEMENT_MISSING,
    MSS_FOUND,
    MSS_MISSING,
    DATA_WARMUP,
    DATA_GAP,
    DATA_ROLL,
    MARKET_CLOSED,
)
