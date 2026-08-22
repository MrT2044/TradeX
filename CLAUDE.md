# CLAUDE.md

Arbeitshinweise für dieses Repository. Was das Projekt *ist*, steht im
[README](README.md) — hier steht, was beim *Arbeiten daran* zu beachten ist.

> Diese Datei ist die **einzige Quelle** für Arbeitshinweise. `AGENTS.md`
> verweist nur hierher und enthält bewusst keinen Inhalt: Eine zweite Kopie hat
> genau einmal existiert, ist unbemerkt veraltet und hat dabei ihr Encoding
> verloren (`fÃ¼r` statt `für`). Wer hier etwas ändert, ändert es an genau
> einer Stelle.

---

## Stand

**Phasen 1–4 fertig. Aktuell läuft Phase 4b (Day-Trading-Umbau).**

| | |
|---|---|
| fertig | Registry, Multi-Instrument, Musterstatistik, News-Filter (Termine) |
| offen | News-Filter (Schlagzeilen) — braucht erst eine Messung, siehe unten |
| **Phase 5 läuft** | Papertrading-Betrieb steht; Echtzeit-Feed offen |

Phase 4b stand **nicht im ursprünglichen Plan**. Phase 4 hat sie erzwungen: sie
hat erstmals *gemessen*, dass die Pflichtkette nur ~10 Trades im Jahr erzeugt.

### Was gemessen ist — und was nicht

Der wichtigste Kontext für jede Arbeit hier:

| Lauf | Trades | Erwartungswert | 95-%-Band |
|---|---|---|---|
| Kette, 5m, 3,6 Jahre | 37 | +0,048 R | −0,46 … +0,56 |
| Kette, 1m, 2024 | 152 | −0,194 R | −0,44 … +0,05 |
| Kette + Eröffnungsspanne, 2024 | 147 | +0,125 R | −0,14 … +0,39 |
| Kette + Eröffnungsspanne, 1m, 2023–2026 | 426 | +0,016 R | −0,14 … +0,17 |
| dieselbe Regel, **MNQ + MES**, 2023–2026 (Lauf `id=11`) | 685 | −0,028 R | −0,14 … +0,09 |

Die beiden letzten Zeilen sind die belastbarsten Messungen, die es gibt — und
die ernüchterndsten. Mit der dreifachen Stichprobe schrumpft der Erwartungswert
von +0,125 R auf +0,016 R; mit dem zweiten Instrument kippt er ins Minus. Der
Zugewinn kommt fast vollständig aus der Eröffnungsspanne (645 von 685 Trades);
die Kette liefert weiterhin 40 Trades in 3,6 Jahren.

**Die Musterstatistik hat beim Zwei-Instrumente-Lauf ihren ersten echten
Treffer geliefert — und ihn selbst wieder kassiert.** Einstieg um 07 UTC:
−0,458 R über 104 Trades, q = 0,009, also auch nach Korrektur über 30 Tests
signifikant. Im hinteren Abschnitt: **+0,12 R über 76 Trades**. Das Vorzeichen
dreht. Ohne Gegenprobe wäre daraus die Regel „vor 08 UTC nicht handeln"
geworden — auf Grundlage von etwas, das im nächsten Zeitraum das Gegenteil tut.

Beim Einzelinstrument-Lauf übersteht keine der 24 getesteten Untergruppen die
Korrektur; die auffälligsten (08 UTC +0,34 R, 07 UTC −0,33 R) fallen out of
sample auf +0,04 R und +0,03 R zusammen.

**Alle Zahlen stammen aus `config/backtest_edge.yaml`** (25.000 statt
10.000 Konto). Mit `default.yaml` liefert derselbe Lauf über 2024 **6 Trades** —
das Risikobudget verwirft den Rest, bevor die Regel überhaupt zum Zug kommt.
Wer die Zahlen oben nachrechnen will, muss `TRADEX_CONFIG` setzen; sonst sieht
es aus, als sei etwas kaputt.

**Es gibt bis heute keinen nachgewiesenen Edge.** Jedes Vertrauensband schließt
null ein.

Das ist kein Grund aufzuhören — aber es bestimmt, welcher Schritt als nächster
erlaubt ist. **Papertrading kostet nichts außer Zeit**, deshalb läuft es
(Phase 5, siehe unten): es prüft die Mechanik des Betriebs und liefert eine
zweite, unabhängige Messung derselben Regel. **Echtes Geld wartet weiter.**
`execution.live_trading_enabled` ist aus und braucht eine zweite ausdrückliche
Bestätigung (Spec §24, Phase 8/9). Der Unterschied ist der ganze Punkt: der
eine Schritt kostet nichts, wenn die Regel verliert, der andere schon.

Wer hier etwas baut, baut Messwerkzeuge, bis diese Frage beantwortet ist.

---

## Umgebung

**Die Bash-Tools funktionieren auf dieser Maschine nicht.** Git Bash bricht mit
Cygwin-Fork-Fehlern ab (`dofork: child -1`, `Resource temporarily unavailable`).
**Immer PowerShell benutzen.** Für Dateien die Datei-Tools (Read/Write/Edit),
nicht Shell-Textmanipulation — Gründe unten.

Für den Alltag reicht ein Doppelklick auf `TradeX.bat` (prüft Umgebung, baut das
UI beim ersten Start, öffnet das Fenster; Argumente werden durchgereicht).
**Batchdateien brauchen CRLF** — `.gitattributes` erzwingt das, weil cmd.exe
reine LF-Dateien nur teilweise verarbeitet und `goto` dabei still bricht.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -q      # Tests (aktuell 422)
.\.venv\Scripts\python.exe -m ruff check tradex tests scripts
.\.venv\Scripts\python.exe -m tradex.shell          # Desktop-Fenster
.\.venv\Scripts\python.exe -m tradex.shell --server # nur Engine, Port 8765
```

```powershell
cd ui ; npm run build      # UI bauen (nötig, bevor die Shell startet)
cd ui ; npm run typecheck  # TypeScript prüfen
```

Vor jedem Commit: **ruff sauber + alle Tests grün + `npm run typecheck`**.

### Git

Im Repo ist **keine Git-Identity gesetzt** — weder lokal noch global. Ein
blankes `git commit` bricht mit „Author identity unknown" ab. Die Identität pro
Befehl durchreichen, nichts dauerhaft setzen:

```powershell
git -c user.name="Niklas" -c user.email="niklas.klingler50@gmail.com" commit -F <datei>
```

Kein Remote, ein Commit je Phase, direkt auf `main`. Commit-Messages auf
Deutsch mit ausführlichem Body, der die *Begründungen* trägt — nicht nur die
Dateiliste.

### PowerShell-5.1-Fallen, die hier schon zugeschlagen haben

1. **`-replace` interpretiert `` `n `` NICHT als Zeilenumbruch.** Die Ersetzung
   läuft über .NET-Regex, dort ist der Backtick ein normales Zeichen. Ergebnis
   waren literale `` `n `` mitten im Quelltext und vier kaputte Dateien.
   → Mehrzeilige Änderungen **immer** mit dem Edit-Tool.

2. **`Get-Content` / `Set-Content` zerstören UTF-8.** Ohne BOM liest
   PowerShell 5.1 als Windows-1252; `§` wurde zu `Â§`, plus ein Fremdzeichen
   am Dateianfang. Genau so ist `AGENTS.md` zerschossen worden.
   → Datei-Tools benutzen. Wenn es unvermeidlich ist:
   `[System.IO.File]::ReadAllText/WriteAllText` mit `UTF8Encoding($false)`.

3. **Commit-Messages mit Anführungszeichen scheitern.** Die native
   Argumentübergabe zerlegt sie. → Immer `git commit -F <datei>`.

4. **`.venv\Scripts\python.exe` ist ein Starter-Stub**, der den echten
   Interpreter als Kindprozess startet. In der Prozessliste erscheinen deshalb
   *zwei* Einträge pro Lauf — das ist normal, kein Doppelstart.

5. **`cmd /c "pfad mit leerzeichen\datei.bat" args` scheitert.** Die äußeren
   Anführungszeichen werden abgeräumt, cmd sucht dann `D:\Claude`.
   → `cmd.exe /c call "<pfad>" args`.

6. **Lange Läufe nicht an die Sitzung hängen.** Backtests über Jahre laufen
   30+ Minuten, Datenabrufe Stunden. `Start-Process ... -RedirectStandardOutput`
   überlebt das Sitzungsende; ein Hintergrundbefehl im Werkzeug nicht zwingend.

---

## Die vier Invarianten

Sie machen Spec §29 („Backtest ≡ Live") technisch erzwingbar. **Nichts davon
aufweichen** — jede Verletzung entwertet rückwirkend jede Backtest-Aussage.

1. **Nur geschlossene Bars werden analysiert.** Die laufende Bar wird angezeigt,
   erreicht aber nie einen Detektor.
2. **Detektoren sind reproduzierbar.** Zustand ist reine Funktion aus
   (gesehene Bars, Parameter). Kein I/O, keine Uhr, keine Zufallszahlen.
3. **Ein einziger Analysepfad:** `MarketContext.on_base_bar()` — identisch für
   Replay, Backtest und später Live. Niemals einen zweiten „optimierten" Weg.
4. **Keine Magic Numbers.** Jeder Schwellenwert steht in `config/default.yaml`.
   Die Config ist `extra="forbid"` — ein Tippfehler bricht den Start ab.

Tests prüfen das direkt (`tests/test_context.py`, `tests/test_strategy_engine.py`):
zwei Läufe → byte-identische Snapshots; `feed()` ≡ `on_base_bar()`.

Die schärfste Prüfung ist
`tests/test_backtest_runner.py::test_backtest_faellt_dieselben_entscheidungen_wie_die_strategie`:
Sie stellt den Backtest-Lauf dem blanken Strategielauf gegenüber und verlangt
identische Entscheidungen. **Fällt dieser Test, ist jede Backtest-Aussage
wertlos** — egal wie gut sie aussieht. Der Backtest darf der Analyse
ausschließlich die *Ausführung* hinzufügen, nie eine zweite Regelauslegung.

Invariante 3 gilt seit Phase 4b **auf zwei weiteren Ebenen**:
`Backtester.run()` ist `run_many()` mit einem Buch (ein Instrument = Sonderfall,
kein zweiter Pfad), und die Registry ist die einzige Stelle, an der Strategien
entstehen.

---

## Strategien

Es laufen **mehrere Strategien parallel an einem Konto**, auf **mehreren
Instrumenten**. Vier Regeln:

1. **Strategien schlagen vor, das Portfolio entscheidet.** Positionsgröße,
   Tagesgrenzen und Handelsfenster gehören `portfolio.py`, weil sie für das
   ganze Konto gelten. Rechnete jede Strategie ihre eigene Größe, wäre das
   tatsächliche Gesamtrisiko die Summe der Einzelbudgets.
2. **Neue Strategien werden in `registry.py` eingetragen — nirgends sonst.**
   Sonst liefe der Backtest irgendwann mit einer anderen Zusammenstellung als
   der Live-Betrieb (Spec §29, eine Ebene höher).
3. **Der Ledger-Schlüssel wird im Risikobuch vergeben** (`ledger.next_trade_id()`),
   nicht in Strategie oder Portfolio. Diese Verwechslung ist **zweimal**
   aufgetreten — erst kollidierten Setup-Nummern zwischen Strategien, dann
   `trade_id` zwischen Instrumenten. Wer den Zähler dort führt, wo das Konto
   liegt, kann sie kein drittes Mal bauen.
4. **Bars mehrerer Instrumente werden chronologisch verschränkt.** Liefe der
   Backtest Symbol für Symbol durch, sähe das gemeinsame Risikobuch beim
   zweiten bereits alle Ergebnisse des ersten.

Eine neue Strategie ist eine **Hypothese**, kein Feature. Sie wird vor der
ersten Messung festgelegt und danach ehrlich vermessen — auch mit dem Ergebnis
„kein Edge". Was nicht passiert: Werte verändern, bis der Backtest gefällt.

**Prüfen, ob eine Strategie überhaupt auslösen KANN.** Der Opening Range
Breakout stand anfangs auf `target_range_mult: 2.0` bei `min_rr: 2.0` — der
Stop liegt auf der Gegenseite der Spanne, das erreichbare CRV ist deshalb
`mult × W / (W + Puffer)` und damit *immer* kleiner als `mult`. Ergebnis: 387
Ablehnungen, null Trades, und ein Backtest, der brav „kein Edge" gemeldet
hätte, obwohl die Regel nie zum Zug kam. `risk/consistency.py` meldet diesen
Fall jetzt.

### Warum die Kette allein kein Day Trading kann

Gemessen auf MNQ_PROXY 2024, pro Handelstag:

```
Sweeps auf 5m                    50,9
davon in Bias-Richtung           18,9   -> Kandidat
  -> Impuls + FVG                 2,1   −89 %
  -> Rücklauf                     2,1     0 %   (filtert nichts)
  -> MSS bestätigt               0,25   −88 %
```

Zwei Pflichtglieder filtern je rund 89 %, multipliziert 1,3 %. **Das ist
Arithmetik, kein Fehler** — kein Schwellenwert ändert daran etwas. Für tägliche
Trades braucht es weitere Strategien *neben* ihr, nicht schnellere Werte *in*
ihr.

---

## Phase 5: das Programm handelt selbst

```powershell
.\.venv\Scripts\python.exe scripts\run_paper.py --symbol MNQ_PROXY --speed 3600
```

Es entstehen echte Entscheidungen und vollständig durchsimulierte Trades — mit
Schlupf, Gebühren, „Stop zuerst". Es fließt **kein Geld**: es gibt keine
Order-Anbindung an einen Broker, und `execution.live_trading_enabled` bleibt
aus (Spec §24, Phase 8/9).

### Warum das kein zweites Programm ist

`tradex/live/` enthält **keine Analyse, keine Regel, keine Füll-Logik, keine
Positionsgröße.** All das kommt aus `SymbolBook` — genau der Klasse, die auch
den Backtest führt. Backtest und Papertrading sind nicht „zwei Wege, die
dasselbe tun sollen", sondern derselbe Weg mit verschiedenen Bar-Quellen: der
Backtest zieht Bars aus einer Datei, die Sitzung aus einem Feed.

Der Test `test_papertrading_faellt_dieselben_entscheidungen_wie_der_backtest`
hält das fest — Trades Feld für Feld, Entscheidungen Zeitstempel für
Zeitstempel. **Fällt er, sagt kein Backtest mehr etwas über den Betrieb aus.**

An echten Daten geprüft (MNQ_PROXY, Q1 2024, Messkonfiguration): Sitzung und
Backtest liefern beide 48 Trades, +0,300 R, +847,76 USD. Und über Juni 2026
beide *null* Signale — die Übereinstimmung gilt in beide Richtungen.

### Was die Sitzung beiträgt

Vier Dinge, die ein Backtest nicht braucht:

1. **Bars kommen einzeln und unvorhersehbar.** Doppelte oder rückwärts
   laufende Bars (nach einem Feed-Neustart) werden verworfen — zweimal
   analysiert sähen die Detektoren einen Verlauf, den es nie gab.
2. **Der Feed kann ausfallen**, und Stille ist von einem ruhigen Markt nicht zu
   unterscheiden. Nur der Blick auf die Uhr trennt beides, deshalb läuft
   `check_liveness()` in jedem Durchgang — auch wenn nichts passiert.
3. **Ergebnisse müssen sofort haltbar sein.** Jede Sitzung bekommt ihre Zeile
   beim Start, jeder Trade wird beim Schließen geschrieben. Eine Sitzung ohne
   `ended_utc` ist eine laufende oder eine abgestürzte.
4. **Es braucht einen Not-Aus.**

### Der Not-Aus — die wichtigste Einzelentscheidung

Angehalten wird über die Risk Engine (`halt_reason`), **nicht** dadurch, dass
keine Bars mehr eingespeist werden. Eine angehaltene Sitzung verarbeitet Bars
weiter und führt offene Positionen zu Ende; sie nimmt nur keine neuen auf.

Wer stattdessen die Bars abklemmt, lässt offene Positionen **ohne
Stopüberwachung** zurück — die gefährlichste denkbare Reaktion auf eine
Störung. Ein Test hält beides fest.

Ebenso: **jede Sitzung beginnt angehalten** (`not_connected`) und wird erst
handlungsfähig, wenn der Feed sich gemeldet hat. Die Umkehrung fällt nicht auf
— eine Sitzung, die Bars aus einer nie bestätigten Quelle handelt, sieht von
außen normal aus.

### Feeds

| Feed | Stand | Daten |
|---|---|---|
| `replay` | fertig | lokaler Bestand, beschleunigt oder in Echtzeit |
| `nt8` | offen | NinjaTrader + AddOn aus `bridge_nt8/` |

Die Wiedergabe ist **kein Ersatz für echte Daten** — sie prüft die Mechanik des
Betriebs (Ausfallerkennung, Not-Aus, Persistenz) deterministisch und kostenlos.
Genau das sieht `bridge_nt8/README.md` als ersten Schritt vor. Jede gespeicherte
Sitzung trägt deshalb ihren Feed-Namen.

---

## News-Filter

Zwei Dinge, die nichts miteinander zu tun haben:

| | Termine | Schlagzeilen |
|---|---|---|
| Beispiel | FOMC, CPI, NFP | „Trump sagt etwas" |
| bekannt | Wochen vorher, minutengenau | gar nicht |
| historisch abrufbar | ja | schwer, mit exaktem Zeitstempel kaum |
| **backtestbar** | **ja** | **nein** (ohne Archiv) |
| Stand | gebaut | offen |

Der Unterschied ist keine Haarspalterei, sondern Spec §29. **Blockiert die
Live-Engine Trades wegen einer Schlagzeile, die der Backtest nie gesehen hat,
ist jede Backtest-Aussage rückwirkend wertlos.** Deshalb ist nur der Teil
gebaut, der in beiden Welten dieselbe Antwort gibt.

### Wie er funktioniert

Dieselbe Aufteilung wie bei Kursdaten: **Skript holt, Speicher hält, Engine
liest.** `scripts/fetch_news.py` schreibt nach `data/news/events.jsonl`, die
Engine liest ausschließlich diese Datei. Ein HTTP-Aufruf mitten in einer
Entscheidung würde Invariante 2 dreifach verletzen — er kann scheitern, er
liefert je nach Zeitpunkt anderes, und er ist nicht wiederholbar.

Drei Quellen, weil keine kostenlose beides kann (Historie **und** Uhrzeit):

| Quelle | liefert | Schlüssel | Haken |
|---|---|---|---|
| `holidays` | Börsenfeiertage | – | reine Rechnung, kein Netz |
| `forexfactory` | exakte Uhrzeit + Impact | **nein** | nur die laufende Woche |
| `fred` | Historie ab 2000 | frei, ohne Karte | nur der **Tag**, keine Uhrzeit |

Termine aus FRED tragen deshalb `TimePrecision.ASSUMED` und bekommen ein
breiteres Fenster (`assumed_time_extra_minutes`), statt eine Genauigkeit
vorzutäuschen, die die Quelle nicht hat. Der Bestand für den laufenden Betrieb
wächst wöchentlich aus ForexFactory — `merge()` führt zusammen, überschreibt
nie, und ein bekannter Termin behält seine Fassung.

### Vier Regeln

1. **Nur Einstiege werden gesperrt, nie Ausstiege.** Eine offene Position ohne
   Stop wäre das Gegenteil von Risikosenkung. Das ist keine Einstellung,
   sondern Architektur: `tradex/news/` kennt die Ausführung nicht, ein Test
   verbietet den Import.
2. **Ein Filter ohne Daten meldet sich.** Der teuerste Fehler wäre ein
   eingeschalteter Filter, der außerhalb seiner Abdeckung alles durchwinkt und
   dabei aussieht wie einer, der nichts zu beanstanden hat. Der Kalender kennt
   seine Grenzen (`covers()`), es gibt einen eigenen Reason-Code
   (`news.no_data`), und der Backtest-Bericht warnt, wenn ein Lauf weitgehend
   ohne Filter gerechnet wurde.
3. **Unbekannte Impact-Stufen werden übersprungen, nicht geraten.**
   „Vermutlich mittel" wäre eine erfundene Sperre.
4. **Der Kalender wird an genau einer Stelle geladen** (`registry.py`) und an
   alle Instrumente durchgereicht — dieselbe Überlegung wie beim Risikobuch.

### Warum Schlagzeilen noch fehlen

Die einzige kostenlose Quelle mit Historie **und** 15-Minuten-Auflösung ist
GDELT (ohne Schlüssel, ab 2015). Die Artikel-API antwortet dieser IP derzeit
mit HTTP 429; verifizieren ließ sie sich nicht.

Wichtiger als das Netzproblem: Eine Schlagzeilen-Sperre wäre die erste Regel im
System, die niemals gemessen wurde. Der richtige Weg ist die Reihenfolge, die
dieses Projekt überall verwendet — erst messen (bewegt ein Schlagzeilen-Ausschlag
den Kurs überhaupt?), dann filtern. Die Musterstatistik ist genau dafür da.

---

## Musterstatistik

`scripts\run_backtest.py --muster` prüft, ob von den Aufschlüsselungen des
Berichts etwas einer Prüfung standhält. Vier Regeln, die den Unterschied
zwischen Statistik und Selbstbetrug ausmachen:

1. **Nur Merkmale, die beim Einstieg feststanden.** `exit_reason`, `mae_r`,
   `bars_held`, `pnl` sind Ergebnisse. „Trades, die am Ziel schlossen, laufen
   besser" ist wahr und wertlos. `OUTCOME_FIELDS` führt die Gegenliste, und ein
   Test reicht jeder Bedingung einen Trade herein, der genau diese Felder
   verweigert — die Regel ist damit nicht bloß dokumentiert.
2. **Die Bedingungsliste steht vor der Messung fest.** Wer eine Bedingung
   hinzufügt, weil die vorhandenen nichts gefunden haben, betreibt Mustersuche
   mit zusätzlichen Schritten.
3. **Jede getestete Gruppe verschärft die Korrektur für alle anderen.** Deshalb
   fallen Bedingungen mit nur einem Wert heraus, und deshalb werden Gruppen
   unter `patterns.min_trades` zwar gezeigt, aber nicht getestet.
4. **Ein Fund ist eine Hypothese, kein Befund.** Die Gegenprobe auf dem hinteren
   Abschnitt kann ein Muster widerlegen, keines beweisen — dort liegen je Gruppe
   zu wenige Trades.

`significance.py` rechnet die t-Verteilung von Hand (regularisierte
unvollständige Betafunktion), statt `scipy` als 60-MB-Abhängigkeit für drei
Formeln aufzunehmen. Die Tests stellen sie deshalb gegen **unabhängig bekannte**
Werte: geschlossene Sonderfälle der Betafunktion und Zahlen aus der t-Tabelle.
Gegen sich selbst geprüft wäre eine eigene Implementierung wertlos.

---

## Konventionen

- **Kommentare, Docstrings und UI auf Deutsch.** Trading-Fachbegriffe bleiben
  englisch (FVG, Sweep, MSS, Displacement), im UI mit Erklärung (Spec §23).
- **Umlaute in Python-Quelltext vermeiden** (`ue`, `ae`, `oe`, `ss`) — siehe
  die Encoding-Falle oben. In Markdown und `.ts` sind sie in Ordnung.
- **Begründungen sind Reason-Codes, keine Sätze.** Die Engine liefert
  `code + params`, `ui/src/i18n/de.ts` übersetzt. Zwei Tests prüfen, dass jeder
  Code eine Übersetzung hat — ein fehlender zeigte sich sonst erst als
  `undefined` im Dashboard.
- **Neue Schwellenwerte** gehören in `config/default.yaml` *und* als
  pydantic-Feld in `tradex/config.py`.
- **Schlupf läuft beim Ein- und Ausstieg in entgegengesetzte Richtungen.**
  Teurer *kaufen* heißt höher, schlechter *verkaufen* heißt tiefer. Eine
  gemeinsame „gegen die Position"-Funktion für beides hat genau hier schon
  zugeschlagen: Stops füllten *besser* als ihr Kurs, was aus dem pessimistischen
  Simulator einen geschönten machte. Deshalb `_slip_entry` **und** `_slip_exit`
  in `tradex/backtest/execution.py`.
- **„Die nächste Bar" ist nicht „die nächste Minute".** Eine Bar gilt erst als
  geschlossen, wenn die *nächste* eintrifft (Invariante 1). Fällt der Handel
  dazwischen aus, liegen dazwischen Stunden. An echten Daten aufgefallen:
  Juneteenth 2023, Handelsende 11:58 CT, Fortsetzung 17:00 CT — ein Signal vom
  Vormittag wurde um 17:01 gefüllt. `backtest.max_signal_age_bars` verwirft
  solche Signale, gerechnet **ab dem Schluss der Signalbar** (eine 5m-Bar ist
  beim Schließen schon fünf Minuten alt; in Basis-Bars gerechnet wäre sie immer
  „veraltet" und 139 gültige Signale wurden so fälschlich verworfen).
  **Achtung:** Sämtliche Zeitfenster der Strategie (`sweep_max_age_bars`,
  `fvg_max_age_bars`, …) zählen Bars, nicht Zeit — sie sind für Lücken blind.
  Ob das so bleiben soll, ist eine offene Frage.
- **Der `config_hash` muss zur tatsächlich geladenen Datei gehören.** Nie fest
  `default.yaml` annehmen — unter `TRADEX_CONFIG` etikettiert das jeden
  gespeicherten Lauf falsch. `resolved_config_path()` benutzen.
- **Provider-Abstraktion respektieren.** Die Engine darf nie eine konkrete
  Datenquelle kennen. `ProviderCapabilities` macht explizit, was eine Quelle
  kann — die Kernstrategie darf sich nur auf Fähigkeiten stützen, die jede
  produktive Quelle hat (also **kein** Level 2, **kein** Pflicht-Volumen).

---

## Testkonventionen

- **Handgebaute Fixtures statt Zufallsdaten**, wo das Ergebnis nachrechenbar
  sein soll. Zufall findet Abstürze, beweist aber keine Regel.
- **Wächter-Tests gegen leere Wahrheit.** `test_kette_wird_ueberhaupt_vollstaendig`
  existiert, weil alle Invarianten-Tests grün blieben, während die Strategie
  strukturell *nie* auslöste. Wenn ein Test „für alle X gilt Y" prüft, braucht
  es einen zweiten, der sicherstellt, dass es überhaupt X gibt.
- **Zwei Implementierungen gegeneinander stellen**, wo es sie gibt: Batch vs.
  Streaming (Aggregator, ATR, Swings), vektorisiert vs. skalar
  (SessionCalendar vs. SessionResolver), ein Instrument vs. mehrere.
- **Gegen Konstanten prüfen, nicht gegen Zahlen.** `assert n == _MAX_ATTEMPTS`,
  nicht `assert n == 3`.
- **Ein Test, der nur unter Testbedingungen gilt, gehört dorthin, wo er gilt.**
  Die Tagesgrenze greift ausschließlich im Backtest — im reinen Strategielauf
  füllt niemand das Risikobuch. Ein Test dafür im Strategielauf wäre grün und
  bedeutungslos.

---

## Daten

| Symbol | Was | Kosten |
|---|---|---|
| `MNQ_PROXY` | Nasdaq-100-Index-CFD via Dukascopy, 1m ab 2011 | 0 € |
| `MES_PROXY` | S&P-500-Index-CFD via Dukascopy | 0 € |
| `MNQ_DEMO` | synthetisch, nur zum Prüfen der Oberfläche | – |
| `MNQ` / `NQ` | echte Futures (Databento/NinjaTrader) | Karte / 4 $ Monat |

```powershell
.\.venv\Scripts\python.exe scripts\fetch_dukascopy.py --symbol MNQ_PROXY --from 2023-01-01
.\.venv\Scripts\python.exe scripts\generate_demo_data.py --days 45
.\.venv\Scripts\python.exe scripts\run_analysis.py --symbol MNQ_PROXY
.\.venv\Scripts\python.exe scripts\run_backtest.py --symbol MNQ_PROXY,MES_PROXY --save
.\.venv\Scripts\python.exe scripts\run_backtest.py --symbol MNQ_PROXY --muster
.\.venv\Scripts\python.exe scripts\fetch_news.py --source forexfactory
.\.venv\Scripts\python.exe scripts\run_paper.py --symbol MNQ_PROXY --speed 3600
```

`run_backtest.py` liefert **Rückgabewert 2**, wenn kein einziger Trade zustande
kam. Das ist kein Fehler, sondern ein Befund, der eine Antwort verlangt — und
in einem Skript nicht als Erfolg durchgehen darf.

**Mehrere Instrumente sind nicht mehrfach so viel Information.** S&P und Nasdaq
sind stark korreliert; die Vertrauensbänder schrumpfen langsamer als die
Trade-Anzahl wächst. Für echten Zugewinn braucht es weniger korrelierte Märkte.

Inzwischen gemessen (Lauf `id=11`): 426 → 685 Trades, das Band schrumpft von
0,31 R auf 0,23 R Breite. **Das ist genau die Schrumpfung, die unabhängige
Stichproben liefern würden — und deshalb ein Warnzeichen, kein Erfolg.** Der
t-Test rechnet mit ⁠1/√n und weiß nichts davon, dass ein MNQ- und ein
MES-Trade zur selben Stunde derselbe Trade in zwei Verkleidungen sein können.
Das ausgewiesene Band ist bei zwei korrelierten Märkten also **zu schmal**;
die wahre Unsicherheit liegt darüber. Wie weit, sagt keine der Zahlen hier —
das wäre erst über gemeinsame Zeitfenster zu schätzen.

Die aussagekräftigere Zahl steht in der Aufschlüsselung: MES bringt −0,105 R,
MNQ +0,025 R. Die Regel trägt nicht überall.

### Dukascopy — Fallen, die schon zugeschlagen haben

- **Feldreihenfolge ist `(offset, open, close, low, high, volume)` — nicht
  OHLC.** Als OHLC gelesen verletzten 1281 von 1329 echten Kerzen
  `low ≤ open,close ≤ high`. Nichts stürzt ab, alles darauf ist wertlos.
- **Monat in der URL ist nullbasiert** (Januar = `00`). Off-by-one holt
  klaglos den Vormonat.
- **1440 Sätze pro Tag**, auch für handelsfreie Zeiten. Füllminuten haben
  Volumen 0 und werden verworfen.
- **Der CFD folgt nicht den CME-Zeiten** — Pause 15:15–17:06 CT statt
  16:00–17:00. Steht als Instrument-Override in `config/instruments.yaml`.
- **Drosselung mit HTTP 503.** 429/503 heißt „zu schnell", nicht „kaputt".
  **Nie zwei Läufe parallel starten** — das löst die Sperre zuverlässig aus.
  Rate: ~5 Handelstage/Minute, drei Jahre also gut drei Stunden. Der Abruf ist
  fortsetzbar; vorhandene Tage werden übersprungen.
- **Ein neues Symbol immer erst mit drei Tagen prüfen**, bevor der lange Abruf
  startet. Ein falscher Symbolname kostet sonst Stunden.

---

## Was hier bewusst NICHT passiert

- **Schwellenwerte an Demo- oder Kurzzeitdaten anpassen.** Die Regeln sind
  *Hypothesen*. Ob sie einen Edge haben, beantwortet der Backtest über Jahre —
  inklusive der Möglichkeit „nein".
- **Strategie-Defaults ohne Backtest ändern.** Der Stop-Anker steht auf
  `retracement`, weil er zum Einstiegsmodell passt (Einstieg auf den MSS *nach*
  dem Rücklauf) — nicht, weil er auf Demodaten besser aussah. `sweep`, `swing`
  und `fvg` bleiben konfigurierbar.
- **Regeln zurechtbiegen, damit ein Trade zustande kommt.** Ergibt die
  Positionsgröße 0, gibt es keinen Trade — der Stop wird *nicht* enger gesetzt.
- **Konfigurationswerte stillschweigend korrigieren.** `tradex/risk/consistency.py`
  *meldet* Widersprüche und ändert nichts. Welche Seite angepasst wird,
  entscheidet der Nutzer.
- **Parameter optimieren.** Ein Suchlauf über Schwellenwerte findet zuverlässig
  eine Kombination, die auf der Vergangenheit gut aussieht — und sagt nichts über
  die Zukunft. Der Backtest beantwortet eine Ja/Nein-Frage zu *einer* Regelfassung.
  Bei n ≈ 150 und einer Streuung von 1,6 R verschiebt **jede** Änderung das
  Ergebnis um mehr als den Messfehler: man würde garantiert Rauschen auswählen.
- **Ausführungsannahmen aufweichen, damit der Backtest besser aussieht.** Die
  Werte unter `backtest:` sind absichtlich pessimistisch. `entry_fill:
  signal_close` existiert nur zum *Messen* des Unterschieds und meldet sich im
  Bericht selbst als beschönigend.
- **Musterspuren aus der Historie suchen.** Ein Algorithmus, der die
  Vergangenheit nach dem besten Muster durchsucht, findet immer eines. Die
  belastbare Form steht seit Phase 4b in `tradex/backtest/patterns.py`:
  eine *feste, vorher festgelegte* Liste von Bedingungen, bedingte Verteilungen
  mit Mehrfachtest-Korrektur, Gegenprobe auf dem hinteren Abschnitt. Der
  Unterschied zur Suche ist die Zahl der Hypothesen — und dass die Korrektur
  sie kennt.
- **Live-Trading freischalten.** `execution.live_trading_enabled` ist aus und
  braucht eine zweite ausdrückliche Bestätigung (Spec §24). Phase 8/9.

---

## Aufbau

```
tradex/domain/      Bars, Instrumente, Enums — keine I/O
tradex/data/        Provider, Parquet-Store, Sessions, Aggregation, Integrität
tradex/analysis/    Swings, Struktur, FVG, Liquidität, Displacement, Bias
                    → MarketContext = der einzige Analysepfad
tradex/strategy/    base.py         Vertrag: Strategie schlägt vor, Portfolio entscheidet
                    chain.py        Strategie 1 — ICT-Pflichtkette (niederfrequent)
                    opening_range   Strategie 2 — Ausbruch aus der Eröffnungsspanne
                    portfolio.py    mehrere Strategien, EINE Risikoprüfung
                    registry.py     welche Strategien laufen — an genau einer Stelle
tradex/news/        events.py       Zeitpunkt, Name, Land, Wucht — mehr nicht
                    store.py        lokaler Bestand (JSONL) = einzige Quelle der Engine
                    calendar.py     Sperrfenster, reine Funktion ohne I/O
                    providers.py    NUR fürs Abrufskript, nie für die Engine
tradex/risk/        Größenberechnung, Tagesgrenzen, Konsistenzprüfung, Risikobuch
tradex/backtest/    execution.py    wie ein Signal gefüllt und beendet worden wäre
                    runner.py       SymbolBook (ein Instrument) + Backtester (das Konto)
                    metrics.py      reine Kennzahlen über Trade-Mengen
                    significance.py t-Test, Vertrauensband, FDR — reine Mathematik
                    patterns.py     bedingte Verteilungen: hält davon etwas stand?
                    report.py       Bündelung, Einordnung, Ausgabe
                    store.py        Laufarchiv
tradex/live/        feed.py         was ein Live-Feed liefern darf: nur GESCHLOSSENE Bars
                    replay_feed.py  gespeicherte Bars als Feed — deterministisch, gratis
                    session.py      Zustandsmaschine: Bars rein, Papertrades raus
                    runner.py       die Schleife: Zeit, Stille, Abbruch
                    store.py        Sitzung und Trades SOFORT haltbar machen
tradex/api/         FastAPI + DTOs = einziger UI-Vertrag
tradex/service.py   Anwendungsschicht (Laden, Replay-Cursor, Protokoll)
ui/                 React + lightweight-charts v5, deutsch
bridge_nt8/         Protokoll-Spezifikation für Phase 5 (noch nicht gebaut)
```

Das Schema der Backtest-Tabellen steht in `tradex/persistence/db.py`
(Migration 2), der Zugriff darauf in `tradex/backtest/store.py`: die
Persistenzschicht darf nichts über Backtests wissen, sonst zeigt eine untere
Schicht auf eine obere.

`config/backtest_edge.yaml` ist eine **Messkonfiguration**, kein Betriebsprofil:
einziger Unterschied zu `default.yaml` ist die Kontogröße (25.000 statt 10.000),
damit das Risikobudget nicht die eigentliche Frage überdeckt. Ein Test hält
fest, dass es bei diesem einen Unterschied bleibt.
