# NinjaTrader-8-Bridge — Spezifikation (Phase 5)

**Stand: noch nicht implementiert.** Dieses Dokument legt die Schnittstelle fest,
damit Phase 5 ohne Umbau an der Engine andocken kann. Die Gegenstelle in Python
existiert bereits als Stub: [`tradex/data/nt8_provider.py`](../tradex/data/nt8_provider.py).

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

- Adresse: `127.0.0.1:36973` (in beiden Seiten konfigurierbar)
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

1. NinjaTrader 8 installieren (Free License genügt zum Entwickeln)
2. NinjaScript-AddOn schreiben: `BarsRequest` je abonniertem Instrument,
   Bar-Abschluss serialisieren, über den Socket senden
3. `tradex/data/nt8_provider.py` ausimplementieren (Socket-Client,
   Heartbeat-Überwachung, Wiederverbindung)
4. Gegen **Market Replay** testen — deterministisch und kostenlos, deshalb
   der richtige erste Schritt vor jedem Echtzeitbezug
5. CME Level 1 abonnieren (~4 USD/Monat) und gegen den Live-Feed testen
6. Reconciliation: Position und Kontostand regelmäßig gegen NinjaTrader
   abgleichen (Spec §24)

## Abnahmekriterien

Die Bridge gilt erst als fertig, wenn:

- [ ] Über Market Replay erzeugte Bars **exakt** denen entsprechen, die
      `MultiTimeframeAggregator` aus denselben Ticks aggregiert
- [ ] Verbindungsabbruch innerhalb von 15 Sekunden erkannt wird und die Engine
      in einen Zustand geht, in dem keine neuen Trades entstehen
- [ ] Ein Kontraktwechsel korrekt als `roll_boundary` ankommt
- [ ] Ein Neustart des AddOns keine doppelten oder fehlenden Bars erzeugt
