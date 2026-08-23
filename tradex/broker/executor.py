"""Fuellungen vom Broker statt aus den Bars.

Erfuellt `TradeExecutor` (`tradex/backtest/execution.py`) und ist damit die
EINZIGE Stelle, an der sich der Echtbetrieb vom Backtest unterscheidet. Alles
davor - Analyse, Regel, Positionsgroesse, Risikopruefung - laeuft durch
denselben Code.

Was das kostet, und warum es trotzdem richtig ist
-------------------------------------------------
Mit angeschlossenem Broker sind die Fuellungen echt und damit nicht mehr
identisch mit den simulierten. Die Aussage "Papertrading fuellt wie der
Backtest" gilt dann nicht mehr - sie kann nicht gelten, echte Fills sind keine
Modellfills. Was weiterhin gelten MUSS und per Test festgehalten ist: dieselben
ENTSCHEIDUNGEN. Der Broker ersetzt die Ausfuehrung, nie eine Regel.

Der Unterschied ist genau die Zahl, die man messen will: `planned_entry` gegen
`entry_price`.

Zwischen zwei Bars
------------------
Ein Stop wird nicht zum Bar-Schluss ausgeloest, sondern wenn er ausgeloest
wird. Deshalb gibt es `poll()`: die Sitzungsschleife holt Rueckmeldungen im
Sekundentakt ab, unabhaengig davon, ob eine Bar kam. Auf die naechste Minute zu
warten waere ein erfundener Zeitverzug - und ausgerechnet beim Ausstieg.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from tradex.analysis import reasons as R
from tradex.backtest.execution import (
    ExecutionOrder,
    ExecutionUpdate,
    SimulatedTrade,
)
from tradex.broker.base import BrokerInterface
from tradex.broker.manager import OrderManager, OrderUpdate, build_order_key
from tradex.broker.types import (
    ROLE_ENTRY,
    ROLE_STOP,
    ROLE_TARGET,
    BrokerOrder,
    OrderKind,
    OrderRequest,
    OrderSide,
    OrderState,
)
from tradex.config import Config
from tradex.domain.bars import Bar
from tradex.domain.enums import Direction, ExitReason
from tradex.domain.instruments import Instrument
from tradex.logging_setup import get_logger
from tradex.persistence.models import Reason
from tradex.risk.ledger import RiskLedger

log = get_logger(__name__)

#: Welche Rolle welchen Ausstiegsgrund bedeutet.
_EXIT_REASONS: dict[str, ExitReason] = {
    ROLE_STOP: ExitReason.STOP,
    ROLE_TARGET: ExitReason.TARGET,
}


@dataclass(slots=True)
class _LivePosition:
    """Eine echte Position im Werden oder im Lauf."""

    order: ExecutionOrder
    request: OrderRequest
    entry_price: float = 0.0
    entry_ts: int = 0
    entry_index: int = -1
    quantity: int = 0
    """Tatsaechlich gefuellte Menge - kann nach Teilfuellung kleiner sein."""
    filled: bool = False
    high_water: float = float("-inf")
    low_water: float = float("inf")
    entry_commission: float = 0.0
    fields: dict[str, float] = field(default_factory=dict)

    @property
    def is_bullish(self) -> bool:
        return self.order.signal.direction is Direction.BULLISH


class BrokerExecutor:
    """Ausfuehrung EINES Instruments ueber einen Broker.

    Der `OrderManager` dahinter gehoert dem Konto und wird geteilt - dieselbe
    Aufteilung wie zwischen `SymbolBook` und `RiskLedger`.
    """

    def __init__(
        self,
        symbol: str,
        instrument: Instrument,
        config: Config,
        ledger: RiskLedger,
        manager: OrderManager,
        broker: BrokerInterface,
        session_id: int | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.symbol = symbol.upper()
        self.instrument = instrument
        self.config = config
        self.params = config.backtest
        self.ledger = ledger
        self.manager = manager
        self.broker = broker
        self.session_id = session_id
        self.clock = clock

        self._positions: dict[str, _LivePosition] = {}
        self._last_index = 0
        self._base_seconds = config.data.base_timeframe.seconds

    @property
    def open_count(self) -> int:
        return sum(1 for position in self._positions.values() if position.filled)

    # ----------------------------------------------------------------- Senden
    def place(self, order: ExecutionOrder, bar: Bar, index: int) -> ExecutionUpdate:
        self._last_index = index
        update = ExecutionUpdate()
        signal = order.signal
        request = OrderRequest(
            order_key=build_order_key(self.session_id, signal.trade_id),
            symbol=self.symbol,
            side=OrderSide.from_direction(signal.direction),
            quantity=signal.quantity,
            kind=OrderKind.MARKET,
            stop_loss=signal.stop,
            take_profit=signal.target,
            planned_entry=signal.entry,
            signal_id=signal.trade_id,
            strategy=signal.strategy,
        )

        blocked = self._pre_check(request, bar)
        if blocked is not None:
            self.manager.reject(request, blocked)
            self._abandon(order, update, blocked.code)
            return update

        placed = self.manager.submit(request, self.clock())
        if placed is None:
            self._abandon(order, update, R.BROKER_ORDER_FAILED)
            return update

        self._positions[request.order_key] = _LivePosition(order=order, request=request)
        return update

    def _pre_check(self, request: OrderRequest, bar: Bar) -> Reason | None:
        """Die Stufen, die erst zur Orderzeit beantwortbar sind."""
        tradeable, detail = self.broker.can_trade(self.symbol)
        if not tradeable:
            return Reason(
                R.BROKER_CONTRACT_UNKNOWN, False, {"symbol": self.symbol, "grund": detail}
            )
        return self.manager.check(request.order_key, self.clock(), self._data_age(bar))

    def _data_age(self, bar: Bar) -> float:
        """Wie alt die Bar ist, aus der das Signal stammt - in Sekunden.

        `bar.ts` bezeichnet die EROEFFNUNG; geschlossen ist die Bar erst eine
        Bar-Laenge spaeter. Ohne diesen Zuschlag waere jede Bar schon beim
        Entstehen "eine Minute alt" und die Grenze von fuenf Sekunden nie
        einzuhalten.
        """
        closed_at = bar.ts / 1e9 + self._base_seconds
        return max(self.clock() - closed_at, 0.0)

    def _abandon(self, order: ExecutionOrder, update: ExecutionUpdate, code: str) -> None:
        """Die vorsorglich gebuchte Position zuruecknehmen.

        `SymbolBook` bucht vor der Ausfuehrung, damit im Fenster zwischen Signal
        und Fuellung keine zweite Position dieselbe freie Stelle belegt. Kommt
        keine Order zustande, muss die Buchung wieder weg - sonst blockiert ein
        Phantom den Platz bis zum Tagesende.
        """
        self.ledger.cancel_position(order.signal.trade_id, order.trading_day)
        update.unfilled += 1
        log.warning(
            "order_nicht_gesendet",
            symbol=self.symbol,
            trade_id=order.signal.trade_id,
            grund=code,
        )

    # ------------------------------------------------------------------ Ablauf
    def advance(self, bar: Bar, index: int) -> ExecutionUpdate:
        self._last_index = index
        for position in self._positions.values():
            if not position.filled:
                continue
            position.high_water = max(position.high_water, bar.high)
            position.low_water = min(position.low_water, bar.low)
        return self.poll()

    def poll(self) -> ExecutionUpdate:
        update = ExecutionUpdate()
        # Nur die eigenen: der Manager gehoert dem Konto und sortiert die
        # Rueckmeldungen nach Instrument vor. Ohne diese Vorsortierung wuerde
        # der erste abholende Executor die Fills der anderen verschlucken.
        for change in self.manager.drain(self.symbol):
            self._absorb(change, update)
        return update

    def _absorb(self, change: OrderUpdate, update: ExecutionUpdate) -> None:
        position = self._positions.get(change.order_key)
        if position is None:
            # Gehoert einem anderen Symbol oder einer Sitzung von frueher. Der
            # Manager ist kontoweit, die Executoren sind es nicht.
            return
        if change.role == ROLE_ENTRY:
            self._absorb_entry(position, change.order, update)
        elif change.role in _EXIT_REASONS:
            self._absorb_exit(position, change, update)

    def _absorb_entry(
        self, position: _LivePosition, order: BrokerOrder, update: ExecutionUpdate
    ) -> None:
        if order.state is OrderState.FILLED or (
            order.state.is_terminal and order.filled_quantity > 0
        ):
            if position.filled:
                return
            # Teilfuellung: die Position ist so gross, wie sie tatsaechlich
            # geworden ist - nicht so gross, wie sie gedacht war.
            position.quantity = order.filled_quantity or order.quantity
            position.entry_price = order.avg_fill_price
            position.entry_ts = int(self.clock() * 1e9)
            position.entry_index = self._last_index
            position.entry_commission = order.commission
            position.filled = True
            position.high_water = order.avg_fill_price
            position.low_water = order.avg_fill_price
            self._sync_ledger_entry(position)
            return

        if order.state.is_terminal and order.filled_quantity == 0:
            # Abgelehnt oder storniert, ohne dass etwas entstanden ist.
            self._abandon(position.order, update, R.BROKER_ORDER_REJECTED)
            self._positions.pop(position.request.order_key, None)
            self.manager.forget(position.request.order_key)

    def _absorb_exit(
        self, position: _LivePosition, change: OrderUpdate, update: ExecutionUpdate
    ) -> None:
        order = change.order
        if order.state is not OrderState.FILLED:
            return
        if not position.filled:
            # Ein Ausstieg ohne Einstieg ist ein Zustand, den es nicht geben
            # darf. Er wird gemeldet und nicht stillschweigend verrechnet.
            log.error(
                "ausstieg_ohne_einstieg",
                symbol=self.symbol,
                order_key=position.request.order_key,
                rolle=change.role,
            )
            return
        trade = self._build_trade(
            position,
            exit_price=order.avg_fill_price,
            exit_reason=_EXIT_REASONS[change.role],
            exit_commission=order.commission,
            commission_reported=order.commission_reported,
        )
        update.closed.append(trade)
        self._positions.pop(position.request.order_key, None)
        self.manager.forget(position.request.order_key)

    def _sync_ledger_entry(self, position: _LivePosition) -> None:
        """Den gebuchten Einstieg auf den ECHTEN Fuellkurs nachziehen."""
        booked = self.ledger.position(position.order.signal.trade_id)
        if booked is not None:
            booked.entry_price = position.entry_price
            booked.entry_ts = position.entry_ts
            booked.quantity = position.quantity

    # ------------------------------------------------------------------- Ende
    def finish(self, last_bar: Bar, last_index: int) -> ExecutionUpdate:
        """Sitzungsende - offene Positionen bleiben OFFEN.

        Sie hier zwangszuschliessen waere eine Handelsentscheidung, die niemand
        getroffen hat, und ein erfundener Trade obendrein. Sie bleiben beim
        Broker liegen, geschuetzt durch ihre Bracket-Orders; genau dafuer liegen
        Stop und Ziel dort und nicht in diesem Programm.
        """
        update = ExecutionUpdate()
        for key, position in list(self._positions.items()):
            if position.filled:
                log.warning(
                    "position_bleibt_offen",
                    symbol=self.symbol,
                    order_key=key,
                    quantity=position.quantity,
                    entry=position.entry_price,
                    hinweis="Stop und Ziel liegen als Orders beim Broker",
                )
                continue
            # Nie gefuellt: die Order zaehlt nicht als genommener Trade.
            self._abandon(position.order, update, R.BROKER_ORDER_FAILED)
            self._positions.pop(key, None)
        return update

    # ------------------------------------------------------------------ Trade
    def _build_trade(
        self,
        position: _LivePosition,
        exit_price: float,
        exit_reason: ExitReason,
        exit_commission: float,
        commission_reported: bool,
    ) -> SimulatedTrade:
        """Aus echten Fuellungen denselben Datensatz bauen wie die Simulation.

        Bewusst derselbe Typ: Kennzahlen, Speicher und Bericht sollen nicht
        zwischen "echt" und "simuliert" unterscheiden muessen. Der Unterschied
        steht in den Zahlen - `planned_entry` gegen `entry_price` - und nicht
        in zwei parallelen Auswertungen.
        """
        signal = position.order.signal
        quantity = position.quantity
        direction = 1.0 if position.is_bullish else -1.0

        points = (exit_price - position.entry_price) * direction
        gross = self.instrument.points_to_currency(points, quantity)

        commission = position.entry_commission + exit_commission
        if not commission_reported and commission == 0.0:
            # Der Broker hat (noch) nichts gemeldet. Lieber die konfigurierte
            # Schaetzung als eine Null, die das Ergebnis schoenrechnet.
            commission = self.params.commission_per_contract * quantity
            log.warning(
                "gebuehren_geschaetzt",
                symbol=self.symbol,
                trade_id=signal.trade_id,
                geschaetzt=commission,
            )
        pnl = gross - commission

        risk_points = abs(position.entry_price - signal.stop)
        risk_amount = self.instrument.points_to_currency(risk_points, quantity)

        favourable = (
            position.high_water - position.entry_price
            if position.is_bullish
            else position.entry_price - position.low_water
        )
        adverse = (
            position.entry_price - position.low_water
            if position.is_bullish
            else position.high_water - position.entry_price
        )

        return SimulatedTrade(
            trade_id=signal.trade_id,
            setup_id=signal.setup_id,
            symbol=signal.symbol,
            strategy=signal.strategy,
            direction=signal.direction,
            quantity=quantity,
            planned_entry=signal.entry,
            stop=signal.stop,
            target=signal.target,
            planned_rr=signal.rr,
            planned_stop_ticks=signal.stop_ticks,
            stop_anchor=signal.stop_anchor,
            target_source=signal.target_source,
            timeframe=signal.timeframe,
            htf_bias=position.order.htf_bias,
            session=position.order.session,
            trading_day=position.order.trading_day,
            signal_ts=signal.entry_ts,
            entry_ts=position.entry_ts,
            entry_index=position.entry_index,
            entry_price=position.entry_price,
            exit_ts=int(self.clock() * 1e9),
            exit_index=self._last_index,
            exit_price=exit_price,
            exit_reason=exit_reason,
            bars_held=max(self._last_index - position.entry_index, 0),
            risk_points=risk_points,
            risk_amount=risk_amount,
            gross_pnl=gross,
            commission=commission,
            pnl=pnl,
            r_multiple=pnl / risk_amount if risk_amount else 0.0,
            mae_points=max(adverse, 0.0),
            mfe_points=max(favourable, 0.0),
        )
