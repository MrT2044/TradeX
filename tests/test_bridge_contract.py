"""Was das NinjaScript-AddOn tatsaechlich tut - nicht was der Client annimmt.

Warum es diesen Test gibt
-------------------------
`test_nt8_feed.py` speist Ticks von Hand in einen nachgebauten Bridge-Server
und prueft, was die Python-Seite daraus macht. Das ist richtig und war
trotzdem eine Luecke: geprueft war der KONSUMENT, nie der PRODUZENT. Und der
produzierte gar keine Ticks - `TradeXBridge.cs` hatte weder ein
Marktdaten-Abonnement noch eine `tick`-Nachricht. Im Betrieb standen deshalb
bei tausenden Bars `ticks_seen = 0` und `last_price = {}`, waehrend jeder Test
gruen war.

Was hier geht und was nicht
---------------------------
C# laesst sich hier nicht uebersetzen und schon gar nicht gegen NinjaTraders
Assemblies binden - dafuer braucht es die Installation. Geprueft wird deshalb
der Quelltext: dass die Bausteine da sind, ohne die es keine Ticks geben KANN.
Das ersetzt keinen Lauf in NinjaTrader; es faengt aber genau den Fehler ab, der
hier passiert ist - eine Protokollseite, die nur auf dem Papier existiert.
"""

from __future__ import annotations

import pytest

from tests.conftest import PROJECT_ROOT

QUELLE = PROJECT_ROOT / "bridge_nt8" / "TradeXBridge.cs"


@pytest.fixture(scope="module")
def addon() -> str:
    return QUELLE.read_text(encoding="utf-8")


def test_das_addon_abonniert_marktdaten(addon: str):
    """Ohne Abonnement feuert nie ein Marktdaten-Ereignis."""
    assert "MarketData.Update +=" in addon, (
        "kein Marktdaten-Abonnement - dann kann das AddOn keine Ticks kennen"
    )
    assert "MarketDataType.Last" in addon, (
        "ohne Filter auf Abschluesse gingen auch Bid und Ask als Kurs hinaus"
    )


def test_das_addon_sendet_tick_nachrichten(addon: str):
    """Und zwar in der Form, die `nt8_feed._handle_tick` liest."""
    assert '\\"type\\":\\"tick\\"' in addon
    for feld in ('\\"symbol\\"', '\\"ts\\"', '\\"price\\"', '\\"size\\"'):
        assert feld in addon, f"Pflichtfeld {feld} fehlt in der Tick-Nachricht"


def test_abonnements_werden_wieder_freigegeben(addon: str):
    """Ein vergessenes `-=` ueberlebt das AddOn und tickt weiter."""
    assert "MarketData.Update -=" in addon
    assert "ReleaseTicks" in addon, "unsubscribe muss das Abonnement freigeben"
    assert "RefCount" in addon, (
        "ohne Zaehler haengt dasselbe Instrument auf zwei Zeitebenen zwei "
        "Handler an - und jeder Tick ginge doppelt hinaus"
    )


def test_ticks_blockieren_den_ninjatrader_faden_nicht(addon: str):
    """Ein `Write` im Marktdaten-Callback haengt NinjaTrader an einen langsamen
    Leser. Gesendet wird deshalb aus einem eigenen Faden."""
    assert "SendLoop" in addon and "tradex-send" in addon
    # Der Handler legt nur ab. Wuerde er selbst senden, waere der eigene Faden
    # wirkungslos.
    handler = addon[addon.index("private void OnMarketData(") :]
    handler = handler[: handler.index("\n        }")]
    assert "QueueTick(" in handler
    assert "Send(" not in handler, "der Marktdaten-Callback darf nicht selbst senden"


def test_alte_ticks_werden_ersetzt_statt_aufgestaut(addon: str):
    """Fuer eine Kursanzeige zaehlt nur der neueste Kurs.

    Eine Warteschlange, die jeden Tick aufhebt, waechst bei einem langsamen
    Client unbegrenzt und zeigt am Ende Kurse von vor einer Minute.
    """
    assert "pendingTicks[symbol] = json" in addon, (
        "Ticks muessen je Symbol ERSETZT werden, nicht angehaengt"
    )
    assert "MaxQueuedMessages" in addon, "auch die uebrige Warteschlange braucht einen Deckel"


def test_bars_bleiben_bei_geschlossenen_bars(addon: str):
    """Der Waechter fuer Invariante 1 auf der Produzentenseite.

    Ticks sind Anzeige. Die laufende Bar geht weiterhin NICHT als `bar`
    hinaus - sonst saehe die Engine live einen Zustand, den der Backtest nie
    sieht, und jede Backtest-Aussage waere hinfaellig.
    """
    assert "int lastClosed = bars.Count - 2;" in addon


# ---------------------------------------------------------------------- Orders
#
# Ab Phase 9 ist NinjaTrader nicht mehr nur Datenquelle, sondern auch
# Ausfuehrungsweg. Damit faellt die alte Zusage "KEINE ORDERS" - sie stand
# frueher als Wortlaut im Quelltext und wurde hier geprueft.
#
# An ihre Stelle tritt eine ENGERE Zusage, und die Tests unten sind der Grund,
# warum sie belastbar ist: Orders duerfen ausschliesslich auf ein
# Simulationskonto gehen, und das entscheidet das AddOn selbst - nicht die
# Python-Seite, die man austauschen koennte.
#
# Diese Tests sind absichtlich VOR der Umsetzung geschrieben (Schritt A2 vor
# A3). Sie sind rot, bis `TradeXBridge.cs` sie erfuellt. Genau anders herum
# entstand seinerzeit `ticks_seen = 0`: erst Code, dann ein Test, der ihn
# bestaetigt.


def test_orderbefehle_sind_eine_whitelist(addon: str):
    """Kein Default-Zweig: was nicht auf der Liste steht, wird verworfen.

    Der Grund ist die aufgegebene Pfadtrennung. Marktdaten und Orders teilen
    sich jetzt einen Socket; eine verstuemmelte Zeile darf deshalb unter
    keinen Umstaenden versehentlich als Order gelesen werden. Eine Whitelist
    kann das nicht - eine Blacklist oder ein `else` schon.
    """
    for befehl in ("order_submit", "order_cancel", "flatten", "account_query"):
        assert f'type != "{befehl}"' in addon or f'"{befehl}"' in addon, (
            f"Befehl {befehl} aus dem Protokoll fehlt im AddOn"
        )


def test_die_kontoabfrage_reicht_den_namen_durch(addon: str):
    """Sonst fragt sie nach "irgendeinem Simulationskonto".

    An dieser Installation sind `Sim101` UND `Backtest` beide
    Provider.Simulator; die Aufloesung lehnt Mehrdeutigkeit ab. Die Verbindung
    scheiterte damit an einer Stufe, die richtig arbeitete - falsch gestellt
    war die Frage. Genau dieser Fehler ist am 26.08.2026 im Betrieb
    aufgetreten, nachdem alle Tests gruen waren.
    """
    assert 'SendAccount(ExtractString(line, "account"))' in addon, (
        "account_query muss den Kontonamen weiterreichen"
    )
    assert "private void SendAccount(string wanted)" in addon
    assert "ResolveSimAccount(string.Empty)" not in addon, (
        "die Kontoabfrage darf nicht mehr nach <beliebig> fragen"
    )


def test_eine_abgelehnte_kontoabfrage_nennt_die_kandidaten(addon: str):
    """Ein leeres Ergebnis ohne Begruendung zwingt zum Raten.

    Die Meldung muss sagen, WONACH gesucht wurde und WAS es gibt - sonst ist
    "kein Konto mit Provider=Simulator" bei zwei vorhandenen Simulationskonten
    schlicht irrefuehrend.
    """
    assert '\\"candidates\\"' in addon
    assert '\\"account_provider\\"' in addon
    assert "gesucht: " in addon, "die Ablehnung muss das gesuchte Konto nennen"


def test_orders_nur_auf_simulationskonten(addon: str):
    """Die Sperre, auf der die ganze Ausbaustufe ruht.

    `Provider.Simulator` ist eine Eigenschaft des Kontos, keine
    Namenskonvention - das ist der Grund, warum dieser Nachweis staerker ist
    als der frueher bei IBKR moegliche (Port + Praefix + Allowlist).

    Sie steht ABSICHTLICH doppelt: hier und in `tradex/broker/guard.py`. Eine
    Sicherheitskette, die nur auf der Seite laeuft, die man selbst
    kontrolliert, beschreibt die Grenze, statt sie zu pruefen.
    """
    assert "Provider.Simulator" in addon, (
        "ohne diese Pruefung koennte eine Order auf ein Echtgeldkonto gehen"
    )
    assert "account_not_simulated" in addon, (
        "die Ablehnung braucht einen Reason-Code, keinen Satz"
    )


def test_kein_schalter_hebelt_die_kontopruefung_aus(addon: str):
    """Es darf keinen Weg geben, die Simulator-Pruefung zu umgehen.

    Ein Konfigurationsschalter waere genau der Punkt, an dem aus einem
    Papertrading-System versehentlich ein Echtgeldsystem wird.
    """
    for verdaechtig in ("allowLiveOrders", "AllowLiveOrders", "skipAccountCheck", "forceOrders"):
        assert verdaechtig not in addon, (
            f"{verdaechtig} waere ein Schalter an der Kontopruefung vorbei"
        )


def test_order_ereignisse_werden_nie_zusammengefasst(addon: str):
    """Anders als Ticks. Ein verworfener Tick kostet einen Kursstand, eine
    verworfene Fuellung erzeugt eine Position, die TradeX nicht kennt.
    """
    assert "OrderUpdate" in addon and "ExecutionUpdate" in addon, (
        "ohne diese Ereignisse gibt es keinen Rueckkanal - der Lifecycle "
        "Accepted/Working/Filled waere reine Behauptung"
    )
    # Die Tick-Zusammenfassung arbeitet auf `pendingTicks`. Order-Ereignisse
    # duerfen dort nicht landen, sonst ueberschreibt eine Fuellung die andere.
    assert "pendingOrders" not in addon, (
        "Order-Ereignisse gehoeren in die normale Warteschlange, nicht in eine "
        "zusammenfassende"
    )


def test_orderweg_blockiert_den_ninjatrader_faden_nicht(addon: str):
    """Dieselbe Regel wie bei den Ticks, derselbe Grund.

    Ein blockierender Aufruf im NinjaTrader-Faden haengt die Plattform auf -
    und zwar die, die gerade eine Position haelt.
    """
    assert "OnOrderUpdate" in addon or "OnExecutionUpdate" in addon
    for name in ("OnOrderUpdate(", "OnExecutionUpdate("):
        if name not in addon:
            continue
        handler = addon[addon.index(name) :]
        handler = handler[: handler.index("\n        }")]
        assert "Send(" not in handler, f"{name} darf nicht selbst senden"


def test_order_key_ist_pflicht(addon: str):
    """Duplikatschutz ueber Prozessneustarts hinweg.

    Die interne `trade_id` taugt dafuer nicht: das Risikobuch lebt im Speicher
    und zaehlt nach einem Neustart wieder bei 1 - der Broker haette zwei
    verschiedene Trades unter derselben Kennung.
    """
    assert "order_key" in addon
    assert "duplicate_order_key" in addon, (
        "ein zweites Mal derselbe Schluessel muss abgelehnt werden, auch wenn "
        "die erste Order laengst geschlossen ist"
    )


def test_flatten_storniert_vor_dem_glattstellen(addon: str):
    """Reihenfolge ist hier kein Stil, sondern Korrektheit.

    Wird zuerst glattgestellt, loest eine noch stehende Klammerorder auf der
    geschlossenen Position eine GEGENposition aus - aus einem NOTAUS wuerde
    ein neuer Trade.
    """
    assert "flatten" in addon
    assert "private void Flatten" in addon, "kein Flatten-Block gefunden"
    block = addon[addon.index("private void Flatten") :]
    block = block[: block.index("\n        }")]
    # Ab dem Rumpf, nicht ab der Signatur: die heisst selbst `Flatten(` und
    # stuende sonst immer vor jedem Storno.
    block = block[block.index("{") :]
    storno = min(
        (block.index(n) for n in ("CancelAllOrders", "Cancel(") if n in block),
        default=-1,
    )
    glatt = min((block.index(n) for n in ("Flatten(", "ClosePosition") if n in block), default=-1)
    assert storno >= 0 and glatt >= 0, "Flatten muss stornieren UND glattstellen"
    assert storno < glatt, "erst stornieren, dann glattstellen"
