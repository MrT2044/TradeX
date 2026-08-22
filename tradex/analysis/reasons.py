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

# --- Setup-Lebenszyklus (Spec §7-§9) ----------------------------------------
SETUP_STARTED: Final = "setup.started"
SETUP_INVALIDATED_BEYOND_SWEEP: Final = "setup.invalidated_beyond_sweep"
SETUP_INVALIDATED_BIAS_FLIP: Final = "setup.invalidated_bias_flip"
SETUP_EXPIRED: Final = "setup.expired"
SETUP_CROWDED_OUT: Final = "setup.crowded_out"

# --- Opening Range Breakout (zweite Strategie) ------------------------------
OPENING_RANGE_BREAKOUT: Final = "opening_range.breakout"
SETUP_COMPLETE: Final = "setup.complete"

# --- Stop Loss (Spec §11) ---------------------------------------------------
STOP_PLACED: Final = "stop.placed"
STOP_TOO_WIDE: Final = "stop.too_wide"
STOP_TOO_TIGHT: Final = "stop.too_tight"

# --- Take Profit (Spec §12) -------------------------------------------------
TARGET_LIQUIDITY: Final = "target.liquidity"
TARGET_FALLBACK: Final = "target.fallback_r_multiple"
TARGET_NONE: Final = "target.none"
TARGET_RR_TOO_LOW: Final = "target.rr_too_low"

# --- Risiko (Spec §10, §24) -------------------------------------------------
RISK_SIZE_OK: Final = "risk.size_ok"
RISK_SIZE_ZERO: Final = "risk.size_zero"
RISK_SIZE_CAPPED: Final = "risk.size_capped"
RISK_DAILY_LOSS_LIMIT: Final = "risk.daily_loss_limit"
RISK_MAX_TRADES: Final = "risk.max_trades_per_day"
RISK_MAX_POSITIONS: Final = "risk.max_open_positions"
RISK_DISABLED: Final = "risk.disabled"

# --- Handelsfenster (Spec §13) ----------------------------------------------
WINDOW_SESSION_BLOCKED: Final = "window.session_blocked"
WINDOW_VOLATILITY_LOW: Final = "window.volatility_low"
WINDOW_VOLATILITY_HIGH: Final = "window.volatility_high"
WINDOW_OK: Final = "window.ok"

# --- Endergebnis ------------------------------------------------------------
DECISION_TRADE: Final = "decision.trade"
DECISION_NO_TRADE: Final = "decision.no_trade"

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
    SETUP_STARTED,
    SETUP_INVALIDATED_BEYOND_SWEEP,
    SETUP_INVALIDATED_BIAS_FLIP,
    SETUP_EXPIRED,
    SETUP_CROWDED_OUT,
    OPENING_RANGE_BREAKOUT,
    SETUP_COMPLETE,
    STOP_PLACED,
    STOP_TOO_WIDE,
    STOP_TOO_TIGHT,
    TARGET_LIQUIDITY,
    TARGET_FALLBACK,
    TARGET_NONE,
    TARGET_RR_TOO_LOW,
    RISK_SIZE_OK,
    RISK_SIZE_ZERO,
    RISK_SIZE_CAPPED,
    RISK_DAILY_LOSS_LIMIT,
    RISK_MAX_TRADES,
    RISK_MAX_POSITIONS,
    RISK_DISABLED,
    WINDOW_SESSION_BLOCKED,
    WINDOW_VOLATILITY_LOW,
    WINDOW_VOLATILITY_HIGH,
    WINDOW_OK,
    DECISION_TRADE,
    DECISION_NO_TRADE,
    DATA_WARMUP,
    DATA_GAP,
    DATA_ROLL,
    MARKET_CLOSED,
)
