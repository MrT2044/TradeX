"""Das IBKR-Unterpaket: Importgrenze, Bracket-Aufbau, Zustandsabbildung.

Diese Tests brauchen KEIN laufendes IB Gateway. Das ist Absicht und nicht
Bequemlichkeit: die Eigenschaften, auf die es ankommt - uebertraegt ein Kind
vor seinem Elternteil, wird ein mehrdeutiger Kontrakt gehandelt, gilt eine
Order faelschlich als abgeschlossen - lassen sich an einer echten Verbindung
gar nicht auf Bestellung herstellen.

Was hier NICHT geprueft wird, und das muss deutlich bleiben: ob IB Gateway die
so gebauten Orders auch annimmt. Das kann nur ein Lauf gegen ein echtes
Paper-Konto zeigen.
"""

from __future__ import annotations

import ast

import pytest

from tests.conftest import PROJECT_ROOT
from tradex.analysis import reasons as R
from tradex.broker.base import BrokerError as BrokerErrorAlias
from tradex.broker.ibkr.contracts import (
    ContractMatch,
    ContractRegistry,
    build_contract,
    judge_matches,
    order_contract,
)
from tradex.broker.ibkr.orders import (
    IB_LIMIT,
    IB_MARKET,
    IB_STOP,
    OrderIdAllocator,
    build_bracket,
    check_transmit_sequence,
    map_status,
    required_order_ids,
    state_for_error,
    to_ibapi_order,
)
from tradex.broker.types import (
    ROLE_ENTRY,
    ROLE_STOP,
    ROLE_TARGET,
    OrderRequest,
    OrderSide,
    OrderState,
)
from tradex.domain.instruments import IbkrContract, Instrument


def _request(**overrides) -> OrderRequest:
    basis = {
        "order_key": "tx-s3-17",
        "symbol": "MNQ",
        "side": OrderSide.BUY,
        "quantity": 2,
        "stop_loss": 20980.0,
        "take_profit": 21040.0,
    }
    return OrderRequest(**{**basis, **overrides})


# ------------------------------------------------------------- Importgrenze
def test_ibapi_wird_nur_im_ibkr_unterpaket_importiert():
    """Die Grenze, die das ganze Paket zusammenhaelt.

    Sickerte ein `ibapi`-Import nach draussen, waere ein zweiter Broker keine
    zweite Datei mehr, sondern ein Eingriff in den Betriebscode - und das
    restliche Projekt liesse sich ohne eine Bibliothek nicht mehr starten, die
    von Hand installiert werden muss.
    """
    erlaubt = (PROJECT_ROOT / "tradex" / "broker" / "ibkr").resolve()
    verstoesse: list[str] = []

    for datei in (PROJECT_ROOT / "tradex").rglob("*.py"):
        if erlaubt in datei.resolve().parents:
            continue
        # `utf-8-sig`: einige `__init__.py` tragen seit Phase 1 ein BOM. Python
        # selbst stoert das nicht, `ast.parse` schon.
        baum = ast.parse(datei.read_text(encoding="utf-8-sig"))
        for knoten in ast.walk(baum):
            if isinstance(knoten, ast.Import):
                namen = [alias.name for alias in knoten.names]
            elif isinstance(knoten, ast.ImportFrom):
                namen = [knoten.module or ""]
            else:
                continue
            if any(name == "ibapi" or name.startswith("ibapi.") for name in namen):
                relativ = datei.relative_to(PROJECT_ROOT)
                verstoesse.append(f"{relativ}:{knoten.lineno}")

    assert not verstoesse, "ibapi ausserhalb von tradex/broker/ibkr/: " + ", ".join(verstoesse)


def test_das_ibkr_paket_ist_ohne_adapter_importierbar():
    """`import tradex.broker.ibkr` darf `ibapi` nicht verlangen.

    Sonst haengt die gesamte Testsammlung an einer Bibliothek, die nicht von
    PyPI kommt.
    """
    quelle = (PROJECT_ROOT / "tradex" / "broker" / "ibkr" / "__init__.py").read_text(
        encoding="utf-8-sig"
    )
    baum = ast.parse(quelle)
    modulweit = [
        knoten
        for knoten in baum.body
        if isinstance(knoten, (ast.Import, ast.ImportFrom))
    ]
    for knoten in modulweit:
        namen = (
            [alias.name for alias in knoten.names]
            if isinstance(knoten, ast.Import)
            else [knoten.module or ""]
        )
        assert not any(n.startswith("ibapi") for n in namen)
        assert "adapter" not in " ".join(namen), "adapter.py zieht ibapi nach"


# ------------------------------------------------------------------ Bracket
def test_das_bracket_uebertraegt_erst_mit_der_letzten_order(mnq: Instrument):
    """Ein zu frueh uebertragenes Kind liegt als nackte Stop-Order im Markt -
    ohne Position dahinter. Der Standardfehler beim Bracket."""
    plans = build_bracket(_request(), mnq, (41, 42, 43))

    assert [p.role for p in plans] == [ROLE_ENTRY, ROLE_STOP, ROLE_TARGET]
    assert [p.transmit for p in plans] == [False, False, True]
    assert [p.order_type for p in plans] == [IB_MARKET, IB_STOP, IB_LIMIT]
    assert all(p.parent_id == 41 for p in plans[1:])
    assert plans[0].parent_id == 0


def test_ohne_ziel_uebertraegt_der_stop(mnq: Instrument):
    plans = build_bracket(_request(take_profit=0.0), mnq, (41, 42))
    assert [p.role for p in plans] == [ROLE_ENTRY, ROLE_STOP]
    assert [p.transmit for p in plans] == [False, True]


def test_ohne_stop_und_ziel_uebertraegt_der_einstieg_selbst(mnq: Instrument):
    plans = build_bracket(_request(stop_loss=0.0, take_profit=0.0), mnq, (41,))
    assert len(plans) == 1
    assert plans[0].transmit is True


def test_die_kinder_laufen_gtc_der_einstieg_nicht(mnq: Instrument):
    """Eine Position, deren Schutzorders am Abend verfallen, waere ueber Nacht
    ungesichert - und genau darauf beruht die Entscheidung, offene Positionen
    beim Sitzungsende offen zu lassen."""
    plans = build_bracket(_request(), mnq, (41, 42, 43))
    assert plans[0].tif == "DAY"
    assert all(p.tif == "GTC" for p in plans[1:])


def test_stop_und_ziel_liegen_auf_dem_tickraster(mnq: Instrument):
    """IBKR lehnt Preise ausserhalb des Rasters ab (Fehler 110)."""
    plans = build_bracket(_request(stop_loss=20980.13, take_profit=21040.07), mnq, (41, 42, 43))
    assert plans[1].stop_price == pytest.approx(20980.25)
    assert plans[2].limit_price == pytest.approx(21040.0)


def test_zu_wenige_ordernummern_werden_bemerkt(mnq: Instrument):
    with pytest.raises(ValueError, match="Ordernummern"):
        build_bracket(_request(), mnq, (41, 42))


def test_der_waechter_erkennt_ein_falsch_gebautes_bracket(mnq: Instrument):
    """Waechter gegen leere Wahrheit: die Pruefung muss auch anschlagen."""
    plans = list(build_bracket(_request(), mnq, (41, 42, 43)))
    from dataclasses import replace

    kaputt = [replace(plans[0], transmit=True), plans[1], plans[2]]
    assert "transmit=True" in check_transmit_sequence(kaputt)

    ohne_sender = [plans[0], plans[1], replace(plans[2], transmit=False)]
    assert "uebertraegt aber nicht" in check_transmit_sequence(ohne_sender)

    fremdes_kind = [plans[0], replace(plans[1], parent_id=999), plans[2]]
    assert "parentId=999" in check_transmit_sequence(fremdes_kind)


def test_required_order_ids(mnq: Instrument):
    assert required_order_ids(_request()) == 3
    assert required_order_ids(_request(take_profit=0.0)) == 2
    assert required_order_ids(_request(stop_loss=0.0, take_profit=0.0)) == 1


# --------------------------------------------------------------- Ordernummern
def test_ordernummern_werden_nie_doppelt_vergeben():
    """Eine doppelt vergebene Nummer waere beim Broker eine AENDERUNG der
    bestehenden Order statt einer neuen."""
    allocator = OrderIdAllocator()
    allocator.seed(100)
    vergeben = [n for _ in range(50) for n in allocator.take(3)]
    assert len(set(vergeben)) == len(vergeben)
    assert vergeben[0] == 100


def test_seed_zaehlt_nur_nach_oben():
    """IBKR schickt `nextValidId` bei JEDEM Reconnect - und dann kann sie
    niedriger sein als das, was diese Sitzung schon vergeben hat."""
    allocator = OrderIdAllocator()
    allocator.seed(500)
    allocator.take(5)
    allocator.seed(100)
    assert allocator.peek() == 505


def test_ohne_seed_gibt_es_keine_ordernummern():
    with pytest.raises(RuntimeError, match="nextValidId"):
        OrderIdAllocator().take(1)


# ------------------------------------------------------- Zustandsabbildung
@pytest.mark.parametrize(
    ("status", "filled", "remaining", "erwartet"),
    [
        ("PendingSubmit", 0, 2, OrderState.SUBMITTED),
        ("PreSubmitted", 0, 2, OrderState.SUBMITTED),
        ("Submitted", 0, 2, OrderState.ACCEPTED),
        ("Submitted", 1, 1, OrderState.PARTIALLY_FILLED),
        ("Filled", 2, 0, OrderState.FILLED),
        ("Filled", 1, 1, OrderState.PARTIALLY_FILLED),
        ("Cancelled", 0, 2, OrderState.CANCELLED),
        ("ApiCancelled", 0, 2, OrderState.CANCELLED),
        ("Inactive", 0, 2, OrderState.INACTIVE),
    ],
)
def test_statusabbildung(status: str, filled: int, remaining: int, erwartet: OrderState):
    assert map_status(status, filled, remaining) is erwartet


def test_pendingcancel_ist_kein_endzustand():
    """Die Order liegt noch im Markt und kann in genau diesem Moment fuellen."""
    zustand = map_status("PendingCancel", 0, 2)
    assert zustand is OrderState.ACCEPTED
    assert not zustand.is_terminal


def test_ein_unbekannter_status_aendert_nichts():
    """Lieber eine Order, die im Protokoll als unverstanden auftaucht, als
    eine, die faelschlich als abgeschlossen gilt."""
    assert map_status("Voellig Neuer Status", 0, 2) is None


def test_rejected_entsteht_nur_aus_fehlercodes():
    """IBKR kennt keinen Status "Rejected" - Ablehnungen kommen ueber
    `error()`. Wer nur `orderStatus` auswertet, sieht eine abgelehnte Order
    ewig auf "PreSubmitted" stehen."""
    assert OrderState.REJECTED not in set(
        map_status(s, 0, 1) for s in ("PendingSubmit", "Submitted", "Filled", "Cancelled")
    )
    assert state_for_error(201) is OrderState.REJECTED
    assert state_for_error(202) is OrderState.CANCELLED
    assert state_for_error(2104) is None  # Datenfarm-Meldung, kein Orderzustand


# ------------------------------------------------------------------ Kontrakte
def _spec(**overrides) -> IbkrContract:
    basis = {
        "symbol": "MNQ",
        "sec_type": "FUT",
        "exchange": "CME",
        "currency": "USD",
        "expiry": "202609",
    }
    return IbkrContract(**{**basis, **overrides})


def _match(con_id: int = 1, local: str = "MNQU6") -> ContractMatch:
    return ContractMatch(
        con_id=con_id, local_symbol=local, exchange="CME", currency="USD", expiry="20260918"
    )


def test_genau_ein_treffer_ist_handelbar():
    urteil = judge_matches("MNQ", _spec(), (_match(),))
    assert urteil.ok
    assert urteil.con_id == 1


def test_kein_treffer_sperrt():
    urteil = judge_matches("MNQ", _spec(), ())
    assert not urteil.ok
    assert urteil.reason_code == R.BROKER_CONTRACT_UNKNOWN


def test_mehrere_treffer_sperren_statt_zu_waehlen():
    """Es waere leicht, den naechsten Verfall zu nehmen - und genau das ist
    der Griff, der im Zweifel den falschen Kontrakt handelt."""
    urteil = judge_matches("MNQ", _spec(), (_match(1, "MNQU6"), _match(2, "MNQZ6")))
    assert not urteil.ok
    assert urteil.reason_code == R.BROKER_CONTRACT_AMBIGUOUS
    assert urteil.matches == 2


def test_ohne_ibkr_block_ist_ein_symbol_nicht_handelbar():
    urteil = judge_matches("MNQ_PROXY", None, (_match(),))
    assert not urteil.ok
    assert urteil.reason_code == R.BROKER_CONTRACT_UNKNOWN


def test_ein_future_ohne_verfall_ist_mehrdeutig():
    urteil = judge_matches("MNQ", _spec(expiry=""), (_match(),))
    assert not urteil.ok


def test_ein_unbekanntes_symbol_ist_im_register_gesperrt():
    """Der negative Zustand ist der Standard: was nie aufgeloest wurde, wird
    nicht gehandelt."""
    registry = ContractRegistry()
    ok, grund = registry.can_trade("MNQ")
    assert not ok
    assert "nicht aufgeloest" in grund


def test_aufgeloest_ohne_kontrakt_bleibt_gesperrt():
    """Ein Urteil allein reicht nicht - ohne gebauten Kontrakt geht nichts."""
    registry = ContractRegistry()
    registry.record(judge_matches("MNQ", _spec(), (_match(),)))
    ok, _ = registry.can_trade("MNQ")
    assert not ok


# ------------------------------------------------------------- gegen echtes ibapi
def test_der_bauplan_wird_korrekt_in_eine_ibapi_order_gefuellt(mnq: Instrument):
    """Prueft gegen die tatsaechlich installierte Bibliothek, nicht gegen eine
    Annahme darueber."""
    plans = build_bracket(_request(), mnq, (41, 42, 43), account="DU123456")
    entry, stop, target = (to_ibapi_order(p) for p in plans)

    assert (entry.action, entry.orderType, entry.transmit) == ("BUY", "MKT", False)
    assert (stop.action, stop.orderType, stop.parentId) == ("SELL", "STP", 41)
    assert stop.auxPrice == pytest.approx(20980.0)
    assert (target.action, target.orderType, target.transmit) == ("SELL", "LMT", True)
    assert target.lmtPrice == pytest.approx(21040.0)
    assert entry.account == "DU123456"
    assert int(entry.totalQuantity) == 2

    # 9.81 setzt diese Felder auf True und handelt sich Fehler 10268 ein.
    assert getattr(entry, "eTradeOnly", False) is False
    assert getattr(entry, "firmQuoteOnly", False) is False


# --------------------------------------------------------------- Adapter
def _adapter(config, instruments, mode: str = "", allow_orders: bool = True, **broker_overrides):
    """Adapter mit AUSDRUECKLICH gesetztem Modus.

    Der Modus wird hier immer gesetzt und nie aus `default.yaml` uebernommen:
    welcher Wert dort steht, aendert sich im Betrieb (analysis_only ->
    paper_auto). Ein Test, der die gesperrte Kette prueft, muss den Zustand
    herstellen, den er behauptet - sonst prueft er beim naechsten
    Konfigurationswechsel klaglos etwas anderes. Genau das ist beim
    Scharfschalten passiert: zwei Tests liefen in einen Verbindungsversuch,
    statt an der Kette zu scheitern.
    """
    from tradex.broker.ibkr.adapter import IbkrAdapter
    from tradex.config import BrokerConfig, Config, ExecutionConfig

    teile: dict = {
        "broker": BrokerConfig(**{**config.broker.model_dump(), **broker_overrides}),
        "execution": ExecutionConfig(
            **{**config.execution.model_dump(), "mode": mode or "analysis_only"}
        ),
    }
    cfg = Config(**{**config.model_dump(), **teile})
    return IbkrAdapter(cfg, {"MNQ": instruments["MNQ"]}, allow_orders=allow_orders)


def test_der_adapter_waehlt_niemals_den_live_port(config, instruments):
    """Die Behauptung "Live ist strukturell ausgeschlossen" - hier geprueft
    und nicht nur im Kommentar behauptet."""
    adapter = _adapter(config, instruments)
    assert adapter._chosen_port() == config.broker.ibkr.paper_port
    assert adapter._chosen_port() != config.broker.ibkr.live_port


def test_ohne_broker_enabled_wird_gar_nicht_erst_verbunden(config, instruments):
    """Eine Sitzung, die sich anmeldet und danach feststellt, dass sie nicht
    handeln darf, hat bereits einen Zustand beim Broker erzeugt."""
    from tradex.broker.base import BrokerError

    adapter = _adapter(config, instruments, mode="paper_auto", enabled=False)
    with pytest.raises(BrokerError, match=R.BROKER_DISABLED):
        adapter.connect()
    assert not adapter.is_connected()


def test_im_analysemodus_wird_nicht_verbunden(config, instruments):
    """Steht `execution.mode` auf `analysis_only`, dann
    entsteht keine Order, auch wenn `broker.enabled` gesetzt ist - der Modus
    ist Stufe 1 der Kette und schlaegt alles Weitere."""
    from tradex.broker.base import BrokerError

    adapter = _adapter(config, instruments, mode="analysis_only", enabled=True)
    with pytest.raises(BrokerError, match=R.BROKER_MODE_NOT_PAPER):
        adapter.connect()


def test_ohne_orderrecht_sperrt_die_kette_den_socket_nicht(config, instruments, monkeypatch):
    """Der Verbindungstest muss laufen KOENNEN, bevor der Handel scharf ist.

    Sonst muesste man `paper_auto` einschalten, nur um zu pruefen, ob die
    Verbindung ueberhaupt steht - also genau die Reihenfolge umkehren, die
    diese Kette schuetzen soll. Ohne Orderrecht gibt es keinen Sendeweg, den
    sie noch schuetzen koennte.
    """
    # `analysis_only` + broker.enabled=False: die Kette sperrt zweifach.
    adapter = _adapter(
        config, instruments, mode="analysis_only", allow_orders=False, enabled=False
    )

    verbunden: list[tuple] = []
    monkeypatch.setattr(
        adapter._client, "connect", lambda *args: verbunden.append(args)
    )
    monkeypatch.setattr(adapter._ids, "wait_until_seeded", lambda timeout: False)

    # Scheitert erst am fehlenden Gateway, NICHT an der Sicherheitskette.
    with pytest.raises(BrokerErrorAlias, match="antwortet nicht"):
        adapter.connect()
    assert verbunden, "es wurde gar nicht erst verbunden"
    assert verbunden[0][1] == config.broker.ibkr.paper_port


def test_mit_orderrecht_sperrt_die_kette_den_socket_sehr_wohl(config, instruments, monkeypatch):
    """Waechter zur Gegenprobe: die Lockerung gilt NUR ohne Orderrecht."""
    adapter = _adapter(
        config, instruments, mode="analysis_only", allow_orders=True, enabled=False
    )
    verbunden: list[tuple] = []
    monkeypatch.setattr(adapter._client, "connect", lambda *args: verbunden.append(args))

    with pytest.raises(BrokerErrorAlias, match=R.BROKER_MODE_NOT_PAPER):
        adapter.connect()
    assert not verbunden, "es wurde trotz gesperrter Kette ein Socket geoeffnet"


def test_ohne_orderrecht_gibt_es_keinen_sendeweg(config, instruments):
    """`allow_orders=False` ist kein Versprechen im aufrufenden Skript,
    sondern ein Zweig, den es nicht gibt - `test_ibkr_connection.py` verlaesst
    sich darauf."""
    from tradex.broker.base import BrokerError

    adapter = _adapter(config, instruments, allow_orders=False)
    with pytest.raises(BrokerError, match="ohne Orderrecht"):
        adapter.place_market_order(_request())


def test_der_kontrakt_wird_korrekt_gebaut():
    contract = build_contract(_spec(trading_class="MNQ", multiplier="2"))
    assert (contract.symbol, contract.secType, contract.exchange) == ("MNQ", "FUT", "CME")
    assert contract.lastTradeDateOrContractMonth == "202609"
    assert contract.tradingClass == "MNQ"


def test_local_symbol_schlaegt_den_verfallmonat():
    """`local_symbol` bezeichnet genau einen Kontrakt; beides zusammen zu
    senden waere ein Widerspruchsrisiko ohne Gewinn."""
    contract = build_contract(_spec(local_symbol="MNQU6"))
    assert contract.localSymbol == "MNQU6"
    assert not contract.lastTradeDateOrContractMonth


def test_die_order_geht_ueber_die_kontraktnummer():
    """Nach der Aufloesung ist die conId bekannt und eindeutig. Weiterhin die
    Beschreibung zu senden hiesse, IBKR bei jeder Order neu raten zu lassen."""
    contract = order_contract(_match(con_id=637533), "CME")
    assert contract.conId == 637533
    assert contract.exchange == "CME"
    assert not contract.symbol
