"""DTOs - der EINZIGE Vertrag zwischen Engine und Benutzeroberflaeche.

Das UI kennt ausschliesslich diese Typen. Es rechnet nichts selbst nach, es
stellt dar, was hier herauskommt (Spec §27: strikte Trennung von Trading Engine
und UI).

Begruendungen werden als Code + Parameter uebertragen, nie als fertiger Satz.
Uebersetzt wird in `ui/src/i18n/de.ts` (Spec §23).

Dieselben DTOs erzeugen die Snapshots, die als Regressionsbasis eingecheckt
werden - die JSON-Ausgabe ist damit stabil und diffbar.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from tradex.analysis.bias import BiasResult, TimeframeBias
from tradex.analysis.context import ContextSnapshot, TimeframeSnapshot
from tradex.analysis.displacement import Displacement
from tradex.analysis.fvg import Fvg
from tradex.analysis.liquidity import LiquidityPool, Sweep
from tradex.analysis.structure import StructureEvent
from tradex.analysis.swings import Swing
from tradex.backtest.execution import SimulatedTrade
from tradex.backtest.metrics import EquityPoint, Metrics
from tradex.backtest.report import BacktestReport
from tradex.data.integrity import Gap, IntegrityReport
from tradex.data.provider import ProviderCapabilities, ProviderHealth
from tradex.data.store import Coverage
from tradex.domain.bars import Bar, BarSeries
from tradex.domain.instruments import Instrument
from tradex.live.manager import BrokerState, ManagerState
from tradex.persistence.models import Reason
from tradex.strategy.setup import SetupCandidate
from tradex.strategy.signal import StrategyDecision, TradeSignal


class _Dto(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ------------------------------------------------------------------- Basis
class BarDto(_Dto):
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    roll_boundary: bool = False

    @classmethod
    def of(cls, bar: Bar) -> BarDto:
        return cls(
            ts=bar.ts,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            roll_boundary=bar.roll_boundary,
        )


class BarsDto(_Dto):
    symbol: str
    timeframe: str
    bars: tuple[BarDto, ...]
    forming: BarDto | None = None
    """Der Zwischenstand aus GESCHLOSSENEN Basis-Bars - nur zur Anzeige
    (Architektur-Invariante 1). Auf 1m ist das die zuletzt geschlossene Minute,
    auf 5m der Teil des Buckets, der aus fertigen Minuten besteht."""
    live: BarDto | None = None
    """Die Kerze, die sich GERADE bildet - aus Ticks, nie analysiert.

    Getrennt von `forming`, weil die beiden verschiedene Dinge sind: `forming`
    besteht aus Bars, die die Engine gesehen hat, `live` aus Ticks, die sie nie
    sehen wird. Liegt `live` im selben Bucket wie `forming`, ersetzt es dieses
    in der Anzeige; sonst kommt es dahinter. None heisst schlicht: kein
    laufender Kurs - dann sieht der Chart aus wie bisher."""

    @classmethod
    def of(
        cls,
        symbol: str,
        timeframe: str,
        series: BarSeries,
        forming: Bar | None = None,
        live: Bar | None = None,
    ) -> BarsDto:
        return cls(
            symbol=symbol,
            timeframe=timeframe,
            bars=tuple(BarDto.of(bar) for bar in series),
            forming=BarDto.of(forming) if forming else None,
            live=BarDto.of(live) if live else None,
        )


class ReasonDto(_Dto):
    code: str
    ok: bool
    params: dict[str, object] = {}

    @classmethod
    def of(cls, reason: Reason) -> ReasonDto:
        return cls(code=reason.code, ok=reason.ok, params=dict(reason.params))


# ------------------------------------------------------------ Analyseobjekte
class SwingDto(_Dto):
    index: int
    ts: int
    price: float
    type: str
    strength: int
    confirmed_at_index: int

    @classmethod
    def of(cls, swing: Swing) -> SwingDto:
        return cls(
            index=swing.index,
            ts=swing.ts,
            price=swing.price,
            type=swing.type.value,
            strength=swing.strength,
            confirmed_at_index=swing.confirmed_at_index,
        )


class FvgDto(_Dto):
    id: int
    direction: str
    created_index: int
    created_ts: int
    bottom: float
    top: float
    size_ticks: float
    state: str
    max_fill: float
    touched_index: int | None = None
    mitigated_index: int | None = None
    closed_ts: int | None = None

    @classmethod
    def of(cls, zone: Fvg) -> FvgDto:
        return cls(
            id=zone.id,
            direction=zone.direction.value,
            created_index=zone.created_index,
            created_ts=zone.created_ts,
            bottom=zone.bottom,
            top=zone.top,
            size_ticks=round(zone.size_ticks, 4),
            state=zone.state.value,
            max_fill=round(zone.max_fill, 4),
            touched_index=zone.touched_index,
            mitigated_index=zone.mitigated_index,
            closed_ts=zone.closed_ts,
        )


class LiquidityPoolDto(_Dto):
    id: int
    price: float
    side: str
    kind: str
    label: str
    strength: int
    state: str
    created_ts: int
    tapped_index: int | None = None

    @classmethod
    def of(cls, pool: LiquidityPool) -> LiquidityPoolDto:
        return cls(
            id=pool.id,
            price=pool.price,
            side=pool.side.value,
            kind=pool.kind.value,
            label=pool.label,
            strength=pool.strength,
            state=pool.state.value,
            created_ts=pool.created_ts,
            tapped_index=pool.tapped_index,
        )


class SweepDto(_Dto):
    pool_id: int
    pool_kind: str
    pool_price: float
    side: str
    direction: str
    penetration_ts: int
    reclaim_ts: int
    depth_ticks: float
    bars_to_reclaim: int

    @classmethod
    def of(cls, sweep: Sweep) -> SweepDto:
        return cls(
            pool_id=sweep.pool_id,
            pool_kind=sweep.pool_kind.value,
            pool_price=sweep.pool_price,
            side=sweep.side.value,
            direction=sweep.direction.value,
            penetration_ts=sweep.penetration_ts,
            reclaim_ts=sweep.reclaim_ts,
            depth_ticks=round(sweep.depth_ticks, 4),
            bars_to_reclaim=sweep.bars_to_reclaim,
        )


class StructureEventDto(_Dto):
    index: int
    ts: int
    type: str
    broken_price: float
    break_price: float
    previous_state: str
    new_state: str

    @classmethod
    def of(cls, event: StructureEvent) -> StructureEventDto:
        return cls(
            index=event.index,
            ts=event.ts,
            type=event.type.value,
            broken_price=event.broken_price,
            break_price=event.break_price,
            previous_state=event.previous_state.value,
            new_state=event.new_state.value,
        )


class DisplacementDto(_Dto):
    index: int
    ts: int
    direction: str
    range: float
    body_ratio: float
    range_atr_mult: float
    volume_ratio: float | None
    volume_confirmed: bool
    strength: float

    @classmethod
    def of(cls, item: Displacement) -> DisplacementDto:
        ratio = item.volume_ratio
        return cls(
            index=item.index,
            ts=item.ts,
            direction=item.direction.value,
            range=round(item.range, 6),
            body_ratio=round(item.body_ratio, 4),
            range_atr_mult=round(item.range_atr_mult, 4),
            volume_ratio=None if ratio != ratio else round(ratio, 4),
            volume_confirmed=item.volume_confirmed,
            strength=round(item.strength, 4),
        )


# -------------------------------------------------------------------- Bias
class TimeframeBiasDto(_Dto):
    timeframe: str
    score: float
    structure_score: float
    fvg_score: float
    liquidity_score: float
    structure_state: str
    active_bullish_fvgs: int
    active_bearish_fvgs: int
    nearest_buy_side: float | None
    nearest_sell_side: float | None

    @classmethod
    def of(cls, item: TimeframeBias) -> TimeframeBiasDto:
        return cls(
            timeframe=item.timeframe.value,
            score=round(item.score, 4),
            structure_score=round(item.structure_score, 4),
            fvg_score=round(item.fvg_score, 4),
            liquidity_score=round(item.liquidity_score, 4),
            structure_state=item.structure_state.value,
            active_bullish_fvgs=item.active_bullish_fvgs,
            active_bearish_fvgs=item.active_bearish_fvgs,
            nearest_buy_side=item.nearest_buy_side,
            nearest_sell_side=item.nearest_sell_side,
        )


class BiasDto(_Dto):
    bias: str
    score: float
    per_timeframe: tuple[TimeframeBiasDto, ...]
    reasons: tuple[ReasonDto, ...]

    @classmethod
    def of(cls, result: BiasResult) -> BiasDto:
        return cls(
            bias=result.bias.value,
            score=round(result.score, 4),
            per_timeframe=tuple(TimeframeBiasDto.of(item) for item in result.per_timeframe),
            reasons=tuple(ReasonDto.of(reason) for reason in result.reasons),
        )


# --------------------------------------------------------------- Snapshots
def _finite(value: float) -> float | None:
    """NaN/Inf sind kein gueltiges JSON - im UI wird daraus schlicht "noch unbekannt"."""
    return None if value != value or value in (float("inf"), float("-inf")) else round(value, 6)


def _rounded(value: float | None, digits: int) -> float | None:
    """Wie `round`, laesst None aber None - "nicht bestimmbar" ist keine Zahl."""
    return None if value is None else round(value, digits)


class TimeframeSnapshotDto(_Dto):
    timeframe: str
    bar_count: int
    ready: bool
    session: str
    atr: float | None
    volume_avg: float | None
    last_bar: BarDto | None
    structure_state: str
    last_structure_event: StructureEventDto | None
    last_mss: StructureEventDto | None
    swings: tuple[SwingDto, ...]
    active_fvgs: tuple[FvgDto, ...]
    untapped_pools: tuple[LiquidityPoolDto, ...]
    recent_sweeps: tuple[SweepDto, ...]
    last_displacement: DisplacementDto | None

    @classmethod
    def of(cls, snapshot: TimeframeSnapshot) -> TimeframeSnapshotDto:
        return cls(
            timeframe=snapshot.timeframe.value,
            bar_count=snapshot.bar_count,
            ready=snapshot.ready,
            session=snapshot.session,
            atr=_finite(snapshot.atr),
            volume_avg=_finite(snapshot.volume_avg),
            last_bar=BarDto.of(snapshot.last_bar) if snapshot.last_bar else None,
            structure_state=snapshot.structure_state.value,
            last_structure_event=(
                StructureEventDto.of(snapshot.last_structure_event)
                if snapshot.last_structure_event
                else None
            ),
            last_mss=StructureEventDto.of(snapshot.last_mss) if snapshot.last_mss else None,
            swings=tuple(SwingDto.of(s) for s in snapshot.swings),
            active_fvgs=tuple(FvgDto.of(z) for z in snapshot.active_fvgs),
            untapped_pools=tuple(LiquidityPoolDto.of(p) for p in snapshot.untapped_pools),
            recent_sweeps=tuple(SweepDto.of(s) for s in snapshot.recent_sweeps),
            last_displacement=(
                DisplacementDto.of(snapshot.last_displacement)
                if snapshot.last_displacement
                else None
            ),
        )


class ContextSnapshotDto(_Dto):
    symbol: str
    last_ts: int
    session: str
    bias: BiasDto
    timeframes: dict[str, TimeframeSnapshotDto]

    @classmethod
    def of(cls, snapshot: ContextSnapshot) -> ContextSnapshotDto:
        return cls(
            symbol=snapshot.symbol,
            last_ts=snapshot.last_ts,
            session=snapshot.session,
            bias=BiasDto.of(snapshot.bias),
            timeframes={
                tf.value: TimeframeSnapshotDto.of(item)
                for tf, item in sorted(
                    snapshot.timeframes.items(), key=lambda kv: kv[0].seconds, reverse=True
                )
            },
        )


# ------------------------------------------------------- Instrument / System
class InstrumentDto(_Dto):
    symbol: str
    name: str
    exchange: str
    currency: str
    tick_size: float
    tick_value: float
    point_value: float
    price_decimals: int
    live_capable: bool
    """Ob NinjaTrader dieses Instrument kennt (`nt8_symbol` gesetzt).

    Die Oberflaeche braucht das, um die Marktbeobachtung nur dort zu starten,
    wo sie ueberhaupt etwas liefern kann. Der Kontraktname selbst gehoert
    nicht in den UI-Vertrag - er ist eine Angelegenheit des Feeds."""

    @classmethod
    def of(cls, instrument: Instrument) -> InstrumentDto:
        return cls(
            symbol=instrument.symbol,
            name=instrument.name,
            exchange=instrument.exchange,
            currency=instrument.currency,
            tick_size=instrument.tick_size,
            tick_value=instrument.tick_value,
            point_value=instrument.point_value,
            price_decimals=instrument.price_decimals,
            live_capable=bool(instrument.nt8_symbol),
        )


class CoverageDto(_Dto):
    symbol: str
    timeframe: str
    first_ts: int
    last_ts: int
    bar_count: int

    @classmethod
    def of(cls, coverage: Coverage) -> CoverageDto:
        return cls(
            symbol=coverage.symbol,
            timeframe=coverage.timeframe.value,
            first_ts=coverage.first_ts,
            last_ts=coverage.last_ts,
            bar_count=coverage.bar_count,
        )


class GapDto(_Dto):
    start_ts: int
    end_ts: int
    missing_bars: int

    @classmethod
    def of(cls, gap: Gap) -> GapDto:
        return cls(start_ts=gap.start_ts, end_ts=gap.end_ts, missing_bars=gap.missing_bars)


class IntegrityDto(_Dto):
    symbol: str
    timeframe: str
    bar_count: int
    is_clean: bool
    missing_bars: int
    gaps: tuple[GapDto, ...]
    invalid_bars: tuple[int, ...]
    duplicate_timestamps: int

    @classmethod
    def of(cls, report: IntegrityReport) -> IntegrityDto:
        return cls(
            symbol=report.symbol,
            timeframe=report.timeframe.value,
            bar_count=report.bar_count,
            is_clean=report.is_clean,
            missing_bars=report.missing_bars,
            gaps=tuple(GapDto.of(g) for g in report.gaps),
            invalid_bars=report.invalid_bars,
            duplicate_timestamps=report.duplicate_timestamps,
        )


class ProviderDto(_Dto):
    name: str
    status: str
    detail: str
    historical_bars: bool
    live_bars: bool
    live_ticks: bool
    bid_ask: bool
    market_depth: bool
    volume: bool
    order_execution: bool
    notes: str

    @classmethod
    def of(cls, name: str, health: ProviderHealth, caps: ProviderCapabilities) -> ProviderDto:
        return cls(
            name=name,
            status=health.status.value,
            detail=health.detail,
            historical_bars=caps.historical_bars,
            live_bars=caps.live_bars,
            live_ticks=caps.live_ticks,
            bid_ask=caps.bid_ask,
            market_depth=caps.market_depth,
            volume=caps.volume,
            order_execution=caps.order_execution,
            notes=caps.notes,
        )


class HealthDto(_Dto):
    """System Health fuer das Dashboard (Spec §22, §24)."""

    ok: bool
    mode: str
    live_trading_enabled: bool
    symbol: str
    config_hash: str
    strategy_version: str
    display_timezone: str = "UTC"
    """IANA-Zeitzone fuer alle sichtbaren Zeitangaben (`app.display_timezone`).

    Gehoert in den Vertrag, weil die Oberflaeche sie sonst raten muesste - und
    ihre einzige Alternative waere UTC oder die Zeitzone des Browsers. Beides
    ist falsch: die eine zeigt 12:20, wo 14:20 gehandelt wird, die andere
    zeigt je nach Geraet etwas anderes. Intern bleibt alles UTC; lokalisiert
    wird ausschliesslich die Darstellung."""
    providers: tuple[ProviderDto, ...]
    warnings: tuple[str, ...] = ()


class LogEntryDto(_Dto):
    timestamp: str
    level: str
    event: str
    fields: dict[str, object] = {}


# ------------------------------------------------------------ Strategie (Ph. 3)
class SetupDto(_Dto):
    """Ein Setup-Kandidat samt Fortschritt entlang der Pflichtkette."""

    id: int
    direction: str
    stage: str
    created_ts: int
    sweep_price: float
    sweep_kind: str
    invalidation_price: float
    checklist: dict[str, bool]
    missing: tuple[str, ...]
    fvg_top: float | None = None
    fvg_bottom: float | None = None
    retracement_extreme: float | None = None
    displacement_strength: float | None = None

    @classmethod
    def of(cls, candidate: SetupCandidate) -> SetupDto:
        return cls(
            id=candidate.id,
            direction=candidate.direction.value,
            stage=candidate.stage.value,
            created_ts=candidate.created_ts,
            sweep_price=candidate.sweep.pool_price,
            sweep_kind=candidate.sweep.pool_kind.value,
            invalidation_price=candidate.invalidation_price,
            checklist=candidate.checklist(),
            missing=tuple(candidate.missing_conditions()),
            fvg_top=candidate.fvg.top if candidate.fvg else None,
            fvg_bottom=candidate.fvg.bottom if candidate.fvg else None,
            retracement_extreme=candidate.retracement_extreme,
            displacement_strength=(
                round(candidate.displacement.strength, 4) if candidate.displacement else None
            ),
        )


class TradeSignalDto(_Dto):
    """Ein durchgerechneter Einstieg. In Phase 3 wird er NICHT ausgefuehrt."""

    setup_id: int
    symbol: str
    strategy: str
    side: str
    entry: float
    stop: float
    target: float
    stop_ticks: float
    rr: float
    quantity: int
    risk_amount: float
    reward_amount: float
    entry_ts: int
    stop_anchor: str
    target_source: str

    @classmethod
    def of(cls, signal: TradeSignal) -> TradeSignalDto:
        return cls(
            setup_id=signal.setup_id,
            symbol=signal.symbol,
            strategy=signal.strategy,
            side=signal.side,
            entry=signal.entry,
            stop=signal.stop,
            target=signal.target,
            stop_ticks=round(signal.stop_ticks, 1),
            rr=round(signal.rr, 2),
            quantity=signal.quantity,
            risk_amount=round(signal.risk_amount, 2),
            reward_amount=round(signal.reward_amount, 2),
            entry_ts=signal.entry_ts,
            stop_anchor=signal.stop_anchor,
            target_source=signal.target_source,
        )


class StrategyDecisionDto(_Dto):
    """Eine protokollierte Entscheidung - mit oder ohne Trade (Spec §25)."""

    ts: int
    symbol: str
    timeframe: str
    setup_id: int
    direction: str
    decision: str
    stage: str
    htf_bias: str
    strategy: str
    checklist: dict[str, bool]
    missing: tuple[str, ...]
    blocking_reason: str
    reasons: tuple[ReasonDto, ...]
    signal: TradeSignalDto | None = None

    @classmethod
    def of(cls, decision: StrategyDecision) -> StrategyDecisionDto:
        return cls(
            ts=decision.ts,
            symbol=decision.symbol,
            timeframe=decision.timeframe,
            setup_id=decision.setup_id,
            direction=decision.direction.value,
            decision=decision.decision,
            stage=decision.stage,
            htf_bias=decision.htf_bias,
            strategy=decision.strategy,
            checklist=decision.checklist,
            missing=tuple(decision.missing),
            blocking_reason=decision.blocking_reason,
            reasons=tuple(ReasonDto.of(r) for r in decision.reasons),
            signal=TradeSignalDto.of(decision.signal) if decision.signal else None,
        )


class StrategyStateDto(_Dto):
    """Gesamtbild der Strategie fuer das Dashboard."""

    symbol: str
    enabled: bool
    setup_timeframe: str
    confirmation_timeframe: str
    stop_anchor: str
    min_rr: float
    active_setups: tuple[SetupDto, ...]
    recent_decisions: tuple[StrategyDecisionDto, ...]
    last_signal: TradeSignalDto | None
    decisions_total: int
    trades_total: int
    no_trades_total: int
    rejection_counts: dict[str, int]
    """Wie oft welcher Grund einen Trade verhindert hat - beantwortet die Frage
    'warum wird nicht gehandelt?' auf einen Blick."""


# ------------------------------------------------------------ Backtest (Ph. 4)
class MetricsDto(_Dto):
    """Kennzahlen einer Trade-Menge (Spec §19).

    `profit_factor`, `payoff_ratio` und `sqn` duerfen null sein - das heisst
    "aus dieser Stichprobe nicht bestimmbar", nicht "null".
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

    @classmethod
    def of(cls, metrics: Metrics) -> MetricsDto:
        return cls(
            trades=metrics.trades,
            wins=metrics.wins,
            losses=metrics.losses,
            scratches=metrics.scratches,
            win_rate=round(metrics.win_rate, 2),
            gross_profit=round(metrics.gross_profit, 2),
            gross_loss=round(metrics.gross_loss, 2),
            commission=round(metrics.commission, 2),
            net_pnl=round(metrics.net_pnl, 2),
            profit_factor=_rounded(metrics.profit_factor, 3),
            expectancy_r=round(metrics.expectancy_r, 4),
            expectancy_usd=round(metrics.expectancy_usd, 2),
            avg_win_r=round(metrics.avg_win_r, 4),
            avg_loss_r=round(metrics.avg_loss_r, 4),
            payoff_ratio=_rounded(metrics.payoff_ratio, 3),
            best_r=round(metrics.best_r, 3),
            worst_r=round(metrics.worst_r, 3),
            stdev_r=round(metrics.stdev_r, 4),
            sqn=_rounded(metrics.sqn, 3),
            max_drawdown_usd=round(metrics.max_drawdown_usd, 2),
            max_drawdown_pct=round(metrics.max_drawdown_pct, 3),
            max_drawdown_r=round(metrics.max_drawdown_r, 3),
            max_consecutive_wins=metrics.max_consecutive_wins,
            max_consecutive_losses=metrics.max_consecutive_losses,
            avg_bars_held=round(metrics.avg_bars_held, 1),
            avg_mae_r=round(metrics.avg_mae_r, 3),
            avg_mfe_r=round(metrics.avg_mfe_r, 3),
            avg_planned_rr=round(metrics.avg_planned_rr, 3),
            start_equity=round(metrics.start_equity, 2),
            final_equity=round(metrics.final_equity, 2),
            return_pct=round(metrics.return_pct, 3),
            first_ts=metrics.first_ts,
            last_ts=metrics.last_ts,
            unresolved=metrics.unresolved,
        )


class EquityPointDto(_Dto):
    ts: int
    trade_number: int
    equity: float
    drawdown: float

    @classmethod
    def of(cls, point: EquityPoint) -> EquityPointDto:
        return cls(
            ts=point.ts,
            trade_number=point.trade_number,
            equity=round(point.equity, 2),
            drawdown=round(point.drawdown, 2),
        )


class SimulatedTradeDto(_Dto):
    """Ein simulierter Trade - geplant und tatsaechlich gefuellt."""

    trade_id: int
    """Kontoweit eindeutig. `setup_id` zaehlt je Strategie und taugt deshalb
    nicht als Schluessel: Kette #5 und Opening-Range #5 waeren derselbe."""
    symbol: str
    """Ohne das ist die Trade-Liste einer Sitzung ueber mehrere Instrumente
    mehrdeutig - man saehe Ergebnisse, ohne zu wissen, wovon."""
    setup_id: int
    strategy: str
    side: str
    session: str
    quantity: int
    planned_entry: float
    entry_price: float
    exit_price: float
    stop: float
    target: float
    planned_rr: float
    entry_ts: int
    exit_ts: int
    bars_held: int
    exit_reason: str
    pnl: float
    commission: float
    r_multiple: float
    mae_r: float
    mfe_r: float
    stop_anchor: str
    target_source: str
    htf_bias: str

    @classmethod
    def of(cls, trade: SimulatedTrade) -> SimulatedTradeDto:
        return cls(
            trade_id=trade.trade_id,
            symbol=trade.symbol,
            setup_id=trade.setup_id,
            strategy=trade.strategy,
            side=trade.side,
            session=trade.session,
            quantity=trade.quantity,
            planned_entry=trade.planned_entry,
            entry_price=trade.entry_price,
            exit_price=trade.exit_price,
            stop=trade.stop,
            target=trade.target,
            planned_rr=round(trade.planned_rr, 2),
            entry_ts=trade.entry_ts,
            exit_ts=trade.exit_ts,
            bars_held=trade.bars_held,
            exit_reason=trade.exit_reason.value,
            pnl=round(trade.pnl, 2),
            commission=round(trade.commission, 2),
            r_multiple=round(trade.r_multiple, 3),
            mae_r=round(trade.mae_r, 3),
            mfe_r=round(trade.mfe_r, 3),
            stop_anchor=trade.stop_anchor,
            target_source=trade.target_source,
            htf_bias=trade.htf_bias,
        )


class BacktestReportDto(_Dto):
    """Vollstaendiges Backtest-Ergebnis fuer Anzeige und Archiv.

    `warnings` steht bewusst weit vorn im Vertrag: es sagt, ob die Kennzahlen
    ueberhaupt gelesen werden duerfen. Das UI zeigt sie oberhalb der Zahlen an.
    """

    symbol: str
    instrument_name: str
    base_timeframe: str
    bars: int
    first_ts: int
    last_ts: int
    backtest_version: str
    warnings: tuple[str, ...]
    is_significant: bool
    min_trades: int

    overall: MetricsDto
    in_sample: MetricsDto
    out_of_sample: MetricsDto

    by_strategy: dict[str, MetricsDto]
    by_symbol: dict[str, MetricsDto]
    by_session: dict[str, MetricsDto]
    by_direction: dict[str, MetricsDto]
    by_exit: dict[str, MetricsDto]
    by_stop_anchor: dict[str, MetricsDto]
    by_target_source: dict[str, MetricsDto]

    exit_counts: dict[str, int]
    rejections: dict[str, int]
    signals: int
    unfilled: int
    stale: int
    trades_total: int
    equity: tuple[EquityPointDto, ...]
    trades: tuple[SimulatedTradeDto, ...]
    assumptions: dict[str, object]

    @classmethod
    def of(cls, report: BacktestReport) -> BacktestReportDto:
        def table(items: dict[str, Metrics]) -> dict[str, MetricsDto]:
            return {name: MetricsDto.of(value) for name, value in items.items()}

        return cls(
            symbol=report.symbol,
            instrument_name=report.instrument_name,
            base_timeframe=report.base_timeframe,
            bars=report.bars,
            first_ts=report.first_ts,
            last_ts=report.last_ts,
            backtest_version=report.backtest_version,
            warnings=report.warnings,
            is_significant=report.is_significant,
            min_trades=report.min_trades,
            overall=MetricsDto.of(report.overall),
            in_sample=MetricsDto.of(report.in_sample),
            out_of_sample=MetricsDto.of(report.out_of_sample),
            by_strategy=table(report.by_strategy),
            by_symbol=table(report.by_symbol),
            by_session=table(report.by_session),
            by_direction=table(report.by_direction),
            by_exit=table(report.by_exit),
            by_stop_anchor=table(report.by_stop_anchor),
            by_target_source=table(report.by_target_source),
            exit_counts=report.exit_counts,
            rejections=report.rejections,
            signals=report.signals,
            unfilled=report.unfilled,
            stale=report.stale,
            trades_total=report.trades_total,
            equity=tuple(EquityPointDto.of(p) for p in report.equity),
            trades=tuple(SimulatedTradeDto.of(t) for t in report.trades),
            assumptions=dict(report.assumptions),
        )


class BacktestRunDto(_Dto):
    """Kopfzeile eines gespeicherten Laufs (Spec §21)."""

    id: int
    ts_utc: str
    symbol: str
    base_timeframe: str
    first_ts: int
    last_ts: int
    bars: int
    config_hash: str
    strategy_version: str
    backtest_version: str
    trades: int
    wins: int
    losses: int
    net_pnl: float
    expectancy_r: float
    profit_factor: float | None
    max_drawdown_pct: float
    notes: str = ""

class BrokerStateDto(_Dto):
    """Zustand der Orderanbindung.

    `ready` ist die einzige Zahl, auf die es beim Hinsehen ankommt: entstehen
    aus Signalen gerade echte Orders, oder nicht? Alles andere erklaert nur,
    warum.
    """

    enabled: bool
    provider: str
    connected: bool
    account: str
    is_paper: bool
    paper_evidence: str
    blocked_reason: str
    open_orders: int
    tradeable_symbols: tuple[str, ...]
    ready: bool

    @classmethod
    def of(cls, state: BrokerState) -> BrokerStateDto:
        return cls(
            enabled=state.enabled,
            provider=state.provider,
            connected=state.connected,
            account=state.account,
            is_paper=state.is_paper,
            paper_evidence=state.paper_evidence,
            blocked_reason=state.blocked_reason,
            open_orders=state.open_orders,
            tradeable_symbols=state.tradeable_symbols,
            ready=state.ready,
        )


class SessionStatusDto(_Dto):
    """Zustand des laufenden Betriebs (Spec Paragraph 22, Paragraph 24).

    `warnings` steht wie beim Backtest-Bericht bewusst weit vorn: sie sagen,
    ob den Zahlen daneben ueberhaupt zu trauen ist. Ein angehaltener Betrieb
    mit huebschem Kontostand sieht sonst aus wie ein laufender.
    """

    active: bool
    feed: str
    symbols: tuple[str, ...]
    session_id: int
    warnings: tuple[str, ...]
    stopped_by: str
    error: str

    running: bool
    connected: bool
    halted_reason: str
    accepts_entries: bool
    mode: str
    started_ts: int
    last_bar_ts: int
    last_message_ts: int
    bars_seen: int
    signals: int
    trades_closed: int
    open_positions: int
    start_equity: float
    equity: float
    realized_pnl: float
    day_pnl: float
    trading_day: int
    last_prices: dict[str, float]
    """Zuletzt gehandelte Kurse je Symbol - AUSSCHLIESSLICH zur Anzeige.

    Kommt aus den Ticks des Feeds und geht in keine Entscheidung ein
    (Invariante 1: analysiert wird nur auf geschlossenen Bars). Ohne diesen
    Wert steht die Anzeige zwischen zwei Minutenschluessen still, obwohl sich
    der Markt bewegt - und das sieht aus wie ein toter Feed."""
    broker: BrokerStateDto

    @classmethod
    def of(cls, state: ManagerState) -> SessionStatusDto:
        s = state.status
        return cls(
            broker=BrokerStateDto.of(state.broker),
            active=state.active,
            feed=state.feed,
            symbols=state.symbols,
            session_id=state.session_id,
            warnings=state.warnings,
            stopped_by=state.stopped_by,
            error=state.error,
            running=bool(s and s.running),
            connected=bool(s and s.connected),
            halted_reason=s.halted_reason if s else "",
            accepts_entries=bool(s and s.accepts_entries),
            mode=s.mode if s else "",
            started_ts=s.started_ts if s else 0,
            last_bar_ts=s.last_bar_ts if s else 0,
            last_message_ts=s.last_message_ts if s else 0,
            bars_seen=s.bars_seen if s else 0,
            signals=s.signals if s else 0,
            trades_closed=s.trades_closed if s else 0,
            open_positions=s.open_positions if s else 0,
            start_equity=s.start_equity if s else 0.0,
            equity=s.equity if s else 0.0,
            realized_pnl=s.realized_pnl if s else 0.0,
            day_pnl=s.day_pnl if s else 0.0,
            trading_day=s.trading_day if s else 0,
            last_prices=dict(state.last_prices),
        )


class SessionRunDto(_Dto):
    """Kopfzeile einer gespeicherten Sitzung."""

    id: int
    started_utc: str
    ended_utc: str | None
    mode: str
    feed: str
    symbols: str
    config_hash: str
    strategy_version: str
    backtest_version: str
    start_equity: float
    notes: str
    trades: int
    net_pnl: float
