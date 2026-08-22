# CLAUDE.md

Arbeitshinweise für dieses Repository. Was das Projekt *ist*, steht im
[README](README.md) — hier steht, was beim *Arbeiten daran* zu beachten ist.

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
.\.venv\Scripts\python.exe -m pytest tests/ -q      # Tests (aktuell 274)
.\.venv\Scripts\python.exe -m ruff check tradex tests scripts
.\.venv\Scripts\python.exe -m tradex.shell          # Desktop-Fenster
.\.venv\Scripts\python.exe -m tradex.shell --server # nur Engine, Port 8765
```

```powershell
cd ui ; npm run build      # UI bauen (nötig, bevor die Shell startet)
cd ui ; npm run typecheck  # TypeScript prüfen
```

Vor jedem Commit: **ruff sauber + alle Tests grün + `npm run typecheck`**.

### PowerShell-5.1-Fallen, die hier schon zugeschlagen haben

1. **`-replace` interpretiert `` `n `` NICHT als Zeilenumbruch.** Die Ersetzung
   läuft über .NET-Regex, dort ist der Backtick ein normales Zeichen. Ergebnis
   waren literale `` `n `` mitten im Quelltext und vier kaputte Dateien.
   → Mehrzeilige Änderungen **immer** mit dem Edit-Tool.

2. **`Get-Content` / `Set-Content` zerstören UTF-8.** Ohne BOM liest
   PowerShell 5.1 als Windows-1252; `§` wurde zu `Â§`, plus ein Fremdzeichen
   am Dateianfang. → Datei-Tools benutzen. Wenn es unvermeidlich ist:
   `[System.IO.File]::ReadAllText/WriteAllText` mit `UTF8Encoding($false)`.

3. **Commit-Messages mit Anführungszeichen scheitern.** Die native
   Argumentübergabe zerlegt sie. → Immer `git commit -F <datei>`.

4. **`.venv\Scripts\python.exe` ist ein Starter-Stub**, der den echten
   Interpreter als Kindprozess startet. In der Prozessliste erscheinen deshalb
   *zwei* Einträge pro Lauf — das ist normal, kein Doppelstart.

---

## Strategien

Seit dem Day-Trading-Umbau laufen **mehrere Strategien parallel an einem
Konto**. Drei Regeln dazu:

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

Seit Phase 4 kommt die schärfste Prüfung dazu:
`tests/test_backtest_runner.py::test_backtest_faellt_dieselben_entscheidungen_wie_die_strategie`
stellt den Backtest-Lauf dem blanken Strategielauf gegenüber und verlangt
identische Entscheidungen. **Fällt dieser Test, ist jede Backtest-Aussage
wertlos** — egal wie gut sie aussieht. Der Backtest darf der Analyse
ausschließlich die *Ausführung* hinzufügen, nie eine zweite Regelauslegung.

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
  solche Signale. **Achtung:** Sämtliche Zeitfenster der Strategie
  (`sweep_max_age_bars`, `fvg_max_age_bars`, …) zählen Bars, nicht Zeit — sie
  sind für Lücken blind. Ob das so bleiben soll, ist eine offene Frage.
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
  (SessionCalendar vs. SessionResolver).
- **Gegen Konstanten prüfen, nicht gegen Zahlen.** `assert n == _MAX_ATTEMPTS`,
  nicht `assert n == 3`.

---

## Daten

Zwei Quellen, beide mit eigenem Symbol, damit nichts verwechselt wird:

| Symbol | Was | Kosten |
|---|---|---|
| `MNQ_PROXY` | Nasdaq-100-Index-CFD via Dukascopy, 1m ab 2011 | 0 € |
| `MNQ_DEMO` | synthetisch, nur zum Prüfen der Oberfläche | – |
| `MNQ` / `NQ` | echte Futures (Databento/NinjaTrader) | Karte / 4 $ Monat |

```powershell
.\.venv\Scripts\python.exe scripts\fetch_dukascopy.py --from 2023-01-01
.\.venv\Scripts\python.exe scripts\generate_demo_data.py --days 45
.\.venv\Scripts\python.exe scripts\run_analysis.py --symbol MNQ_PROXY
.\.venv\Scripts\python.exe scripts\run_backtest.py --symbol MNQ_PROXY --save
```

`run_backtest.py` liefert **Rückgabewert 2**, wenn kein einziger Trade zustande
kam. Das ist kein Fehler, sondern ein Befund, der eine Antwort verlangt — und
in einem Skript nicht als Erfolg durchgehen darf.

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
  Rate: ~5 Handelstage/Minute. Der Abruf ist fortsetzbar.

---

## Was hier bewusst NICHT passiert

- **Schwellenwerte an Demo- oder Kurzzeitdaten anpassen.** Die FVG-/Liquidity-
  Regeln sind eine *Hypothese*. Ob sie einen Edge haben, beantwortet der
  Backtest in Phase 4 über Jahre — inklusive der Möglichkeit „nein".
- **Strategie-Defaults ohne Backtest ändern.** Der Stop-Anker steht auf
  `retracement`, weil er zum Einstiegsmodell passt (Einstieg auf den MSS *nach*
  dem Rücklauf) — nicht, weil er auf Demodaten besser aussah. `sweep`, `swing`
  und `fvg` bleiben konfigurierbar; Phase 4 entscheidet.
- **Regeln zurechtbiegen, damit ein Trade zustande kommt.** Ergibt die
  Positionsgröße 0, gibt es keinen Trade — der Stop wird *nicht* enger gesetzt.
- **Konfigurationswerte stillschweigend korrigieren.** `tradex/risk/consistency.py`
  *meldet* Widersprüche (z. B. `max_stop_ticks` größer als das Risikobudget
  erlaubt) und ändert nichts. Welche Seite angepasst wird, entscheidet der Nutzer.
- **Parameter optimieren.** Ein Suchlauf über Schwellenwerte findet zuverlässig
  eine Kombination, die auf der Vergangenheit gut aussieht — und sagt nichts über
  die Zukunft. Der Backtest beantwortet eine Ja/Nein-Frage zu *einer* Regelfassung.
- **Ausführungsannahmen aufweichen, damit der Backtest besser aussieht.** Die
  Werte unter `backtest:` sind absichtlich pessimistisch. `entry_fill:
  signal_close` existiert nur zum *Messen* des Unterschieds und meldet sich im
  Bericht selbst als beschönigend.
- **Live-Trading freischalten.** `execution.live_trading_enabled` ist aus und
  braucht eine zweite ausdrückliche Bestätigung (Spec §24). Phase 8/9.

---

## Aufbau

```
tradex/domain/      Bars, Instrumente, Enums — keine I/O
tradex/data/        Provider, Parquet-Store, Sessions, Aggregation, Integrität
tradex/analysis/    Swings, Struktur, FVG, Liquidität, Displacement, Bias
                    → MarketContext = der einzige Analysepfad
tradex/strategy/    base.py       Vertrag: Strategie schlaegt vor, Portfolio entscheidet
                    chain.py      Strategie 1 — ICT-Pflichtkette (niederfrequent)
                    opening_range Strategie 2 — Ausbruch aus der Eroeffnungsspanne
                    portfolio.py  mehrere Strategien, EINE Risikopruefung
                    registry.py   welche Strategien laufen — an genau einer Stelle
tradex/risk/        Größenberechnung, Tagesgrenzen, Konsistenzprüfung
tradex/backtest/    Ausführungssimulation, Kennzahlen, Bericht, Laufarchiv
tradex/api/         FastAPI + DTOs = einziger UI-Vertrag
tradex/service.py   Anwendungsschicht (Laden, Replay-Cursor, Protokoll)
ui/                 React + lightweight-charts v5, deutsch
bridge_nt8/         Protokoll-Spezifikation für Phase 5 (noch nicht gebaut)
```

Das Schema der Backtest-Tabellen steht in `tradex/persistence/db.py`
(Migration 2), der Zugriff darauf in `tradex/backtest/store.py`: die
Persistenzschicht darf nichts über Backtests wissen, sonst zeigt eine untere
Schicht auf eine obere.

Phasen 1–4 fertig. Aktuell läuft **Phase 4b: Day-Trading-Umbau** (Registry
fertig; offen: Multi-Instrument, News-Filter, Musterstatistik). Sie stand nicht
im ursprünglichen Plan — Phase 4 hat sie erzwungen, weil sie erstmals gemessen
hat, dass die Pflichtkette nur ~10 Trades im Jahr erzeugt.

**Phase 5 (Broker-Anschluss über NinjaTrader) wartet bewusst.** Sie ist der
einzige Baustein, der Geld kostet, wenn die Regel verliert. Stand: +0,125 R über
147 Trades, Vertrauensband −0,14 bis +0,39 — null ist eingeschlossen.
