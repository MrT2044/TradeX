# TradeX

Regelbasiertes, backtestbares Analyse- und (später) Handelssystem für
Nasdaq-100-Futures (MNQ/NQ).

**Aktueller Stand: Phase 1–4 — Analyse, Strategie, Risiko und Backtest.**
Setups werden erkannt, Einstieg, Stop, Ziel und Positionsgröße werden
durchgerechnet, protokolliert und über die Historie ausgewertet. **Es werden
keine Orders ausgeführt** — das kommt ab Phase 5.

---

## Schnellstart

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

```bash
cd ui && npm install && npm run build && cd ..
```

Ohne Marktdatenkonto lässt sich alles mit synthetischen Testdaten ausprobieren:

```bash
python scripts/generate_demo_data.py --days 45
```

```bash
python -m tradex.shell
```

> Die Demodaten sind **keine Marktdaten**. Sie liegen unter dem eigenen Symbol
> `MNQ_DEMO` und die Oberfläche warnt deutlich davor. Sie eignen sich, um
> Oberfläche und Detektoren zu prüfen — **nicht**, um irgendetwas über die
> Strategie auszusagen.

Echte Marktdaten — kostenlos, ohne Konto und ohne Kreditkarte:

```bash
python scripts/fetch_dukascopy.py --from 2023-01-01
```

---

## Marktdaten und laufende Kosten

Recherchestand August 2026. Zielvorgabe war ≈ 5 USD/Monat (Spec §3, §26).

| Zweck | Quelle | Kosten | Konto nötig? |
|---|---|---|---|
| **Historie / Backtest** | **Dukascopy** — Nasdaq-100-Index-CFD, 1m ab 2011 | **0 USD** | **nein** |
| Historie (echte Futures) | Databento `GLBX.MDP3` | ≈ 0 USD (125 USD Startguthaben) | ja, **Kreditkarte** |
| **Live / Paper** (ab Phase 5) | NinjaTrader + CME Level 1 non-professional | **≈ 4 USD/Monat** | ja |
| Level 2 / Markttiefe | NinjaTrader CME Level 2 | ~16 USD/Monat — **außerhalb Budget, bewusst nicht genutzt** | ja |

**Laufende Gesamtkosten im Zielzustand: ≈ 4 USD/Monat.**

Databento verlangt zur Freischaltung eine Kreditkarte. Deshalb ist **Dukascopy**
die Standardquelle für die Historie: öffentlicher Datenfeed, kein Konto, keine
Zahlungsdaten, 1-Minuten-Kerzen ab 2011.

### Was der Dukascopy-Weg liefert — und was nicht

Geladen wird der Nasdaq-100-**Index** als CFD, nicht der MNQ-**Future**. Der
Unterschied muss beim Deuten von Backtest-Ergebnissen präsent bleiben:

| | Index-CFD (`MNQ_PROXY`) | MNQ-Future |
|---|---|---|
| Preis | Index | Index ± Basis (Finanzierung, Dividenden) |
| Rollen | keine | quartalsweise |
| Volumen | Aktivitätskennzahl | gehandelte Kontrakte |
| Handelspause | 15:15–17:06 CT *(gemessen)* | 16:00–17:00 CT |

Die Intraday-Struktur ist praktisch identisch — für die Frage „hat die
Regelmechanik einen Edge?" ist das eine gute Näherung. **Für die Freigabe von
Echtgeld braucht es echte MNQ-Daten** (NinjaTrader ab Phase 5).

Weil Markttiefe nicht im Budget liegt, darf die Kernstrategie nie davon abhängen
(Spec §4). `ProviderCapabilities` macht für jede Quelle explizit, was sie liefert.
Dass Volumen bei diesem Proxy keine Kontrakte sind, ist genau der Grund, warum
`volume_is_gate` in der Displacement-Regel per Default aus ist.

> **Fair bleiben:** Der Feed ist kostenlos und wird bei zu dichten Anfragen
> gedrosselt (HTTP 503). Der Importer hält deshalb einen Mindestabstand ein und
> wartet bei Drosselung deutlich länger. **Nie zwei Läufe parallel starten** —
> das löst die Sperre zuverlässig aus. Ein Lauf schafft rund 5 Handelstage pro
> Minute; drei Jahre dauern etwa 2,5 Stunden. Der Abruf ist unterbrechbar und
> setzt beim nächsten Start dort fort, wo er aufgehört hat.

**Technischer Befund zu NinjaTrader:** Die externe API (`NTDirect.dll` / ATI)
liefert nur gepolltes last/bid/ask sowie Order-Entry — **keine Bars, keine
Historie**. Bar-Daten erfordern ein NinjaScript-AddOn *innerhalb* von NinjaTrader.
Spezifikation: [`bridge_nt8/README.md`](bridge_nt8/README.md).

---

## Architektur

```
             Marktdaten (Databento / CSV / Replay / später NinjaTrader)
                                  │
                     tradex/data/  ── Provider-Abstraktion, Parquet-Store,
                                      Session-Kalender, Multi-TF-Aggregation
                                  │
                  tradex/analysis/ ── Swings, Struktur, FVG, Liquidität,
                                      Displacement, Sweeps, HTF-Bias
                                  │
                     MarketContext ── der EINZIGE Analysepfad
                                  │
                  tradex/strategy/ ── Pflichtkette, Stop, Ziel
                  tradex/risk/     ── Größe, Grenzen, Konsistenz
                                  │
                  tradex/backtest/ ── Ausführungssimulation und Statistik
                       │          └── tradex/live/ ── Papertrading-Betrieb:
                       │                              dieselbe Ausführung,
                       │                              Bars aus einem Feed
                     tradex/api/   ── FastAPI + DTOs (einziger UI-Vertrag)
                                  │
                            ui/    ── React + lightweight-charts
```

Die Oberfläche rechnet **nichts** selbst nach. Sie zeigt an, was die Engine
liefert (Spec §27). Dadurch lässt sich dieselbe Engine unverändert headless im
Backtest oder als Dienst betreiben.

### Die vier Invarianten

Sie machen Spec §29 ("Backtest ≡ Live") technisch erzwingbar statt nur vereinbart:

1. **Nur geschlossene Bars werden analysiert.** Die laufende Bar wird angezeigt,
   erreicht aber nie einen Detektor. Kein Look-ahead, kein Repainting.
2. **Detektoren sind reproduzierbar.** Ihr Zustand ist eine reine Funktion aus
   (gesehene Bars, Parameter). Kein I/O, keine Uhr, keine Zufallszahlen.
3. **Ein einziger Analysepfad.** `MarketContext.on_base_bar()` — identisch benutzt
   von Replay, Backtest und später Live. Es gibt keinen zweiten, "optimierten" Weg.
4. **Keine Magic Numbers.** Jeder Schwellenwert steht in `config/default.yaml`.
   Die Konfiguration ist `extra="forbid"` — ein Tippfehler bricht den Start ab,
   statt still ignoriert zu werden.

Die Tests prüfen diese Invarianten direkt: zwei Läufe über dieselben Daten liefern
byte-identische Snapshots, und `feed()` (Backtest) ergibt exakt denselben Zustand
wie `on_base_bar()` (Live).

---

## Was die Analyse erkennt

Alle Regeln sind quantitativ definiert (Spec §29) und stehen mit ihren
Schwellenwerten in `config/default.yaml`. **Die Werte sind Startannahmen, keine
belegten Größen** — sie zu prüfen ist Aufgabe des Backtests in Phase 4.

| Detektor | Definition |
|---|---|
| **Swing** | `high[i]` strikt größer als alle n links, ≥ allen n rechts. Bestätigung erst n Bars später — der Lag wird mitgeführt. |
| **BOS / MSS** | Close über/unter dem zuletzt bestätigten, noch ungebrochenen Swing. MSS = Bruch **gegen** den bestehenden Zustand. Close-basiert, nicht Docht-basiert. |
| **FVG** | `low[i] > high[i-2]` (bullish). Gültig nur, wenn Größe ≥ `min_size_ticks` **und** ≥ `min_atr_mult × ATR`. Lebenszyklus OPEN → TOUCHED → MITIGATED/EXPIRED. |
| **Displacement** | `range > 1.5×ATR` **und** `body/range > 0.6` **und** Ausbruch über das vorherige Extrem. Volumen ist bewusst **kein** Pflichtkriterium. |
| **Liquidität** | Swing-, Equal-, Session-, Vortages- und Vorwochen-Level. |
| **Sweep** | Durchstich **und** Rückeroberung innerhalb von `max_reclaim_bars`. Ohne Rückeroberung ist es ein Ausbruch, kein Sweep. |
| **HTF Bias** | Gewichtet aus Struktur, FVG-Balance und Liquiditäts-Zug über 4H/1H, mit Neutralband. |

## Strategien

Es laufen **mehrere Strategien parallel an einem Konto**. Sie schlagen vor; über
Positionsgröße, Tagesgrenzen und Handelsfenster entscheidet eine gemeinsame
Risiko-Pipeline — sonst wäre das tatsächliche Gesamtrisiko die Summe der
Einzelbudgets.

| Strategie | Regel | Frequenz (gemessen 2024) |
|---|---|---|
| **Pflichtkette (ICT)** | §7 als Zustandsmaschine, sechs Pflichtglieder | 0,03 Trades/Tag |
| **Eröffnungsspanne** | Ausbruch aus der Spanne der ersten 30 Session-Minuten | 0,44 Trades/Tag |

Die Kette ist strukturell niederfrequent: zwei ihrer Glieder filtern je rund
89 %, aus 18,9 Kandidaten pro Tag werden 0,25 vollständige Ketten. Für tägliche
Trades braucht es weitere Strategien **neben** ihr — nicht schnellere
Schwellenwerte **in** ihr.

Jede Strategie ist eine **Hypothese**. Ob sie einen Edge hat, sagt der Backtest,
einschließlich der Antwort „nein".

### Der News-Filter (Spec §14/§15)

```bash
python scripts/fetch_news.py --source holidays --from 2023-01-01 --to 2027-01-01
python scripts/fetch_news.py --source forexfactory     # wöchentlich
```

Um Wirtschaftstermine herum wird **nicht eingestiegen**. Ausstiege bleiben immer
möglich — eine offene Position ohne Stop wäre das Gegenteil von Risikosenkung.

Die Engine fragt dabei **nie** eine API: ein Skript holt die Termine, eine Datei
hält sie, die Engine liest nur diese Datei. Ein HTTP-Aufruf mitten in einer
Entscheidung wäre nicht wiederholbar — und Backtest und Live sähen Verschiedenes.

Drei Quellen, weil keine kostenlose beides kann. `holidays` rechnet
Börsenfeiertage ohne Netz aus, `forexfactory` liefert exakte Uhrzeiten ohne
Schlüssel (aber nur die laufende Woche), `fred` liefert Historie mit freiem
Schlüssel (aber nur den Tag). Ergänzte Uhrzeiten bekommen ein **breiteres**
Fenster, statt eine Genauigkeit vorzutäuschen, die die Quelle nicht hat.

Der gefährlichste Zustand wäre ein eingeschalteter Filter ohne Daten: er würde
alles durchwinken und dabei aussehen wie einer, der nichts zu beanstanden hat.
Deshalb kennt der Kalender seine eigene Abdeckung, es gibt dafür einen eigenen
Reason-Code, und der Backtest-Bericht warnt ausdrücklich, wenn ein Lauf
weitgehend ohne Filter gerechnet wurde.

**Nicht enthalten: Schlagzeilen.** „Trump sagt etwas" ist nicht vorhersehbar und
historisch kaum mit exaktem Zeitstempel zu bekommen. Eine Sperre, die live
greift und im Backtest fehlt, würde jede Backtest-Aussage entwerten (Spec §29).

### Mehrere Instrumente an einem Konto

```bash
python scripts/run_backtest.py --symbol MNQ_PROXY,MES_PROXY --from 2023-01-01
```

Dieselben Regeln auf mehreren Symbolen — der einzige Hebel, der die
Trade-Anzahl linear erhöht, **ohne eine Regel anzufassen**. Die Bars werden
streng chronologisch verschränkt und teilen sich **ein** Risikobuch; liefe
Symbol für Symbol durch, sähe das gemeinsame Buch beim zweiten bereits alle
Ergebnisse des ersten.

> **Nicht überschätzen:** S&P und Nasdaq sind stark korreliert. Zwei
> Instrumente liefern deshalb *nicht* die doppelte unabhängige Stichprobe —
> die Vertrauensbänder schrumpfen langsamer als die Trade-Anzahl wächst.

### Die Pflichtkette im Detail (Spec §7)

Fehlt ein Glied, entsteht kein Trade — der Bot ergänzt nichts:

```
HTF Bias → Liquidity Sweep → Displacement → FVG → Retracement → MSS → Entry
   4H/1H  └──────────── Setup-Ebene 5m ────────────┘  └─ 1m ─┘
```

| Baustein | Regel |
|---|---|
| **Stop** (§11) | Anker ist das **Rücklauf-Tief** — der Punkt, an dem die Einstiegsidee kippt. Puffer wächst mit dem ATR. Zu enge und zu weite Stops werden abgelehnt, nicht zurechtgebogen. |
| **Ziel** (§12) | Nächste unberührte Liquidität, die das Mindest-CRV schafft. Schafft es keine, entsteht kein Trade. |
| **Größe** (§10) | `abrunden(Risikobudget ÷ (Stopabstand × Punktwert))`. Ergibt das 0, gibt es keinen Trade — der Stop wird **nicht** enger gesetzt, damit es passt. |
| **Grenzen** (§10, §24) | Tagesverlust, Trades/Tag, offene Positionen — geführt je Globex-Handelstag. |
| **Handelsfenster** (§13) | Session und Volatilität sind **Filter, keine Auslöser**. Eine erreichte Uhrzeit löst nie einen Trade aus. |

### Warum der Stop am Rücklauf-Tief hängt

Der Einstieg erfolgt auf den MSS **nach** dem Rücklauf. Zwischen dem
ursprünglichen Sweep und diesem Einstieg liegt die ganze Impulsbewegung — ein
Stop am Sweep-Extrem wäre dadurch systematisch weit. Auf den Demodaten halbierte
der passende Anker den Stop-Median von 113 auf 52 Ticks.

**Welcher Anker über einen langen Zeitraum tatsächlich besser ist, muss der
Backtest in Phase 4 beantworten.** `retracement` ist gesetzt, weil es zum
Einstiegsmodell passt — nicht, weil es auf irgendwelchen Daten besser aussah.
`sweep`, `swing` und `fvg` bleiben als Alternativen konfigurierbar.

### Kontogröße und Stopweite hängen zusammen

Bei 10.000 $ und 0,25 % Risiko sind 25 $ pro Trade erlaubt. MNQ kostet 2 $ je
Punkt — der größte bezahlbare Stop ist damit **12,5 Punkte (50 Ticks)**. Für
5m-Setups ist das knapp. Eine Konsistenzprüfung meldet beim Start, wenn
`stops.max_stop_ticks`, Kontogröße und Instrument nicht zusammenpassen, statt
stillschweigend jedes Setup mit „Positionsgröße 0" zu verwerfen. Sie **ändert
keine Werte** — ob kleineres Risiko, größeres Konto oder engerer Stop, ist deine
Entscheidung.

## Der Backtest (Phase 4)

Die Frage, für die dieses Projekt gebaut wurde: **Hat die Pflichtkette einen
Edge — oder nicht?**

```bash
python scripts/run_backtest.py --symbol MNQ_PROXY --from 2023-01-01 --save
```

Der Backtest benutzt denselben Analysepfad wie Replay und später Live
(`MarketContext.on_base_bar`). Er fügt **ausschließlich die Ausführung** hinzu,
nie eine zweite Regelauslegung — ein Test stellt beide Wege gegenüber und
verlangt identische Entscheidungen. Das ist Spec §29 in ausführbarer Form.

### Die vier Annahmen, an denen ein Backtest lügen kann

Alle vier stehen in `config/default.yaml` unter `backtest:` und sind bewusst
pessimistisch voreingestellt. Ein Backtest, der günstiger füllt als die
Wirklichkeit, produziert genau die Zahlen, die man sehen will.

| Annahme | Voreinstellung | Warum |
|---|---|---|
| **Einstiegskurs** | Eröffnung der Folgebar + 1 Tick Schlupf | Das Signal entsteht am *Schluss* der Bestätigungsbar. Zu diesem Kurs kann man real nicht mehr kaufen. |
| **Stop und Ziel in derselben Bar** | `stop_first` | OHLC sagt nicht, was zuerst kam. Angenommen wird der schlechtere Fall. |
| **Kurssprung über den Stop** | Füllung am Eröffnungskurs | Eröffnet die Bar jenseits des Stops, gibt es den Stopkurs nicht mehr. |
| **Schlupf** | Stop 1 Tick, Ziel 0 | Der Stop ist eine Market-Order und rutscht. Das Ziel ist eine Limit-Order: sie füllt zum Kurs oder gar nicht. |
| **Signal über einer Datenlücke** | verworfen ab 2 Bars Abstand | „Die nächste Bar" ist nicht „die nächste Minute" — siehe unten. |

Die letzte Zeile stammt aus einem echten Fund. Am 19.06.2023 (Juneteenth)
endete der Handel um 11:58 CT und lief erst um 17:00 CT weiter. Eine Bar gilt
erst als geschlossen, wenn die nächste eintrifft — die Signalbar von 11:58
wurde also fünf Stunden später ausgewertet und der Einstieg um 17:01 gefüllt.
Ein Trade, den es nie gab, mit −1,50 R im Ergebnis. Normal liegen zwischen
Signal und Füllung genau zwei Bars; alles darüber ist eine Lücke.

Dazu Gebühren (0,74 $ je Kontrakt und Round Turn) und ein Zeitstop. Alle
Ergebnisse sind **netto** — auch das R-Vielfache. Eine Statistik, die Kosten
ausklammert, beschreibt einen Handel, den es nicht gibt.

### Was der Bericht sagt, bevor er Zahlen zeigt

Ein Erwartungswert aus zwölf Trades sieht genauso aus wie einer aus
zwölfhundert. Deshalb steht ganz oben, was man wissen muss, *bevor* man die
Zahlen liest: zu kleine Stichprobe, synthetische Daten, Index-CFD statt Future,
Trades die nur das Datenende beendet hat — und ob erste und zweite Hälfte des
Zeitraums überhaupt zueinander passen. Tun sie es nicht, hängt das Ergebnis am
Zeitraum und nicht an der Regel.

Gerechnet wird in **R** (Vielfaches des eingegangenen Risikos), nicht in Dollar:
ein Dollarergebnis hängt an Kontogröße, Stopweite und Stückzahl — drei Größen,
die mit der Regel nichts zu tun haben.

Jeder Lauf lässt sich mit `--save` festhalten. Gespeichert werden Kennzahlen,
Einzeltrades **und** der `config_hash` — ohne den wären zwei Ergebnisse nicht
vergleichbar, sondern nur zwei Zahlen (Spec §21).

### Musterstatistik statt Mustersuche

```bash
python scripts/run_backtest.py --symbol MNQ_PROXY --from 2023-01-01 --muster
```

Die Aufschlüsselungen des Berichts — nach Session, Richtung, Wochentag —
verleiten zu genau einem Fehler: „RTH bringt +0,4 R, also handeln wir nur noch
RTH." Zwölf Trades reichen für diese Zahl, und wer acht Aufschlüsselungen
anschaut, findet die beste davon fast garantiert im Zufall.

`--muster` beantwortet dieselbe Frage mit dem Verfahren, das sie beantworten
kann:

1. Jede Untergruppe wird gegen „Erwartungswert null" getestet (t-Test, mit
   Vertrauensband).
2. **Alle Tests zusammen** bekommen eine Mehrfachtest-Korrektur nach
   Benjamini-Hochberg. Bei zwanzig Untergruppen und 5 % Niveau ist *ein*
   Zufallstreffer die Erwartung, nicht die Ausnahme — die Korrektur rechnet
   diese Erwartung heraus.
3. Getestet wird nur der **vordere** Teil des Zeitraums. Der hintere bleibt
   unangetastet und dient als Gegenprobe: gleiches Vorzeichen oder nicht.

Bedingungen sind ausschließlich Merkmale, die **beim Einstieg feststanden**.
„Trades, die am Ziel schlossen, laufen besser" ist wahr und wertlos. Ein Test
erzwingt das strukturell: er reicht den Bedingungen einen Trade herein, der
seine Ergebnisfelder verweigert.

Gruppen unter 30 Trades werden angezeigt, aber nicht getestet — und zählen
deshalb auch nicht in die Korrektur hinein, sonst verwässerten Splittergruppen
jeden echten Fund. Alle drei Schwellen stehen unter `patterns:` in
`config/default.yaml`.

Was überlebt, ist eine **Hypothese für den nächsten Zeitraum**, kein Befund.

### Was hier bewusst nicht passiert

**Keine Parameteroptimierung.** Ein Suchlauf über Schwellenwerte findet
zuverlässig eine Kombination, die auf der Vergangenheit gut aussieht, und sagt
nichts über die Zukunft. Der Backtest beantwortet eine Ja/Nein-Frage zu *einer*
festgelegten Regelfassung.

---

Zwei Entscheidungen, die eine Begründung verdienen:

- **Volumen ist kein Gate.** Ob Volumen verfügbar ist, hängt an der Datenquelle.
  Wäre es Pflichtbedingung, würde die Strategie je nach Quelle unterschiedlich
  handeln — ein direkter Verstoß gegen "Backtest ≡ Live". Es wird als
  `volume_confirmed` protokolliert, damit Phase 4 messen kann, ob es etwas bringt.
- **Contract Rolls werden übersprungen.** Der Preissprung an der Kontraktnaht sieht
  aus wie eine riesige Imbalance mit starkem Impuls, ist aber ein Buchungsartefakt.

---

## Bedienung

Unter Windows genügt ein Doppelklick auf **`TradeX.bat`**. Die Datei prüft
virtuelle Umgebung, Installation und Oberflächen-Build, baut das UI beim ersten
Start selbst und öffnet dann das Fenster. Fehlt etwas, bleibt das Fenster offen
und sagt, was zu tun ist. Argumente werden durchgereicht
(`TradeX.bat --server`).

```bash
python -m tradex.shell            # Desktop-Fenster (Edge WebView2)
python -m tradex.shell --server   # nur die Engine, ohne Fenster
```

Die Wiedergabesteuerung geht Bar für Bar vorwärts und zeigt, **wann** welcher
Detektor anspringt. Das ist der eigentliche Zweck dieser Phase: die
Schwellenwerte gegen echte Kursverläufe prüfen, bevor eine Strategie darauf
aufsetzt. Sie benutzt denselben Engine-Aufruf wie später der Live-Feed.

Headless, mit Kennzahlen und einem eincheckbaren Regressions-Snapshot:

```bash
python scripts/run_analysis.py --symbol MNQ --out snapshot.json
```

```bash
python scripts/run_backtest.py --symbol MNQ_PROXY --out backtest.json --save
```

---

## Entwicklung

```bash
pytest -q
ruff check tradex tests
cd ui && npm run typecheck
```

```bash
cd ui && npm run dev
```

(Vite auf 5173 mit Proxy auf die Engine — für UI-Arbeit mit Hot Reload.
Parallel `python -m tradex.shell --server` laufen lassen.)

---

## Wie es weitergeht

| Phase | Inhalt | Stand |
|---|---|---|
| 1 | Architektur, Daten, Speicher, UI-Gerüst | **fertig** |
| 2 | Multi-Timeframe-Analyse, alle Detektoren | **fertig** |
| 3 | Strategy Engine, SL/TP, Risk Management | **fertig** |
| 4 | Backtesting und Statistik (Spec §19) | **fertig** |
| **4b** | **Day-Trading-Umbau: Registry, Multi-Instrument, Musterstatistik, News-Filter** | **fertig** |
| **5** | **Papertrading-Betrieb** | **läuft** — Wiedergabe-Feed fertig, Echtzeit offen |
| 6 | News- und KI-Kontextschicht | Termine fertig, Schlagzeilen offen |
| **7** | **Dashboard, Kill Switch, Monitoring** | **läuft** — Betriebspanel und Not-Aus im UI |
| 8 | Live Manual | offen |
| 9 | Live Auto | offen |

**Phase 4b stand nicht im ursprünglichen Plan.** Sie wurde nötig, weil Phase 4
erstmals *gemessen* hat, was die Pflichtkette leistet: 0,25 vollständige Setups
pro Handelstag, rund 10 Trades im Jahr. Das ist kein Day-Trading-System, und vor
Phase 4 konnte das niemand wissen — es gab keine Messung. Genau dafür war sie da.

### Papertrading läuft — echtes Geld nicht

```bash
python scripts/run_paper.py --symbol MNQ_PROXY --speed 3600
```

Das Programm trifft eigene Entscheidungen, eröffnet und schließt Positionen und
schreibt jeden Trade sofort in die Datenbank. Es fließt **kein Geld**: es gibt
keine Order-Anbindung an einen Broker, und `execution.live_trading_enabled`
bleibt aus (Spec §24).

Der Unterschied ist die ganze Begründung: Papertrading kostet nichts außer Zeit
und liefert eine zweite, unabhängige Messung derselben Regel. Echtes Geld kostet
etwas, wenn die Regel verliert — und der aktuelle Messstand (−0,028 R über 685
Trades, Band −0,14 bis +0,09) weist keinen Edge nach.

`tradex/live/` enthält **keine Regel und keine Füll-Logik.** Ein Papertrade
entsteht in derselben Klasse wie ein Backtest-Trade; der Unterschied ist nur,
woher die Bars kommen. Ein Test hält fest, dass beide Wege dieselben Trades
liefern — an echten Daten geprüft: 48 Trades, +0,300 R, identisch in Backtest
und Sitzung.

Live Auto darf erst freigeschaltet werden, wenn Paper Trading und Backtesting
nachweislich funktionieren.

**Wichtig:** Die FVG-/Liquidity-Regeln sind eine **Hypothese**, keine
nachgewiesene profitable Strategie. Das System ist von Anfang an so gebaut, dass
der Backtest in Phase 4 diese Frage beantworten kann — inklusive der Möglichkeit,
dass die Antwort "kein Edge" lautet.
