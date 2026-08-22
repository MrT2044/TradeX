"""Tagesbuch fuer Risikogrenzen (Spec §10, §24).

Haelt fest, was an einem Handelstag bereits passiert ist: wie viele Trades
genommen wurden, wie viel realisiert verloren oder gewonnen wurde und wie viele
Positionen offen sind. Darauf setzen die harten Grenzen auf - Tagesverlustlimit,
maximale Trades pro Tag, maximal gleichzeitig offene Positionen.

Warum als eigene, fuellbare Schicht
-----------------------------------
In Phase 3 gibt es noch keine Ausfuehrung, also bleibt das Buch leer. Die
Grenzen sind aber bereits implementiert und getestet - Tests fuellen das Buch
direkt. Ab Phase 4 speist der Backtest seine simulierten Ergebnisse hier ein,
ab Phase 5 das Paper Trading und spaeter der Live-Betrieb. Alle benutzen
dieselbe Grenzlogik, was Voraussetzung dafuer ist, dass ein Backtest ueber das
Risikoverhalten ueberhaupt etwas aussagt.

Der Handelstag ist der Globex-Tag (Beginn 17:00 Boersenzeit), nicht der
Kalendertag - sonst wuerde das Tagesverlustlimit mitten in der US-Session
zuruecksetzen.
"""

from __future__ import annotations

from dataclasses import dataclass

from tradex.domain.enums import Direction


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """Ein abgeschlossener Trade."""

    setup_id: int
    direction: Direction
    entry_ts: int
    exit_ts: int
    quantity: int
    pnl: float
    r_multiple: float


@dataclass(slots=True)
class OpenPosition:
    setup_id: int
    direction: Direction
    entry_ts: int
    entry_price: float
    stop: float
    target: float
    quantity: int
    risk_amount: float


@dataclass(slots=True)
class DayState:
    """Stand eines einzelnen Handelstages."""

    trading_day: int
    trades_taken: int = 0
    realized_pnl: float = 0.0
    wins: int = 0
    losses: int = 0

    @property
    def realized_loss(self) -> float:
        """Verlust als positive Zahl. 0, wenn der Tag im Plus steht."""
        return -self.realized_pnl if self.realized_pnl < 0 else 0.0


class RiskLedger:
    """Buchfuehrung ueber Trades, offene Positionen und Tagesergebnisse."""

    def __init__(self) -> None:
        self._days: dict[int, DayState] = {}
        self._open: dict[int, OpenPosition] = {}
        self._closed: list[ClosedTrade] = []
        self._current_day: int | None = None
        self._next_trade_id = 1

    def next_trade_id(self) -> int:
        """Naechste kontoweit eindeutige Trade-Nummer.

        Sie entsteht ABSICHTLICH hier und nicht in der Strategie oder im
        Portfolio: der Schluessel muss ueber alles eindeutig sein, was sich
        dieses Buch teilt - alle Strategien UND alle Instrumente. Genau diese
        Verwechslung ist beim Umbau zweimal aufgetreten, erst zwischen
        Strategien, dann zwischen Symbolen. Wer den Zaehler dort fuehrt, wo das
        Konto liegt, kann sie kein drittes Mal bauen.
        """
        current = self._next_trade_id
        self._next_trade_id += 1
        return current

    # ---------------------------------------------------------------- Handelstag
    def day(self, trading_day: int) -> DayState:
        state = self._days.get(trading_day)
        if state is None:
            state = DayState(trading_day=trading_day)
            self._days[trading_day] = state
        self._current_day = trading_day
        return state

    @property
    def current_day(self) -> DayState | None:
        if self._current_day is None:
            return None
        return self._days.get(self._current_day)

    # ------------------------------------------------------------------ Zugriff
    @property
    def open_positions(self) -> list[OpenPosition]:
        return list(self._open.values())

    @property
    def open_count(self) -> int:
        return len(self._open)

    @property
    def closed_trades(self) -> list[ClosedTrade]:
        return list(self._closed)

    def is_open(self, setup_id: int) -> bool:
        return setup_id in self._open

    def position(self, setup_id: int) -> OpenPosition | None:
        return self._open.get(setup_id)

    # ---------------------------------------------------------------- Buchungen
    def open_position(self, position: OpenPosition, trading_day: int) -> None:
        if position.setup_id in self._open:
            # Doppelte Order zum selben Setup ist ein Fehlerfall, kein
            # Normalzustand (Spec §24 Duplicate Order Protection).
            raise ValueError(f"Setup {position.setup_id} hat bereits eine offene Position")
        self._open[position.setup_id] = position
        self.day(trading_day).trades_taken += 1

    def cancel_position(self, setup_id: int, trading_day: int) -> None:
        """Eine gebuchte, aber nie gefuellte Order zuruecknehmen.

        Der Backtest bucht die Position bereits beim Signal, damit die Grenzen
        (offene Positionen, Trades pro Tag) sofort greifen - sonst koennte
        zwischen Signal und Fuellung ein zweites Signal dieselbe freie Stelle
        belegen. Wird die Order nie gefuellt, weil die Daten enden, darf sie
        auch nicht als genommener Trade zaehlen.
        """
        if self._open.pop(setup_id, None) is None:
            raise ValueError(f"Setup {setup_id} hat keine offene Position")
        state = self.day(trading_day)
        state.trades_taken = max(0, state.trades_taken - 1)

    def close_position(self, setup_id: int, exit_ts: int, pnl: float, trading_day: int) -> ClosedTrade:
        position = self._open.pop(setup_id, None)
        if position is None:
            raise ValueError(f"Setup {setup_id} hat keine offene Position")

        state = self.day(trading_day)
        state.realized_pnl += pnl
        if pnl > 0:
            state.wins += 1
        elif pnl < 0:
            state.losses += 1

        r_multiple = pnl / position.risk_amount if position.risk_amount else 0.0
        trade = ClosedTrade(
            setup_id=setup_id,
            direction=position.direction,
            entry_ts=position.entry_ts,
            exit_ts=exit_ts,
            quantity=position.quantity,
            pnl=pnl,
            r_multiple=r_multiple,
        )
        self._closed.append(trade)
        return trade

    # ------------------------------------------------------------------- Bericht
    def summary(self) -> dict[str, float | int]:
        total = sum(t.pnl for t in self._closed)
        wins = sum(1 for t in self._closed if t.pnl > 0)
        return {
            "trades": len(self._closed),
            "wins": wins,
            "losses": len(self._closed) - wins,
            "net_pnl": total,
            "open_positions": len(self._open),
        }

    def reset(self) -> None:
        self._days.clear()
        self._open.clear()
        self._closed.clear()
        self._current_day = None
        self._next_trade_id = 1
