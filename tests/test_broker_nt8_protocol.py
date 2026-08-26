"""Das Orderprotokoll der Bridge - Python-Seite.

Gegenstueck zu `test_bridge_contract.py`: dort wird der C#-Quelltext gegen die
Spezifikation gehalten, hier die Python-Seite. Beide Seiten einzeln zu pruefen
ist genau das, was bei den Ticks gefehlt hat - dort war der Konsument getestet,
der Produzent existierte nicht, und alle Tests waren gruen.

Kein Socket, kein NinjaTrader: `protocol.py` bildet nur ab.
"""

from __future__ import annotations

import json

import pytest

from tradex.broker.nt8 import protocol
from tradex.broker.types import (
    ROLE_STOP,
    ROLE_TARGET,
    OrderKind,
    OrderRequest,
    OrderSide,
    OrderState,
    order_ref,
)


def _request(**overrides: object) -> OrderRequest:
    felder: dict[str, object] = {
        "order_key": "S17-4",
        "symbol": "MNQ",
        "side": OrderSide.BUY,
        "quantity": 2,
        "stop_loss": 29180.25,
        "take_profit": 29310.50,
    }
    felder.update(overrides)
    return OrderRequest(**felder)  # type: ignore[arg-type]


# ------------------------------------------------------------------ Zustaende
@pytest.mark.parametrize(
    ("gesendet", "erwartet"),
    [
        ("submitted", OrderState.SUBMITTED),
        ("accepted", OrderState.ACCEPTED),
        ("partially_filled", OrderState.PARTIALLY_FILLED),
        ("filled", OrderState.FILLED),
        ("cancelled", OrderState.CANCELLED),
        ("rejected", OrderState.REJECTED),
        ("inactive", OrderState.INACTIVE),
    ],
)
def test_jeder_zustand_des_addons_wird_gelesen(gesendet: str, erwartet: OrderState):
    """Das AddOn bildet NinjaTraders Zustaende bereits ab (`MapOrderState`).

    Hier wird nur eingelesen - eine zweite Abbildung waere eine zweite
    Wahrheit. Faellt einer dieser Werte weg, sprechen die Seiten verschiedene
    Fassungen des Protokolls.
    """
    assert protocol.parse_state(gesendet) is erwartet


def test_ein_unbekannter_zustand_gilt_als_endgueltig():
    """Die Richtung des Irrtums entscheidet.

    `INACTIVE` ist endgueltig - TradeX nimmt darauf keine Position mehr auf.
    Wuerde hier auf `ACCEPTED` geraten, fuehrte das Programm eine Order als
    lebend, die es vielleicht nicht mehr gibt.
    """
    assert protocol.parse_state("voellig_neu") is OrderState.INACTIVE
    assert protocol.parse_state(None) is OrderState.INACTIVE
    assert not protocol.parse_state("voellig_neu").is_live


def test_working_ist_kein_eigener_zustand():
    """Waechter gegen einen NinjaTrader-Begriff im brokerunabhaengigen Enum.

    Das AddOn bildet `Working` auf `accepted` ab. Taucht "working" je als
    eigener Wert auf, ist die Trennung durchbrochen.
    """
    assert protocol.parse_state("working") is OrderState.INACTIVE
    assert "working" not in {zustand.value for zustand in OrderState}


# -------------------------------------------------------------- Befehle hinaus
def test_order_submit_traegt_alles_was_das_addon_braucht():
    befehl = protocol.submit_command(_request(), account="Sim101")

    assert befehl["type"] == "order_submit"
    assert befehl["order_key"] == "S17-4"
    assert befehl["account"] == "Sim101"
    assert befehl["side"] == "BUY"
    assert befehl["quantity"] == 2
    assert befehl["kind"] == "MARKET"
    assert befehl["stop_loss"] == 29180.25
    assert befehl["take_profit"] == 29310.50


def test_der_gewuenschte_einstiegskurs_geht_nicht_hinaus():
    """Eine Market-Order hat keinen Wunschkurs.

    `planned_entry` steht im Request, weil die Differenz zum tatsaechlichen
    Fuellkurs die interessante Zahl ist - beim Broker haette sie nichts zu
    suchen und wuerde dort bestenfalls ignoriert.
    """
    befehl = protocol.submit_command(_request(planned_entry=29200.0), account="Sim101")
    assert "planned_entry" not in befehl
    assert "signal_id" not in befehl
    assert "strategy" not in befehl


def test_hinaus_geht_der_kontrakt_nicht_das_wurzelsymbol():
    """Der Fehler, an dem A8 zweimal gescheitert ist.

    NinjaTrader kennt "MNQ SEP26", TradeX rechnet mit "MNQ". Ohne die
    Uebersetzung loest das AddOn den GENERISCHEN `MNQ`-Eintrag auf - und der
    hat keine Marktdaten. NinjaTrader nahm die Order an und lehnte sie zwanzig
    Sekunden spaeter ab ("There is no market data available to drive the
    simulation engine"), im Log stand `Instrument='MNQ'`.

    Der Feed uebersetzt seit jeher. Dass der Orderweg es nicht tat, faellt
    nirgends auf ausser am abgelehnten Auftrag.
    """
    befehl = protocol.submit_command(_request(symbol="MNQ"), "Sim101", contract="MNQ SEP26")
    assert befehl["symbol"] == "MNQ SEP26"


def test_ohne_kontrakt_bleibt_das_symbol_stehen():
    """Fuer Instrumente ohne Kontraktnamen (Aktien, Devisen) gibt es nichts zu
    uebersetzen - dann ist das Symbol selbst richtig."""
    befehl = protocol.submit_command(_request(symbol="mnq"), account="Sim101")
    assert befehl["symbol"] == "MNQ"


def test_eine_limit_order_traegt_ihren_kurs():
    befehl = protocol.submit_command(
        _request(kind=OrderKind.LIMIT, limit_price=29200.25), account="Sim101"
    )
    assert befehl["kind"] == "LIMIT"
    assert befehl["limit_price"] == 29200.25


def test_flatten_ohne_symbol_meint_alles():
    assert "symbol" not in protocol.flatten_command("Sim101")
    assert protocol.flatten_command("Sim101", "mnq")["symbol"] == "MNQ"


def test_jeder_befehl_ist_genau_eine_zeile():
    """Das Rahmenformat der Bridge. Ein eingebetteter Zeilenumbruch teilte eine
    Nachricht in zwei halbe - und die zweite Haelfte passt auf keinen
    Befehlsnamen der Whitelist."""
    for befehl in (
        protocol.submit_command(_request(), "Sim101"),
        protocol.cancel_command("S17-4"),
        protocol.flatten_command("Sim101"),
        protocol.account_query_command(),
    ):
        roh = protocol.encode(befehl)
        assert roh.endswith(b"\n")
        assert roh.count(b"\n") == 1
        assert json.loads(roh) == befehl


def test_die_klammerorders_heissen_wie_im_addon():
    stop, ziel = protocol.bracket_refs("S17-4")
    assert stop == order_ref("S17-4", ROLE_STOP) == "S17-4#stop"
    assert ziel == order_ref("S17-4", ROLE_TARGET) == "S17-4#target"


# --------------------------------------------------------- Nachrichten herein
def test_marktdaten_gehen_den_orderweg_nichts_an():
    """None heisst "nicht meine Nachricht" und ist kein Fehler.

    Beide Wege teilen sich die Leitung, seit der Orderweg dazugekommen ist.
    """
    for art in ("bar", "tick", "heartbeat", "status", "history_end"):
        assert protocol.parse_event({"type": art}) is None


def test_order_update_wird_vollstaendig_gelesen():
    ereignis = protocol.parse_event(
        {
            "type": "order_update",
            "order_key": "S17-4",
            "order_id": "a91f-0042",
            "ts": 1_740_000_000_000_000_000,
            "state": "accepted",
            "filled_quantity": 1,
            "avg_fill_price": 29245.75,
            "error": "",
        }
    )
    assert ereignis is not None
    assert ereignis.kind == "order"
    assert ereignis.order_key == "S17-4"
    assert ereignis.state is OrderState.ACCEPTED
    assert ereignis.payload["filled_quantity"] == 1
    assert ereignis.payload["avg_fill_price"] == 29245.75
    # NinjaTrader vergibt Zeichenketten als Order-ID. Sie wandert roh mit; die
    # Uebersetzung auf int braucht Zustand und gehoert in den Adapter.
    assert ereignis.payload["broker_order_id"] == "a91f-0042"
    assert ereignis.order_id == 0


def test_die_rolle_wird_wieder_an_den_schluessel_gehaengt():
    """Der Fehler, der im Betrieb am 26.08.2026 sichtbar wurde.

    Alle drei Orders einer Klammer tragen denselben `order_key`; das AddOn
    schickt die Rolle in einem EIGENEN Feld (`KeyOfRef`/`RoleOfRef`). Wurde
    nur der Schluessel gelesen, landeten Stop- und Zielmeldungen auf der
    Entry-Order - und die Klammerteile blieben fuer immer `submitted`, obwohl
    sie laengst abgelehnt waren. Im Skript standen deshalb zwei offene Orders,
    die es beim Broker nicht mehr gab.
    """
    for rolle, erwartet in (("entry", "S17-4"), ("stop", "S17-4#stop"), ("target", "S17-4#target")):
        ereignis = protocol.parse_event(
            {"type": "order_update", "order_key": "S17-4", "role": rolle, "state": "rejected"}
        )
        assert ereignis is not None
        assert ereignis.order_key == erwartet, f"Rolle {rolle} falsch zugeordnet"


def test_auch_fuellungen_tragen_ihre_rolle():
    """Eine Fuellung auf dem Stop schliesst eine Position, eine auf dem Entry
    oeffnet sie. Ohne den Unterschied ist sie nicht auswertbar."""
    ereignis = protocol.parse_event(
        {
            "type": "execution",
            "order_key": "S17-4",
            "role": "stop",
            "quantity": 1,
            "price": 29100.0,
        }
    )
    assert ereignis is not None
    assert ereignis.order_key == "S17-4#stop"


def test_ohne_rolle_gilt_entry():
    """Faellt das Feld weg, ist es der Einstieg - nicht ein unbekannter Faden."""
    ereignis = protocol.parse_event(
        {"type": "order_update", "order_key": "S17-4", "state": "accepted"}
    )
    assert ereignis is not None
    assert ereignis.order_key == "S17-4"


def test_eine_fuellung_wird_nie_zusammengefasst():
    """Anders als Ticks: ein verworfener Tick kostet einen Kursstand, eine
    verworfene Fuellung erzeugt eine Position, die TradeX nicht kennt."""
    ereignis = protocol.parse_event(
        {
            "type": "execution",
            "order_key": "S17-4",
            "exec_id": "e77b",
            "ts": 1_740_000_000_000_000_000,
            "quantity": 1,
            "price": 29245.75,
            "commission": 0.37,
        }
    )
    assert ereignis is not None
    assert ereignis.kind == "fill"
    assert ereignis.payload["quantity"] == 1
    assert ereignis.payload["price"] == 29245.75
    assert ereignis.payload["commission"] == 0.37
    assert ereignis.payload["commission_reported"] is True


def test_eine_fehlende_gebuehr_ist_nicht_dasselbe_wie_keine():
    """0.0 heisst "noch nicht gemeldet". Der Unterschied entscheidet, ob
    `BrokerExecutor` schaetzen muss - und eine geschaetzte Gebuehr, die als
    gemeldete durchgeht, faellt in keiner Abrechnung auf."""
    ereignis = protocol.parse_event(
        {"type": "execution", "order_key": "S17-4", "quantity": 1, "price": 29245.75}
    )
    assert ereignis is not None
    assert ereignis.payload["commission"] == 0.0
    assert ereignis.payload["commission_reported"] is False


def test_die_position_behaelt_ihr_vorzeichen():
    """Negativ = short. Ein verlorenes Vorzeichen machte aus einer Short- eine
    Long-Position - und der Abgleich mit dem Risikobuch waere wertlos."""
    ereignis = protocol.parse_event(
        {
            "type": "position",
            "account": "Sim101",
            "symbol": "mnq",
            "quantity": -2,
            "avg_price": 29245.75,
            "unrealized_pnl": -31.5,
        }
    )
    assert ereignis is not None
    assert ereignis.kind == "position"
    assert ereignis.payload["quantity"] == -2
    assert ereignis.payload["symbol"] == "MNQ"


def test_der_paper_nachweis_kommt_aus_dem_kontoereignis():
    """`is_simulation` stammt aus `Account.Provider == Provider.Simulator` -
    einer Eigenschaft des KONTOS. Darauf stuetzt sich die Sicherheitskette."""
    ereignis = protocol.parse_event(
        {
            "type": "account",
            "name": "Sim101",
            "provider": "Simulator",
            "is_simulation": True,
            "currency": "USD",
            "net_liquidation": 100000.0,
            "buying_power": 100000.0,
        }
    )
    assert ereignis is not None
    assert ereignis.kind == "account"
    assert ereignis.payload["is_simulation"] is True
    assert ereignis.payload["account"] == "Sim101"
    assert ereignis.payload["net_liquidation"] == 100000.0


def test_die_kontoabfrage_traegt_den_namen():
    """Ohne Namen ist die Frage an einer echten Installation mehrdeutig -
    `Sim101` und `Backtest` sind beide Provider.Simulator."""
    assert protocol.account_query_command("Sim101") == {
        "type": "account_query",
        "account": "Sim101",
    }
    # Ohne Konfiguration bleibt das Feld weg statt leer zu sein: ein leerer
    # Name heisst im AddOn "beliebig", und das ist etwas anderes als "keine
    # Angabe gemacht".
    assert protocol.account_query_command() == {"type": "account_query"}


def test_eine_abgelehnte_kontoabfrage_behaelt_ihre_begruendung():
    """Das AddOn erhebt Begruendung und Kandidatenliste ausdruecklich. Sie
    wegzuwerfen zwaenge zum Raten - genau dafuer gibt es sie."""
    ereignis = protocol.parse_event(
        {
            "type": "account",
            "name": "",
            "is_simulation": False,
            "detail": "kein eindeutiges Konto mit Provider=Simulator (gesucht: <beliebig>)",
            "candidates": [
                {"name": "Sim101", "account_provider": "Simulator"},
                {"name": "Backtest", "account_provider": "Simulator"},
            ],
        }
    )
    assert ereignis is not None
    assert "gesucht" in str(ereignis.payload["detail"])
    assert len(ereignis.payload["candidates"]) == 2  # type: ignore[arg-type]


def test_ein_fehlendes_is_simulation_gilt_als_nicht_simuliert():
    """Fail closed. Ein fehlendes Feld darf nie als Freigabe gelesen werden -
    das ist der Unterschied zwischen "nicht geprueft" und "geprueft und gut"."""
    ereignis = protocol.parse_event({"type": "account", "name": "Irgendwas"})
    assert ereignis is not None
    assert ereignis.payload["is_simulation"] is False


def test_eine_ablehnung_ist_endgueltig():
    ereignis = protocol.parse_event(
        {
            "type": "order_rejected",
            "order_key": "S17-4",
            "code": "account_not_simulated",
            "detail": "Konto Playback101 hat Provider=Playback",
        }
    )
    assert ereignis is not None
    assert ereignis.kind == "error"
    assert ereignis.state is OrderState.REJECTED
    assert not ereignis.state.is_live, "es ging nichts hinaus - da kommt nichts mehr"
    assert ereignis.payload["code"] == "account_not_simulated"
    assert ereignis.payload["known_code"] is True


def test_ein_unbekannter_reason_code_faellt_auf():
    """Sonst sprechen AddOn und Python verschiedene Fassungen des Protokolls,
    und niemand merkt es."""
    ereignis = protocol.parse_event({"type": "order_rejected", "code": "voellig_neu"})
    assert ereignis is not None
    assert ereignis.payload["known_code"] is False


def test_die_reason_codes_stehen_auch_im_addon():
    """Beide Seiten muessen dieselbe Liste kennen.

    Beim ersten Lauf hat dieser Test eine echte Drift gefunden: das AddOn
    sendet `order_key_missing` und `submit_failed`, die in der Spezifikation
    fehlten, und die Spezifikation nannte `account_unknown`, das nirgends
    entsteht. Ein Code, den nur eine Seite kennt, ist eine Ablehnung, die
    niemand uebersetzen kann.
    """
    from tests.conftest import PROJECT_ROOT

    addon = (PROJECT_ROOT / "bridge_nt8" / "TradeXBridge.cs").read_text(encoding="utf-8")
    for code in protocol.ADDON_REJECT_CODES:
        assert f'"{code}"' in addon, f"Reason-Code {code} fehlt im AddOn"


def test_not_connected_ist_kein_addon_code():
    """Waechter fuer die Grenze: wenn keine Leitung steht, kann das AddOn nicht
    antworten - diesen Grund vergibt der Adapter selbst."""
    assert protocol.REJECT_NOT_CONNECTED not in protocol.ADDON_REJECT_CODES
    assert protocol.REJECT_NOT_CONNECTED in protocol.REJECT_CODES


def test_kein_code_ohne_gegenstueck():
    """Die Gegenrichtung: was das AddOn ablehnen kann, muss Python kennen.

    Sonst laeuft eine Ablehnung als `known_code: False` durch und die
    Oberflaeche zeigt einen rohen Bezeichner.
    """
    from tests.conftest import PROJECT_ROOT

    addon = (PROJECT_ROOT / "bridge_nt8" / "TradeXBridge.cs").read_text(encoding="utf-8")
    import re

    gefunden = set(re.findall(r'Reject\([^,]+,\s*"([a-z_]+)"', addon))
    assert gefunden, "im AddOn wurde gar kein Reject gefunden - Test ist blind"
    assert gefunden <= protocol.ADDON_REJECT_CODES, (
        f"das AddOn kennt Codes, die Python nicht kennt: "
        f"{sorted(gefunden - protocol.ADDON_REJECT_CODES)}"
    )


def test_kaputte_zahlen_reissen_nichts_ab():
    """Beim Verbindungsabriss bleibt regelmaessig eine halbe Zeile zurueck.
    Daran darf der Lesefaden nicht sterben."""
    ereignis = protocol.parse_event(
        {"type": "execution", "order_key": "X", "quantity": "kaputt", "price": None}
    )
    assert ereignis is not None
    assert ereignis.payload["quantity"] == 0
    assert ereignis.payload["price"] == 0.0
