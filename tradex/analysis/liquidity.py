"""Liquiditaet und Sweeps (Spec §7 Schritt 2).

Was ein Liquiditaetslevel ist
-----------------------------
Ein Kursniveau, an dem sich mit hoher Wahrscheinlichkeit Stop-Orders sammeln:

    BUY_SIDE   ueber dem Preis  - Stops der Shorts, Ziel bearisher Sweeps
    SELL_SIDE  unter dem Preis  - Stops der Longs,  Ziel bullisher Sweeps

Quellen (alle einzeln abschaltbar):
    SWING       jeder bestaetigte Swing High / Low
    EQUAL       mehrere Swings innerhalb equal_tolerance_ticks (Equal Highs/Lows)
    SESSION     High/Low einer abgeschlossenen Session (Asia, London, NY AM, NY PM)
    PRIOR_DAY   High/Low des vorherigen Handelstages
    PRIOR_WEEK  High/Low der vorherigen Handelswoche

Sweep - die objektive Definition
--------------------------------
Ein Sweep ist NICHT einfach "Preis war unter dem Tief". Er besteht aus zwei
Teilen, die beide erfuellt sein muessen:

    1. Durchstich   low[i] < level - min_penetration_ticks   (bzw. high[i] > level + ...)
    2. Rueckeroberung  close kehrt innerhalb max_reclaim_bars auf die
                       Ursprungsseite des Levels zurueck

Genau diese Kombination unterscheidet den Sweep (Liquiditaet geholt, Preis
abgewiesen) vom Ausbruch (Level durchhandelt, Preis bleibt drueben). Bleibt die
Rueckeroberung aus, gilt das Level als durchhandelt: der Pool wechselt auf SWEPT,
aber es entsteht KEIN Sweep-Ereignis.
"""

from __future__ import annotations

from dataclasses import dataclass

from tradex.analysis.swings import Swing
from tradex.config import LiquidityParams, SweepParams
from tradex.domain.bars import BarSeries
from tradex.domain.enums import (
    Direction,
    LiquidityKind,
    LiquiditySide,
    LiquidityState,
    SessionName,
    SwingType,
)


@dataclass(slots=True)
class LiquidityPool:
    """Ein Liquiditaetslevel."""

    id: int
    price: float
    side: LiquiditySide
    kind: LiquidityKind
    created_index: int
    created_ts: int
    label: str = ""
    strength: int = 1
    """Anzahl der Swings, die dieses Level bilden. Nur bei EQUAL > 1."""
    state: LiquidityState = LiquidityState.UNTAPPED
    tapped_index: int | None = None
    source_indices: tuple[int, ...] = ()

    @property
    def is_untapped(self) -> bool:
        return self.state is LiquidityState.UNTAPPED

    @property
    def is_buy_side(self) -> bool:
        return self.side is LiquiditySide.BUY_SIDE


@dataclass(frozen=True, slots=True)
class Sweep:
    """Ein vollstaendiger Sweep: Durchstich plus Rueckeroberung."""

    pool_id: int
    pool_kind: LiquidityKind
    pool_price: float
    side: LiquiditySide
    direction: Direction
    """Richtung des SETUPS, das der Sweep ermoeglicht.

    Ein Sweep der Sell-Side (Tiefs geholt) leitet ein bullishes Setup ein.
    """
    penetration_index: int
    penetration_ts: int
    reclaim_index: int
    reclaim_ts: int
    depth_ticks: float
    bars_to_reclaim: int


@dataclass(slots=True)
class _Pending:
    """Ein Durchstich, dessen Rueckeroberung noch aussteht."""

    pool: LiquidityPool
    penetration_index: int
    extreme_price: float


@dataclass(slots=True)
class _Extremes:
    """High/Low eines laufenden Zeitabschnitts."""

    high: float = float("-inf")
    low: float = float("inf")
    high_index: int = -1
    low_index: int = -1

    def update(self, high: float, low: float, index: int) -> None:
        if high > self.high:
            self.high = high
            self.high_index = index
        if low < self.low:
            self.low = low
            self.low_index = index

    @property
    def valid(self) -> bool:
        return self.high_index >= 0 and self.low_index >= 0

    def reset(self) -> None:
        self.high = float("-inf")
        self.low = float("inf")
        self.high_index = -1
        self.low_index = -1


class LiquidityTracker:
    """Inkrementelle Verwaltung von Liquiditaetsleveln und Sweeps."""

    __slots__ = (
        "params",
        "sweep_params",
        "tick_size",
        "pools",
        "sweeps",
        "_next_id",
        "_pending",
        "_day",
        "_week",
        "_session",
        "_current_day",
        "_current_week",
        "_current_session",
        "_swing_history",
    )

    def __init__(
        self, params: LiquidityParams, sweep_params: SweepParams, tick_size: float
    ) -> None:
        self.params = params
        self.sweep_params = sweep_params
        self.tick_size = tick_size
        self.pools: list[LiquidityPool] = []
        self.sweeps: list[Sweep] = []
        self._next_id = 1
        self._pending: dict[int, _Pending] = {}
        self._day = _Extremes()
        self._week = _Extremes()
        self._session = _Extremes()
        self._current_day: object | None = None
        self._current_week: object | None = None
        self._current_session: str | None = None
        # Rohe Swing-Historie je Seite. Sie ist die Quelle fuer Equal Highs/Lows
        # und bewusst getrennt von `pools`: dort werden nahe beieinander liegende
        # Level zusammengefasst, und genau diese Information braucht das Clustering.
        self._swing_history: dict[SwingType, list[Swing]] = {
            SwingType.HIGH: [],
            SwingType.LOW: [],
        }

    # ------------------------------------------------------------------- API
    def update(
        self,
        series: BarSeries,
        index: int,
        new_swings: list[Swing],
        session: str,
        trading_day: object,
        trading_week: object,
    ) -> list[Sweep]:
        """Bar `index` verarbeiten.

        Reihenfolge ist bewusst gewaehlt:
            1. Zeitabschnitte abschliessen (erzeugt Session-/Tages-/Wochenlevel)
            2. neue Swing- und Equal-Level aus bestaetigten Swings
            3. Sweeps gegen den JETZIGEN Levelbestand pruefen
        Ein Level, das dieselbe Bar gerade erst erzeugt hat, kann von ihr nicht
        schon gesweept werden - das verhindert Schritt 1 vor Schritt 3.
        """
        self._roll_periods(series, index, session, trading_day, trading_week)
        if new_swings:
            self._remember_swings(new_swings)
            if self.params.include_swing_levels:
                self._add_swing_pools(new_swings)
            self._add_equal_pools(new_swings)
        sweeps = self._check_sweeps(series, index)
        self._update_extremes(series, index)
        self._prune()
        return sweeps

    # ------------------------------------------------- Zeitabschnitte
    def _roll_periods(
        self,
        series: BarSeries,
        index: int,
        session: str,
        trading_day: object,
        trading_week: object,
    ) -> None:
        if self._current_week is None:
            self._current_week = trading_week
        elif trading_week != self._current_week:
            if self.params.include_prior_week and self._week.valid:
                self._add_period_pools(series, index, self._week, LiquidityKind.PRIOR_WEEK, "PWH", "PWL")
            self._week.reset()
            self._current_week = trading_week

        if self._current_day is None:
            self._current_day = trading_day
        elif trading_day != self._current_day:
            if self.params.include_prior_day and self._day.valid:
                self._add_period_pools(series, index, self._day, LiquidityKind.PRIOR_DAY, "PDH", "PDL")
            self._day.reset()
            self._current_day = trading_day

        if self._current_session is None:
            self._current_session = session
        elif session != self._current_session:
            completed = self._current_session
            if (
                self.params.include_session_levels
                and self._session.valid
                and completed != SessionName.CLOSED.value
            ):
                self._add_period_pools(
                    series,
                    index,
                    self._session,
                    LiquidityKind.SESSION,
                    f"{completed}_high",
                    f"{completed}_low",
                )
            self._session.reset()
            self._current_session = session

    def _add_period_pools(
        self,
        series: BarSeries,
        index: int,
        extremes: _Extremes,
        kind: LiquidityKind,
        high_label: str,
        low_label: str,
    ) -> None:
        self._add_pool(
            series, index, extremes.high, LiquiditySide.BUY_SIDE, kind, high_label,
            source=(extremes.high_index,)
        )
        self._add_pool(
            series, index, extremes.low, LiquiditySide.SELL_SIDE, kind, low_label,
            source=(extremes.low_index,)
        )

    def _update_extremes(self, series: BarSeries, index: int) -> None:
        high = float(series.high[index])
        low = float(series.low[index])
        self._day.update(high, low, index)
        self._week.update(high, low, index)
        self._session.update(high, low, index)

    # ------------------------------------------------------------ Swing-Level
    def _remember_swings(self, swings: list[Swing]) -> None:
        keep = max(self.params.equal_lookback_swings, 1)
        for swing in swings:
            history = self._swing_history[swing.type]
            history.append(swing)
            if len(history) > keep:
                del history[: len(history) - keep]

    def _add_swing_pools(self, swings: list[Swing]) -> None:
        for swing in swings:
            side = (
                LiquiditySide.BUY_SIDE
                if swing.type is SwingType.HIGH
                else LiquiditySide.SELL_SIDE
            )
            self._add_pool_at(
                price=swing.price,
                side=side,
                kind=LiquidityKind.SWING,
                created_index=swing.confirmed_at_index,
                created_ts=swing.ts,
                label="swing_high" if swing.type is SwingType.HIGH else "swing_low",
                source=(swing.index,),
            )

    def _add_equal_pools(self, new_swings: list[Swing]) -> None:
        """Equal Highs/Lows aus den zuletzt bestaetigten Swings bilden.

        Massgeblich ist der EXTREMWERT des Clusters, nicht sein Mittelwert: die
        Stops liegen jenseits des hoechsten Hochs bzw. tiefsten Tiefs, und genau
        dieses Level muss ein Sweep durchstechen.
        """
        tolerance = self.params.equal_tolerance_ticks * self.tick_size

        for swing_type, side in (
            (SwingType.HIGH, LiquiditySide.BUY_SIDE),
            (SwingType.LOW, LiquiditySide.SELL_SIDE),
        ):
            trigger = [s for s in new_swings if s.type is swing_type]
            if not trigger:
                continue
            history = self._swing_history[swing_type]
            if len(history) < self.params.equal_min_count:
                continue

            latest = trigger[-1]
            cluster = [s for s in history if abs(s.price - latest.price) <= tolerance]
            if len(cluster) < self.params.equal_min_count:
                continue

            prices = [s.price for s in cluster]
            level = max(prices) if side is LiquiditySide.BUY_SIDE else min(prices)
            self._add_pool_at(
                price=level,
                side=side,
                kind=LiquidityKind.EQUAL,
                created_index=latest.confirmed_at_index,
                created_ts=latest.ts,
                label="equal_highs" if side is LiquiditySide.BUY_SIDE else "equal_lows",
                source=tuple(s.index for s in cluster),
                strength=len(cluster),
            )

    # ------------------------------------------------------------- Pool-Anlage
    def _add_pool(
        self,
        series: BarSeries,
        index: int,
        price: float,
        side: LiquiditySide,
        kind: LiquidityKind,
        label: str,
        source: tuple[int, ...],
    ) -> None:
        self._add_pool_at(price, side, kind, index, int(series.ts[index]), label, source)

    def _add_pool_at(
        self,
        price: float,
        side: LiquiditySide,
        kind: LiquidityKind,
        created_index: int,
        created_ts: int,
        label: str,
        source: tuple[int, ...],
        strength: int = 1,
    ) -> LiquidityPool | None:
        """Level anlegen, sofern nicht bereits ein gleichartiges in Tick-Naehe existiert."""
        tolerance = self.params.equal_tolerance_ticks * self.tick_size
        for existing in self.pools:
            if (
                existing.kind is kind
                and existing.side is side
                and existing.is_untapped
                and abs(existing.price - price) <= tolerance
            ):
                # Doppelte Level bringen keine Information, verzerren aber jede
                # Statistik ueber "wie viele Level wurden gesweept".
                existing.strength = max(existing.strength, strength)
                if kind is LiquidityKind.EQUAL:
                    # Waechst ein Cluster ueber sein bisheriges Extrem hinaus,
                    # wandert das Level mit: die Stops liegen jenseits des
                    # tiefsten Tiefs bzw. hoechsten Hochs, und genau dieses
                    # Niveau muss ein Sweep durchstechen.
                    existing.price = (
                        max(existing.price, price)
                        if side is LiquiditySide.BUY_SIDE
                        else min(existing.price, price)
                    )
                    existing.source_indices = tuple(
                        sorted(set(existing.source_indices) | set(source))
                    )
                return None

        pool = LiquidityPool(
            id=self._next_id,
            price=price,
            side=side,
            kind=kind,
            created_index=created_index,
            created_ts=created_ts,
            label=label,
            strength=strength,
            source_indices=source,
        )
        self._next_id += 1
        self.pools.append(pool)
        return pool

    # ------------------------------------------------------------------ Sweeps
    def _check_sweeps(self, series: BarSeries, index: int) -> list[Sweep]:
        high = float(series.high[index])
        low = float(series.low[index])
        close = float(series.close[index])
        use_close = self.sweep_params.reclaim_on == "close"
        reclaim_up = close if use_close else high
        reclaim_down = close if use_close else low
        penetration = self.sweep_params.min_penetration_ticks * self.tick_size

        completed: list[Sweep] = []

        # 1) Offene Durchstiche fortschreiben.
        for pool_id, pending in list(self._pending.items()):
            pool = pending.pool
            elapsed = index - pending.penetration_index
            if pool.is_buy_side:
                pending.extreme_price = max(pending.extreme_price, high)
                reclaimed = reclaim_down < pool.price
            else:
                pending.extreme_price = min(pending.extreme_price, low)
                reclaimed = reclaim_up > pool.price

            if reclaimed:
                depth = (
                    pending.extreme_price - pool.price
                    if pool.is_buy_side
                    else pool.price - pending.extreme_price
                )
                completed.append(
                    Sweep(
                        pool_id=pool.id,
                        pool_kind=pool.kind,
                        pool_price=pool.price,
                        side=pool.side,
                        direction=(
                            Direction.BEARISH if pool.is_buy_side else Direction.BULLISH
                        ),
                        penetration_index=pending.penetration_index,
                        penetration_ts=int(series.ts[pending.penetration_index]),
                        reclaim_index=index,
                        reclaim_ts=int(series.ts[index]),
                        depth_ticks=depth / self.tick_size,
                        bars_to_reclaim=elapsed,
                    )
                )
                del self._pending[pool_id]
            elif elapsed >= self.sweep_params.max_reclaim_bars:
                # Keine Rueckeroberung im Zeitfenster: Level wurde durchhandelt,
                # nicht gesweept. Kein Ereignis, Pool bleibt verbraucht.
                del self._pending[pool_id]

        # 2) Neue Durchstiche erkennen.
        for pool in self.pools:
            if not pool.is_untapped or pool.id in self._pending:
                continue
            if pool.created_index >= index:
                continue
            if pool.is_buy_side:
                pierced = high > pool.price + penetration
                extreme = high
            else:
                pierced = low < pool.price - penetration
                extreme = low
            if not pierced:
                continue
            pool.state = LiquidityState.SWEPT
            pool.tapped_index = index
            self._pending[pool.id] = _Pending(pool, index, extreme)

        # 3) Sweeps, die am Durchstichs-Bar selbst schon zurueckerobert wurden.
        for pool_id, pending in list(self._pending.items()):
            if pending.penetration_index != index:
                continue
            pool = pending.pool
            reclaimed = (
                reclaim_down < pool.price if pool.is_buy_side else reclaim_up > pool.price
            )
            if not reclaimed:
                continue
            depth = (
                pending.extreme_price - pool.price
                if pool.is_buy_side
                else pool.price - pending.extreme_price
            )
            completed.append(
                Sweep(
                    pool_id=pool.id,
                    pool_kind=pool.kind,
                    pool_price=pool.price,
                    side=pool.side,
                    direction=Direction.BEARISH if pool.is_buy_side else Direction.BULLISH,
                    penetration_index=index,
                    penetration_ts=int(series.ts[index]),
                    reclaim_index=index,
                    reclaim_ts=int(series.ts[index]),
                    depth_ticks=depth / self.tick_size,
                    bars_to_reclaim=0,
                )
            )
            del self._pending[pool_id]

        completed.sort(key=lambda s: (s.pool_price, s.pool_id))
        self.sweeps.extend(completed)
        if len(self.sweeps) > self.sweep_params.max_tracked:
            del self.sweeps[: len(self.sweeps) - self.sweep_params.max_tracked]
        return completed

    # -------------------------------------------------------------------- Pflege
    def _prune(self) -> None:
        if len(self.pools) <= self.params.max_tracked:
            return
        untapped = [p for p in self.pools if p.is_untapped]
        tapped = [p for p in self.pools if not p.is_untapped]
        keep = max(self.params.max_tracked - len(untapped), 0)
        retained = untapped + (tapped[-keep:] if keep else [])
        self.pools = sorted(retained, key=lambda p: p.created_index)

    # ------------------------------------------------------------------ Abfragen
    def untapped(self, side: LiquiditySide | None = None) -> list[LiquidityPool]:
        return [p for p in self.pools if p.is_untapped and (side is None or p.side is side)]

    def nearest_untapped(self, price: float, side: LiquiditySide) -> LiquidityPool | None:
        """Naechstes unberuehrtes Level in der jeweiligen Richtung.

        Das ist der Kandidat fuer das Take-Profit-Ziel in Phase 3 (Spec §12).
        """
        if side is LiquiditySide.BUY_SIDE:
            above = [p for p in self.untapped(side) if p.price > price]
            return min(above, key=lambda p: p.price) if above else None
        below = [p for p in self.untapped(side) if p.price < price]
        return max(below, key=lambda p: p.price) if below else None

    def sweeps_within(
        self, index: int, lookback_bars: int, direction: Direction | None = None
    ) -> list[Sweep]:
        """Sweeps der letzten `lookback_bars` Bars - so fragt die Strategie."""
        result = []
        for sweep in reversed(self.sweeps):
            if index - sweep.reclaim_index > lookback_bars:
                break
            if direction is None or sweep.direction is direction:
                result.append(sweep)
        return list(reversed(result))
