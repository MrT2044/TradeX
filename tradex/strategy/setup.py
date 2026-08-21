"""Setup-Kandidat: die Pflichtkette als Zustandsmaschine (Spec §7-§9).

Die Kette
---------
    HTF Bias -> Liquidity Sweep -> Displacement -> FVG -> Retracement
             -> Lower-Timeframe Confirmation (MSS) -> Entry

Jeder Schritt ist eine PFLICHTbedingung. Fehlt einer, entsteht kein Trade -
der Bot ergaenzt nichts (Spec §9). Deshalb ist das hier bewusst eine
Zustandsmaschine und keine Sammlung unabhaengiger Pruefungen: die Reihenfolge
und der zeitliche Zusammenhang sind Teil der Regel. Ein Displacement, das VOR
dem Sweep stattfand, zaehlt nicht. Ein Sweep von vor drei Stunden hat mit dem
jetzigen Impuls nichts mehr zu tun.

Warum Kandidaten verfallen
--------------------------
Ohne Zeitfenster bliebe jeder Sweep ewig als halbfertiges Setup stehen und
wuerde irgendwann zufaellig "bestaetigt". Die Fenster in `config/default.yaml`
sind die Regel, die das verhindert.

Ebenenverteilung (Spec §5)
--------------------------
Sweep, Displacement, FVG und Retracement entwickeln sich auf dem
Setup-Timeframe (Default 5m). Die abschliessende MSS-Bestaetigung kommt vom
Confirmation-Timeframe (Default 1m). Weil der Aggregator kleine Timeframes vor
grossen schliesst, kann eine Bestaetigung fruehestens auf der NAECHSTEN
1m-Bar nach dem Retracement erfolgen - nie auf derselben. Das ist gewollt und
schliesst Look-ahead aus.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from tradex.analysis.displacement import Displacement
from tradex.analysis.fvg import Fvg
from tradex.analysis.liquidity import Sweep
from tradex.analysis.structure import StructureEvent
from tradex.domain.enums import Direction


class SetupStage(StrEnum):
    """Fortschritt eines Kandidaten entlang der Pflichtkette."""

    SWEPT = "swept"
    """Liquiditaet geholt und zurueckerobert. Wartet auf Displacement."""

    DISPLACED = "displaced"
    """Impuls samt gueltiger FVG vorhanden. Wartet auf Retracement."""

    RETRACED = "retraced"
    """Kurs ist in die FVG zurueckgelaufen. Wartet auf MSS-Bestaetigung."""

    CONFIRMED = "confirmed"
    """Alle Pflichtbedingungen erfuellt. Erzeugt ein Einstiegssignal."""

    INVALIDATED = "invalidated"
    """Die Praemisse ist gebrochen (Kurs jenseits des Sweeps, Bias gedreht)."""

    EXPIRED = "expired"
    """Zeitfenster abgelaufen, ohne dass die Kette vollstaendig wurde."""

    @property
    def is_open(self) -> bool:
        return self in (SetupStage.SWEPT, SetupStage.DISPLACED, SetupStage.RETRACED)

    @property
    def is_dead(self) -> bool:
        return self in (SetupStage.INVALIDATED, SetupStage.EXPIRED)


@dataclass(slots=True)
class StageChange:
    """Ein Uebergang in der Kette - fuer Protokoll und Anzeige."""

    index: int
    ts: int
    stage: SetupStage
    note: str = ""


@dataclass(slots=True)
class SetupCandidate:
    """Ein Setup im Aufbau.

    Der Kandidat ist die einzige Stelle, an der die zeitliche Verkettung der
    Pflichtbedingungen festgehalten wird. Alles, was zur Begruendung eines
    Trades - oder eines Nicht-Trades - noetig ist, steht hier.
    """

    id: int
    symbol: str
    direction: Direction
    sweep: Sweep
    created_index: int
    created_ts: int
    stage: SetupStage = SetupStage.SWEPT

    displacement: Displacement | None = None
    fvg: Fvg | None = None
    retraced_index: int | None = None
    retraced_ts: int | None = None
    retraced_price: float | None = None
    retracement_extreme: float | None = None
    """Tiefster (bullish) bzw. hoechster (bearish) Kurs seit dem Ruecklauf.

    Das ist der Punkt, an dem die EINSTIEGSIDEE kippt: Der Einstieg erfolgt auf
    den MSS nach dem Ruecklauf; wird dessen Extrem wieder unterboten, war der
    Strukturbruch nicht tragfaehig. Wird bis zur Bestaetigung fortgeschrieben.
    """
    confirmation: StructureEvent | None = None
    confirmed_index: int | None = None

    #: Index der letzten Bar des Confirmation-Timeframes beim Retracement.
    #: Ab hier laeuft das Fenster fuer die MSS-Bestaetigung.
    retraced_confirmation_index: int | None = None

    history: list[StageChange] = field(default_factory=list)
    closed_reason: str = ""

    # ------------------------------------------------------------- Eigenschaften
    @property
    def is_bullish(self) -> bool:
        return self.direction is Direction.BULLISH

    @property
    def invalidation_price(self) -> float:
        """Kurs, jenseits dessen die Praemisse des Setups gebrochen ist.

        Das ist das Extrem des Sweeps: Wird es wieder durchhandelt, war die
        Liquiditaet nicht "geholt und abgelehnt", sondern schlicht durchbrochen.
        """
        return self.sweep.extreme_price

    @property
    def is_open(self) -> bool:
        return self.stage.is_open

    def track_retracement_extreme(self, low: float, high: float) -> None:
        """Extrem des Ruecklaufs fortschreiben, solange die Bestaetigung aussteht."""
        probe = low if self.is_bullish else high
        if self.retracement_extreme is None:
            self.retracement_extreme = probe
        elif self.is_bullish:
            self.retracement_extreme = min(self.retracement_extreme, probe)
        else:
            self.retracement_extreme = max(self.retracement_extreme, probe)

    def advance(self, stage: SetupStage, index: int, ts: int, note: str = "") -> None:
        self.stage = stage
        self.history.append(StageChange(index=index, ts=ts, stage=stage, note=note))

    def kill(self, stage: SetupStage, index: int, ts: int, reason: str) -> None:
        self.closed_reason = reason
        self.advance(stage, index, ts, reason)

    # --------------------------------------------------------------- Checkliste
    def checklist(self) -> dict[str, bool]:
        """Zustand der Pflichtbedingungen - Grundlage der Anzeige (Spec §23)."""
        return {
            "sweep": True,  # ohne Sweep gaebe es diesen Kandidaten nicht
            "displacement": self.displacement is not None,
            "fvg": self.fvg is not None,
            "retracement": self.retraced_index is not None,
            "mss": self.confirmation is not None,
        }

    def missing_conditions(self) -> list[str]:
        return [name for name, ok in self.checklist().items() if not ok]

    def describe(self) -> str:
        return (
            f"Setup #{self.id} {self.direction.value} [{self.stage.value}] "
            f"Sweep @ {self.sweep.pool_price:.2f} ({self.sweep.pool_kind.value})"
        )
