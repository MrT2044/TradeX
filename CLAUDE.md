# CLAUDE.md

Arbeitshinweise für dieses Repository. Was das Projekt *ist*, steht im
[README](README.md) — hier steht, was beim *Arbeiten daran* zu beachten ist.

> Diese Datei ist die **einzige Quelle** für Arbeitshinweise. `AGENTS.md`
> verweist nur hierher.

---

## Stand

**Phasen 1–7 fertig** (Registry, Multi-Instrument, Musterstatistik,
News-Filter/Termine, Papertrading, NinjaTrader-Bridge, Dashboard/Kill Switch).
**Phase 8 fertig: IBKR-Paper-Anbindung** — Adapter ist gegen ein **laufendes
IB Gateway** gelaufen (24.08.2026): Konto `DUR972761`, Paper über Allowlist
belegt, MNQ/NQ eindeutig aufgelöst (`MNQU6`/`NQU6`, 20260918). Config steht
auf `execution.mode: paper_auto` + `broker.enabled: true`;
`live_trading_enabled` bleibt aus. Siehe unten und NEXT STEPS.

**Es gibt bis heute keinen nachgewiesenen Edge** — jedes Vertrauensband über
alle Backtest-Läufe schließt null ein (belastbarster Lauf: MNQ+MES
2023–2026, 685 Trades, −0,028 R, Band −0,14…+0,09; Zahlen via
`config/backtest_edge.yaml`, 25.000er Konto). Das bestimmt die Reihenfolge:
**Papertrading und IBKR-Paper kosten nichts außer Zeit**, echtes Geld wartet
auf Phase 9 — `execution.live_trading_enabled` bleibt aus.

---

## Umgebung

**Bash funktioniert auf dieser Maschine NICHT** (Cygwin-Fork-Fehler). **Immer
PowerShell**, für Dateien die Datei-Tools (Read/Write/Edit) statt
Shell-Textmanipulation.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -q      # Tests (536, alle grün)
.\.venv\Scripts\python.exe -m ruff check tradex tests scripts
cd ui ; npm run typecheck
```

**`ibapi` ist installiert** — 9.81.1.post1 von PyPI. Das ist eine **Fremdkopie**
von IBKRs Quelltext (Upload durch `freemo`, Stand 2020), nicht IBKRs eigene
Veröffentlichung; die aktuelle 10.x gibt es nur über den TWS-API-Installer.
9.81 genügt: der Adapter erkennt die Signaturunterschiede zur Laufzeit.

Vor jedem Commit: **ruff sauber + Tests grün + `npm run typecheck`**.

**Start:** `start_tradex.bat` prüft die ganze Betriebskette
(`scripts/preflight.py --stage pre|post`: Umgebung, Daten, UI-Build,
NT8-Bridge, Sicherheitskette, Gateway, Kontrakte), baut die Oberfläche beim
ersten Mal und verhindert Doppelstarts über ein **gehaltenes File-Handle**
(`%TEMP%\tradex-%COMPUTERNAME%.lock`) — überlebt einen Absturz, im Gegensatz
zu einem Existenztest. `--check` prüft nur. **Im gesperrten Block darf nie
`pause` stehen** — ein wartendes Fenster hält sonst die Sperre für immer.
`TradeX.bat` startet nur die Oberfläche.

**Git:** keine Identity gesetzt, pro Befehl durchreichen: `git -c
user.name="Niklas" -c user.email="niklas.klingler50@gmail.com" commit -F
<datei>`. Kein Remote, ein Commit je Phase, direkt auf `main`,
Commit-Messages auf Deutsch mit Begründungen im Body.

### PowerShell-5.1-Fallen

`-replace` interpretiert `` `n `` NICHT als Zeilenumbruch (.NET-Regex) → immer
Edit-Tool für Mehrzeiliges. `Get-Content`/`Set-Content` zerstören UTF-8 ohne
BOM (`§`→`Â§`) → Datei-Tools benutzen. Commit-Messages mit Anführungszeichen
scheitern → `git commit -F <datei>`. `cmd /c "pfad mit leerzeichen\datei.bat"`
scheitert → `cmd.exe /c call "<pfad>" args`. Lange Läufe nicht an die Sitzung
hängen. `Measure-Object -Line` zählt hier falsch → `(Get-Content datei).Count`.

---

## Die vier Invarianten

Machen Spec §29 („Backtest ≡ Live") technisch erzwingbar. **Nichts davon aufweichen.**

1. Nur geschlossene Bars werden analysiert.
2. Detektoren sind reproduzierbar — reine Funktion aus (Bars, Parameter),
   kein I/O, keine Uhr, keine Zufallszahlen.
3. **Ein einziger Analysepfad:** `MarketContext.on_base_bar()`, identisch für
   Replay/Backtest/Live. Gilt seit Phase 8 auch für die Ausführung: die
   Füllquelle in `SymbolBook` ist austauschbar (`TradeExecutor`-Protokoll),
   aber Analyse/Regel/Sizing/Risikoprüfung laufen für Simulation UND Broker
   durch denselben Code.
4. Keine Magic Numbers — jeder Schwellenwert in `config/default.yaml`, Config
   ist `extra="forbid"`.

Schärfste Prüfung: `test_backtest_faellt_dieselben_entscheidungen_wie_die_strategie`
und `test_papertrading_faellt_dieselben_entscheidungen_wie_der_backtest`. Fällt
einer, ist die entsprechende Aussage wertlos.

---

## Strategien

Mehrere Strategien parallel an **einem** Konto, auf mehreren Instrumenten.
**Strategien schlagen vor, das Portfolio entscheidet** (`portfolio.py`) —
Positionsgröße/Tagesgrenzen/Handelsfenster gelten dem Konto. Neue Strategien
nur in `registry.py` (sonst Backtest ≠ Live). **Ledger-Schlüssel wird im
Risikobuch vergeben** (`ledger.next_trade_id()`), nie in Strategie/Portfolio —
diese Verwechslung ist zweimal passiert. Bars mehrerer Instrumente werden
chronologisch verschränkt, nie Symbol für Symbol (sonst Zukunftswissen im
gemeinsamen Risikobuch).

**Cooldowns** (`risk.cooldown_minutes_after_trade`/`_after_loss`) rechnen in
**Marktzeit**, nicht Wanduhr — sonst griffen sie im Backtest nie und live
immer. Default 0: die Mechanik ist da, die Zahl braucht erst einen Backtest.

Neue Strategie = Hypothese, vor der Messung festgelegt. **Prüfen, ob sie
überhaupt auslösen KANN** (`risk/consistency.py` meldet unerreichbare
CRV-Kombinationen). Die ICT-Pflichtkette allein kann kein Day-Trading: zwei
Pflichtglieder filtern je ~89 %, macht 1,3 % der Sweeps.

---

## Live-Betrieb (Phasen 5–8)

```powershell
.\.venv\Scripts\python.exe scripts\run_paper.py --symbol MNQ_PROXY --speed 3600
```

`tradex/live/` enthält **keine Analyse, keine Regel, keine Positionsgröße** —
alles kommt aus `SymbolBook`. Backtest und Live sind derselbe Weg mit anderer
Bar-Quelle. **Der Not-Aus** wirkt über `RiskEngine.halt_reason`, **nicht**
durch Abklemmen der Bars — eine angehaltene Sitzung führt offene Positionen
zu Ende, nimmt nur keine neuen auf. Jede Sitzung beginnt angehalten
(`not_connected`). `halt`/`stop` sind getrennt. Höchstens **eine** Sitzung
gleichzeitig (gemeinsames Risikobuch). Betriebsereignisse gehen in
`system_events`, auch ohne archivierte Sitzung.

**Feeds:** `replay` (fertig) und `nt8` (läuft — AddOn in NT 8.1.8.2, Bridge
auf Port 39473, `nt8_symbol` in `instruments.yaml` beim Roll nachziehen).
NinjaTrader ist **reine Datenquelle** — kein Order-Kanal.

### Phase 8: IBKR-Paper-Anbindung — Architektur

Ziel: Marktdaten weiter von NinjaTrader, Ausführung über IB Gateway auf ein
Paper-Konto, Strategie/Analyse unangetastet. **Vollständiger Plan:**
`C:\Users\nikla\.claude\plans\ich-m-chte-in-meinem-squishy-mist.md`.

**Kernentscheidung:** `SymbolBook` bekommt eine austauschbare Füllquelle über
das `TradeExecutor`-Protokoll (`backtest/execution.py`): `SimulatedExecutor`
(Default, unverändert — alte `OpenTrade`-Logik zog dorthin um) oder
`BrokerExecutor` (`broker/executor.py`, IBKR-Fills führend). Ohne
`executor`-Arg ist `SymbolBook` byte-identisch zu vorher. Preis: mit Broker
gilt „Papertrading = Backtest" nur für die Entscheidungen, nicht die Fills —
beabsichtigt, im Plan begründet.

**`tradex/broker/` (fertig):** `types.py` (DTOs) · `base.py`
(`BrokerInterface`-Protocol, keine IBKR-Begriffe) · `env.py` (`.env` kann NUR
sperren, nie freischalten) · `guard.py` (Sicherheitskette, reine Funktionen)
· `journal.py` (`TradeJournal`) · `store.py` (`broker_orders`, **Migration
4**) · `manager.py` (`OrderManager`, Duplikatschutz, Ratenlimit) ·
`executor.py` (`BrokerExecutor`, baut `SimulatedTrade` aus echten Fills).

**Config:** `LiveConfig` (Section `live:`, Betriebsparameter ohne Backtest-
Entsprechung — `nt8_history_days`/`_timeout_seconds`) ·
`BrokerConfig`/`IbkrConfig` in `config.py`, Section `broker:` in
`default.yaml` (Default `enabled: false`). `IbkrContract` in
`domain/instruments.py`, `ibkr:`-Block bei MNQ/NQ in `instruments.yaml`
(`expiry` beim Roll nachziehen, wie `nt8_symbol`).

**Sicherheitskette (fail closed):** paper-Mode UND live_trading_enabled=false
UND broker.enabled UND `.env` sperrt nicht UND Paper-Port UND Verbindung UND
Paper-Konto bestätigt UND Kontrakt eindeutig UND RiskEngine UND Datenalter
UND Rate-Limit UND `order_key` unbekannt — jede Stufe eigener Reason-Code.

**`tradex/broker/ibkr/` (fertig, gegen `ibapi` UND gegen ein laufendes
Gateway geprüft):** `contracts.py` (Auflösung via
`reqContractDetails`, 0/>1 Treffer sperren) · `orders.py` (Bracket,
`OrderIdAllocator`, Status-/Fehlercode-Abbildung — ohne `ibapi` prüfbar) ·
`adapter.py` (Reader-Thread → Queue → `drain_events()`, einziger
`ibapi`-Import; ein Test hält die Grenze fest).

**Einhängung (fertig):** `SessionConfig.executor_factory` + `broker_health` +
`pump_broker()` in `live/session.py`, Aufruf in der Runner-Schleife (ein Stop
löst aus wenn er auslöst, nicht zum Bar-Schluss), `BrokerLink` +
`build_broker()` in `live/manager.py`. **Beide** Wege bauen den Broker —
`SessionManager.start()` und `scripts/run_paper.py`. Neuer Haltegrund
`broker_disconnected`.

**Ohne Orderrecht (`allow_orders=False`) darf verbunden werden**, auch wenn
die Sicherheitskette sperrt: es gibt dann keinen Sendeweg, den sie schützen
könnte. Sonst müsste man den Handel scharfschalten, nur um die Verbindung zu
testen. Mit Orderrecht sperrt sie unverändert — beides per Test belegt.

**Zwei Handskripte, beide in `scripts/`** (nicht im Wurzelverzeichnis: ein
blankes `pytest` sammelt `test_*.py` dort ein und würde bei jedem Testlauf das
Gateway anfassen). `test_ibkr_connection.py` prüft nur die Verbindung —
`allow_orders=False`, es gibt keinen Sendeweg. `test_paper_order.py` sendet
**tatsächlich** eine Order: zwei Bestätigungen (`--yes-send-paper-order` plus
Eingabe `JA`), kein Schalter an der Sicherheitskette vorbei, Konto muss in
`allowed_accounts` stehen, und ein `finally`-Block storniert und stellt glatt
— auch bei Strg+C.

---

## Konventionen

Kommentare/Docstrings/UI auf Deutsch, Fachbegriffe englisch (FVG, Sweep, MSS,
Displacement). Umlaute in Python-Quelltext vermeiden (`ue`/`ae`/`oe`/`ss` —
Markdown/`.ts` OK). **Begründungen sind Reason-Codes, keine Sätze** —
`code + params`, `de.ts` übersetzt, zwei Tests erzwingen Vollständigkeit.
Neue Schwellenwerte: `default.yaml` UND pydantic-Feld. Schlupf läuft beim
Ein-/Ausstieg gegenläufig — `_slip_entry`/`_slip_exit` getrennt halten. „Die
nächste Bar" ist nicht „die nächste Minute" — `max_signal_age_bars` verwirft
Signale über Datenlücken. `config_hash` muss zur geladenen Datei gehören —
`resolved_config_path()` benutzen. Provider-Abstraktion respektieren.
**Migrationsnummern vor dem Anlegen prüfen** — Phase 8 hat Migration 3
fälschlich doppelt vergeben, korrekt ist 4.

**Oberfläche:** Livedaten kommen über **SSE** (`/api/stream`), nicht per
Dauerabfrage — gesendet wird nur bei Zustandsänderung, dazu alle 10 s ein
**benanntes** `heartbeat`-Ereignis (eine SSE-Kommentarzeile löst im Browser
keinen Listener aus; der Client hielte die Leitung sonst für tot und meldete
fälschlich „veraltet"). Client fällt bei Stille auf Abfragen zurück und zeigt
das Alter des Stands an. Steuerbefehle bleiben bei den POST-Endpunkten — ein
zweiter Befehlskanal müsste dieselbe Prüfung noch einmal führen. Die
Handyansicht (`MobileDashboard`) ist ein **eigenes Layout**, kein
schmalgerechneter Desktop, und enthält **keine Steuerbefehle**. Statusabfragen
dürfen **nie** blockierende Broker-Aufrufe machen (`get_open_orders()` wartet
auf Antwort) — sonst steht die Anzeige, wenn der Broker klemmt.

**Marktbeobachtung ≠ Betrieb.** `live/watch.py` liest ein Symbol live mit —
Historie, Bars, Ticks — **ohne Risikobuch, Broker oder Executor**; eine Order
kann daraus strukturell nicht entstehen. Sie läuft, sobald ein Instrument mit
`nt8_symbol` gewählt ist. Es gibt nur **eine** Verbindung zur Bridge: eine
startende Handelssitzung beendet die Beobachtung (`stop_watch()` in der
Start-Route), ebenso der Historienabruf. Anzeige: `BEOBACHTUNG` grau vs.
`ECHTZEIT` grün — der Unterschied entscheidet, ob Orders entstehen können.
Der laufende Kurs kommt über `/api/watch` (winzig, 250 ms), **nicht** über
SSE — der Zustandsstrom prüft einmal je Sekunde und trägt den ganzen
Betriebszustand mit sich.

**Ein Startweg:** `start_tradex.bat`. `TradeX.bat` leitet nur weiter — sie war
früher ein zweiter Weg **ohne** Doppelstartsperre. Zwei Startwege heißt immer,
dass einer die Prüfungen nicht hat.

**Chart:** Woher die Bars kommen, entscheidet `TradexService.chart_context()`
— **läuft eine Sitzung für das Symbol, gilt deren Analysezustand**, sonst der
geladene Wiedergabe-Zustand. Die Auswahl gehört in den Service, nicht in die
API-Schicht: sonst trifft sie jeder Endpunkt einzeln und der erste, der es
vergisst, zeigt im Betrieb alte Kurse. Gelesen wird ohne Sperre (dieselbe
Begründung wie `state()`); ungefährlich, weil `BarSeries` erst **nach** dem
Schreiben hochzählt. Die Historie umfasst 30.000 Bars und wird **vollständig**
analysiert — ein Chart, der weiter reicht als die Analyse, zeigt Kerzen ohne
die Muster, die dort liegen. Der volle Bestand wären ~105 s je Symbolwechsel;
für längere Zeiträume ist der Backtest da. `/load` nimmt 400.000 Bars, `/step`
nur 100.000 je Anfrage — `client.ts` teilt selbst auf.

**Testkonventionen:** Handgebaute Fixtures statt Zufallsdaten. **Kein Test
fasst je ein echtes Gateway an** — Fixtures, die `default.yaml` laden, müssen
`raw["broker"]["enabled"] = False` setzen. Seit dem Scharfschalten baute sonst
jede Testsitzung einen echten IBKR-Adapter, hing in 15-s-Timeouts und war grün
oder rot, je nachdem ob auf dieser Maschine gerade ein Gateway lief. Die
Orderanbindung testet der `FakeBroker` (`tests/fake_broker.py`). Wächter-Tests
gegen leere Wahrheit. **Nie gegen den Auslieferungszustand der Konfiguration
prüfen** — vier Tests behaupteten `analysis_only`/`broker.enabled: false` als
festen Text und rissen beim Scharfschalten; einer lief dabei statt in die
erwartete Ausnahme in einen echten Verbindungsversuch und hing im
Seeding-Timeout. Wer einen Zustand prüft, stellt ihn her (`_adapter(...,
mode=...)`); wer eine Anzeige prüft, vergleicht mit der geladenen Config. Zwei Implementierungen gegeneinanderstellen, wo
vorhanden. Gegen Konstanten prüfen, nicht gegen Zahlen. Ein endloser
SSE-Generator wird **direkt** geprüft, nicht über `TestClient` — der hält ihn
offen, bis der Server schliesst, und der schliesst nie.

---

## Daten

`MNQ_PROXY`/`MES_PROXY` (Nasdaq/S&P-CFD via Dukascopy, 1m ab 2011, kostenlos),
`MNQ_DEMO` (synthetisch), `MNQ`/`NQ` (echte Futures via Databento/NT/IBKR).

**Für `MNQ` liegt lokal nichts** — die Bars kommen live von NinjaTrader. Ist
NT zu, zeigt die Oberfläche beim `default_symbol: MNQ` deshalb leere Charts.
Das ist kein Fehler: zum Ansehen ohne NT `MNQ_PROXY` wählen (echte Historie)
oder `MNQ_DEMO`.

```powershell
.\.venv\Scripts\python.exe scripts\fetch_dukascopy.py --symbol MNQ_PROXY --from 2023-01-01
.\.venv\Scripts\python.exe scripts\run_backtest.py --symbol MNQ_PROXY,MES_PROXY --save
.\.venv\Scripts\python.exe scripts\run_backtest.py --symbol MNQ_PROXY --muster
```

`run_backtest.py` liefert **Rückgabewert 2** bei null Trades (Befund, kein
Fehler). **Dukascopy-Fallen:** Feldreihenfolge `(offset,open,close,low,high,volume)`
nicht OHLC; Monat in URL nullbasiert; CFD-Pause 15:15–17:06 CT (Override in
`instruments.yaml`); HTTP 503 = Drosselung, nie zwei Läufe parallel.

---

## Was hier bewusst NICHT passiert

Schwellenwerte an Demo-/Kurzzeitdaten anpassen. Strategie-Defaults ohne
Backtest ändern. Regeln zurechtbiegen für einen Trade (Größe 0 → kein Trade).
Konfigurationswerte still korrigieren (`consistency.py` meldet nur). Parameter
optimieren (Rauschen bei n≈150). Ausführungsannahmen aufweichen (`backtest:`
absichtlich pessimistisch). Musterspuren suchen (nur feste Liste in
`patterns.py`, Mehrfachtest-Korrektur). **Live-Trading freischalten** —
`live_trading_enabled` aus, braucht Phase 9; der IBKR-Adapter verweigert
`live_port` strukturell.

---

## Aufbau

```
tradex/domain/    Bars, Instrumente (inkl. IbkrContract), Enums — keine I/O
tradex/data/      Provider, Parquet-Store, Sessions, Aggregation, Integrität
tradex/analysis/  MarketContext = der einzige Analysepfad; reasons.py
tradex/strategy/  base/chain/opening_range/portfolio/registry
tradex/news/      events/store(JSONL)/calendar/providers (nur Abrufskript)
tradex/risk/      Sizing, Tagesgrenzen, Konsistenzprüfung, Risikobuch (ledger.py)
tradex/backtest/  execution (TradeExecutor+SimulatedExecutor), runner (SymbolBook+
                  Backtester), metrics/significance/patterns/report/store
tradex/live/      feed/replay_feed/nt8_feed/session/runner/store/manager (Kill Switch),
                  watch.py (Beobachtung ohne Handel), nt8_history.py (Historienabruf)
tradex/broker/    Phase 8, siehe oben; ibkr/ = contracts/orders/adapter,
                  einziger Ort mit ibapi
tradex/api/       FastAPI + DTOs = einziger UI-Vertrag; routes/stream.py = SSE
ui/               React+lightweight-charts; MobileDashboard = eigene Handyansicht
bridge_nt8/       Protokoll + NinjaScript-AddOn (C#), in NT 8.1.8.2 im Einsatz
```

`persistence/db.py` = Schema+Migrationen (Version 4), Store-Module je Domäne
greifen zu — Persistenzschicht darf nichts über Backtest/Broker wissen.

---

## NEXT STEPS

Plan (vollständig, mit Begründungen):
`C:\Users\nikla\.claude\plans\ich-m-chte-in-meinem-squishy-mist.md`

**Broker-Entscheidung (Aug 2026):** Alpaca geprüft und verworfen — handelt
**keine Futures**, MNQ dort nicht verfügbar. Tradovate-Demo endet nach 14
Tagen. Es bleibt bei IBKR; das **Free-Trial-Paper-Konto verlangt keine
Ausweisprüfung** (KYC erst bei Echtgeld).

**IB Gateway (10.45, eingerichtet und geprüft)** — nicht TWS, das Gateway hat
keine Chart-Oberfläche. Ports **4002 Paper / 4001 Live** (TWS wäre
7497/7496). Die Option „Enable ActiveX and Socket Clients" **existiert im
Gateway nicht** — sie ist TWS-only, der Socket ist dort immer an. Gesetzt
sind: „Schreibgeschützte API" (Read-Only) **aus**, Socket Port 4002,
Master-API-Client-ID leer, „Nur Verbindungen vom lokalen Host zulassen" **an**.

„API-Client: getrennt" (rot) im Gateway ist der **Ruhezustand**, kein Fehler —
die Skripte verbinden, arbeiten, trennen. „Verlaufsdaten: Inactive: ushmds"
(gelb) ebenfalls: Historie kommt über NinjaTrader, nicht über IBKR.

**Tägliche Zwangsabmeldung:** Gateway/TWS erzwingen einen Neustart pro Tag,
abschalten geht nicht, nur die Uhrzeit wählen. Steht auf **23:30** — mitten in
der MNQ-Pause (16:00–17:00 CT). Ohne IBC verlangt das Gateway danach eine
Anmeldung von Hand; bis dahin ist das ein echter Betriebsfall, kein Randfall.

**Offene Punkte:** IBC-Auto-Login für die `.bat` (speichert das Passwort im
Klartext — bewusst so entschieden, noch nicht gebaut). Sicherer
Handy-Fernzugriff (Auth + HTTPS): Tailscale / Cloudflare Tunnel / Reverse
Proxy — noch nicht gewählt.

**Dauerhaft offen / bewusst so:** Der Paper-Nachweis bleibt technisch indirekt
— Port + `DU`-Präfix + Allowlist sind zusammen das Stärkste, was die TWS-API
zulässt. Gebühren, die NACH der Füllmeldung eintreffen, landen im Schätzpfad
von `BrokerExecutor._build_trade` (mit Warnung) — bewusst, statt ein zweites
Order-Ereignis zu erzeugen, das im Protokoll wie eine zweite Füllung aussähe.
