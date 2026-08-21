"""MarketContext - der EINZIGE Analysepfad (Architektur-Invariante 3).

Replay, Backtest (Phase 4) und Live-Betrieb (Phase 5+) rufen alle dieselbe
Methode auf: `MarketContext.on_base_bar(bar)`. Es gibt keinen zweiten,
"optimierten" Weg durch die Analyse. Genau das macht Spec §29 durchsetzbar -
was der Backtest zeigt, passiert im Live-Betrieb identisch.

Ablauf je geschlossener Bar eines Timeframes
--------------------------------------------
    1. ATR und Volumen-Durchschnitt AUS DEN VORHERIGEN Bars merken
    2. ATR/Volumen mit der neuen Bar fortschreiben
    3. Swings pruefen (bestaetigt sich der Kandidat bei index - strength?)
    4. Struktur: neue Swings registrieren, dann auf BOS/MSS pruefen
    5. FVG: bestehende Zonen fortschreiben, dann neue erkennen
    6. Displacement der neuen Bar bewerten
    7. Liquiditaet: Zeitabschnitte abschliessen, Level bilden, Sweeps pruefen

Warum ATR aus Schritt 1 und nicht aus Schritt 2
-----------------------------------------------
Displacement und FVG-Groesse werden gegen die Volatilitaet der VORAUSGEGANGENEN
Bars gemessen. Wuerde die auslosende Bar in ihren eigenen Massstab eingehen,
wuerde eine sehr grosse Bar den ATR anheben und damit ihr eigenes Verhaeltnis
druecken - starke Impulse wuerden systematisch unterschaetzt.
"""

from __future__ import annotations

from dataclasses import dataclass

from tradex.analysis.bias import BiasResult, combine, evaluate_timeframe
from tradex.analysis.displacement import Displacement, DisplacementDetector
from tradex.analysis.fvg import Fvg, FvgTracker
from tradex.analysis.liquidity import LiquidityPool, LiquidityTracker, Sweep
from tradex.analysis.structure import StructureEvent, StructureTracker
from tradex.analysis.swings import Swing, SwingDetector
from tradex.analysis.volatility import RollingAtr, RollingSma
from tradex.config import Config
from tradex.data.aggregator import MultiTimeframeAggregator
from tradex.data.sessions import SessionResolver
from tradex.domain.bars import Bar, BarSeries
from tradex.domain.enums import Direction, SessionName, StructureState, Timeframe
from tradex.domain.events import BarClosed
from tradex.domain.instruments import Instrument


@dataclass(frozen=True, slots=True)
class TimeframeUpdate:
    """Was sich durch eine geschlossene Bar in einem Timeframe geaendert hat."""

    timeframe: Timeframe
    index: int
    bar: Bar
    atr: float
    volume_avg: float
    new_swings: tuple[Swing, ...]
    structure_event: StructureEvent | None
    new_fvgs: tuple[Fvg, ...]
    displacement: Displacement | None
    sweeps: tuple[Sweep, ...]
    session: str

    @property
    def has_signal(self) -> bool:
        """Ist in dieser Bar ueberhaupt etwas Berichtenswertes passiert?"""
        return bool(
            self.new_swings
            or self.structure_event
            or self.new_fvgs
            or self.displacement
            or self.sweeps
        )


@dataclass(frozen=True, slots=True)
class TimeframeSnapshot:
    """Vollstaendiger Analysezustand eines Timeframes."""

    timeframe: Timeframe
    bar_count: int
    ready: bool
    last_bar: Bar | None
    atr: float
    volume_avg: float
    session: str
    structure_state: StructureState
    last_structure_event: StructureEvent | None
    last_mss: StructureEvent | None
    swings: tuple[Swing, ...]
    active_fvgs: tuple[Fvg, ...]
    untapped_pools: tuple[LiquidityPool, ...]
    recent_sweeps: tuple[Sweep, ...]
    last_displacement: Displacement | None


@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    """Gesamtzustand ueber alle Timeframes - Grundlage fuer UI und Protokoll."""

    symbol: str
    last_ts: int
    session: str
    bias: BiasResult
    timeframes: dict[Timeframe, TimeframeSnapshot]

    def timeframe(self, tf: Timeframe) -> TimeframeSnapshot | None:
        return self.timeframes.get(tf)


class TimeframeState:
    """Alle Detektoren eines Timeframes samt ihrem Zustand."""

    __slots__ = (
        "timeframe",
        "instrument",
        "config",
        "atr",
        "volume_sma",
        "swings",
        "structure",
        "fvg",
        "displacement",
        "liquidity",
        "bars_seen",
        "last_atr",
        "last_volume_avg",
        "last_session",
    )

    def __init__(self, timeframe: Timeframe, instrument: Instrument, config: Config) -> None:
        analysis = config.analysis
        self.timeframe = timeframe
        self.instrument = instrument
        self.config = config
        self.atr = RollingAtr(analysis.volatility.atr_period, analysis.volatility.atr_method)
        self.volume_sma = RollingSma(analysis.volatility.volume_sma_period)
        self.swings = SwingDetector(
            analysis.swings.strength_for(timeframe), analysis.swings.max_tracked
        )
        self.structure = StructureTracker(analysis.structure, instrument.tick_size)
        self.fvg = FvgTracker(analysis.fvg, instrument.tick_size)
        self.displacement = DisplacementDetector(analysis.displacement)
        self.liquidity = LiquidityTracker(
            analysis.liquidity, analysis.sweep, instrument.tick_size
        )
        self.bars_seen = 0
        self.last_atr = float("nan")
        self.last_volume_avg = float("nan")
        self.last_session = SessionName.CLOSED.value

    @property
    def ready(self) -> bool:
        """Genug Historie, damit die Detektoren belastbare Werte liefern."""
        return self.bars_seen >= self.config.analysis.volatility.min_bars_required

    def on_bar_closed(
        self, series: BarSeries, event: BarClosed, resolver: SessionResolver
    ) -> TimeframeUpdate:
        index = event.index
        bar = event.bar

        # Schritt 1: Massstab aus den VORHERIGEN Bars sichern (siehe Modul-Docstring).
        atr_reference = self.atr.value
        volume_reference = self.volume_sma.value

        # Schritt 2: fortschreiben.
        self.atr.update(bar.high, bar.low, bar.close)
        self.volume_sma.update(bar.volume)
        self.bars_seen += 1
        self.last_atr = self.atr.value
        self.last_volume_avg = self.volume_sma.value

        # Schritt 3+4: Swings und Struktur.
        new_swings = self.swings.update(series, index)
        self.structure.register_swings(new_swings)
        structure_event = self.structure.update(series, index)

        # Schritt 5+6: Imbalances und Impuls.
        new_fvgs = self.fvg.update(series, index, atr_reference)
        displacement = self.displacement.update(series, index, atr_reference, volume_reference)

        # Schritt 7: Liquiditaet.
        session, trading_day, trading_week = resolver.resolve(bar.ts)
        self.last_session = session
        sweeps = self.liquidity.update(
            series, index, new_swings, session, trading_day, trading_week
        )

        return TimeframeUpdate(
            timeframe=self.timeframe,
            index=index,
            bar=bar,
            atr=self.last_atr,
            volume_avg=self.last_volume_avg,
            new_swings=tuple(new_swings),
            structure_event=structure_event,
            new_fvgs=tuple(new_fvgs),
            displacement=displacement,
            sweeps=tuple(sweeps),
            session=session,
        )

    def snapshot(self, series: BarSeries, max_items: int) -> TimeframeSnapshot:
        return TimeframeSnapshot(
            timeframe=self.timeframe,
            bar_count=len(series),
            ready=self.ready,
            last_bar=series.last() if len(series) else None,
            atr=self.last_atr,
            volume_avg=self.last_volume_avg,
            session=self.last_session,
            structure_state=self.structure.state,
            last_structure_event=self.structure.last_event,
            last_mss=self.structure.last_mss(),
            swings=tuple(self.swings.all_swings()[-max_items:]),
            active_fvgs=tuple(self.fvg.active()[-max_items:]),
            untapped_pools=tuple(self.liquidity.untapped()[-max_items:]),
            recent_sweeps=tuple(self.liquidity.sweeps[-max_items:]),
            last_displacement=self.displacement.last(),
        )


class MarketContext:
    """Multi-Timeframe-Analysezustand eines Instruments."""

    def __init__(self, symbol: str, instrument: Instrument, config: Config) -> None:
        self.symbol = symbol.upper()
        self.instrument = instrument
        self.config = config
        self.timeframes = config.timeframes.all
        self.aggregator = MultiTimeframeAggregator(
            self.symbol,
            instrument,
            self.timeframes,
            base_timeframe=config.data.base_timeframe,
        )
        self.states = {
            tf: TimeframeState(tf, instrument, config) for tf in self.timeframes
        }
        self.resolver = SessionResolver(instrument)
        self.last_ts: int = 0
        self.last_session: str = SessionName.CLOSED.value

    # ------------------------------------------------------------------- API
    def on_base_bar(self, bar: Bar) -> list[TimeframeUpdate]:
        """Eine geschlossene Basis-Bar verarbeiten.

        Der einzige Einstieg in die Analyse. Liefert einen Eintrag je Timeframe,
        dessen Bar durch diese Basis-Bar geschlossen wurde - aufsteigend nach
        Timeframe-Dauer.
        """
        self.last_ts = bar.ts
        self.last_session = self.resolver.resolve(bar.ts)[0]

        updates: list[TimeframeUpdate] = []
        for event in self.aggregator.on_base_bar(bar):
            state = self.states[event.timeframe]
            series = self.aggregator.series[event.timeframe]
            updates.append(state.on_bar_closed(series, event, self.resolver))
        return updates

    def feed(self, series: BarSeries) -> list[TimeframeUpdate]:
        """Eine ganze Basis-Serie verarbeiten (historischer Aufbau / Replay)."""
        updates: list[TimeframeUpdate] = []
        for bar in series:
            updates.extend(self.on_base_bar(bar))
        return updates

    def series(self, timeframe: Timeframe) -> BarSeries:
        return self.aggregator.series[timeframe]

    def forming(self, timeframe: Timeframe) -> Bar | None:
        """Die noch laufende Bar - AUSSCHLIESSLICH zur Anzeige (Invariante 1)."""
        return self.aggregator.forming(timeframe)

    # -------------------------------------------------------------- Auswertung
    def bias(self) -> BiasResult:
        """Aktueller HTF-Bias (Spec §7 Schritt 1)."""
        per_timeframe = []
        for tf in self.config.analysis.bias.timeframe_weights:
            state = self.states.get(tf)
            series = self.aggregator.series.get(tf)
            if state is None or series is None or not len(series) or not state.ready:
                continue
            per_timeframe.append(
                evaluate_timeframe(
                    tf,
                    self.config.analysis.bias,
                    state.structure,
                    state.fvg,
                    state.liquidity,
                    float(series.close[-1]),
                )
            )
        return combine(self.config.analysis.bias, per_timeframe)

    def snapshot(self, max_items: int = 50) -> ContextSnapshot:
        """Vollstaendiger Zustand - fuer UI und Entscheidungsprotokoll."""
        return ContextSnapshot(
            symbol=self.symbol,
            last_ts=self.last_ts,
            session=self.last_session,
            bias=self.bias(),
            timeframes={
                tf: state.snapshot(self.aggregator.series[tf], max_items)
                for tf, state in self.states.items()
            },
        )

    # ----------------------------------------------------------------- Abfragen
    def entry_timeframe(self) -> Timeframe:
        return self.config.timeframes.entry[0]

    def active_fvgs(self, timeframe: Timeframe, direction: Direction | None = None) -> list[Fvg]:
        return self.states[timeframe].fvg.active(direction)

    def __repr__(self) -> str:
        counts = ", ".join(
            f"{tf.value}={len(self.aggregator.series[tf])}" for tf in self.timeframes
        )
        return f"MarketContext({self.symbol}, {counts})"
