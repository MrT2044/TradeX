"""Der Vertrag zwischen einer Strategie und dem Rest des Systems.

Die entscheidende Trennlinie
---------------------------
Eine Strategie **schlaegt vor**. Sie entscheidet nie, ob gehandelt wird.

    Strategie  ->  TradeProposal (Richtung, Einstieg, Stop, Ziel, Begruendung)
    Portfolio  ->  Risikopruefung, Groesse, Grenzen  ->  StrategyDecision

Solange es nur eine Strategie gab, war diese Trennung Kosmetik - die Kette
rechnete ihre Groesse selbst aus. Mit mehreren Strategien am selben Konto ist
sie zwingend: das Risikobudget, das Tagesverlustlimit und die Obergrenze fuer
offene Positionen gelten fuer das GANZE Konto, nicht je Strategie. Wuerde jede
Strategie ihre eigene Groesse bestimmen, waere das erlaubte Gesamtrisiko die
Summe der Einzelbudgets - also ein Vielfaches dessen, was erlaubt ist.

Was eine Strategie liefern muss
-------------------------------
Beides: Vorschlaege UND Ablehnungen. Ein Setup, das gestorben ist, gehoert
genauso ins Protokoll wie eines, das durchkam (Spec §25). Nur Vorschlaege
laufen durch die Risikopruefung; Ablehnungen sind bereits fertige
Entscheidungen.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from tradex.analysis.context import MarketContext, TimeframeUpdate
from tradex.domain.enums import Direction
from tradex.persistence.models import Reason
from tradex.strategy.signal import StrategyDecision


@dataclass(frozen=True, slots=True)
class TradeProposal:
    """Ein fertig gerechneter Handelsvorschlag - ohne Risikopruefung.

    Enthaelt alles, was die Strategie zu sagen hat: Richtung, Einstieg, Stop,
    Ziel und die vollstaendige Begruendung. Was NICHT drinsteht, ist die
    Stueckzahl - die haengt am Konto und nicht an der Strategie.
    """

    strategy: str
    setup_id: int
    symbol: str
    direction: Direction
    entry: float
    stop: float
    target: float
    stop_ticks: float
    stop_points: float
    target_points: float
    rr: float
    stop_anchor: str
    target_source: str

    ts: int
    index: int
    timeframe: str
    stage: str
    checklist: dict[str, bool]
    reasons: tuple[Reason, ...]
    htf_bias: str
    session: str
    trading_day: int
    atr: float

    @property
    def side(self) -> str:
        return "LONG" if self.direction is Direction.BULLISH else "SHORT"


@dataclass(slots=True)
class StrategyOutput:
    """Was eine Strategie auf einer Basis-Bar produziert hat."""

    decisions: list[StrategyDecision] = field(default_factory=list)
    """Bereits abgeschlossene Entscheidungen - Ablehnungen und tote Setups."""
    proposals: list[TradeProposal] = field(default_factory=list)
    """Vorschlaege, die noch durch die Risikopruefung muessen."""

    def extend(self, other: StrategyOutput) -> None:
        self.decisions.extend(other.decisions)
        self.proposals.extend(other.proposals)


class Strategy(ABC):
    """Basisklasse aller Strategien.

    Eine Strategie darf ausschliesslich auf `MarketContext` zugreifen - also
    auf denselben Analysepfad, den Replay, Backtest und spaeter Live benutzen
    (Architektur-Invariante 3). Eigene Detektoren, eigene Datenzugriffe oder
    eine eigene Uhr sind ausgeschlossen; sonst waere ihr Verhalten nicht mehr
    reproduzierbar.
    """

    #: Kurzname, der in jedem Protokolleintrag und jeder Statistik auftaucht.
    #: Er wird zum Schluessel in der Auswertung - deshalb stabil halten.
    name: str = "unbenannt"

    @abstractmethod
    def on_updates(
        self, updates: list[TimeframeUpdate], context: MarketContext
    ) -> StrategyOutput:
        """Alle Timeframe-Aenderungen einer Basis-Bar verarbeiten."""

    def active_setups(self) -> list[object]:
        """Setups im Aufbau - rein fuer die Anzeige. Standard: keine."""
        return []

    @abstractmethod
    def reset(self) -> None:
        """Zustand verwerfen. Wird beim Zuruecksetzen des Replays gerufen.

        Bewusst PFLICHT und nicht mit leerem Standard: eine Strategie, die
        ihren Zustand beim Zuruecksetzen behaelt, liefert nach einem Reset
        andere Ergebnisse als beim ersten Lauf - und macht damit jede
        Reproduzierbarkeitsaussage zunichte. Wer wirklich keinen Zustand hat,
        schreibt eine leere Implementierung hin und hat es damit bedacht.
        """

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.name})"
