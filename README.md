# TradeX

Regelbasiertes, backtestbares Analyse- und (später) Handelssystem für
Nasdaq-100-Futures (MNQ/NQ).

**Aktueller Stand: Phase 1–3 — Analyse, Strategie und Risikosteuerung.**
Setups werden erkannt, Einstieg, Stop, Ziel und Positionsgröße werden
durchgerechnet und protokolliert. **Es werden keine Orders ausgeführt** — das
kommt ab Phase 5. Einen Backtest gibt es noch nicht (Phase 4).

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

Echte MNQ-Historie (zeigt vor jedem Abruf den Preis, siehe unten):

```bash
python scripts/fetch_databento.py --symbol MNQ --from 2023-01-01 --dry-run
```

---

## Marktdaten und laufende Kosten

Recherchestand August 2026. Zielvorgabe war ≈ 5 USD/Monat (Spec §3, §26).

| Zweck | Quelle | Kosten |
|---|---|---|
| **Historie / Backtest** | Databento `GLBX.MDP3`, nach Verbrauch | **≈ 0 USD** — 125 USD Startguthaben decken mehrere Jahre MNQ 1m |
| **Live / Paper** (ab Phase 5) | NinjaTrader + CME Level 1 non-professional | **≈ 4 USD/Monat** |
| Level 2 / Markttiefe | NinjaTrader CME Level 2 | ~16 USD/Monat — **außerhalb Budget, bewusst nicht genutzt** |
| Entwicklung ohne Kosten | NinjaTrader Free License: Sim, Market Replay | 0 USD |

**Laufende Gesamtkosten im Zielzustand: ≈ 4 USD/Monat.**

Databentos *Live*-Abo (~199 USD/Monat) wird nicht verwendet — nur die Historie.
Weil Markttiefe nicht im Budget liegt, darf die Kernstrategie nie davon abhängen
(Spec §4). `ProviderCapabilities` macht für jede Quelle explizit, was sie liefert.

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

## Die Strategie (Phase 3)

Die Pflichtkette aus §7 als Zustandsmaschine. Fehlt ein Glied, entsteht kein
Trade — der Bot ergänzt nichts:

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

```bash
python -m tradex.shell            # Desktop-Fenster (Edge WebView2)
python -m tradex.shell --server   # nur die Engine, ohne Fenster
```

Die Wiedergabesteuerung geht Bar für Bar vorwärts und zeigt, **wann** welcher
Detektor anspringt. Das ist der eigentliche Zweck dieser Phase: die
Schwellenwerte gegen echte Kursverläufe prüfen, bevor eine Strategie darauf
aufsetzt. Sie benutzt denselben Engine-Aufruf wie später der Live-Feed.

Headless, mit Kennzahlen und einem einchreckbaren Regressions-Snapshot:

```bash
python scripts/run_analysis.py --symbol MNQ --out snapshot.json
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
| 4 | Backtesting und Statistik (Spec §19) | offen |
| 5 | NinjaTrader Paper Trading | offen |
| 6 | News- und KI-Kontextschicht | offen |
| 7 | Dashboard, Kill Switch, Monitoring | offen |
| 8 | Live Manual | offen |
| 9 | Live Auto | offen |

Live Auto darf erst freigeschaltet werden, wenn Paper Trading und Backtesting
nachweislich funktionieren.

**Wichtig:** Die FVG-/Liquidity-Regeln sind eine **Hypothese**, keine
nachgewiesene profitable Strategie. Das System ist von Anfang an so gebaut, dass
der Backtest in Phase 4 diese Frage beantworten kann — inklusive der Möglichkeit,
dass die Antwort "kein Edge" lautet.
