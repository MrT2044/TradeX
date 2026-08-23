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

### `tick` — Einzelgeschäft (optional)

```json
{"type":"tick","symbol":"MNQ","ts":1740000000000000000,
 "price":21003.00,"size":2,"bid":21002.75,"ask":21003.00}
```

### `status` — Verbindungszustand

```json
{"type":"status","connected":true,"data_feed":"connected","detail":""}
```

### `heartbeat` — Lebenszeichen

Alle 5 Sekunden. Bleibt er länger als 15 Sekunden aus, gilt der Feed als
verloren: **keine neuen Trades** (Spec §24).

```json
{"type":"heartbeat","ts":1740000000000000000}
```

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

## Order-Ausführung (Phase 8/9)

Orders laufen **nicht** über diesen Socket, sondern über die ATI-Schnittstelle
(`NinjaTrader.Client.dll`). Gründe:

1. Sie ist von NinjaTrader dafür vorgesehen und dokumentiert.
2. Order-Routing bleibt damit vom selbstgebauten Datenpfad getrennt — ein Fehler
   im Bar-Streaming kann keine Order auslösen.

Bis Phase 8 wird diese Seite gar nicht angefasst. Live-Trading ist per Konfiguration
standardmäßig deaktiviert und lässt sich nur mit einer zweiten, ausdrücklichen
Bestätigung einschalten (`execution.live_trading_enabled`, geprüft in
`tradex/config.py`).

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
