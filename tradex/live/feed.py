"""Was ein Live-Feed liefert - und was er niemals liefern darf.

Ein Feed schickt drei Arten von Nachrichten: geschlossene Bars, Lebenszeichen
und Zustandsmeldungen. Mehr nicht. Insbesondere:

**Nie die laufende Bar.** Architektur-Invariante 1: analysiert wird
ausschliesslich auf geschlossenen Bars. Wuerde ein Feed die laufende Bar
schicken, saehe die Engine live einen Zustand, den sie im Backtest nie sieht -
und jede Backtest-Aussage waere hinfaellig. Die Zusicherung gehoert deshalb an
die Aussenkante, in die Feed-Schnittstelle, und nicht in eine Bitte an den
Entwickler des jeweiligen Feeds.

**Lebenszeichen sind Pflicht, keine Zierde.** Ein Feed, der nichts mehr
schickt, ist von einem ruhigen Markt nicht zu unterscheiden. Ohne Herzschlag
handelte die Engine nach einem Verbindungsabbruch munter mit veralteten Kursen
weiter (Spec Paragraph 24).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from tradex.domain.bars import Bar


@dataclass(frozen=True, slots=True)
class BarMessage:
    """Eine ABGESCHLOSSENE Bar eines Symbols."""

    symbol: str
    bar: Bar
    received_ts: int
    """Wanduhr beim Eintreffen (Epoch-ns). Ausdruecklich getrennt von `bar.ts`:
    die Differenz ist die Verzoegerung des Feeds, und die will man sehen."""


@dataclass(frozen=True, slots=True)
class HeartbeatMessage:
    """Lebenszeichen. Sagt nur: die Verbindung steht noch."""

    ts: int


@dataclass(frozen=True, slots=True)
class StatusMessage:
    """Zustandswechsel der Verbindung."""

    ts: int
    connected: bool
    detail: str = ""


FeedMessage = BarMessage | HeartbeatMessage | StatusMessage


class LiveFeed(Protocol):
    """Eine Quelle geschlossener Bars in Echtzeit."""

    name: str

    def start(self) -> None:
        """Verbindung aufbauen. Darf blockieren, aber nicht ewig."""
        ...

    def stop(self) -> None:
        """Verbindung abbauen. Muss mehrfach aufrufbar sein."""
        ...

    def messages(self, timeout: float) -> Iterator[FeedMessage]:
        """Alle bis `timeout` eingetroffenen Nachrichten, aelteste zuerst.

        Liefert einen leeren Iterator, wenn nichts kam - das ist kein Fehler,
        sondern der Normalfall zwischen zwei Bars. Ob daraus ein Problem wird,
        entscheidet die Sitzung anhand des letzten Lebenszeichens.
        """
        ...

    @property
    def is_finished(self) -> bool:
        """True, wenn nichts mehr kommen wird (Wiedergabe am Ende).

        Ein echter Marktfeed liefert hier immer False - er endet nicht, er
        wird getrennt. Der Unterschied ist wichtig: das Ende einer Wiedergabe
        ist ein regulaerer Abschluss, ein stiller Marktfeed ein Alarm.
        """
        ...
