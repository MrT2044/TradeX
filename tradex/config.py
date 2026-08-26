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
    IbkrContract,
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
    min_gap_bars: int = Field(default=5, ge=1)


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


class OpeningRangeConfig(_Frozen):
    """Opening Range Breakout - die zweite Strategie im Portfolio.

    Bewusst wenige Parameter. Jeder zusaetzliche Schalter ist eine weitere
    Dimension, in der man sich an die Vergangenheit anpassen kann.
    """

    enabled: bool = True
    timeframe: Timeframe = Timeframe.M5
    sessions: tuple[SessionName, ...] = (SessionName.LONDON, SessionName.NY_AM)
    range_minutes: int = Field(default=30, ge=1)
    min_range_atr_mult: float = Field(default=0.8, ge=0)
    """Unter dieser Spannenbreite waere ein Ausbruch nur Rauschen."""
    max_range_ticks: float = Field(default=400, gt=0)
    stop_buffer_ticks: float = Field(default=4, ge=0)
    target_range_mult: float = Field(default=3.0, gt=0)
    """Ziel als Vielfaches der Spannenbreite.

    Muss GROESSER als `risk.min_rr` sein. Der Stop liegt auf der Gegenseite der
    Spanne, das erreichbare CRV ist deshalb `mult * W / (W + Puffer)` und damit
    immer kleiner als `mult`. Ein Wert von 2.0 bei min_rr 2.0 macht die
    Strategie rechnerisch handlungsunfaehig - `risk/consistency.py` meldet das."""
    max_trades_per_session: int = Field(default=2, ge=1)
    max_tracked_sessions: int = Field(default=8, ge=1)

    @field_validator("sessions", mode="before")
    @classmethod
    def _parse_sessions(cls, value: Any) -> Any:
        if isinstance(value, list | tuple):
            return tuple(SessionName(str(item)) for item in value)
        return value


class StopsConfig(_Frozen):
    """Spec §11."""

    anchor: Literal["retracement", "sweep", "swing", "fvg"] = "retracement"
    buffer_atr_mult: float = Field(default=0.25, ge=0)
    buffer_min_ticks: float = Field(default=4, ge=0)

    #: Untergrenze in Ticks. Diese Seite darf absolut bleiben: das Tickraster
    #: ist absolut, und ein Stop von wenigen Ticks steckt unabhaengig vom
    #: Kursniveau im Rauschen.
    min_stop_ticks: float = Field(default=8, gt=0)

    #: Obergrenze als Vielfaches der ATR - NICHT in Ticks.
    #:
    #: Hier stand bis zum 25.08.2026 `max_stop_ticks: 240`, und das war ein
    #: Dimensionsfehler mit Ansage: die Zahl wurde bei MNQ-Kursen um 11.000-17.400
    #: kalibriert (Stopweiten damals 71-110 Ticks) und nie wieder angefasst.
    #: Bei 29.200 sind dieselben 240 Ticks nur noch 0,205 % des Kurses statt
    #: 0,545 %, waehrend die Eroeffnungsspanne allein 249-303 Ticks breit ist.
    #: Ergebnis: 21 von 21 Live-Entscheidungen mit `stop.too_wide` verworfen,
    #: drei Tage Papertrading ohne eine einzige Order - ohne Fehlermeldung, weil
    #: formal alles funktionierte.
    #:
    #: Ein ATR-Vielfaches kann so nicht driften: es waechst mit Kursniveau UND
    #: Volatilitaet. Das ist zugleich die inhaltlich richtigere Frage - ein
    #: weiter Stop in einem bewegten Markt ist normal, derselbe Stop in einem
    #: ruhigen Markt ist ein schlechtes Setup.
    #:
    #: WICHTIG: Der Wert wurde so gewaehlt, dass er das Verhalten einer
    #: 500-Tick-Grenze reproduziert (Median-ATR auf 5m lag 2025-2026 bei rund
    #: 76 Ticks). Er ist NICHT auf Rendite optimiert - der Backtest zeigt ueber
    #: alle geprueften Werte (240/320/400/500) ein Vertrauensband, das null
    #: einschliesst. Die Aenderung behebt einen Konfigurationsfehler, sie
    #: erzeugt keinen Edge.
    max_stop_atr_mult: float = Field(default=6.0, gt=0)


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

    #: Sperrfrist nach JEDEM geschlossenen Trade, in Minuten Marktzeit. 0 = aus.
    #: Gemessen an der Bar-Zeit und nicht an der Wanduhr - sonst waere die
    #: Sperre im Backtest wirkungslos und im Live-Betrieb wirksam, und beide
    #: waeren nicht mehr vergleichbar (Invariante 3).
    cooldown_minutes_after_trade: float = Field(default=0.0, ge=0)

    #: Zusaetzliche, laengere Sperrfrist nach einem VERLUST. 0 = aus.
    #: Bewusst getrennt: nach einem Stop ist die Marktlage haeufig genau die,
    #: die den Stop ausgeloest hat - der naechste Vorschlag entsteht dann aus
    #: demselben Geschehen.
    cooldown_minutes_after_loss: float = Field(default=0.0, ge=0)

    @property
    def risk_per_trade_amount(self) -> float:
        return self.account_size * self.risk_per_trade_pct / 100.0

    @property
    def max_daily_loss_amount(self) -> float:
        return self.account_size * self.max_daily_loss_pct / 100.0


class BacktestConfig(_Frozen):
    """Spec §19: die Ausfuehrungsannahmen des Backtests.

    Sie sind bewusst pessimistisch gewaehlt. Ein Backtest, der guenstiger fuellt
    als die Realitaet, produziert genau die Zahlen, die man sehen moechte - und
    ist damit wertlos. Jede Annahme steht hier explizit, statt im Simulator
    versteckt zu sein.
    """

    #: Wo der Einstieg gefuellt wird. Das Signal entsteht am SCHLUSS der
    #: Bestaetigungsbar - zu diesem Kurs kann man real nicht mehr kaufen.
    #: `next_bar_open` ist deshalb der ehrliche Fall; `signal_close` existiert
    #: nur, um den Unterschied messen zu koennen.
    entry_fill: Literal["next_bar_open", "signal_close"] = "next_bar_open"

    #: Liegen Stop UND Ziel innerhalb derselben Bar, sagen OHLC-Daten nicht,
    #: was zuerst kam. `stop_first` nimmt den schlechteren Fall an.
    same_bar_resolution: Literal["stop_first", "target_first"] = "stop_first"

    entry_slippage_ticks: float = Field(default=1.0, ge=0)
    #: Der Stop ist eine Market-Order und rutscht. Das Ziel ist eine
    #: Limit-Order: sie fuellt zum Kurs oder gar nicht - deshalb ohne Schlupf.
    stop_slippage_ticks: float = Field(default=1.0, ge=0)
    commission_per_contract: float = Field(default=0.74, ge=0)
    """USD je Kontrakt und Round Turn (Ein- plus Ausstieg)."""

    #: Zeitstop in Basis-Bars. 0 schaltet ihn ab. Ohne ihn koennen Positionen
    #: ueber Tage offen bleiben, was zu einem Intraday-Modell nicht passt.
    max_holding_bars: int = Field(default=480, ge=0)

    #: Hoechstabstand in Basis-Intervallen zwischen der Bar, auf der das Signal
    #: entstand, und der Bar, auf der gefuellt wird.
    #:
    #: Im Normalfall sind es genau zwei: eine Bar, bis die Signalbar ueberhaupt
    #: als geschlossen gilt (Invariante 1), und eine bis zur Fuellung. Mehr
    #: bedeutet immer eine Luecke - Feiertag, Handelsunterbrechung, fehlende
    #: Daten. Eine Order ueber so eine Luecke hinweg zu fuellen erfindet einen
    #: Einstieg, den es nicht gab: der ausloesende Kursverlauf liegt dann
    #: Stunden zurueck. Solche Signale werden verworfen, nicht gehandelt.
    max_signal_age_bars: int = Field(default=2, ge=1)

    #: Unterhalb dieser Trade-Anzahl weist der Bericht seine Kennzahlen als
    #: nicht belastbar aus, statt sie kommentarlos zu zeigen.
    min_trades_for_significance: int = Field(default=30, ge=1)

    #: Anteil des Zeitraums am Ende, der getrennt ausgewertet wird. Stimmen
    #: erster und zweiter Abschnitt nicht ueberein, ist das Ergebnis vermutlich
    #: an den Zeitraum angepasst und nicht an den Markt.
    out_of_sample_fraction: float = Field(default=0.3, ge=0, lt=1)

    #: So viele Trades und Equity-Punkte gehen hoechstens in einen Bericht.
    #: Schuetzt UI und JSON-Ausgabe vor Berichten mit Zehntausenden Zeilen.
    max_report_trades: int = Field(default=500, ge=1)


class PatternsConfig(_Frozen):
    """Musterstatistik: bedingte Verteilungen ueber die Trades eines Laufs.

    Die Werte hier entscheiden nicht, WAS gehandelt wird - sie entscheiden,
    ab wann eine Auffaelligkeit ueberhaupt berichtet werden darf.
    """

    #: Zulaessige Rate falscher Funde nach Benjamini-Hochberg.
    alpha: float = Field(default=0.05, gt=0, lt=1)

    #: Mindestgroesse einer Untergruppe, damit sie getestet wird. Darunter ist
    #: der t-Test eine Schaetzung mit eigener Unsicherheit, und jede getestete
    #: Gruppe verschaerft ausserdem die Korrektur fuer alle anderen.
    min_trades: int = Field(default=30, ge=2)

    #: Mindestgroesse derselben Gruppe im hinteren Abschnitt, damit deren
    #: Vorzeichen als Gegenprobe zaehlt.
    min_out_of_sample_trades: int = Field(default=10, ge=1)


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


class IbkrConfig(_Frozen):
    """Verbindungsdaten fuer IB Gateway.

    `paper_port` und `live_port` stehen beide hier, damit der Unterschied
    sichtbar ist - der Adapter waehlt aber niemals `live_port`, solange nicht
    `execution.mode` ein Live-Modus UND `execution.live_trading_enabled` gesetzt
    ist. Der Port allein ist ohnehin kein Beweis fuer ein Paper-Konto; er ist
    nur die erste von mehreren Pruefungen.
    """

    host: str = "127.0.0.1"
    paper_port: int = Field(default=4002, ge=1, le=65535)
    live_port: int = Field(default=4001, ge=1, le=65535)
    client_id: int = Field(default=10, ge=0)

    #: Kontonummern, die ausdruecklich erlaubt sind. Das ist der EINZIGE harte
    #: Paper-Nachweis, den die TWS-API zulaesst - sie kennt kein Feld "ist
    #: Paper". Leer bedeutet: das Praefix unten muss passen.
    allowed_accounts: tuple[str, ...] = ()

    #: Fallback, wenn `allowed_accounts` leer ist. "DU" ist IBKRs feste
    #: Konvention fuer Einzel-Paper-Konten, "DF" fuer Advisor-Paper. Es gibt
    #: dafuer keine dokumentierte API-Zusicherung, deshalb ist die Allowlist
    #: die belastbarere Einstellung.
    paper_account_prefixes: tuple[str, ...] = ("DU", "DF")

    #: Jeden Kontrakt vor dem Handel ueber `reqContractDetails` aufloesen.
    #: Ausschalten heisst raten - und ein falsch geratener Future ist ein
    #: stiller Fehler, der erst in der Abrechnung auffaellt.
    require_contract_details: bool = True

    #: Duerfen Stop und Ziel ausserhalb der Kernhandelszeit ausloesen? Bei
    #: Futures laeuft der Handel fast rund um die Uhr; ein Stop, der um 23 Uhr
    #: nicht ausloest, ist kein Stop. Steht trotzdem hier und nicht als
    #: Konstante im Adapter - wer es abschaltet, soll es sehen koennen.
    outside_rth: bool = True

    connect_timeout_seconds: float = Field(default=15.0, gt=0)

    @model_validator(mode="after")
    def _ports_differ(self) -> IbkrConfig:
        if self.paper_port == self.live_port:
            raise ValueError(
                "broker.ibkr.paper_port und live_port duerfen nicht gleich sein - "
                "sonst laesst sich Paper nicht von Live unterscheiden"
            )
        return self


class LiveConfig(_Frozen):
    """Betriebsparameter des Livebetriebs - keine Handelsregeln.

    Steht getrennt von `backtest:`, weil nichts davon im Backtest existiert:
    Wanduhrzeit, Verbindungen, Wartezeiten. Ein Backtest, der diese Werte
    laese, waere von der Tagesform der Maschine abhaengig.
    """

    #: Wie viele Tage Historie beim Start einer NT8-Sitzung aus NinjaTrader
    #: nachgeladen werden. Zur Einordnung des laufenden Kurses - der Chart
    #: faengt sonst leer an und man sieht tagelang nicht, wo man steht. Null
    #: schaltet das Nachladen ab.
    nt8_history_days: int = Field(default=3, ge=0, le=30)

    #: Wie lange auf `history_end` gewartet wird, bevor die Sitzung ohne
    #: Historie weiterlaeuft. Ohne Deckel haengt der Betrieb an einem AddOn,
    #: das den Befehl vielleicht gar nicht kennt - und handelt nie.
    nt8_history_timeout_seconds: float = Field(default=30.0, gt=0)

    #: Wie lange ein Tick die laufende Kerze noch bewegen darf. Danach gilt der
    #: Kurs als veraltet und die Anzeige-Bar verschwindet, statt eine Bewegung
    #: vorzutaeuschen, die es nicht mehr gibt. Eine stehende Kerze, die wie eine
    #: laufende aussieht, ist die gefaehrlichere Anzeige - sie sieht nach Markt
    #: aus, wo in Wirklichkeit die Verbindung weg ist.
    display_tick_max_age_seconds: float = Field(default=15.0, gt=0)


class Nt8Config(_Frozen):
    """Orderanbindung ueber die NinjaTrader-Bridge (Phase 9).

    Auffaellig kurz im Vergleich zu `IbkrConfig` - und das ist der Punkt. Dort
    brauchte es Ports, Praefixe und eine Allowlist, weil die TWS-API kein Feld
    "dies ist ein Paper-Konto" kennt und der Nachweis aus mehreren indirekten
    Hinweisen zusammengesetzt werden musste. Hier entscheidet
    `Account.Provider == Provider.Simulator`, eine Eigenschaft des Kontos, und
    zwar im AddOn.

    **Es gibt keinen Schalter, der das aushebelt.** Nicht hier, nicht in
    `.env`, nicht auf der Kommandozeile.
    """

    host: str = "127.0.0.1"

    #: Derselbe Socket wie die Marktdaten. Der Adapter macht trotzdem eine
    #: eigene Verbindung auf - Begruendung in `broker/nt8/adapter.py`.
    port: int = Field(default=39473, ge=1, le=65535)

    #: Gewuenschtes Konto. Leer heisst "das einzige Simulationskonto"; gibt es
    #: mehrere, lehnt das AddOn ab statt zu waehlen. `Backtest` ist naemlich
    #: ebenfalls Provider.Simulator - und hat net_liquidation 0.
    account: str = "Sim101"

    #: Zusaetzliche Einschraenkung. Sie kann ein Simulationskonto ausschliessen,
    #: aber NIE eines freischalten, das keines ist - sonst waere sie ein
    #: Schalter an der Kontosperre vorbei. Leer heisst: keine weitere
    #: Einschraenkung, die Simulator-Pruefung traegt allein.
    allowed_accounts: tuple[str, ...] = ()

    connect_timeout_seconds: float = Field(default=10.0, gt=0)


class BrokerConfig(_Frozen):
    """Orderanbindung (Spec Paragraph 24, Phase 8).

    Standardmaessig AUS. Der Betrieb ohne Broker ist der bisherige: Signale
    werden intern durchsimuliert und es fliesst nichts. Erst `enabled: true`
    laesst ueberhaupt Orders entstehen - und auch dann noch muss die gesamte
    Pruefkette in `tradex/broker/guard.py` zustimmen.
    """

    enabled: bool = False
    provider: Literal["ibkr", "nt8"] = "nt8"
    """Welche Anbindung Orders sendet.

    `nt8` seit Phase 9: Marktdaten und Ausfuehrung kommen damit aus demselben
    System, und der Paper-Nachweis ist direkt statt indirekt. `ibkr` bleibt
    waehlbar, bis der Ersatz nachweislich traegt (A7 loescht ihn).
    """

    #: Wie alt die Bar sein darf, aus der ein Signal stammt, damit daraus noch
    #: eine Order werden kann - Wanduhrzeit, nicht Bar-Abstand. Steht bewusst
    #: hier und nicht unter `risk:`: das Risikomodell teilt sich der Backtest,
    #: und dort ist Wanduhrzeit bedeutungslos.
    max_data_age_seconds: float = Field(default=5.0, gt=0)

    max_orders_per_minute: int = Field(default=6, ge=1)

    #: Wie lange eine gesendete Order ohne Rueckmeldung des Brokers gelten darf,
    #: bevor sie als verloren gilt.
    order_timeout_seconds: float = Field(default=30.0, gt=0)

    #: Wartezeiten zwischen Wiederverbindungsversuchen. Der letzte Wert wird
    #: danach wiederholt.
    reconnect_delays_seconds: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0)

    ibkr: IbkrConfig = IbkrConfig()
    nt8: Nt8Config = Nt8Config()

    @model_validator(mode="after")
    def _reconnect_delays_are_positive(self) -> BrokerConfig:
        if not self.reconnect_delays_seconds:
            raise ValueError("broker.reconnect_delays_seconds darf nicht leer sein")
        if any(delay <= 0 for delay in self.reconnect_delays_seconds):
            raise ValueError("broker.reconnect_delays_seconds muessen alle > 0 sein")
        return self


class NewsConfig(_Frozen):
    """Spec Paragraph 14/15: Sperrfenster um Wirtschaftstermine.

    Der Filter kann nur EINSTIEGE verhindern. Ausstiege bleiben immer moeglich -
    eine offene Position ohne Stop waere das Gegenteil von Risikosenkung. Das
    ist keine Einstellung, sondern eine Eigenschaft der Architektur:
    `tradex/news/` kennt die Ausfuehrung gar nicht.
    """

    enabled: bool = False

    #: Wo die abgerufenen Termine liegen. Die Engine liest NUR diese Datei,
    #: nie eine API - sonst waere eine Entscheidung nicht wiederholbar
    #: (Invariante 2) und Backtest und Live saehen Verschiedenes.
    store: Path = Path("data/news/events.jsonl")

    #: Ab welcher Wucht gesperrt wird.
    min_impact: Literal["low", "medium", "high"] = "high"

    #: Welche Laender zaehlen. Leer = alle. Fuer Nasdaq/S&P ist USD relevant;
    #: ein CPI aus Neuseeland bewegt den MNQ nicht.
    countries: tuple[str, ...] = ("USD",)

    block_before_minutes: int = Field(default=15, ge=0)
    block_after_minutes: int = Field(default=15, ge=0)

    #: Aufschlag auf beide Seiten, wenn die Uhrzeit nicht gemeldet, sondern aus
    #: der ueblichen Veroeffentlichungszeit ergaenzt wurde. Ohne diesen
    #: Aufschlag taeuschte das Fenster eine Genauigkeit vor, die die Quelle
    #: nicht hat.
    assumed_time_extra_minutes: int = Field(default=15, ge=0)

    #: Was mit Terminen geschieht, von denen nur der TAG bekannt ist.
    #: `block_day` sperrt den ganzen UTC-Tag - drastisch, aber ehrlich.
    #: `ignore` laesst sie weg und zaehlt sie als uebersprungen.
    day_only_policy: Literal["ignore", "block_day"] = "ignore"

    #: Was gilt, wenn fuer einen Zeitpunkt gar keine Termine vorliegen.
    #: `warn` handelt weiter und vermerkt es in jeder Entscheidung;
    #: `block` handelt nicht. Fuer den Live-Betrieb ist `block` die sichere
    #: Wahl, fuer Backtests ueber Zeitraeume ohne Kalenderdaten unbrauchbar.
    on_missing_data: Literal["warn", "block"] = "warn"


# --------------------------------------------------------------------- Root
class Config(_Frozen):
    version: int
    app: AppConfig
    data: DataConfig
    timeframes: TimeframesConfig
    analysis: AnalysisConfig
    strategy: StrategyConfig = StrategyConfig()
    opening_range: OpeningRangeConfig = OpeningRangeConfig()
    stops: StopsConfig = StopsConfig()
    targets: TargetsConfig = TargetsConfig()
    trading_windows: TradingWindowsConfig = TradingWindowsConfig()
    risk: RiskConfig = RiskConfig()
    backtest: BacktestConfig = BacktestConfig()
    patterns: PatternsConfig = PatternsConfig()
    execution: ExecutionConfig = ExecutionConfig()
    live: LiveConfig = LiveConfig()
    broker: BrokerConfig = BrokerConfig()
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
        if self.opening_range.enabled and self.opening_range.timeframe not in available:
            configured = ", ".join(tf.value for tf in self.timeframes.all)
            raise ValueError(
                f"opening_range.timeframe={self.opening_range.timeframe.value} ist in "
                f"`timeframes` nicht enthalten (konfiguriert: {configured})"
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


def resolved_config_path() -> Path:
    """Die Datei, aus der `load_config()` ohne Argument liest.

    Wer den `config_hash` speichert, MUSS diesen Pfad benutzen und nicht
    `default.yaml` annehmen: sonst traegt ein Lauf unter einer
    Variantenkonfiguration den Hash der Standardkonfiguration - und das Archiv
    behauptet etwas, das nie gerechnet wurde.
    """
    return Path(os.environ.get("TRADEX_CONFIG", DEFAULT_CONFIG_PATH))


def load_config(path: Path | None = None) -> Config:
    """Konfiguration laden und validieren.

    Der Pfad kann ueber die Umgebungsvariable TRADEX_CONFIG ueberschrieben werden
    (z.B. fuer Backtest-Varianten in Phase 4).
    """
    return Config.model_validate(_read_yaml(path or resolved_config_path()))


def _parse_time(raw: str) -> time:
    hours, minutes = raw.split(":")
    return time(int(hours), int(minutes))


def _build_ibkr_contract(spec: dict[str, Any], defaults: dict[str, Any]) -> IbkrContract | None:
    """Den IBKR-Kontrakt aus dem Instrumenteintrag lesen.

    Fehlt der Block, ist das Instrument bei IBKR nicht handelbar - das ist ein
    gueltiger Zustand (Proxy- und Demodaten haben dort keinen Gegenpart) und
    wird spaeter als Ablehnung gemeldet, nicht als Fehler beim Laden.
    """
    raw = spec.get("ibkr")
    if not raw:
        return None
    return IbkrContract(
        symbol=str(raw.get("symbol", "")),
        sec_type=str(raw.get("sec_type", "FUT")),
        exchange=str(raw.get("exchange", "")),
        currency=str(raw.get("currency", defaults.get("currency", "USD"))),
        expiry=str(raw.get("expiry", "")),
        multiplier=str(raw.get("multiplier", "")),
        local_symbol=str(raw.get("local_symbol", "")),
        trading_class=str(raw.get("trading_class", "")),
    )


def _build_instrument(symbol: str, spec: dict[str, Any], defaults: dict[str, Any]) -> Instrument:
    # Handelszeiten und Sessions duerfen je Instrument ueberschrieben werden.
    # Noetig, weil nicht jedes Instrument den CME-Zeiten folgt: der
    # Nasdaq-100-Index-CFD pausiert z.B. 15:15-17:05 statt 16:00-17:00.
    # Ohne Ueberschreibung wuerde die Integritaetspruefung dessen normale
    # Handelspause taeglich als Datenluecke melden.
    hours = {**defaults["trading_hours"], **spec.get("trading_hours", {})}
    session_spec = {**defaults["sessions"], **spec.get("sessions", {})}
    sessions = tuple(
        SessionWindow(
            name=SessionName(name),
            start=_parse_time(window["start"]),
            end=_parse_time(window["end"]),
            crosses_midnight=bool(window.get("crosses_midnight", False)),
        )
        for name, window in session_spec.items()
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
        dukascopy_symbol=spec.get("dukascopy_symbol", ""),
        nt8_symbol=spec.get("nt8_symbol", ""),
        ibkr=_build_ibkr_contract(spec, defaults),
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
