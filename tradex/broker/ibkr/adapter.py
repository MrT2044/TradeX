"""Die Verbindung zu IB Gateway.

Die EINZIGE Datei im Projekt, die `ibapi` importiert. Alles darueber spricht
`BrokerInterface` und die DTOs aus `tradex/broker/types.py`.

Zwei Faeden, eine Richtung
--------------------------
    ibapi-Lesefaden                     Sitzungsfaden
    EWrapper-Callbacks   --Queue-->     SessionRunner-Schleife
    schreiben NUR hinein                `drain_events()` holt ab

Der Lesefaden fasst nichts an ausser der Queue und ein paar Ereignisflaggen.
Kein Datenbankzugriff, keine Strategie, kein `sleep`. Der Grund ist derselbe
wie beim NinjaTrader-Feed: haengt der Callback-Faden, hoert IBKR auf zu
liefern - und zwar auch die Fuellmeldungen der Orders, die gerade im Markt
liegen. Ein blockierender Callback ist damit kein Leistungsproblem, sondern
Blindflug mit offener Position.

Umgekehrt darf `drain_events()` nie blockieren: der Aufrufer ist die
Ueberwachungsschleife, und die darf nicht auf den Broker warten.

Was hier NICHT passiert
-----------------------
Kein Wiederverbinden von selbst, keine Order aus dem Nichts, keine
Handelsentscheidung. Faellt die Verbindung, wird das gemeldet - was daraus
folgt, entscheidet die Sitzung ueber `RiskEngine.halt_reason`. Die Bars laufen
weiter, damit offene Positionen beobachtet bleiben (dieselbe Begruendung wie
beim bestehenden Not-Aus).

Der Live-Port wird strukturell nie gewaehlt
-------------------------------------------
`_chosen_port()` kennt nur `paper_port`. Es gibt keinen Zweig, der `live_port`
zurueckgibt - auch nicht, wenn die Konfiguration es verlangte. Live-Handel ist
Phase 9 und braucht eine eigene, ausdrueckliche Aenderung an dieser Stelle.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from itertools import count
from queue import Empty, Queue
from typing import Any

from tradex.analysis import reasons as R
from tradex.broker import journal as J
from tradex.broker.base import BrokerError, BrokerNotConnected
from tradex.broker.env import EnvOverrides, read_env
from tradex.broker.guard import check_configuration, check_port, confirm_paper_account
from tradex.broker.ibkr.contracts import (
    ContractRegistry,
    ContractResolution,
    build_contract,
    judge_matches,
    match_from_details,
    order_contract,
)
from tradex.broker.ibkr.orders import (
    CONNECTION_LOST_CODES,
    CONNECTION_OK_CODES,
    INFO_CODES,
    OrderIdAllocator,
    OrderPlan,
    build_bracket,
    is_connection_code,
    kind_from_ib,
    map_status,
    required_order_ids,
    side_from_action,
    state_for_error,
    to_ibapi_order,
)
from tradex.broker.journal import TradeJournal, utc_now
from tradex.broker.types import (
    ROLE_ENTRY,
    AccountInfo,
    BrokerEvent,
    BrokerOrder,
    BrokerPosition,
    Fill,
    OrderKind,
    OrderRequest,
    OrderSide,
    OrderState,
)
from tradex.config import Config
from tradex.domain.instruments import Instrument
from tradex.logging_setup import get_logger

try:
    from ibapi.client import EClient
    from ibapi.contract import Contract
    from ibapi.execution import ExecutionFilter
    from ibapi.wrapper import EWrapper
except ImportError as _fehlt:  # pragma: no cover - haengt an der Installation
    raise ImportError(
        "Die TWS API fehlt. Sie kommt NICHT von PyPI, sondern aus dem Installer "
        "von https://interactivebrokers.github.io/ - danach:\n"
        '    .\\.venv\\Scripts\\python.exe -m pip install "C:\\TWS API\\source\\pythonclient"\n'
        "Ohne sie laeuft alles ausser der Orderanbindung unveraendert weiter."
    ) from _fehlt

log = get_logger(__name__)

#: Kennungen fuer Datenabfragen. Beginnt hoch, damit sie sich in Protokollen
#: nie mit Ordernummern verwechseln lassen - die zaehlt IBKR ab kleinen Werten.
_REQUEST_ID_START = 900_000

#: Kontokennzahlen, die abgefragt werden. `AccountType` ist der Grund fuer die
#: Abfrage: er ist der einzige Hinweis, den die API selbst zur Kontoart gibt -
#: er reicht als Nachweis nicht, fehlen darf er trotzdem nicht.
_SUMMARY_TAGS = "AccountType,NetLiquidation,AvailableFunds,TotalCashValue"


@dataclass(slots=True)
class _Waiter:
    """Eine offene Abfrage: gesammelte Antworten plus das Ende-Signal."""

    done: threading.Event = field(default_factory=threading.Event)
    items: list[Any] = field(default_factory=list)
    error: str = ""

    def finish(self, error: str = "") -> None:
        self.error = error
        self.done.set()


class IbkrAdapter:
    """`BrokerInterface` fuer Interactive Brokers ueber IB Gateway."""

    name = "ibkr"

    def __init__(
        self,
        config: Config,
        instruments: Mapping[str, Instrument],
        journal: TradeJournal | None = None,
        env: EnvOverrides | None = None,
        allow_orders: bool = True,
    ) -> None:
        self.config = config
        self.params = config.broker
        self.instruments = {symbol.upper(): inst for symbol, inst in instruments.items()}
        self.env = env if env is not None else read_env(config.execution, config.broker)
        self.journal = journal or TradeJournal(
            broker=self.name, trading_mode=config.execution.mode.value
        )
        self.allow_orders = allow_orders
        """Aus heisst: dieser Adapter kann keine Order senden - nicht als
        Versprechen im aufrufenden Skript, sondern als Zweig, den es nicht
        gibt. `scripts/test_ibkr_connection.py` verlaesst sich darauf."""

        self.registry = ContractRegistry(required=self.params.ibkr.require_contract_details)
        self.account: AccountInfo | None = None

        self._client = _IbkrClient(self)
        self._reader: threading.Thread | None = None
        self._events: Queue[BrokerEvent] = Queue()
        self._ids = OrderIdAllocator()
        self._request_ids = count(_REQUEST_ID_START)

        self._state_lock = threading.Lock()
        self._waiters: dict[int, _Waiter] = {}
        self._orders: dict[int, BrokerOrder] = {}
        self._exec_orders: dict[str, int] = {}
        """execId -> orderId. Die Gebuehrenmeldung kennt nur die execId."""
        self._symbol_by_conid: dict[int, str] = {}
        self._accounts: tuple[str, ...] = ()
        self._accounts_seen = threading.Event()
        self._positions: list[BrokerPosition] = []
        self._positions_done = threading.Event()
        self._open_orders_done = threading.Event()
        self._connection_ok = False

    # ------------------------------------------------------------- Verbindung
    def _chosen_port(self) -> int:
        """Immer der Paper-Port. Es gibt hier keinen zweiten Zweig."""
        return self.params.ibkr.paper_port

    def connect(self) -> None:
        """Verbinden und den gesamten Nachweis fuehren - oder gar nicht.

        Faellt eine Stufe durch, wird die Verbindung wieder getrennt. Eine
        offene Sitzung, die nicht handeln darf, waere ein Zustand beim Broker,
        den niemand braucht - und eine Einladung, ihn spaeter "kurz" doch zu
        benutzen.
        """
        gate = check_configuration(self.config.execution, self.params, self.env)
        if not gate.approved:
            code = gate.blocking_code
            self.journal.blocked(code, detail=str(gate.blocking_reason))
            if self.allow_orders:
                raise BrokerError(f"Orderanbindung gesperrt: {code}")
            # Ohne Orderrecht schuetzt die Kette nichts mehr, was hier noch
            # passieren koennte: es gibt keinen Sendeweg. Sie trotzdem den
            # SOCKET verhindern zu lassen waere verkehrt herum - man muesste
            # den Handel scharfschalten, nur um zu pruefen, ob die Verbindung
            # ueberhaupt steht. Genau das soll der Verbindungstest
            # beantworten, BEVOR irgendetwas eingeschaltet wird.
            log.info("ibkr_nur_lesend", grund=code)

        port = self._chosen_port()
        port_reason = check_port(self.params, port)
        if not port_reason.ok:
            self.journal.blocked(R.BROKER_PORT_NOT_PAPER, detail=str(port_reason.params))
            raise BrokerError(f"Port {port} ist nicht der Paper-Port")

        host = self.params.ibkr.host
        client_id = self.params.ibkr.client_id
        timeout = self.params.ibkr.connect_timeout_seconds

        self._client.connect(host, port, client_id)
        self._reader = threading.Thread(target=self._client.run, name="ibkr-reader", daemon=True)
        self._reader.start()

        # `nextValidId` ist die Empfangsbestaetigung der API. Ohne sie steht
        # zwar ein Socket, aber es antwortet niemand.
        if not self._ids.wait_until_seeded(timeout):
            self.disconnect()
            raise BrokerError(
                f"IB Gateway auf {host}:{port} antwortet nicht "
                f"(kein nextValidId in {timeout} s) - laeuft es, und ist "
                '"Enable ActiveX and Socket Clients" gesetzt?'
            )
        self._connection_ok = True
        self.journal.event(
            J.CONNECTION_ESTABLISHED, host=host, port=port, client_id=client_id
        )
        self._after_connect(timeout)

    def _after_connect(self, timeout: float) -> None:
        """Die Reihenfolge nach dem Verbindungsaufbau - auch nach Reconnect.

        Erst wissen, wo man ist, dann was dort liegt, dann handeln duerfen.
        Jede andere Reihenfolge erzeugt ein Fenster, in dem gehandelt wird,
        bevor der eigene Bestand bekannt ist.
        """
        self._confirm_account(timeout)
        self._resolve_contracts(timeout)
        self._synchronise_state(timeout)

    def _confirm_account(self, timeout: float) -> None:
        if not self._accounts_seen.wait(timeout):
            self.disconnect()
            raise BrokerError("IB Gateway hat kein Konto gemeldet (managedAccounts blieb leer)")

        summary = self._request_account_summary(timeout)
        account = confirm_paper_account(
            self.params,
            self._accounts,
            account_type=str(summary.get("AccountType", "")),
        )
        account.currency = str(summary.get("_currency", ""))
        account.net_liquidation = _as_float(summary.get("NetLiquidation"))
        account.available_funds = _as_float(summary.get("AvailableFunds"))
        self.account = account
        self.journal.bind_account(account.account)
        self.journal.event(
            J.ACCOUNT_VALIDATED,
            account=account.account,
            account_type=account.account_type,
            currency=account.currency,
            net_liquidation=account.net_liquidation,
        )

        if not account.is_paper:
            # Der teuerste denkbare Fehler waere eine Order auf einem Konto,
            # von dem niemand nachweisen kann, dass es ein Paper-Konto ist.
            self.journal.blocked(
                R.BROKER_ACCOUNT_UNCONFIRMED,
                detail=account.paper_evidence,
                account=account.account,
            )
            if self.allow_orders:
                self.disconnect()
                raise BrokerError(
                    f"Konto {account.account!r} ist nicht als Paper-Konto belegt: "
                    f"{account.paper_evidence}. Nach dem ersten Verbindungstest die "
                    "Kontonummer in broker.ibkr.allowed_accounts eintragen."
                )
            # Ohne Orderrecht wird gemeldet statt getrennt: die Kontonummer ist
            # genau die Angabe, die man braucht, um sie danach einzutragen.
            # Sie zu verschweigen und aufzulegen macht den Test nutzlos.
            log.warning(
                "ibkr_konto_unbestaetigt",
                account=account.account,
                nachweis=account.paper_evidence,
            )
            return
        self.journal.event(
            J.PAPER_ACCOUNT_CONFIRMED,
            account=account.account,
            nachweis=account.paper_evidence,
        )

    def _request_account_summary(self, timeout: float) -> dict[str, str]:
        req_id = next(self._request_ids)
        waiter = self._new_waiter(req_id)
        self._client.reqAccountSummary(req_id, "All", _SUMMARY_TAGS)
        waiter.done.wait(timeout)
        self._client.cancelAccountSummary(req_id)
        self._drop_waiter(req_id)

        summary: dict[str, str] = {}
        for tag, value, currency in waiter.items:
            summary[tag] = value
            if currency:
                summary["_currency"] = currency
        return summary

    def _resolve_contracts(self, timeout: float) -> None:
        """Jedes Instrument genau einmal aufloesen - oder sperren."""
        for symbol, instrument in sorted(self.instruments.items()):
            resolution, contract = self._resolve_one(symbol, instrument, timeout)
            self.registry.record(resolution, contract)
            if resolution.ok and resolution.con_id:
                self._symbol_by_conid[resolution.con_id] = symbol
            self.journal.event(
                J.CONTRACT_RESOLVED,
                message=resolution.describe(),
                symbol=symbol,
                con_id=resolution.con_id or None,
                treffer=resolution.matches,
                gesperrt=None if resolution.ok else resolution.reason_code,
            )

    def _resolve_one(
        self, symbol: str, instrument: Instrument, timeout: float
    ) -> tuple[ContractResolution, Any]:
        spec = instrument.ibkr
        if spec is None or not spec.is_complete:
            return judge_matches(symbol, spec, ()), None

        if not self.registry.required:
            # Ausdruecklich abgeschaltet: dann wird die Beschreibung aus der
            # YAML gesendet, wie sie dasteht. Das ist Raten mit Ansage - der
            # Schalter steht in der Config, damit die Entscheidung sichtbar
            # bleibt.
            resolution = ContractResolution(
                symbol=symbol,
                ok=True,
                detail="ungeprueft (require_contract_details: false)",
                matches=1,
            )
            return resolution, build_contract(spec)

        req_id = next(self._request_ids)
        waiter = self._new_waiter(req_id)
        self._client.reqContractDetails(req_id, build_contract(spec))
        waiter.done.wait(timeout)
        self._drop_waiter(req_id)

        candidates = tuple(waiter.items)
        if not candidates and waiter.error:
            return (
                ContractResolution(
                    symbol=symbol,
                    ok=False,
                    detail=waiter.error,
                    reason_code=R.BROKER_CONTRACT_UNKNOWN,
                ),
                None,
            )
        resolution = judge_matches(symbol, spec, candidates)
        if not resolution.ok:
            return resolution, None
        return resolution, order_contract(candidates[0], spec.exchange)

    def _synchronise_state(self, timeout: float) -> None:
        """Was beim Broker liegt, bevor diese Sitzung etwas tut.

        Nach einem Reconnect ist das der einzige Weg, eigene Orders von frueher
        wiederzufinden: `orderRef` traegt den `order_key`, den diese Sitzung
        vergeben hat.
        """
        positions = self.get_positions(timeout)
        orders = self.get_open_orders(timeout)
        req_id = next(self._request_ids)
        waiter = self._new_waiter(req_id)
        self._client.reqExecutions(req_id, ExecutionFilter())
        waiter.done.wait(timeout)
        self._drop_waiter(req_id)

        self.journal.event(
            J.STATE_SYNCHRONISED,
            positionen=len(positions),
            offene_orders=len(orders),
            ausfuehrungen=len(waiter.items),
            fremde_orders=sum(1 for order in orders if not order.order_key.startswith("tx-")),
        )

    def disconnect(self) -> None:
        self._connection_ok = False
        try:
            self._client.disconnect()
        except Exception as fehler:  # pragma: no cover - Abbau darf nie werfen
            log.warning("ibkr_trennen_fehlgeschlagen", fehler=str(fehler))
        reader, self._reader = self._reader, None
        if reader is not None and reader.is_alive():
            reader.join(timeout=5.0)
        # Offene Abfragen freigeben, sonst wartet ein Aufrufer auf eine
        # Antwort, die nach dem Trennen nie kommt.
        with self._state_lock:
            waiters = list(self._waiters.values())
            self._waiters.clear()
        for waiter in waiters:
            waiter.finish("Verbindung getrennt")

    def is_connected(self) -> bool:
        return bool(self._connection_ok and self._client.isConnected())

    # ---------------------------------------------------------------- Auskunft
    def get_account_info(self) -> AccountInfo:
        if self.account is None:
            raise BrokerNotConnected("Kontodaten liegen erst nach connect() vor")
        return self.account

    def get_positions(self, timeout: float | None = None) -> list[BrokerPosition]:
        wait = timeout if timeout is not None else self.params.ibkr.connect_timeout_seconds
        with self._state_lock:
            self._positions = []
        self._positions_done.clear()
        self._client.reqPositions()
        self._positions_done.wait(wait)
        self._client.cancelPositions()
        with self._state_lock:
            return list(self._positions)

    def get_open_orders(self, timeout: float | None = None) -> list[BrokerOrder]:
        wait = timeout if timeout is not None else self.params.ibkr.connect_timeout_seconds
        self._open_orders_done.clear()
        self._client.reqOpenOrders()
        self._open_orders_done.wait(wait)
        with self._state_lock:
            return [replace(order) for order in self._orders.values() if order.state.is_live]

    def can_trade(self, symbol: str) -> tuple[bool, str]:
        if not self.is_connected():
            return False, "keine Verbindung"
        return self.registry.can_trade(symbol)

    # ------------------------------------------------------------------ Orders
    def place_market_order(self, request: OrderRequest) -> BrokerOrder:
        return self._place(request)

    def place_limit_order(self, request: OrderRequest) -> BrokerOrder:
        if request.kind is not OrderKind.LIMIT:
            request = replace(request, kind=OrderKind.LIMIT)
        return self._place(request)

    def _place(self, request: OrderRequest) -> BrokerOrder:
        """Bracket bauen, eintragen, senden - in dieser Reihenfolge.

        Eingetragen wird VOR dem Senden. Die erste Rueckmeldung kann eintreffen,
        waehrend `placeOrder` noch laeuft; faende der Callback die Order dann
        nicht, ginge die erste Zustandsmeldung verloren.
        """
        if not self.allow_orders:
            raise BrokerError("Dieser Adapter wurde ohne Orderrecht gebaut (allow_orders=False)")
        if not self.is_connected():
            raise BrokerNotConnected("keine Verbindung zu IB Gateway")

        symbol = request.symbol.upper()
        tradeable, detail = self.registry.can_trade(symbol)
        if not tradeable:
            raise BrokerError(f"{symbol} ist nicht handelbar: {detail}")
        instrument = self.instruments.get(symbol)
        if instrument is None:
            raise BrokerError(f"{symbol} ist diesem Adapter nicht bekannt")

        contract = self.registry.contract(symbol)
        ids = self._ids.take(required_order_ids(request))
        plans = build_bracket(
            request,
            instrument,
            ids,
            outside_rth=self.params.ibkr.outside_rth,
            account=self.account.account if self.account is not None else "",
        )

        orders = {plan.role: self._register(plan, symbol) for plan in plans}
        for plan in plans:
            self._client.placeOrder(plan.order_id, contract, to_ibapi_order(plan))

        entry = orders[ROLE_ENTRY]
        log.info(
            "order_gesendet",
            symbol=symbol,
            order_key=request.order_key,
            order_id=entry.order_id,
            teile=len(plans),
        )
        return replace(entry)

    def _register(self, plan: OrderPlan, symbol: str) -> BrokerOrder:
        order = BrokerOrder(
            order_id=plan.order_id,
            order_key=plan.order_ref,
            symbol=symbol,
            side=side_from_action(plan.action),
            quantity=plan.quantity,
            kind=kind_from_ib(plan.order_type),
            state=OrderState.SUBMITTED,
            limit_price=plan.limit_price,
            stop_price=plan.stop_price,
            parent_order_id=plan.parent_id,
            updated_ts=time.time_ns(),
        )
        with self._state_lock:
            self._orders[plan.order_id] = order
        return order

    def cancel_order(self, order_id: int) -> None:
        if not self.is_connected():
            raise BrokerNotConnected("keine Verbindung zu IB Gateway")
        _call_cancel(self._client, order_id)

    def cancel_all_orders(self) -> None:
        """Nur die EIGENEN offenen Orders.

        `reqGlobalCancel` waere ein Aufruf - und wuerde auch Orders loeschen,
        die dieses Programm nie gesendet hat. Auf einem Konto, an dem auch ein
        Mensch arbeitet, ist das kein zulaessiger Eingriff.
        """
        with self._state_lock:
            offen = [order.order_id for order in self._orders.values() if order.state.is_live]
        for order_id in offen:
            try:
                self.cancel_order(order_id)
            except BrokerError as fehler:
                log.warning("order_stornieren_fehlgeschlagen", order_id=order_id, fehler=str(fehler))

    def close_position(self, symbol: str) -> BrokerOrder | None:
        """Eine Position glattstellen - eine nackte Market-Order, kein Bracket."""
        symbol = symbol.upper()
        position = next(
            (p for p in self.get_positions() if p.symbol == symbol and p.quantity != 0), None
        )
        if position is None:
            return None
        request = OrderRequest(
            order_key=f"flat-{symbol}-{time.time_ns()}",
            symbol=symbol,
            side=OrderSide.SELL if position.quantity > 0 else OrderSide.BUY,
            quantity=abs(position.quantity),
            kind=OrderKind.MARKET,
        )
        return self._place(request)

    def close_all_positions(self) -> list[BrokerOrder]:
        geschlossen: list[BrokerOrder] = []
        for position in self.get_positions():
            if position.quantity == 0:
                continue
            order = self.close_position(position.symbol)
            if order is not None:
                geschlossen.append(order)
        return geschlossen

    # ------------------------------------------------------------------ Ablauf
    def drain_events(self) -> list[BrokerEvent]:
        """Alles abholen, was seit dem letzten Aufruf angekommen ist."""
        events: list[BrokerEvent] = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except Empty:
                return events

    def _emit(self, event: BrokerEvent) -> None:
        self._events.put(event)

    # ------------------------------------------------------------- Callback-Weg
    # Alles ab hier laeuft im ibapi-Lesefaden. Diese Methoden duerfen nichts
    # tun ausser Zustand fortschreiben und in die Queue schreiben.
    def _new_waiter(self, req_id: int) -> _Waiter:
        waiter = _Waiter()
        with self._state_lock:
            self._waiters[req_id] = waiter
        return waiter

    def _drop_waiter(self, req_id: int) -> None:
        with self._state_lock:
            self._waiters.pop(req_id, None)

    def _waiter(self, req_id: int) -> _Waiter | None:
        with self._state_lock:
            return self._waiters.get(req_id)

    def _on_next_valid_id(self, order_id: int) -> None:
        self._ids.seed(order_id)

    def _on_managed_accounts(self, accounts_list: str) -> None:
        self._accounts = tuple(
            entry.strip() for entry in accounts_list.split(",") if entry.strip()
        )
        self._accounts_seen.set()

    def _on_account_summary(self, req_id: int, tag: str, value: str, currency: str) -> None:
        waiter = self._waiter(req_id)
        if waiter is not None:
            waiter.items.append((tag, value, currency))

    def _on_contract_details(self, req_id: int, details: Any) -> None:
        waiter = self._waiter(req_id)
        if waiter is not None:
            waiter.items.append(match_from_details(details))

    def _on_end(self, req_id: int) -> None:
        waiter = self._waiter(req_id)
        if waiter is not None:
            waiter.finish()

    def _on_position(self, account: str, contract: Any, quantity: float, avg_cost: float) -> None:
        with self._state_lock:
            self._positions.append(
                BrokerPosition(
                    symbol=self._symbol_for(contract),
                    quantity=_as_int(quantity),
                    avg_price=float(avg_cost),
                    account=account,
                )
            )

    def _on_position_end(self) -> None:
        self._positions_done.set()

    def _on_open_order(self, order_id: int, contract: Any, order: Any) -> None:
        """Eine Order, wie der Broker sie kennt - auch eine aus einer frueheren
        Sitzung. Der `orderRef` ist der Faden zurueck zum eigenen Signal.
        """
        ref = str(getattr(order, "orderRef", "") or "")
        with self._state_lock:
            bekannt = self._orders.get(order_id)
            if bekannt is None:
                self._orders[order_id] = BrokerOrder(
                    order_id=order_id,
                    order_key=ref,
                    symbol=self._symbol_for(contract),
                    side=side_from_action(str(order.action)),
                    quantity=_as_int(order.totalQuantity),
                    kind=kind_from_ib(str(order.orderType)),
                    limit_price=_as_float(getattr(order, "lmtPrice", 0.0)),
                    stop_price=_as_float(getattr(order, "auxPrice", 0.0)),
                    parent_order_id=int(getattr(order, "parentId", 0) or 0),
                    updated_ts=time.time_ns(),
                )
            elif ref and not bekannt.order_key:
                bekannt.order_key = ref

    def _on_open_order_end(self) -> None:
        self._open_orders_done.set()

    def _on_order_status(
        self,
        order_id: int,
        status: str,
        filled: float,
        remaining: float,
        avg_fill_price: float,
        parent_id: int,
    ) -> None:
        filled_qty = _as_int(filled)
        state = map_status(status, filled_qty, _as_int(remaining))
        with self._state_lock:
            order = self._orders.get(order_id)
            if order is None:
                # Eine Order, die dieses Programm nicht kennt. Sie gehoert einer
                # anderen Sitzung oder einem Menschen - nicht anfassen, nur
                # vermerken.
                log.info("fremde_order_gemeldet", order_id=order_id, status=status)
                return
            if state is None:
                log.warning("ibkr_status_unbekannt", order_id=order_id, status=status)
                return
            order.state = state
            order.filled_quantity = filled_qty
            order.avg_fill_price = _as_float(avg_fill_price)
            if parent_id:
                order.parent_order_id = int(parent_id)
            order.updated_ts = time.time_ns()
            momentaufnahme = replace(order)

        self._emit(
            BrokerEvent(
                kind="order",
                ts_utc=utc_now(),
                order_key=momentaufnahme.order_key,
                order_id=order_id,
                state=state,
                payload={"order": momentaufnahme, "status": status},
            )
        )

    def _on_exec_details(self, req_id: int, contract: Any, execution: Any) -> None:
        order_id = int(getattr(execution, "orderId", 0) or 0)
        exec_id = str(getattr(execution, "execId", "") or "")
        fill = Fill(
            order_id=order_id,
            quantity=_as_int(getattr(execution, "shares", 0)),
            price=_as_float(getattr(execution, "price", 0.0)),
            ts_utc=str(getattr(execution, "time", "")),
            exec_id=exec_id,
        )
        with self._state_lock:
            if exec_id:
                self._exec_orders[exec_id] = order_id
        waiter = self._waiter(req_id)
        if waiter is not None:
            waiter.items.append(fill)
        self._emit(
            BrokerEvent(
                kind="fill",
                ts_utc=utc_now(),
                order_id=order_id,
                payload={"fill": fill, "symbol": self._symbol_for(contract)},
            )
        )

    def _on_commission(self, exec_id: str, commission: float) -> None:
        """Gebuehren nachtragen.

        Sie kommen als eigener Callback und oft NACH der Fuellmeldung. Trifft
        das zu, hat `BrokerExecutor` den Trade bereits gebaut und schaetzt die
        Gebuehren aus der Config - mit Warnung. Hier nachtraeglich ein zweites
        Order-Ereignis zu erzeugen waere schlimmer: es saehe im Protokoll aus
        wie eine zweite Fuellung.
        """
        with self._state_lock:
            order_id = self._exec_orders.get(exec_id, 0)
            order = self._orders.get(order_id)
            if order is not None:
                order.commission += float(commission)
                order.commission_reported = True
        self._emit(
            BrokerEvent(
                kind="fill",
                ts_utc=utc_now(),
                order_id=order_id,
                payload={"commission": float(commission), "exec_id": exec_id},
            )
        )

    def _on_error(self, req_id: int, code: int, message: str) -> None:
        if code in INFO_CODES:
            log.info("ibkr_meldung", code=code, text=message)
            return

        if is_connection_code(code):
            verbunden = code in CONNECTION_OK_CODES
            self._connection_ok = verbunden
            self._emit(
                BrokerEvent(
                    kind="connection",
                    ts_utc=utc_now(),
                    payload={"connected": verbunden, "code": code, "message": message},
                )
            )
            log.warning("ibkr_verbindung", code=code, text=message, verbunden=verbunden)
            if code in CONNECTION_LOST_CODES:
                # Wer auf eine Antwort wartet, bekommt jetzt keine mehr.
                self._release_all_waiters(f"{code}: {message}")
            return

        state = state_for_error(code)
        with self._state_lock:
            order = self._orders.get(req_id)
            if order is not None and state is not None:
                order.state = state
                order.error = f"{code}: {message}"
                order.updated_ts = time.time_ns()
                momentaufnahme: BrokerOrder | None = replace(order)
            else:
                momentaufnahme = None

        if momentaufnahme is not None and state is not None:
            # Der Zustand REJECTED entsteht ausschliesslich hier: IBKR kennt
            # ihn als Status nicht.
            self._emit(
                BrokerEvent(
                    kind="order",
                    ts_utc=utc_now(),
                    order_key=momentaufnahme.order_key,
                    order_id=momentaufnahme.order_id,
                    state=state,
                    payload={"order": momentaufnahme, "code": code, "message": message},
                )
            )
            return

        waiter = self._waiter(req_id)
        if waiter is not None:
            # Eine fehlgeschlagene Abfrage muss den Wartenden freigeben - sonst
            # haengt `connect()` bis zum Timeout an einem Kontrakt, den es gar
            # nicht gibt (Fehler 200 schickt kein `contractDetailsEnd`).
            waiter.finish(f"{code}: {message}")
            return

        self._emit(
            BrokerEvent(
                kind="error",
                ts_utc=utc_now(),
                order_id=req_id if req_id > 0 else 0,
                payload={"code": code, "message": message},
            )
        )
        log.error("ibkr_fehler", req_id=req_id, code=code, text=message)

    def _release_all_waiters(self, grund: str) -> None:
        with self._state_lock:
            waiters = list(self._waiters.values())
        for waiter in waiters:
            waiter.finish(grund)

    def _on_connection_closed(self) -> None:
        self._connection_ok = False
        self._emit(
            BrokerEvent(
                kind="connection",
                ts_utc=utc_now(),
                payload={"connected": False, "message": "connectionClosed"},
            )
        )
        self._release_all_waiters("Verbindung geschlossen")

    def _symbol_for(self, contract: Any) -> str:
        """IBKR-Kontrakt -> TradeX-Symbol.

        Ueber die Kontraktnummer, weil sie eindeutig ist. Ist sie unbekannt,
        wird der IBKR-Name durchgereicht statt geraten - eine Position unter
        falschem Symbol waere schlimmer als eine unter fremdem Namen.
        """
        con_id = int(getattr(contract, "conId", 0) or 0)
        bekannt = self._symbol_by_conid.get(con_id)
        if bekannt is not None:
            return bekannt
        return str(getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "") or "")


class _IbkrClient(EWrapper, EClient):  # type: ignore[misc]
    """Der Lesefaden. Reicht alles weiter und entscheidet nichts.

    Die Callback-Namen sind von IBKR vorgegeben (camelCase) und weichen
    deshalb von den Konventionen dieses Projekts ab - sie zu uebersetzen wuerde
    bedeuten, sie gar nicht erst zu bekommen.
    """

    def __init__(self, adapter: IbkrAdapter) -> None:
        EWrapper.__init__(self)
        EClient.__init__(self, wrapper=self)
        self._adapter = adapter

    # --- Verbindung ---------------------------------------------------------
    def nextValidId(self, orderId: int) -> None:
        self._adapter._on_next_valid_id(orderId)

    def managedAccounts(self, accountsList: str) -> None:
        self._adapter._on_managed_accounts(accountsList)

    def connectionClosed(self) -> None:
        self._adapter._on_connection_closed()

    def error(self, *args: Any) -> None:
        """Fehler-Callback in beiden bekannten Signaturen.

        Bis `ibapi` 10.19: (reqId, code, text[, advanced]).
        Ab 10.30 kam `errorTime` als zweites Argument dazu. Welche Fassung der
        TWS-API-Installer liefert, laesst sich vorab nicht feststellen - also
        wird die Form an der Stelle erkannt, an der sie sich unterscheidet.
        """
        if not args:
            return
        req_id = int(args[0]) if isinstance(args[0], int) else -1
        if len(args) >= 3 and isinstance(args[2], str):
            code, text = args[1], args[2]
        elif len(args) >= 4:
            code, text = args[2], args[3]
        else:  # pragma: no cover - unbekannte Fassung
            log.warning("ibkr_fehlersignatur_unbekannt", argumente=len(args))
            return
        self._adapter._on_error(req_id, int(code), str(text))

    # --- Konto und Kontrakte ------------------------------------------------
    def accountSummary(
        self, reqId: int, account: str, tag: str, value: str, currency: str
    ) -> None:
        self._adapter._on_account_summary(reqId, tag, value, currency)

    def accountSummaryEnd(self, reqId: int) -> None:
        self._adapter._on_end(reqId)

    def contractDetails(self, reqId: int, contractDetails: Any) -> None:
        self._adapter._on_contract_details(reqId, contractDetails)

    def contractDetailsEnd(self, reqId: int) -> None:
        self._adapter._on_end(reqId)

    # --- Bestand ------------------------------------------------------------
    def position(self, account: str, contract: Contract, position: Any, avgCost: float) -> None:
        self._adapter._on_position(account, contract, position, avgCost)

    def positionEnd(self) -> None:
        self._adapter._on_position_end()

    def openOrder(self, orderId: int, contract: Contract, order: Any, orderState: Any) -> None:
        self._adapter._on_open_order(orderId, contract, order)

    def openOrderEnd(self) -> None:
        self._adapter._on_open_order_end()

    # --- Orders und Fuellungen ----------------------------------------------
    def orderStatus(
        self,
        orderId: int,
        status: str,
        filled: Any,
        remaining: Any,
        avgFillPrice: float,
        permId: int,
        parentId: int,
        lastFillPrice: float,
        clientId: int,
        whyHeld: str,
        mktCapPrice: float = 0.0,
    ) -> None:
        self._adapter._on_order_status(
            orderId, status, filled, remaining, avgFillPrice, parentId
        )

    def execDetails(self, reqId: int, contract: Contract, execution: Any) -> None:
        self._adapter._on_exec_details(reqId, contract, execution)

    def execDetailsEnd(self, reqId: int) -> None:
        self._adapter._on_end(reqId)

    def commissionReport(self, commissionReport: Any) -> None:
        self._adapter._on_commission(
            str(getattr(commissionReport, "execId", "")),
            _as_float(getattr(commissionReport, "commission", 0.0)),
        )

    def commissionAndFeesReport(self, commissionAndFeesReport: Any) -> None:
        """Der Name ab `ibapi` 10.30. Inhaltlich derselbe Callback."""
        self._adapter._on_commission(
            str(getattr(commissionAndFeesReport, "execId", "")),
            _as_float(getattr(commissionAndFeesReport, "commissionAndFees", 0.0)),
        )


# ------------------------------------------------------------------ Werkzeug
def _call_cancel(client: Any, order_id: int) -> None:
    """`cancelOrder` ueber alle bekannten Signaturen hinweg.

    9.81 nimmt nur die Ordernummer, 10.x zusaetzlich einen Zeitstempel, 10.30
    ein `OrderCancel`-Objekt. Die Alternative waere, die Version abzufragen -
    das waere eine weitere Annahme ueber ein Paket, das von Hand installiert
    wird.
    """
    for argumente in ((order_id, ""), (order_id,)):
        try:
            client.cancelOrder(*argumente)
            return
        except TypeError:
            continue

    from ibapi.order_cancel import OrderCancel

    client.cancelOrder(order_id, OrderCancel())


def _as_int(value: Any) -> int:
    """Mengen kommen je nach Fassung als int, float oder Decimal."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


__all__ = ["IbkrAdapter"]
