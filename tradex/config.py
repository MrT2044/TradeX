"""Konfiguration laden und typisieren.

ARCHITEKTUR-INVARIANTE 4 (Spec §29): Jeder Schwellenwert kommt aus
`config/default.yaml`. Alle Modelle sind `frozen` und `extra="forbid"` - ein
Tippfehler in der YAML fuehrt damit zu einem sofortigen, lauten Fehler statt zu
einem stillschweigend ignorierten Parameter.
"""

from __future__ import annotations

import os
from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tradex.domain.enums import SessionName, Timeframe, TradingMode
from tradex.domain.instruments import (
    DailyBreak,
    Instrument,
    SessionWindow,
    TradingHours,
    WeekBoundary,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "default.yaml"
DEFAULT_INSTRUMENTS_PATH = CONFIG_DIR / "instruments.yaml"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------------- App
class AppConfig(_Frozen):
    language: Literal["de", "en"] = "de"
    display_timezone: str = "Europe/Berlin"
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class DataConfig(_Frozen):
    parquet_dir: Path = Path("data/parquet")
    database: Path = Path("data/tradex.db")
    log_dir: Path = Path("logs")
    default_symbol: str = "MNQ"
    base_timeframe: Timeframe = Timeframe.M1


class TimeframesConfig(_Frozen):
    htf: tuple[Timeframe, ...]
    intermediate: tuple[Timeframe, ...]
    entry: tuple[Timeframe, ...]

    @property
    def all(self) -> tuple[Timeframe, ...]:
        """Alle konfigurierten Timeframes, absteigend nach Dauer (gross -> klein)."""
        seen = {*self.htf, *self.intermediate, *self.entry}
        return tuple(sorted(seen, key=lambda tf: tf.seconds, reverse=True))


# ----------------------------------------------------------------- Analyse
class VolatilityParams(_Frozen):
    atr_period: int = Field(ge=2)
    atr_method: Literal["wilder", "sma"] = "wilder"
    volume_sma_period: int = Field(ge=2)
    min_bars_required: int = Field(ge=1)


class SwingParams(_Frozen):
    default_strength: int = Field(ge=1)
    per_timeframe: dict[Timeframe, int] = Field(default_factory=dict)
    max_tracked: int = Field(ge=1)

    @field_validator("per_timeframe", mode="before")
    @classmethod
    def _parse_keys(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {Timeframe.parse(str(k)): v for k, v in value.items()}
        return value

    def strength_for(self, timeframe: Timeframe) -> int:
        return self.per_timeframe.get(timeframe, self.default_strength)


class StructureParams(_Frozen):
    confirm_on: Literal["close", "wick"] = "close"
    min_break_ticks: float = Field(ge=0)
    max_events: int = Field(ge=1)


class FvgParams(_Frozen):
    min_size_ticks: float = Field(ge=0)
    min_atr_mult: float = Field(ge=0)
    mitigation_threshold: float = Field(gt=0, le=1)
    mitigation_on: Literal["close", "wick"] = "close"
    max_age_bars: int = Field(ge=1)
    max_tracked: int = Field(ge=1)
    skip_roll_boundary: bool = True


class DisplacementStrengthWeights(_Frozen):
    range: float = Field(ge=0)
    body: float = Field(ge=0)
    volume: float = Field(ge=0)

    @model_validator(mode="after")
    def _sum_positive(self) -> DisplacementStrengthWeights:
        if self.range + self.body + self.volume <= 0:
            raise ValueError("strength_weights duerfen nicht alle 0 sein")
        return self


class DisplacementParams(_Frozen):
    range_atr_mult: float = Field(gt=0)
    body_ratio_min: float = Field(ge=0, le=1)
    require_break_prev_extreme: bool = True
    volume_mult: float = Field(ge=0)
    volume_is_gate: bool = False
    strength_weights: DisplacementStrengthWeights
    strength_range_cap_atr_mult: float = Field(gt=0)
    strength_volume_cap_mult: float = Field(gt=0)


class LiquidityParams(_Frozen):
    equal_tolerance_ticks: float = Field(ge=0)
    equal_min_count: int = Field(ge=2)
    equal_lookback_swings: int = Field(ge=2)
    include_swing_levels: bool = True
    include_session_levels: bool = True
    include_prior_day: bool = True
    include_prior_week: bool = True
    max_tracked: int = Field(ge=1)


class SweepParams(_Frozen):
    min_penetration_ticks: float = Field(ge=0)
    max_reclaim_bars: int = Field(ge=0)
    reclaim_on: Literal["close", "wick"] = "close"
    max_tracked: int = Field(ge=1)


class BiasComponentWeights(_Frozen):
    structure: float = Field(ge=0)
    fvg: float = Field(ge=0)
    liquidity: float = Field(ge=0)


class BiasParams(_Frozen):
    timeframe_weights: dict[Timeframe, float]
    component_weights: BiasComponentWeights
    neutral_band: float = Field(ge=0, lt=1)

    @field_validator("timeframe_weights", mode="before")
    @classmethod
    def _parse_keys(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {Timeframe.parse(str(k)): float(v) for k, v in value.items()}
        return value

    @model_validator(mode="after")
    def _weights_positive(self) -> BiasParams:
        if sum(self.timeframe_weights.values()) <= 0:
            raise ValueError("timeframe_weights muessen in Summe > 0 sein")
        return self


class AnalysisConfig(_Frozen):
    volatility: VolatilityParams
    swings: SwingParams
    structure: StructureParams
    fvg: FvgParams
    displacement: DisplacementParams
    liquidity: LiquidityParams
    sweep: SweepParams
    bias: BiasParams


# --------------------------------------------------------- Strategie (Phase 3)
class StrategyConfig(_Frozen):
    """Spec §6-§9: die Pflichtkette und ihre Zeitfenster."""

    enabled: bool = True
    setup_timeframe: Timeframe = Timeframe.M5
    confirmation_timeframe: Timeframe = Timeframe.M1
    sweep_max_age_bars: int = Field(default=20, ge=1)
    displacement_max_age_bars: int = Field(default=6, ge=1)
    fvg_max_age_bars: int = Field(default=60, ge=1)
    confirmation_max_age_bars: int = Field(default=20, ge=1)
    retracement_min_fill: float = Field(default=0.0, ge=0, le=1)
    invalidate_beyond_sweep: bool = True
    invalidate_on_bias_flip: bool = True
    max_active_setups: int = Field(default=4, ge=1)


class StopsConfig(_Frozen):
    """Spec §11."""

    anchor: Literal["retracement", "sweep", "swing", "fvg"] = "retracement"
    buffer_atr_mult: float = Field(default=0.25, ge=0)
    buffer_min_ticks: float = Field(default=4, ge=0)
    min_stop_ticks: float = Field(default=8, gt=0)
    max_stop_ticks: float = Field(default=240, gt=0)

    @model_validator(mode="after")
    def _range_is_sane(self) -> StopsConfig:
        if self.min_stop_ticks >= self.max_stop_ticks:
            raise ValueError("stops.min_stop_ticks muss kleiner als max_stop_ticks sein")
        return self


class TargetsConfig(_Frozen):
    """Spec §12."""

    mode: Literal["liquidity", "r_multiple"] = "liquidity"
    max_candidates: int = Field(default=6, ge=1)
    fallback_r_multiple: float = Field(default=3.0, gt=0)
    require_untapped: bool = True


class TradingWindowsConfig(_Frozen):
    """Spec §13: Filter, keine Ausloeser."""

    enabled: bool = True
    sessions: tuple[SessionName, ...] = ()
    min_atr_ticks: float = Field(default=8, ge=0)
    max_atr_ticks: float = Field(default=400, gt=0)

    @field_validator("sessions", mode="before")
    @classmethod
    def _parse_sessions(cls, value: Any) -> Any:
        if isinstance(value, list | tuple):
            return tuple(SessionName(str(item)) for item in value)
        return value

    @model_validator(mode="after")
    def _range_is_sane(self) -> TradingWindowsConfig:
        if self.min_atr_ticks >= self.max_atr_ticks:
            raise ValueError("trading_windows.min_atr_ticks muss kleiner als max_atr_ticks sein")
        return self


class RiskConfig(_Frozen):
    """Spec §10. Positionsgroesse wird berechnet, nie gesetzt."""

    enabled: bool = False
    account_size: float = Field(default=10_000.0, gt=0)
    risk_per_trade_pct: float = Field(default=0.25, gt=0, le=100)
    max_daily_loss_pct: float = Field(default=1.0, gt=0, le=100)
    max_trades_per_day: int = Field(default=3, ge=0)
    max_open_positions: int = Field(default=1, ge=0)
    min_rr: float = Field(default=2.0, gt=0)
    max_spread_ticks: float = Field(default=2, ge=0)
    max_slippage_ticks: float = Field(default=2, ge=0)
    max_position_size: int = Field(default=5, ge=1)

    @property
    def risk_per_trade_amount(self) -> float:
        return self.account_size * self.risk_per_trade_pct / 100.0

    @property
    def max_daily_loss_amount(self) -> float:
        return self.account_size * self.max_daily_loss_pct / 100.0


class ExecutionConfig(_Frozen):
    mode: TradingMode = TradingMode.ANALYSIS_ONLY
    live_trading_enabled: bool = False

    @model_validator(mode="after")
    def _live_requires_flag(self) -> ExecutionConfig:
        live_modes = {TradingMode.LIVE_MANUAL, TradingMode.LIVE_AUTO}
        if self.mode in live_modes and not self.live_trading_enabled:
            raise ValueError(
                f"execution.mode={self.mode.value} erfordert execution.live_trading_enabled=true"
            )
        return self


class NewsConfig(_Frozen):
    enabled: bool = False


# --------------------------------------------------------------------- Root
class Config(_Frozen):
    version: int
    app: AppConfig
    data: DataConfig
    timeframes: TimeframesConfig
    analysis: AnalysisConfig
    strategy: StrategyConfig = StrategyConfig()
    stops: StopsConfig = StopsConfig()
    targets: TargetsConfig = TargetsConfig()
    trading_windows: TradingWindowsConfig = TradingWindowsConfig()
    risk: RiskConfig = RiskConfig()
    execution: ExecutionConfig = ExecutionConfig()
    news: NewsConfig = NewsConfig()

    @model_validator(mode="after")
    def _strategy_timeframes_are_configured(self) -> Config:
        """Setup- und Confirmation-Timeframe muessen tatsaechlich analysiert werden.

        Sonst liefe die Strategie gegen eine Ebene, fuer die es gar keine
        Detektoren gibt - und faende schlicht nie ein Setup.
        """
        available = set(self.timeframes.all)
        for label, timeframe in (
            ("strategy.setup_timeframe", self.strategy.setup_timeframe),
            ("strategy.confirmation_timeframe", self.strategy.confirmation_timeframe),
        ):
            if timeframe not in available:
                configured = ", ".join(tf.value for tf in self.timeframes.all)
                raise ValueError(
                    f"{label}={timeframe.value} ist in `timeframes` nicht enthalten "
                    f"(konfiguriert: {configured})"
                )
        if self.strategy.confirmation_timeframe.seconds > self.strategy.setup_timeframe.seconds:
            raise ValueError(
                "strategy.confirmation_timeframe muss kleiner oder gleich "
                "setup_timeframe sein - die Bestaetigung ist die feinere Ebene"
            )
        return self

    @model_validator(mode="after")
    def _base_timeframe_is_smallest(self) -> Config:
        smallest = min(self.timeframes.all, key=lambda tf: tf.seconds)
        if self.data.base_timeframe.seconds > smallest.seconds:
            raise ValueError(
                f"data.base_timeframe ({self.data.base_timeframe}) ist groesser als der "
                f"kleinste konfigurierte Timeframe ({smallest}) - Aggregation unmoeglich"
            )
        for tf in self.timeframes.all:
            if tf.seconds % self.data.base_timeframe.seconds != 0:
                raise ValueError(
                    f"Timeframe {tf} ist kein ganzzahliges Vielfaches von "
                    f"{self.data.base_timeframe} - Aggregation waere nicht exakt"
                )
        return self

    def path(self, relative: Path) -> Path:
        """Relative Konfigurationspfade gegen den Projekt-Root aufloesen."""
        return relative if relative.is_absolute() else PROJECT_ROOT / relative


# ------------------------------------------------------------------- Loader
def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Konfigurationsdatei nicht gefunden: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} enthaelt kein YAML-Mapping")
    return data


def load_config(path: Path | None = None) -> Config:
    """Konfiguration laden und validieren.

    Der Pfad kann ueber die Umgebungsvariable TRADEX_CONFIG ueberschrieben werden
    (z.B. fuer Backtest-Varianten in Phase 4).
    """
    resolved = path or Path(os.environ.get("TRADEX_CONFIG", DEFAULT_CONFIG_PATH))
    return Config.model_validate(_read_yaml(resolved))


def _parse_time(raw: str) -> time:
    hours, minutes = raw.split(":")
    return time(int(hours), int(minutes))


def _build_instrument(symbol: str, spec: dict[str, Any], defaults: dict[str, Any]) -> Instrument:
    hours = defaults["trading_hours"]
    sessions = tuple(
        SessionWindow(
            name=SessionName(name),
            start=_parse_time(window["start"]),
            end=_parse_time(window["end"]),
            crosses_midnight=bool(window.get("crosses_midnight", False)),
        )
        for name, window in defaults["sessions"].items()
    )
    return Instrument(
        symbol=symbol,
        name=spec["name"],
        exchange=defaults["exchange"],
        exchange_timezone=defaults["exchange_timezone"],
        currency=defaults["currency"],
        tick_size=float(spec["tick_size"]),
        tick_value=float(spec["tick_value"]),
        point_value=float(spec["point_value"]),
        contract_size=int(spec["contract_size"]),
        price_decimals=int(spec["price_decimals"]),
        databento_dataset=spec["databento_dataset"],
        databento_continuous=spec["databento_continuous"],
        contract_months=tuple(defaults["contract_months"]),
        trading_hours=TradingHours(
            week_open=WeekBoundary(
                weekday=int(hours["week_open"]["weekday"]),
                time=_parse_time(hours["week_open"]["time"]),
            ),
            week_close=WeekBoundary(
                weekday=int(hours["week_close"]["weekday"]),
                time=_parse_time(hours["week_close"]["time"]),
            ),
            daily_break=DailyBreak(
                start=_parse_time(hours["daily_break"]["start"]),
                end=_parse_time(hours["daily_break"]["end"]),
            ),
            daily_reset=_parse_time(hours["daily_reset"]),
        ),
        sessions=sessions,
        rth=SessionWindow(
            name=SessionName.NY_AM,
            start=_parse_time(defaults["rth"]["start"]),
            end=_parse_time(defaults["rth"]["end"]),
        ),
    )


def load_instruments(path: Path | None = None) -> dict[str, Instrument]:
    resolved = path or Path(os.environ.get("TRADEX_INSTRUMENTS", DEFAULT_INSTRUMENTS_PATH))
    raw = _read_yaml(resolved)
    defaults = raw["defaults"]
    return {
        symbol: _build_instrument(symbol, spec, defaults)
        for symbol, spec in raw["instruments"].items()
    }


@lru_cache(maxsize=1)
def get_config() -> Config:
    """Prozessweit zwischengespeicherte Konfiguration."""
    return load_config()


@lru_cache(maxsize=1)
def get_instruments() -> dict[str, Instrument]:
    return load_instruments()


def get_instrument(symbol: str) -> Instrument:
    instruments = get_instruments()
    try:
        return instruments[symbol.upper()]
    except KeyError as exc:
        known = ", ".join(sorted(instruments))
        raise KeyError(f"Unbekanntes Instrument {symbol!r}. Bekannt: {known}") from exc


def reset_caches() -> None:
    """Caches leeren - fuer Tests und fuer das Neuladen der Config im UI."""
    get_config.cache_clear()
    get_instruments.cache_clear()
