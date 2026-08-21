"""Higher-Timeframe Bias (Spec §7 Schritt 1).

Der Bias ist die erste Pflichtbedingung der Strategie und entscheidet, in welche
Richtung ueberhaupt nach Setups gesucht wird. Er muss deshalb aus messbaren
Groessen entstehen, nicht aus einer Chartinterpretation.

Drei Komponenten je Timeframe, jeweils normiert auf [-1, +1]
------------------------------------------------------------
structure   Strukturzustand des Timeframes:
                BULLISH -> +1, BEARISH -> -1, RANGE -> 0
            Kommt direkt aus BOS/MSS und damit aus Swing-Bruechen.

fvg         Ungleichgewicht der noch offenen Imbalances:
                (n_bullish - n_bearish) / (n_bullish + n_bearish)
            Offene bullishe FVGs unter dem Kurs sind unerledigte Nachfrage,
            offene bearishe darueber unerledigtes Angebot.

liquidity   Wohin der Kurs gezogen wird, gemessen an der Naehe des jeweils
            naechsten unberuehrten Levels:
                (d_unten - d_oben) / (d_unten + d_oben)
            Ist das naechste unberuehrte Hoch naeher als das naechste Tief,
            ist der Zug nach oben wahrscheinlicher - der Wert wird positiv.

Gesamtwert
----------
    tf_score    = gewichtete Summe der drei Komponenten
    total       = gewichtete Summe der tf_scores (4H staerker als 1H)
    Bias        = BULLISH  wenn total >  neutral_band
                  BEARISH  wenn total < -neutral_band
                  NEUTRAL  sonst

Das Neutralband ist wichtig: ohne es waere jeder minimale Ausschlag eine
Richtungsaussage, und die Strategie wuerde in Seitwaertsphasen dauernd
Setups in beide Richtungen zulassen.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tradex.analysis import reasons
from tradex.analysis.fvg import FvgTracker
from tradex.analysis.liquidity import LiquidityTracker
from tradex.analysis.structure import StructureTracker
from tradex.config import BiasParams
from tradex.domain.enums import Bias, Direction, LiquiditySide, StructureState, Timeframe
from tradex.persistence.models import Reason


@dataclass(frozen=True, slots=True)
class TimeframeBias:
    """Bias-Beitrag eines einzelnen Timeframes."""

    timeframe: Timeframe
    score: float
    structure_score: float
    fvg_score: float
    liquidity_score: float
    structure_state: StructureState
    active_bullish_fvgs: int
    active_bearish_fvgs: int
    nearest_buy_side: float | None
    nearest_sell_side: float | None


@dataclass(frozen=True, slots=True)
class BiasResult:
    """Ergebnis der Bias-Bestimmung ueber alle HTF."""

    bias: Bias
    score: float
    per_timeframe: tuple[TimeframeBias, ...]
    reasons: tuple[Reason, ...] = field(default_factory=tuple)

    @property
    def direction(self) -> Direction | None:
        if self.bias is Bias.BULLISH:
            return Direction.BULLISH
        if self.bias is Bias.BEARISH:
            return Direction.BEARISH
        return None


def structure_score(state: StructureState) -> float:
    if state is StructureState.BULLISH:
        return 1.0
    if state is StructureState.BEARISH:
        return -1.0
    return 0.0


def fvg_score(fvg: FvgTracker) -> tuple[float, int, int]:
    bullish = len(fvg.active(Direction.BULLISH))
    bearish = len(fvg.active(Direction.BEARISH))
    total = bullish + bearish
    if total == 0:
        return 0.0, 0, 0
    return (bullish - bearish) / total, bullish, bearish


def liquidity_score(
    liquidity: LiquidityTracker, price: float
) -> tuple[float, float | None, float | None]:
    """Richtung des Liquiditaets-Zugs plus die beiden naechsten Level."""
    above = liquidity.nearest_untapped(price, LiquiditySide.BUY_SIDE)
    below = liquidity.nearest_untapped(price, LiquiditySide.SELL_SIDE)
    up_price = above.price if above else None
    down_price = below.price if below else None

    if above is None and below is None:
        return 0.0, None, None
    if below is None:
        return 1.0, up_price, None
    if above is None:
        return -1.0, None, down_price

    distance_up = above.price - price
    distance_down = price - below.price
    denominator = distance_up + distance_down
    if denominator <= 0:
        return 0.0, up_price, down_price
    return (distance_down - distance_up) / denominator, up_price, down_price


def evaluate_timeframe(
    timeframe: Timeframe,
    params: BiasParams,
    structure: StructureTracker,
    fvg: FvgTracker,
    liquidity: LiquidityTracker,
    price: float,
) -> TimeframeBias:
    weights = params.component_weights
    s_score = structure_score(structure.state)
    f_score, bullish_count, bearish_count = fvg_score(fvg)
    l_score, up_price, down_price = liquidity_score(liquidity, price)

    total_weight = weights.structure + weights.fvg + weights.liquidity
    combined = (
        weights.structure * s_score + weights.fvg * f_score + weights.liquidity * l_score
    )
    score = combined / total_weight if total_weight > 0 else 0.0

    return TimeframeBias(
        timeframe=timeframe,
        score=score,
        structure_score=s_score,
        fvg_score=f_score,
        liquidity_score=l_score,
        structure_state=structure.state,
        active_bullish_fvgs=bullish_count,
        active_bearish_fvgs=bearish_count,
        nearest_buy_side=up_price,
        nearest_sell_side=down_price,
    )


def combine(params: BiasParams, per_timeframe: list[TimeframeBias]) -> BiasResult:
    """Timeframe-Ergebnisse zum Gesamt-Bias verrechnen.

    Timeframes ohne konfiguriertes Gewicht werden ignoriert - so wirkt sich eine
    Aenderung an `timeframes.htf` nur dort aus, wo sie gemeint ist.
    """
    weighted_sum = 0.0
    weight_total = 0.0
    for item in per_timeframe:
        weight = params.timeframe_weights.get(item.timeframe)
        if weight is None:
            continue
        weighted_sum += weight * item.score
        weight_total += weight

    score = weighted_sum / weight_total if weight_total > 0 else 0.0
    if score > params.neutral_band:
        bias = Bias.BULLISH
    elif score < -params.neutral_band:
        bias = Bias.BEARISH
    else:
        bias = Bias.NEUTRAL

    detail: list[Reason] = [
        Reason(
            code=reasons.HTF_BIAS,
            ok=bias is not Bias.NEUTRAL,
            params={
                "bias": bias.value,
                "score": round(score, 4),
                "neutral_band": params.neutral_band,
            },
        )
    ]
    for item in per_timeframe:
        detail.append(
            Reason(
                code=reasons.HTF_STRUCTURE,
                ok=item.structure_state is not StructureState.RANGE,
                params={
                    "timeframe": item.timeframe.value,
                    "state": item.structure_state.value,
                    "score": round(item.score, 4),
                },
            )
        )
        detail.append(
            Reason(
                code=reasons.HTF_FVG_BALANCE,
                ok=item.fvg_score != 0.0,
                params={
                    "timeframe": item.timeframe.value,
                    "bullish": item.active_bullish_fvgs,
                    "bearish": item.active_bearish_fvgs,
                    "score": round(item.fvg_score, 4),
                },
            )
        )
        detail.append(
            Reason(
                code=reasons.HTF_LIQUIDITY_DRAW,
                ok=item.liquidity_score != 0.0,
                params={
                    "timeframe": item.timeframe.value,
                    "buy_side": item.nearest_buy_side,
                    "sell_side": item.nearest_sell_side,
                    "score": round(item.liquidity_score, 4),
                },
            )
        )

    return BiasResult(
        bias=bias,
        score=score,
        per_timeframe=tuple(per_timeframe),
        reasons=tuple(detail),
    )
