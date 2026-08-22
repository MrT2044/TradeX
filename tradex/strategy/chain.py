"""Die ICT-Pflichtkette als Strategie (Spec §6-§13).

Setzt die Pflichtkette auf die Analyse-Engine auf. Sie erzeugt selbst keine
Marktaussagen - alle Bedingungen kommen aus den Detektoren aus `tradex.analysis`.
Diese Schicht verkettet sie nur zeitlich, berechnet Stop und Ziel und liefert
am Ende einen Vorschlag.

Sie entscheidet NICHT, ob gehandelt wird
----------------------------------------
Positionsgroesse, Tagesgrenzen und Handelsfenster gehoeren dem Portfolio
(`tradex/strategy/portfolio.py`), weil sie fuer das ganze Konto gelten und
nicht je Strategie. Diese Klasse liefert `TradeProposal` - Richtung, Einstieg,
Stop, Ziel, Begruendung - und ist damit fertig.

Was diese Strategie NICHT kann
------------------------------
Sie ist strukturell niederfrequent. Gemessen auf MNQ_PROXY 2024: aus 18,9
Kandidaten pro Tag werden 0,25 vollstaendige Ketten - zwei Pflichtglieder
filtern je rund 89 %. Fuer taegliche Trades braucht es weitere Strategien
neben ihr, nicht schnellere Schwellenwerte in ihr.

Zusammenspiel der Ebenen
------------------------
    Setup-Timeframe (5m)        Sweep -> Displacement -> FVG -> Retracement
    Confirmation-Timeframe (1m) MSS -> Einstieg

Der Aggregator schliesst kleine Timeframes vor grossen. Ein Retracement, das auf
der 5m-Bar um 09:35 erkannt wird, kann deshalb fruehestens von der 1m-Bar um
09:36 bestaetigt werden - nie von der 1m-Bar um 09:35, obwohl die zeitgleich
schloss. Das ist gewollt: im Live-Betrieb weiss man um 09:35 noch nicht, dass
die 5m-Bar gerade ein Retracement war.

Was NICHT hier passiert
-----------------------
Kein Order-Routing, keine Fills, keine Positionsverwaltung, keine
Risikopruefung. Ausfuehrung ist Phase 5 und spaeter.
"""

from __future__ import annotations

from tradex.analysis import reasons as R
from tradex.analysis.context import MarketContext, TimeframeUpdate
from tradex.analysis.displacement import Displacement
from tradex.analysis.fvg import Fvg
from tradex.config import Config
from tradex.domain.enums import Bias, Direction, Timeframe
from tradex.domain.instruments import Instrument
from tradex.logging_setup import get_logger
from tradex.persistence.models import Reason
from tradex.strategy.base import Strategy, StrategyOutput, TradeProposal
from tradex.strategy.setup import SetupCandidate, SetupStage
from tradex.strategy.signal import StrategyDecision
from tradex.strategy.stops import place_stop
from tradex.strategy.targets import place_target

log = get_logger(__name__)

#: Eine Impulskerze bei Index d erzeugt die FVG, die bei d+1 sichtbar wird
#: (Bars d-1, d, d+1). Das Fenster ist minimal groesser, damit auch der Fall
#: abgedeckt ist, in dem die Luecke erst durch die Folgekerze gueltig gross wird.
_FVG_MATCH_WINDOW = 2


#: Kurzname der Strategie. Er landet in jedem Protokolleintrag und in jeder
#: Aufschluesselung des Backtests - deshalb stabil halten.
CHAIN_NAME = "ict_chain"


class ChainStrategy(Strategy):
    """Verfolgt Setup-Kandidaten und liefert Handelsvorschlaege."""

    name = CHAIN_NAME

    def __init__(self, symbol: str, instrument: Instrument, config: Config) -> None:
        self.symbol = symbol.upper()
        self.instrument = instrument
        self.config = config
        self.params = config.strategy

        self.candidates: list[SetupCandidate] = []
        self._next_id = 1
        self._bias: Bias = Bias.NEUTRAL

        self.setup_tf: Timeframe = config.strategy.setup_timeframe
        self.confirmation_tf: Timeframe = config.strategy.confirmation_timeframe

    def reset(self) -> None:
        self.candidates = []
        self._next_id = 1
        self._bias = Bias.NEUTRAL

    # ------------------------------------------------------------------- Einstieg
    def on_updates(
        self, updates: list[TimeframeUpdate], context: MarketContext
    ) -> StrategyOutput:
        """Alle Timeframe-Aenderungen einer Basis-Bar verarbeiten."""
        out = StrategyOutput()
        if not self.params.enabled or not updates:
            return out

        self._bias = context.bias().bias

        for update in updates:
            if update.timeframe is self.confirmation_tf:
                out.extend(self._on_confirmation_bar(update, context))
            if update.timeframe is self.setup_tf:
                out.extend(self._on_setup_bar(update, context))
        return out

    def active_setups(self) -> list[SetupCandidate]:
        return [c for c in self.candidates if c.is_open]

    # --------------------------------------------------------------- Setup-Ebene
    def _on_setup_bar(self, update: TimeframeUpdate, context: MarketContext) -> StrategyOutput:
        out = StrategyOutput()
        produced: list[StrategyDecision] = out.decisions
        state = context.states[self.setup_tf]
        index = update.index

        for candidate in list(self.candidates):
            if not candidate.is_open:
                continue
            decision = self._maintain(candidate, update, index)
            if decision is not None:
                produced.append(decision)
                continue
            if candidate.stage is SetupStage.SWEPT:
                self._try_displacement_and_fvg(candidate, state, index)
            if candidate.stage is SetupStage.DISPLACED:
                self._try_retracement(candidate, update, context, index)

        self._spawn_from_sweeps(update, index)
        produced.extend(self._prune(update, index))
        return out

    def _spawn_from_sweeps(self, update: TimeframeUpdate, index: int) -> None:
        """Neue Kandidaten aus frischen Sweeps - aber nur in Richtung des HTF-Bias.

        Spec §7 Schritt 1: Ohne passenden Bias gibt es kein Setup. Ein Sweep
        gegen die uebergeordnete Richtung wird deshalb gar nicht erst verfolgt.
        """
        if self._bias is Bias.NEUTRAL:
            return
        wanted = Direction.BULLISH if self._bias is Bias.BULLISH else Direction.BEARISH

        for sweep in update.sweeps:
            if sweep.direction is not wanted:
                continue
            if any(
                c.is_open and c.sweep.pool_id == sweep.pool_id for c in self.candidates
            ):
                continue
            candidate = SetupCandidate(
                id=self._next_id,
                symbol=self.symbol,
                direction=sweep.direction,
                sweep=sweep,
                created_index=index,
                created_ts=update.bar.ts,
            )
            candidate.advance(SetupStage.SWEPT, index, update.bar.ts, "sweep")
            self._next_id += 1
            self.candidates.append(candidate)

    def _maintain(
        self, candidate: SetupCandidate, update: TimeframeUpdate, index: int
    ) -> StrategyDecision | None:
        """Ungueltigkeit und Verfall pruefen. Liefert bei Tod eine Entscheidung."""
        bar = update.bar

        if self.params.invalidate_on_bias_flip:
            wanted = Bias.BULLISH if candidate.is_bullish else Bias.BEARISH
            if self._bias is not wanted:
                candidate.kill(SetupStage.INVALIDATED, index, bar.ts, "bias_flip")
                return self._no_trade(
                    candidate,
                    bar.ts,
                    index,
                    self.setup_tf,
                    [Reason(R.SETUP_INVALIDATED_BIAS_FLIP, False, {"bias": self._bias.value})],
                )

        if self.params.invalidate_beyond_sweep:
            broken = (
                bar.close < candidate.invalidation_price
                if candidate.is_bullish
                else bar.close > candidate.invalidation_price
            )
            if broken:
                candidate.kill(SetupStage.INVALIDATED, index, bar.ts, "beyond_sweep")
                return self._no_trade(
                    candidate,
                    bar.ts,
                    index,
                    self.setup_tf,
                    [
                        Reason(
                            R.SETUP_INVALIDATED_BEYOND_SWEEP,
                            False,
                            {
                                "close": bar.close,
                                "sweep_extreme": round(candidate.invalidation_price, 2),
                            },
                        )
                    ],
                )

        expired_after = self._expiry_age(candidate, index)
        if expired_after is not None:
            candidate.kill(SetupStage.EXPIRED, index, bar.ts, "expired")
            return self._no_trade(
                candidate,
                bar.ts,
                index,
                self.setup_tf,
                [
                    Reason(
                        R.SETUP_EXPIRED,
                        False,
                        {"stage": candidate.stage.value, "age_bars": expired_after},
                    )
                ],
            )
        return None

    def _expiry_age(self, candidate: SetupCandidate, index: int) -> int | None:
        """Alter in Bars, falls das Zeitfenster der aktuellen Stufe ueberschritten ist."""
        if candidate.stage is SetupStage.SWEPT:
            age = index - candidate.sweep.reclaim_index
            return age if age > self.params.sweep_max_age_bars else None
        if candidate.stage is SetupStage.DISPLACED and candidate.fvg is not None:
            age = index - candidate.fvg.created_index
            return age if age > self.params.fvg_max_age_bars else None
        return None

    def _try_displacement_and_fvg(
        self, candidate: SetupCandidate, state: object, index: int
    ) -> None:
        """Schritt 3 und 4: Impuls nach dem Sweep und die dadurch entstandene FVG."""
        if candidate.displacement is None:
            found = self._find_displacement(candidate, state, index)
            if found is None:
                return
            candidate.displacement = found

        if candidate.fvg is None:
            zone = self._find_fvg(candidate, state)
            if zone is None:
                return
            candidate.fvg = zone
            candidate.advance(SetupStage.DISPLACED, index, zone.created_ts, "displacement+fvg")

    def _find_displacement(
        self, candidate: SetupCandidate, state: object, index: int
    ) -> Displacement | None:
        window = self.params.displacement_max_age_bars
        for item in reversed(state.displacement.found):  # type: ignore[attr-defined]
            if item.index < candidate.sweep.reclaim_index:
                return None
            if item.direction is not candidate.direction:
                continue
            if item.index - candidate.sweep.reclaim_index > window:
                continue
            if index - item.index > window:
                continue
            return item
        return None

    def _find_fvg(self, candidate: SetupCandidate, state: object) -> Fvg | None:
        """Die FVG, die genau dieser Impuls erzeugt hat.

        Nicht irgendeine offene Zone: sie muss aus dem Impulsfenster stammen,
        sonst waere die Kette nur zufaellig vollstaendig.
        """
        displacement = candidate.displacement
        if displacement is None:
            return None
        lower = displacement.index
        upper = displacement.index + _FVG_MATCH_WINDOW
        for zone in reversed(state.fvg.zones):  # type: ignore[attr-defined]
            if zone.created_index < lower:
                return None
            if zone.direction is not candidate.direction:
                continue
            if lower <= zone.created_index <= upper:
                return zone
        return None

    def _try_retracement(
        self, candidate: SetupCandidate, update: TimeframeUpdate, context: MarketContext, index: int
    ) -> None:
        """Schritt 5: Kurs laeuft in die FVG zurueck.

        Geprueft wird gegen High/Low der Bar, nicht gegen den Close: ein
        Ruecklauf innerhalb der Bar ist ein Ruecklauf, auch wenn die Kerze
        wieder ausserhalb schliesst.
        """
        zone = candidate.fvg
        if zone is None:
            return
        bar = update.bar

        probe = bar.low if candidate.is_bullish else bar.high
        touched = bar.low <= zone.top and bar.high >= zone.bottom
        if not touched:
            return
        fill = zone.fill_fraction(probe)
        if fill < self.params.retracement_min_fill:
            return

        candidate.retraced_index = index
        candidate.retraced_ts = bar.ts
        candidate.retraced_price = probe
        candidate.retraced_confirmation_index = len(context.series(self.confirmation_tf)) - 1
        candidate.track_retracement_extreme(bar.low, bar.high)
        candidate.advance(SetupStage.RETRACED, index, bar.ts, f"fill={fill:.2f}")

    # -------------------------------------------------------- Bestaetigungsebene
    def _on_confirmation_bar(
        self, update: TimeframeUpdate, context: MarketContext
    ) -> StrategyOutput:
        """Schritt 6: MSS auf der Einstiegsebene. Erst hier entsteht ein Vorschlag."""
        out = StrategyOutput()
        produced: list[StrategyDecision] = out.decisions
        index = update.index

        for candidate in list(self.candidates):
            if candidate.stage is not SetupStage.RETRACED:
                continue

            # Das Ruecklauf-Extrem waechst bis zur Bestaetigung weiter - der
            # Stop-Anker muss den tiefsten Punkt kennen, den der Ruecklauf
            # tatsaechlich erreicht hat, nicht nur den der ersten Beruehrung.
            candidate.track_retracement_extreme(update.bar.low, update.bar.high)

            started = candidate.retraced_confirmation_index or 0
            if index - started > self.params.confirmation_max_age_bars:
                candidate.kill(SetupStage.EXPIRED, index, update.bar.ts, "confirmation_timeout")
                produced.append(
                    self._no_trade(
                        candidate,
                        update.bar.ts,
                        index,
                        self.confirmation_tf,
                        [
                            Reason(
                                R.MSS_MISSING,
                                False,
                                {
                                    "timeframe": self.confirmation_tf.value,
                                    "waited_bars": index - started,
                                    "limit": self.params.confirmation_max_age_bars,
                                },
                            )
                        ],
                    )
                )
                continue

            event = update.structure_event
            if event is None or not event.is_mss:
                continue
            if event.is_bullish is not candidate.is_bullish:
                continue

            candidate.confirmation = event
            candidate.confirmed_index = index
            candidate.advance(SetupStage.CONFIRMED, index, update.bar.ts, "mss")
            result = self._finalize(candidate, update, context, index)
            if isinstance(result, TradeProposal):
                out.proposals.append(result)
            else:
                produced.append(result)

        return out

    # ------------------------------------------------------------------ Vorschlag
    def _finalize(
        self, candidate: SetupCandidate, update: TimeframeUpdate, context: MarketContext, index: int
    ) -> StrategyDecision | TradeProposal:
        """Stop und Ziel rechnen. Ergebnis ist ein Vorschlag oder eine Ablehnung."""
        bar = update.bar
        entry_price = self.instrument.round_to_tick(bar.close)
        setup_state = context.states[self.setup_tf]
        atr = setup_state.last_atr

        collected: list[Reason] = [
            Reason(
                R.SETUP_COMPLETE,
                True,
                {
                    "sweep": round(candidate.sweep.pool_price, 2),
                    "displacement_strength": round(candidate.displacement.strength, 3)
                    if candidate.displacement
                    else None,
                    "fvg_ticks": round(candidate.fvg.size_ticks, 1) if candidate.fvg else None,
                },
            )
        ]

        # ---- Stop (Spec §11)
        swing = (
            setup_state.swings.last_low if candidate.is_bullish else setup_state.swings.last_high
        )
        stop = place_stop(
            candidate, entry_price, atr, self.instrument, self.config.stops, swing
        )
        if not stop.ok:
            code = R.STOP_TOO_TIGHT if stop.rejection == "too_tight" else R.STOP_TOO_WIDE
            collected.append(
                Reason(
                    code,
                    False,
                    {
                        "stop_ticks": round(stop.distance_ticks, 1),
                        "min": self.config.stops.min_stop_ticks,
                        "max": self.config.stops.max_stop_ticks,
                    },
                )
            )
            return self._no_trade(candidate, bar.ts, index, self.confirmation_tf, collected)

        collected.append(
            Reason(
                R.STOP_PLACED,
                True,
                {
                    "price": stop.price,
                    "ticks": round(stop.distance_ticks, 1),
                    "anchor": stop.anchor_kind,
                },
            )
        )

        # ---- Ziel (Spec §12)
        target = place_target(
            candidate.direction,
            entry_price,
            stop.distance_points,
            setup_state.liquidity,
            self.instrument,
            self.config.targets,
            self.config.risk.min_rr,
        )
        if not target.ok:
            code = R.TARGET_RR_TOO_LOW if target.rejection == "rr_too_low" else R.TARGET_NONE
            collected.append(
                Reason(
                    code,
                    False,
                    {
                        "best_rr": round(target.best_available_rr, 2),
                        "min_rr": self.config.risk.min_rr,
                    },
                )
            )
            return self._no_trade(candidate, bar.ts, index, self.confirmation_tf, collected)

        collected.append(
            Reason(
                R.TARGET_LIQUIDITY if target.source == "liquidity" else R.TARGET_FALLBACK,
                True,
                {
                    "price": target.price,
                    "rr": round(target.rr, 2),
                    "pool": target.pool.kind.value if target.pool else None,
                },
            )
        )

        # Hier endet die Zustaendigkeit der Strategie. Ueber Groesse, Grenzen
        # und Handelsfenster entscheidet das Portfolio - sie gelten fuer das
        # ganze Konto und nicht je Strategie.
        session, trading_day, _ = context.resolver.resolve(bar.ts)
        return TradeProposal(
            strategy=self.name,
            setup_id=candidate.id,
            symbol=self.symbol,
            direction=candidate.direction,
            entry=entry_price,
            stop=stop.price,
            target=target.price,
            stop_ticks=stop.distance_ticks,
            stop_points=stop.distance_points,
            target_points=target.distance_points,
            rr=target.rr,
            stop_anchor=stop.anchor_kind,
            target_source=target.source,
            ts=bar.ts,
            index=index,
            timeframe=self.confirmation_tf.value,
            stage=candidate.stage.value,
            checklist=candidate.checklist(),
            reasons=tuple(collected),
            htf_bias=self._bias.value,
            session=session,
            trading_day=trading_day,
            atr=atr,
        )

    def _no_trade(
        self,
        candidate: SetupCandidate,
        ts: int,
        index: int,
        timeframe: Timeframe,
        collected: list[Reason],
    ) -> StrategyDecision:
        collected.append(
            Reason(
                R.DECISION_NO_TRADE,
                False,
                {"stage": candidate.stage.value, "missing": candidate.missing_conditions()},
            )
        )
        return StrategyDecision(
            ts=ts,
            index=index,
            symbol=self.symbol,
            timeframe=timeframe.value,
            setup_id=candidate.id,
            direction=candidate.direction,
            decision="NO_TRADE",
            stage=candidate.stage.value,
            checklist=candidate.checklist(),
            reasons=tuple(collected),
            htf_bias=self._bias.value,
            strategy=self.name,
        )

    # -------------------------------------------------------------------- Pflege
    def _prune(self, update: TimeframeUpdate, index: int) -> list[StrategyDecision]:
        """Erledigte Kandidaten begrenzen, offene bis zur Obergrenze behalten.

        Verdraengte Kandidaten bekommen eine PROTOKOLLIERTE Entscheidung. Sie
        verschwanden frueher lautlos - gemessen an echten Daten 627 Stueck
        allein in 2024, also rund zwei pro Handelstag. Spec §25 verlangt, dass
        jedes nicht zustande gekommene Setup einen Grund hat; ohne Eintrag
        fehlten sie in jeder Auswertung der Frage "warum wird nicht gehandelt?".
        """
        open_ones = [c for c in self.candidates if c.is_open]
        produced: list[StrategyDecision] = []

        if len(open_ones) > self.params.max_active_setups:
            # Die aeltesten offenen Kandidaten aufgeben - sie sind am weitesten
            # von ihrem ausloesenden Sweep entfernt.
            keep = self.params.max_active_setups
            for candidate in open_ones[:-keep]:
                # Die erreichte Stufe VOR dem Toeten festhalten - sonst stuende
                # im Protokoll ueberall "expired" und man saehe nicht mehr, wie
                # weit der verdraengte Kandidat gekommen war.
                reached = candidate.stage.value
                candidate.kill(SetupStage.EXPIRED, index, update.bar.ts, "crowded_out")
                produced.append(
                    self._no_trade(
                        candidate,
                        update.bar.ts,
                        index,
                        self.setup_tf,
                        [Reason(R.SETUP_CROWDED_OUT, False, {"stage": reached, "limit": keep})],
                    )
                )
            open_ones = open_ones[-keep:]

        closed = [c for c in self.candidates if not c.is_open][-50:]
        self.candidates = sorted(open_ones + closed, key=lambda c: c.id)
        return produced

    # ------------------------------------------------------------------ Abfragen
    def active_candidates(self) -> list[SetupCandidate]:
        return [c for c in self.candidates if c.is_open]
