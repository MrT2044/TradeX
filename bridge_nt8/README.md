# NinjaTrader-8-Bridge — Spezifikation und AddOn (Phase 5)

| Teil | Stand |
|---|---|
| Protokoll (dieses Dokument) | festgelegt |
| **Python-Client** [`tradex/live/nt8_feed.py`](../tradex/live/nt8_feed.py) | **fertig, 15 Tests** |
| **NinjaScript-AddOn** [`TradeXBridge.cs`](TradeXBridge.cs) | **läuft in NT 8.1.8.2**, 1380 echte Bars übertragen |

**Am 23.08.2026 in einer laufenden Installation 8.1.8.2 nachgewiesen:**

- AddOn kompiliert und startet mit NinjaTrader, lauscht auf 127.0.0.1:39473
- Verbindung, Abonnement, Herzschlag alle 5 s — gemessen: 30 Schläge in 150 s
- **1380 MNQ-Minutenbars** übertragen (Handelstag 21.08.2026). Streng
  aufsteigend, keine Dubletten, exakt 60 s Abstand, `low ≤ open,close ≤ high`,
  Kontraktname in jeder Bar
- Erste Bar auf `22:00 UTC` = 17:00 CT, dem Globex-Start — die Umrechnung von
  NinjaTraders Bar-**Ende** auf TradeX' Bar-**Beginn** stimmt
- `scripts/run_paper.py --symbol MNQ --feed nt8` verbindet sich und läuft

**Was das Übersetzen gegen die echten Assemblies gefunden hat** — Fehler, die
eine Spezifikation allein nicht findet:

- `NinjaScript.AddOnBase` gibt es nicht; die Basisklasse heißt `AddOnBase`
- `Connection.PrimaryConnection` existiert nicht
- `BarsUpdateEventArgs.BarsSeries` ist **nicht** vom Typ `Bars` — gearbeitet
  wird mit `BarsRequest.Bars`
- **Port 36973 war belegt**: das ist NinjaTraders eigener ATI-Port. Die Bridge
  wäre dort nie hochgekommen

**Nachgetragen (24.08.2026):** das AddOn abonniert jetzt Marktdaten und sendet
`tick`-Nachrichten. Vorher gab es die Produzentenseite dieses Protokollteils
gar nicht — der Python-Client konnte Ticks lesen, das AddOn erzeugte aber
keine, und im Betrieb standen bei tausenden Bars `ticks_seen = 0` und
`last_price = {}`. Die Tests waren trotzdem grün, weil sie die Ticks selbst
einspeisten. `tests/test_bridge_contract.py` prüft seitdem den Quelltext des
AddOns gegen dieses Dokument. **Gegen ein laufendes NinjaTrader ist das noch
nicht bestätigt.**

**Was noch offen ist:** Bars bei geöffneter Börse. Der Nachweis oben lief über
den `history`-Befehl, weil am Wochenende keine Bars schließen. Und: das
Wurzelsymbol muss über `nt8_symbol` in `config/instruments.yaml` auf den
Kontrakt abgebildet werden — siehe unten.

Der **Python-Client** dagegen ist vollständig getestet: gegen einen echten
TCP-Server auf Loopback, der diese Spezifikation nachbildet, inklusive
Nachrichten über Paketgrenzen hinweg, kaputter Zeilen, Kontraktwechsel und
Verbindungsabbruch.

```bash
python scripts/run_paper.py --symbol MNQ --feed nt8
```

## Warum es diese Bridge überhaupt braucht

Die dokumentierte externe NinjaTrader-API (`NTDirect.dll` / `NinjaTrader.Client.dll`,
"ATI") kann für Marktdaten ausschließlich:

```
SubscribeMarketData(instrument)
MarketData(instrument, type)     // 0 = last, 1 = bid, 2 = ask
```

Das ist ein **gepollter Zugriff auf den letzten Preis**. Es gibt darüber
ausdrücklich **keine Bars und keine Historie**. Wer Bars will, muss sie selbst
aus Ticks bauen — und dafür braucht es Code, der *innerhalb* von NinjaTrader
läuft.

Deshalb: ein kleines NinjaScript-AddOn (C#) im NinjaTrader-Prozess, das Bars und
Ticks über einen lokalen Socket hinausschickt. Das ist unabhängig von der
Sprache des Backends — auch ein .NET-Backend käme daran nicht vorbei.

## Kosten (Spec §3, §26)

| Position | Preis/Monat | Im Budget |
|---|---|---|
| NinjaTrader Plattform (Free License: Charting, Sim, Backtest) | 0 USD | ja |
| **CME Level 1 non-professional** (dort laufen NQ/MNQ) | **~4 USD** | **ja** |
| CME Level 2 / Markttiefe | ~16 USD | **nein** |

Level 2 bleibt bewusst außen vor. `NinjaTraderProvider.capabilities()` meldet
deshalb `market_depth=False` — das ist eine Budgetentscheidung, kein technisches
Limit. **Die Kernstrategie darf nie von Markttiefe abhängen** (Spec §4).

## Transport

Lokaler TCP-Socket, eine JSON-Nachricht pro Zeile (`\n`-getrennt), UTF-8.
Das AddOn ist Server, Python ist Client.

- Adresse: `127.0.0.1:39473` (in beiden Seiten konfigurierbar)
- **Nicht 36973** — das ist NinjaTraders eigener ATI-Port. An einer laufenden
  Installation 8.1.8.2 nachgemessen: er ist belegt, sobald NinjaTrader startet.
  Der Listener käme dort nie hoch, und ein Client landete stattdessen bei der
  Order-Schnittstelle.
- Nur Loopback — der Socket darf nie nach außen gebunden werden

Zeilenweises JSON statt WebSocket, weil es in C# ohne Zusatzbibliothek
auskommt und sich mit `telnet` von Hand prüfen lässt.

## Nachrichten: AddOn → Python

Alle Zeitstempel sind **Epoch-Nanosekunden UTC** — dieselbe Konvention wie im
gesamten übrigen System (siehe `tradex/domain/bars.py`).

### `bar` — abgeschlossene Bar

Wird **nur bei Bar-Abschluss** gesendet, nie für die laufende Bar. Das ist
Architektur-Invariante 1: Analysiert wird ausschließlich auf geschlossenen Bars.

```json
{"type":"bar","symbol":"MNQ","timeframe":"1m","ts":1740000000000000000,
 "open":21000.25,"high":21005.50,"low":20998.75,"close":21003.00,
 "volume":1420,"contract":"MNQH5"}
```

`contract` ist der laufende Kontraktname. Wechselt er, setzt die Python-Seite
`roll_boundary=True` — siehe `tradex/data/rolls.py`.

### `tick` — Einzelgeschäft

```json
{"type":"tick","symbol":"MNQ","ts":1740000000000000000,"price":21003.00,"size":2}
```

Kommt aus `Instrument.MarketData.Update`, gefiltert auf `MarketDataType.Last` —
also der zuletzt **gehandelte** Preis, nicht Bid oder Ask. Diese Zahl geht auf
der Python-Seite in **keinen Detektor und keine Entscheidung** ein. Sie füllt
`last_price` und die laufende Kerze (`NinjaTraderFeed.live_bar()`), und beides
ist reine Anzeige.

Warum es sie geben muss: das AddOn sendet ausschließlich *geschlossene* Bars.
Um 14:21:36 ist die letzte davon die von 14:20 — die Minute, die gerade läuft,
kennt TradeX sonst überhaupt nicht, und der Chart hängt dauerhaft eine Minute
zurück.

**Drei Eigenschaften des Sendewegs**, die zum Protokoll gehören:

- Ticks werden **zusammengefasst**: ein noch nicht gesendeter Tick wird durch
  den nächsten ersetzt. Ein Client bekommt also nicht jeden Tick, sondern immer
  den neuesten. Für eine Kursanzeige ist das richtig; wer jeden einzelnen Tick
  bräuchte, bekäme ihn hier nicht.
- Gesendet wird höchstens alle 50 ms je Symbol (`TickIntervalMs`).
- **Bars werden nie zusammengefasst.** Dort wäre jeder verworfene Datensatz ein
  stiller Datenverlust.

Ein Instrument, das auf zwei Zeitebenen abonniert ist, erzeugt **einen**
Tickstrom — die Abonnements sind gezählt (`RefCount`), und `unsubscribe` gibt
den Handler erst beim letzten frei.

### `status` — Verbindungszustand

```json
{"type":"status","connected":true,"data_feed":"connected","detail":""}
```

### `heartbeat` — Lebenszeichen

Alle 5 Sekunden. Bleibt er länger als 15 Sekunden aus, gilt der Feed als
verloren: **keine neuen Trades** (Spec §24).

```json
{"type":"heartbeat","ts":1740000000000000000,"dropped":0}
```

`dropped` zählt Nachrichten, die eine übergelaufene Sendewarteschlange
verworfen hat. Praktisch immer 0 — steht dort etwas anderes, hat ein Client
nicht mehr gelesen, und ein Teil des Datenstroms fehlt. Ein Überlauf, den
niemand meldet, sieht von außen aus wie ein ruhiger Markt.

## Nachrichten: Python → AddOn

### `subscribe` / `unsubscribe`

```json
{"type":"subscribe","symbol":"MNQ","timeframe":"1m"}
```

### `history` — historische Bars anfordern

NinjaTrader hält lokal Historie; das AddOn liefert sie als Folge von `bar`-
Nachrichten, abgeschlossen durch `history_end`.

```json
{"type":"history","symbol":"MNQ","timeframe":"1m",
 "from":1740000000000000000,"to":1740086400000000000}
```

## Order-Ausführung (Phase 9)

> **Diese Section ersetzt die frühere Festlegung „Orders laufen über ATI".**
> Was daran richtig war und was nicht, steht unten unter *Warum nicht ATI* —
> die alte Begründung wird nicht stillschweigend überschrieben.

Seit Phase 9 ist NinjaTrader **beide** Seiten: Marktdaten *und* Ausführung.

```
NinjaTrader → Marktdaten → TradeX → Analyse/Strategie → Order → NinjaTrader
```

Getragen wird das von derselben In-Prozess-API, die jede NinjaScript-Strategie
benutzt — `NinjaTrader.Cbi` (`Account`, `Order`, `Execution`, `Position`). Das
AddOn importiert diesen Namespace ohnehin bereits.

### Warum nicht ATI

Die alte Festlegung nannte zwei Gründe, und **einer davon gilt weiter**:

| Argument von damals | Stand heute |
|---|---|
| „ATI ist dafür vorgesehen und dokumentiert" | Stimmt, reicht aber nicht: ATI kennt **keinen Order-Lifecycle**. Es gibt kein `Accepted`/`Working`/`PartiallyFilled`, keine Ausführungspreise je Teilfüllung, keine Positions- oder Kontoereignisse. Genau das ist aber die geforderte Rückmeldung. |
| „Order-Routing bleibt vom Datenpfad getrennt" | **Bleibt ein echter Verlust.** Beide Wege teilen sich jetzt einen Socket. |

Der zweite Punkt wird nicht weggeredet, sondern **anders abgesichert**:

- **Whitelist statt Default-Zweig.** `HandleCommand` kennt eine feste Liste von
  Befehlen und verwirft alles andere wortlos. Eine verstümmelte `bar`-Zeile
  kann nicht versehentlich als Order gelesen werden — sie passt auf keinen
  Befehlsnamen.
- **`order_key` ist Pflicht.** Ohne ihn wird nicht gesendet. Ein zufällig
  zusammengesetzter Puffer hat keinen.
- **Nur Simulationskonten.** Siehe unten — das AddOn lehnt jedes andere Konto
  ab, unabhängig davon, was Python schickt.
- **Getrennte Warteschlangen.** Order-Ereignisse werden nie zusammengefasst,
  Kursticks immer. Sie teilen sich die Leitung, nicht die Behandlung.

### Kontoschutz: der Nachweis liegt im AddOn, nicht in Python

Orders werden **ausschließlich** auf ein Konto mit
`account.Connection.Options.Provider == Provider.Simulator` angenommen. Jede
andere Verbindung wird mit `order_rejected` und Reason-Code beantwortet, bevor
irgendetwas an NinjaTrader geht.

**Es gibt dafür keinen Schalter.** Nicht in `default.yaml`, nicht in `.env`,
nicht als Kommandozeilenargument. Das ist der Unterschied zur bisherigen
IBKR-Anbindung, wo der Paper-Nachweis strukturell indirekt bleiben musste
(Port + `DU`-Präfix + Allowlist): `Provider.Simulator` ist eine **Eigenschaft
des Kontos**, keine Namenskonvention.

Die Prüfung steht bewusst **doppelt** — hier im AddOn und in
`tradex/broker/guard.py`. Eine Sicherheitskette, die nur auf der Seite läuft,
die man selbst kontrolliert, prüft die Grenze nicht, sondern beschreibt sie.

### Nachrichten: Python → AddOn

#### `order_submit`

Entry mit optionaler Klammer. Stop und Ziel gehen als **echte Orders** an
NinjaTrader — nicht als interne Merkposten. Der Grund ist derselbe wie bei
IBKR: sie müssen auch dann wirken, wenn TradeX nicht läuft.

```json
{"type":"order_submit","order_key":"S17-4","symbol":"MNQ","account":"Sim101",
 "side":"BUY","quantity":2,"kind":"MARKET","limit_price":0,
 "stop_loss":29180.25,"take_profit":29310.50}
```

`order_key` ist der Duplikatschutz aus `tradex/broker/types.py` und überlebt
Prozessneustarts. Kommt derselbe Schlüssel zweimal, wird die zweite Nachricht
**abgelehnt, nicht ausgeführt** — auch dann, wenn die erste Order längst
geschlossen ist. Die interne `trade_id` allein taugt dafür nicht: das
Risikobuch lebt im Speicher und zählt nach einem Neustart wieder bei 1.

Die Klammerorders tragen `order_key#stop` bzw. `order_key#target`
(`order_ref()` in `types.py`) — der einzige Faden, an dem sich nach einem
Verbindungsabriss wiederfinden lässt, welche fremde Order zu welchem eigenen
Signal gehört.

#### `order_cancel`

```json
{"type":"order_cancel","order_key":"S17-4"}
```

Storniert Entry **und** beide Klammerorders. Eine stornierte Entry-Order, deren
Stop stehen bleibt, wäre eine Order ohne Position.

#### `flatten` — der NOTAUS-Weg

```json
{"type":"flatten","account":"Sim101","symbol":"MNQ"}
```

Ohne `symbol`: alles. Storniert erst alle offenen Orders, stellt dann glatt —
in dieser Reihenfolge, sonst löst eine noch stehende Klammerorder auf der
glattgestellten Position eine Gegenposition aus.

#### `account_query`

```json
{"type":"account_query"}
```

Beantwortet mit `account` (siehe unten). Fragt **nicht** blockierend nach:
TradeX' Statusabfragen dürfen nie auf den Broker warten.

### Nachrichten: AddOn → Python

#### `order_update` — Zustandswechsel

```json
{"type":"order_update","order_key":"S17-4","order_id":"a91f...","ts":1740000000000000000,
 "state":"accepted","filled_quantity":0,"avg_fill_price":0,"error":""}
```

`order_id` ist eine **Zeichenkette** (NinjaTrader vergibt GUIDs). `BrokerOrder`
rechnet mit `int`; die Übersetzung macht der Python-Adapter, weil sie Zustand
braucht.

`state` trägt bereits die TradeX-Werte — das AddOn bildet NinjaTraders
`OrderState` in `MapOrderState()` ab, die Python-Seite liest nur noch ein. Eine
zweite Abbildung wäre eine zweite Wahrheit:

| NinjaTrader | TradeX |
|---|---|
| `Initialized`, `Submitted` | `submitted` |
| `Accepted`, `Working`, `ChangePending`, `ChangeSubmitted`, `TriggerPending` | `accepted` |
| `PartFilled` | `partially_filled` |
| `Filled` | `filled` |
| `CancelPending`, `CancelSubmitted`, `Cancelled` | `cancelled` |
| `Rejected` | `rejected` |
| alles übrige | `inactive` |

Die Namen sind gegen den Compiler geprüft: `PendingSubmit` und `PendingCancel`
standen ursprünglich hier und **gibt es in `NinjaTrader.Cbi.OrderState` nicht**.
Dabei fiel auf, dass die Abbildung nicht nur falsch benannt, sondern
unvollständig war — `ChangePending`, `ChangeSubmitted`, `TriggerPending` und
`CancelSubmitted` wären über den default-Zweig als `inactive` durchgegangen,
und eine noch arbeitende Order hätte in TradeX als endgültig erledigt gegolten.

**`Working` wird bewusst auf `accepted` abgebildet** und nicht auf einen
eigenen Zustand: für TradeX ist die Frage „liegt sie an der Börse und kann
sich noch ändern?", und die beantworten beide gleich (`is_live`). Ein
zusätzlicher Zustand im brokerunabhängigen Enum wäre ein NinjaTrader-Begriff
an einer Stelle, die keine kennen soll.

#### `execution` — eine einzelne Füllung

```json
{"type":"execution","order_key":"S17-4","exec_id":"e77b...","ts":1740000000000000000,
 "quantity":1,"price":29245.75,"commission":0.37}
```

Teilfüllungen liefern mehrere. **Diese Nachrichten werden nie
zusammengefasst** — anders als Ticks. Ein verworfener Tick kostet einen
Kursstand, eine verworfene Füllung erzeugt eine Position, die TradeX nicht
kennt.

#### `position` — Position, wie NinjaTrader sie sieht

```json
{"type":"position","account":"Sim101","symbol":"MNQ","quantity":-2,
 "avg_price":29245.75,"unrealized_pnl":-31.50}
```

`quantity` ist vorzeichenbehaftet (negativ = short). Diese Zahl ist absichtlich
**getrennt** vom internen Risikobuch: die Differenz zwischen beiden ist genau
die Information, die man nach einem Verbindungsabriss braucht.

#### `account`

```json
{"type":"account","name":"Sim101","provider":"Simulator","is_simulation":true,
 "currency":"USD","net_liquidation":100000.00,"buying_power":100000.00,
 "realized_pnl":0.0}
```

`is_simulation` kommt aus `Provider.Simulator` und ist der Paper-Nachweis, auf
den sich `guard.py` stützt.

#### `order_rejected` — abgelehnt, bevor etwas hinausging

```json
{"type":"order_rejected","order_key":"S17-4","code":"account_not_simulated",
 "detail":"Konto Playback101 hat Provider=Playback"}
```

Reason-Codes, keine Sätze — dieselbe Konvention wie im übrigen System, `de.ts`
übersetzt sie.

**Vom AddOn** (`ADDON_REJECT_CODES` in `tradex/broker/nt8/protocol.py`, ein
Test hält beide Seiten gegeneinander): `order_key_missing`,
`account_not_simulated`, `instrument_unknown`, `duplicate_order_key`,
`quantity_invalid`, `bracket_invalid`, `submit_failed`.

**Vom Python-Adapter**: `not_connected` — steht keine Leitung, kann das AddOn
nicht antworten, also vergibt der Adapter den Grund selbst.

`account_unknown` gibt es **nicht** (die Spezifikation nannte es einmal): ein
unbekanntes Konto wird als `account_not_simulated` abgelehnt. Das ist die
sichere Richtung — ein Konto, das es nicht gibt, ist kein Simulationskonto.

### Live-Trading

Unverändert gesperrt. `execution.live_trading_enabled` steht auf `false`, und
die Sicherheitskette in `tradex/broker/guard.py` verweigert jeden Live-Modus
strukturell (`BROKER_LIVE_BLOCKED`). Phase 9 ist Paper über
`Provider.Simulator` — mehr nicht.

## Umsetzungsschritte für Phase 5

1. ~~NinjaScript-AddOn schreiben~~ → [`TradeXBridge.cs`](TradeXBridge.cs)
2. ~~Socket-Client mit Heartbeat-Überwachung und Wiederverbindung~~ →
   [`tradex/live/nt8_feed.py`](../tradex/live/nt8_feed.py)
3. **AddOn einbauen.** Der schnellste Weg ist kein Copy-Paste im Editor,
   sondern die Datei direkt an ihren Platz zu legen — NinjaTrader liest sie
   von dort:

   ```powershell
   Copy-Item "bridge_nt8\TradeXBridge.cs" `
     "$env:USERPROFILE\Documents\NinjaTrader 8\bin\Custom\AddOns\" -Force
   ```

   Danach in NinjaTrader: *New → NinjaScript Editor*, **F5**. NinjaTrader
   kompiliert neu hinzugekommene Dateien **nicht** beim Start — ohne F5
   passiert nichts, und zwar lautlos.
4. Gegen **Market Replay** prüfen — deterministisch und kostenlos, deshalb der
   richtige erste Schritt vor jedem Echtzeitbezug
5. CME Level 1 abonnieren (~4 USD/Monat) und gegen den Live-Feed prüfen
6. Reconciliation: Position und Kontostand regelmäßig gegen NinjaTrader
   abgleichen (Spec §24) — steht noch aus, betrifft aber erst Phase 8

### Zwei Fallen, die im AddOn schon berücksichtigt sind

**NinjaTrader stempelt eine Minutenbar auf ihr ENDE**, TradeX auf ihren Beginn.
Ohne die Verschiebung in `SendBar` läge jede Bar um ihre eigene Dauer in der
Zukunft, und jeder Vergleich mit dem historischen Bestand wäre um eine Bar
verschoben — ein Fehler, der nirgends knallt.

**`BarsRequest` meldet auch die laufende Bar.** Das AddOn sendet deshalb erst,
wenn die *nächste* Bar begonnen hat (`bars.Count - 2`). Würde die laufende Bar
hinausgehen, sähe die Engine live einen Zustand, den sie im Backtest nie sieht.

**Das ist zugleich der Grund für die Ticks.** Weil nur geschlossene Bars
hinausgehen, kennt TradeX die laufende Minute nicht — sie wird aus Ticks
zusammengesetzt, in `NinjaTraderFeed` und `TradexService.display_bar()`, und
ausschließlich zur Anzeige. Die Trennung ist die ganze Pointe: `bar` geht in
die Analyse, `tick` nie.

**Der Marktdaten-Callback darf nicht selbst senden.** Ein `Write` auf einen
TCP-Socket kann blockieren; im Callback hinge dann bei hoher Tickrate der
Datenfaden von NinjaTrader an einem langsamen Leser. Deshalb Warteschlange und
eigener Sendefaden (`SendLoop`).

### Das Wurzelsymbol muss abgebildet werden

TradeX rechnet mit `MNQ`, NinjaTraders Datenanbieter will `MNQ SEP26`. Drei
Wege, das automatisch aufzulösen, wurden an der laufenden Installation
ausprobiert und **alle drei scheitern**:

| Versuch | Ergebnis |
|---|---|
| `Instrument.GetInstrument("MNQ")` | `null` |
| `Instrument.GetInstrumentFuzzy("MNQ")` | generischer Eintrag `MNQ` → Anbieter: *"Symbol is inaccessible / UnknownSymbol"* |
| `MasterInstrument.GetInstrumentByDate(...)` | liefert denselben generischen Eintrag |
| Suche über `Instrument.All` nach dem nächsten Verfall | findet den Kontrakt nicht |

Deshalb steht der Kontraktname explizit in `config/instruments.yaml`:

```yaml
MNQ:
  nt8_symbol: "MNQ SEP26"
```

Das ist ehrlicher als eine Automatik, die still den falschen Kontrakt zieht —
aber es hat einen Preis: **beim Roll muss der Wert nachgezogen werden.** Der
Feed übersetzt in beide Richtungen; die Sitzung sieht weiterhin nur `MNQ`.

## Abnahmekriterien

- [x] Bars kommen mit korrekten OHLCV-Werten, aufsteigend, ohne Dubletten, im
      richtigen Zeitraster und mit Bar-Beginn als Zeitstempel *(1380 Bars,
      23.08.2026)*
- [x] Verbindungsauf- und -abbau werden gemeldet; die Sitzung nimmt erst nach
      bestätigter Verbindung Positionen auf
- [ ] Bars aus einem **laufenden** Markt (bisher nur über `history` geprüft —
      am Wochenende schließt keine Bar)
- [ ] Über Market Replay erzeugte Bars entsprechen **exakt** denen, die
      `MultiTimeframeAggregator` aus denselben Ticks aggregiert
- [ ] Ein Kontraktwechsel kommt als `roll_boundary` an *(im Python-Client
      getestet, in NinjaTrader erst beim nächsten Roll beobachtbar)*
- [ ] Ein Neustart des AddOns erzeugt keine doppelten oder fehlenden Bars
- [x] **`ticks_seen` steigt fortlaufend, `last_price` ist gesetzt** *(24.08.2026,
      NT 8.1.8.2: 341 Ticks in 20 s über die Bridge, `/api/watch` meldete
      `ticks_seen=1583`, `last_price=29228.75`, `malformed=0`; Kontraktname
      `MNQ SEP26` korrekt nach `MNQ` zurückübersetzt)*
- [x] Die laufende Kerze gehört zur aktuellen Minute und bewegt sich
      *(`/api/bars` lieferte auf 1m drei aufeinanderfolgende Minuten — letzte
      geschlossene, `forming`, `live`; auf 5m lagen `forming` und `live` im
      selben Bucket und wurden verschmolzen)*
- [ ] **Ticks aus einem ECHTEN Datenfeed.** Der Nachweis oben lief gegen
      NinjaTraders Verbindung **„Simulation"** — synthetische Kurse (im Log:
      `Simulation: Primary connection=Connected, Price feed=Connected`). Der
      Weg ist damit vollständig belegt, die *Kurse* sind es nicht: eine
      Bewegung von 88 Punkten je Minute hat MNQ nicht. Für echte Ticks braucht
      es eine Datenverbindung mit CME Level 1 (~4 USD/Monat, siehe Kosten
      oben). Historische Bars über `history` kommen dagegen schon jetzt echt
      vom HDS.
