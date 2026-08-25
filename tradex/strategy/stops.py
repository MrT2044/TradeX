"""Stop-Loss-Bestimmung (Spec §11).

Der Stop darf nicht willkuerlich gewaehlt werden. Anker ist immer ein Kurs mit
inhaltlicher Bedeutung - kein Dollarbetrag.

Welcher Anker passt zu diesem Einstieg?
---------------------------------------
Der Einstieg erfolgt auf den MSS NACH dem Ruecklauf in die FVG. Zwischen dem
urspruenglichen Sweep und diesem Einstieg liegt die gesamte Impulsbewegung.

    retracement  Extrem des Ruecklaufs. Genau hier kippt die EINSTIEGSIDEE:
                 wird es unterboten, war der Strukturbruch nicht tragfaehig.
                 Standard, weil es der Punkt ist, auf den sich der Einstieg
                 tatsaechlich stuetzt.
    sweep        Extrem des Sweeps. Inhaltlich der Ursprung des Setups, liegt
                 zum Einstiegszeitpunkt aber eine ganze Impulsbewegung entfernt.
                 Der Stop wird dadurch systematisch weit und das CRV schlecht.
    swing        Letzter bestaetigter Swing auf der Setup-Ebene.
    fvg          Gegenkante der FVG-Zone.

Die Wahl ist eine STRATEGIEENTSCHEIDUNG, keine Feinabstimmung. Welcher Anker
ueber einen ausreichend langen Zeitraum die bessere Erwartung liefert, muss der
Backtest in Phase 4 beantworten - `retracement` ist als Standard gesetzt, weil
es zum Einstiegsmodell passt, nicht weil es auf irgendwelchen Daten besser
aussah.

Darunter kommt ein Puffer, damit normales Rauschen den Stop nicht abholt:

    Puffer = max(buffer_atr_mult * ATR, buffer_min_ticks * tick_size)

Der ATR-Anteil sorgt dafuer, dass der Abstand mit der Volatilitaet mitwaechst -
in ruhigen Phasen eng, in bewegten weit. Ein fester Tickwert allein waere im
NY-Open zu eng und in der Asia-Session unnoetig weit.

Zwei harte Grenzen
------------------
    min_stop_ticks   Darunter steckt der Stop im Rauschen und wird zufaellig
                     ausgeloest. Solche Setups werden abgelehnt, statt sie mit
                     einem kuenstlich verbreiterten Stop zu "retten" - das
                     waere eine stillschweigende Regelaenderung.
    max_stop_ticks   Darueber ist das Setup zu weit. Die Positionsgroesse
                     wuerde auf null gerundet, und das Risiko liesse sich nicht
                     mehr sinnvoll steuern.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from tradex.analysis.swings import Swing
from tradex.config import StopsConfig
from tradex.domain.enums import Direction
from tradex.domain.instruments import Instrument
from tradex.strategy.setup import SetupCandidate


@dataclass(frozen=True, slots=True)
class StopResult:
    """Ergebnis der Stop-Bestimmung."""

    price: float
    distance_points: float
    distance_ticks: float
    anchor_price: float
    anchor_kind: str
    buffer_points: float
    ok: bool
    rejection: str = ""

    @property
    def is_valid(self) -> bool:
        return self.ok


def max_stop_ticks(atr: float, instrument: Instrument, params: StopsConfig) -> float:
    """Groesste erlaubte Stopweite in Ticks bei dieser ATR.

    Die EINZIGE Stelle, an der aus `max_stop_atr_mult` eine Tickzahl wird.
    Vorher stand die Grenze als blanke Zahl in der Config und wurde an drei
    Stellen einzeln verglichen (`stops.py`, `opening_range.py`, `consistency.py`).
    Das ging gut, solange es eine Konstante war - bei einer Rechnung waere es
    der klassische Weg, wie zwei Strategien stillschweigend verschiedene
    Grenzen bekommen.

    Liefert 0.0 bei nicht belastbarer ATR; der Aufrufer muss diesen Fall
    ausdruecklich behandeln (siehe `place_stop`).
    """
    if not np.isfinite(atr):
        return 0.0
    return params.max_stop_atr_mult * instrument.to_ticks(atr)


def place_stop(
    candidate: SetupCandidate,
    entry_price: float,
    atr: float,
    instrument: Instrument,
    params: StopsConfig,
    recent_swing: Swing | None = None,
) -> StopResult:
    """Stop fuer einen bestaetigten Kandidaten bestimmen.

    `recent_swing` wird nur beim Anker `swing` benutzt; fehlt er dort, faellt
    die Bestimmung auf das Sweep-Extrem zurueck, damit nie ohne Anker
    gearbeitet wird.
    """
    anchor_price, anchor_kind = _anchor(candidate, entry_price, params, recent_swing)

    atr_component = params.buffer_atr_mult * atr if np.isfinite(atr) else 0.0
    tick_component = params.buffer_min_ticks * instrument.tick_size
    buffer_points = max(atr_component, tick_component)

    raw = (
        anchor_price - buffer_points
        if candidate.is_bullish
        else anchor_price + buffer_points
    )
    price = instrument.round_to_tick(raw)
    distance_points = abs(entry_price - price)
    distance_ticks = instrument.to_ticks(distance_points)

    if distance_ticks < params.min_stop_ticks:
        return StopResult(
            price=price,
            distance_points=distance_points,
            distance_ticks=distance_ticks,
            anchor_price=anchor_price,
            anchor_kind=anchor_kind,
            buffer_points=buffer_points,
            ok=False,
            rejection="too_tight",
        )
    # Die Obergrenze ist relativ zur ATR. Ohne belastbare ATR laesst sie sich
    # nicht beurteilen - dann wird abgelehnt, nicht durchgewinkt. Fail closed:
    # eine unbeantwortete Frage ist hier ein Nein. In der Praxis tritt der Fall
    # kaum auf, weil die Aufrufer bereits auf NaN-ATR pruefen; er hat trotzdem
    # einen eigenen Grund, damit er im Entscheidungsprotokoll sichtbar waere
    # statt sich als "zu weit" zu tarnen.
    if not np.isfinite(atr):
        return StopResult(
            price=price,
            distance_points=distance_points,
            distance_ticks=distance_ticks,
            anchor_price=anchor_price,
            anchor_kind=anchor_kind,
            buffer_points=buffer_points,
            ok=False,
            rejection="no_atr",
        )

    if distance_ticks > max_stop_ticks(atr, instrument, params):
        return StopResult(
            price=price,
            distance_points=distance_points,
            distance_ticks=distance_ticks,
            anchor_price=anchor_price,
            anchor_kind=anchor_kind,
            buffer_points=buffer_points,
            ok=False,
            rejection="too_wide",
        )

    return StopResult(
        price=price,
        distance_points=distance_points,
        distance_ticks=distance_ticks,
        anchor_price=anchor_price,
        anchor_kind=anchor_kind,
        buffer_points=buffer_points,
        ok=True,
    )


def _anchor(
    candidate: SetupCandidate,
    entry_price: float,
    params: StopsConfig,
    recent_swing: Swing | None,
) -> tuple[float, str]:
    """Ankerkurs und dessen Herkunft.

    Der Anker MUSS auf der Verlustseite des Einstiegs liegen: bei Long darunter,
    bei Short darueber. Ein zuletzt bestaetigter Swing kann diese Bedingung
    verletzen - etwa wenn der Kurs seit dem Swing bereits deutlich gestiegen ist.
    Ein daraus gebildeter Stop laege ueber dem Einstieg und waere kein Stop mehr,
    sondern ein sofortiger Verlust.

    In diesem Fall wird auf das Sweep-Extrem zurueckgefallen. Das ist per
    Konstruktion immer auf der richtigen Seite: das Setup verlangt, dass der Kurs
    sich davon entfernt hat. Welcher Anker tatsaechlich benutzt wurde, steht in
    `StopResult.anchor_kind` und damit im Protokoll - der Rueckfall passiert
    nicht stillschweigend.
    """
    candidates: list[tuple[float, str]] = []

    if params.anchor == "retracement" and candidate.retracement_extreme is not None:
        candidates.append((candidate.retracement_extreme, "retracement"))
    elif params.anchor == "swing" and recent_swing is not None:
        candidates.append((recent_swing.price, "swing"))
    elif params.anchor == "fvg" and candidate.fvg is not None:
        zone = candidate.fvg
        candidates.append(((zone.bottom if candidate.is_bullish else zone.top), "fvg"))

    candidates.append((candidate.sweep.extreme_price, "sweep"))

    for price, kind in candidates:
        if _is_on_loss_side(price, entry_price, candidate.is_bullish):
            return price, kind
    return candidate.sweep.extreme_price, "sweep"


def _is_on_loss_side(anchor: float, entry: float, bullish: bool) -> bool:
    return anchor < entry if bullish else anchor > entry


def direction_sign(direction: Direction) -> int:
    return 1 if direction is Direction.BULLISH else -1
