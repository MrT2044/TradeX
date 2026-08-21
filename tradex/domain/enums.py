"""Zentrale Aufzaehlungstypen.

Diese Enums sind Teil des UI-Vertrags: ihre `value`-Strings gehen unveraendert
ueber die API an das Frontend. Werte deshalb nie umbenennen, ohne `ui/src/i18n`
und `ui/src/api` mitzuziehen.
"""

from __future__ import annotations

from enum import StrEnum


class Timeframe(StrEnum):
    """Unterstuetzte Timeframes.

    Die Reihenfolge der Definition ist aufsteigend - darauf verlaesst sich
    `MarketContext`, um Timeframes gross-nach-klein zu verarbeiten.
    """

    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"

    @property
    def seconds(self) -> int:
        return _TIMEFRAME_SECONDS[self]

    @property
    def minutes(self) -> int:
        return self.seconds // 60

    @classmethod
    def parse(cls, raw: str) -> Timeframe:
        try:
            return cls(raw.strip().lower())
        except ValueError as exc:
            valid = ", ".join(tf.value for tf in cls)
            raise ValueError(f"Unbekannter Timeframe {raw!r}. Gueltig: {valid}") from exc


_TIMEFRAME_SECONDS: dict[Timeframe, int] = {
    Timeframe.M1: 60,
    Timeframe.M5: 5 * 60,
    Timeframe.M15: 15 * 60,
    Timeframe.H1: 60 * 60,
    Timeframe.H4: 4 * 60 * 60,
}


class Direction(StrEnum):
    """Richtung eines gerichteten Musters (FVG, Displacement, Sweep, ...)."""

    BULLISH = "bullish"
    BEARISH = "bearish"


class Bias(StrEnum):
    """Ergebnis der HTF-Bias-Bestimmung (Spec §7 Schritt 1)."""

    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class SwingType(StrEnum):
    HIGH = "swing_high"
    LOW = "swing_low"


class StructureEventType(StrEnum):
    """Strukturereignisse.

    BOS  = Break of Structure (Fortsetzung der bestehenden Richtung)
    MSS  = Market Structure Shift / CHoCH (erster Bruch gegen die Richtung)
    """

    BOS_BULLISH = "bos_bullish"
    BOS_BEARISH = "bos_bearish"
    MSS_BULLISH = "mss_bullish"
    MSS_BEARISH = "mss_bearish"


class StructureState(StrEnum):
    """Aktueller Strukturzustand eines Timeframes."""

    BULLISH = "bullish"
    BEARISH = "bearish"
    RANGE = "range"


class FvgState(StrEnum):
    """Lebenszyklus einer Fair-Value-Gap-Zone.

    OPEN      -> Preis war seit Entstehung nie in der Zone
    TOUCHED   -> Preis hat die Zone beruehrt, aber nicht ausreichend durchhandelt
    MITIGATED -> Zone gilt als abgearbeitet (Schwelle aus config.fvg)
    EXPIRED   -> max_age_bars ueberschritten, ohne mitigiert zu werden
    """

    OPEN = "open"
    TOUCHED = "touched"
    MITIGATED = "mitigated"
    EXPIRED = "expired"


class LiquidityKind(StrEnum):
    """Herkunft eines Liquiditaets-Levels (Spec §7 Schritt 2)."""

    SWING = "swing"
    EQUAL = "equal"
    SESSION = "session"
    PRIOR_DAY = "prior_day"
    PRIOR_WEEK = "prior_week"


class LiquiditySide(StrEnum):
    """Auf welcher Seite die Liquiditaet liegt.

    BUY_SIDE  = ueber dem Preis (Stops der Shorts) -> Ziel bearisher Sweeps
    SELL_SIDE = unter dem Preis (Stops der Longs)  -> Ziel bullisher Sweeps
    """

    BUY_SIDE = "buy_side"
    SELL_SIDE = "sell_side"


class LiquidityState(StrEnum):
    UNTAPPED = "untapped"
    SWEPT = "swept"


class SessionName(StrEnum):
    ASIA = "asia"
    LONDON = "london"
    NY_AM = "ny_am"
    NY_PM = "ny_pm"
    CLOSED = "closed"


class ProviderStatus(StrEnum):
    """Zustand einer Datenquelle (Spec §24 Data Feed Loss Detection)."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    ERROR = "error"


class TradingMode(StrEnum):
    """Spec §18. In Phase 1+2 ist ausschliesslich ANALYSIS_ONLY erreichbar."""

    ANALYSIS_ONLY = "analysis_only"
    PAPER_MANUAL = "paper_manual"
    PAPER_AUTO = "paper_auto"
    LIVE_MANUAL = "live_manual"
    LIVE_AUTO = "live_auto"
