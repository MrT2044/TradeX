"""Take-Profit-Bestimmung (Spec §12).

Kein fester Dollarbetrag. Ziel ist die naechste UNBERUEHRTE Liquiditaet in
Handelsrichtung: dorthin wird der Kurs gezogen, weil dort die Gegenorders
liegen. Damit hat auch das Ziel eine inhaltliche Bedeutung.

Auswahlverfahren
----------------
Die unberuehrten Level werden von nah nach fern durchgegangen. Genommen wird
das ERSTE, das das Mindest-CRV erfuellt. Das ist bewusst so herum:

  - Immer das naechste Level zu nehmen wuerde viele Setups mit schlechtem CRV
    erzeugen, die dann verworfen werden muessten.
  - Immer das fernste zu nehmen wuerde das CRV kuenstlich schoenrechnen und
    Ziele setzen, die der Kurs realistisch nicht erreicht.

Erfuellt KEIN Level in Reichweite das Mindest-CRV, entsteht kein Trade
(Spec §12: "Trades mit schlechtem Chance-Risiko-Verhaeltnis werden abgelehnt").
Das Setup wird nicht durch ein weiter entferntes Wunschziel gerettet.

Ein Puffer vor dem Level ist Absicht: genau dort sitzt die Liquiditaet, und der
Kurs dreht oft knapp davor. Lieber ein paar Ticks frueher raus als auf den
letzten Tick warten und leer ausgehen.
"""

from __future__ import annotations

from dataclasses import dataclass

from tradex.analysis.liquidity import LiquidityPool, LiquidityTracker
from tradex.config import TargetsConfig
from tradex.domain.enums import Direction, LiquiditySide
from tradex.domain.instruments import Instrument


@dataclass(frozen=True, slots=True)
class TargetResult:
    """Ergebnis der Zielbestimmung."""

    price: float
    distance_points: float
    rr: float
    source: str
    """liquidity | fallback_r_multiple"""
    pool: LiquidityPool | None
    ok: bool
    rejection: str = ""
    best_available_rr: float = 0.0
    """Bestes erreichbares CRV - erklaert im Protokoll, warum abgelehnt wurde."""


def place_target(
    direction: Direction,
    entry_price: float,
    stop_distance_points: float,
    liquidity: LiquidityTracker,
    instrument: Instrument,
    params: TargetsConfig,
    min_rr: float,
) -> TargetResult:
    """Ziel bestimmen und gegen das Mindest-CRV pruefen."""
    if stop_distance_points <= 0:
        return TargetResult(0.0, 0.0, 0.0, "none", None, ok=False, rejection="invalid_stop")

    bullish = direction is Direction.BULLISH
    side = LiquiditySide.BUY_SIDE if bullish else LiquiditySide.SELL_SIDE

    if params.mode == "r_multiple":
        return _fixed_r_multiple(
            bullish, entry_price, stop_distance_points, instrument, params, min_rr
        )

    candidates = _ordered_candidates(liquidity, entry_price, side, bullish, params)
    best_rr = 0.0

    for pool in candidates:
        distance = abs(pool.price - entry_price)
        if distance <= 0:
            continue
        rr = distance / stop_distance_points
        best_rr = max(best_rr, rr)
        if rr < min_rr:
            continue
        return TargetResult(
            price=instrument.round_to_tick(pool.price),
            distance_points=distance,
            rr=rr,
            source="liquidity",
            pool=pool,
            ok=True,
        )

    if not candidates:
        # Gar keine Liquiditaet in Reichweite: das feste R-Vielfache ist die
        # dokumentierte Rueckfallebene, nicht der Normalfall.
        return _fixed_r_multiple(
            bullish, entry_price, stop_distance_points, instrument, params, min_rr
        )

    return TargetResult(
        price=0.0,
        distance_points=0.0,
        rr=best_rr,
        source="liquidity",
        pool=None,
        ok=False,
        rejection="rr_too_low",
        best_available_rr=best_rr,
    )


def _ordered_candidates(
    liquidity: LiquidityTracker,
    entry_price: float,
    side: LiquiditySide,
    bullish: bool,
    params: TargetsConfig,
) -> list[LiquidityPool]:
    """Unberuehrte Level in Handelsrichtung, von nah nach fern."""
    pools = liquidity.untapped(side) if params.require_untapped else [
        p for p in liquidity.pools if p.side is side
    ]
    ahead = [p for p in pools if (p.price > entry_price if bullish else p.price < entry_price)]
    ahead.sort(key=lambda p: abs(p.price - entry_price))
    return ahead[: params.max_candidates]


def _fixed_r_multiple(
    bullish: bool,
    entry_price: float,
    stop_distance_points: float,
    instrument: Instrument,
    params: TargetsConfig,
    min_rr: float,
) -> TargetResult:
    rr = params.fallback_r_multiple
    distance = stop_distance_points * rr
    price = entry_price + distance if bullish else entry_price - distance
    return TargetResult(
        price=instrument.round_to_tick(price),
        distance_points=distance,
        rr=rr,
        source="fallback_r_multiple",
        pool=None,
        ok=rr >= min_rr,
        rejection="" if rr >= min_rr else "rr_too_low",
        best_available_rr=rr,
    )
