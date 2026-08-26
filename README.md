# TradeX

TradeX ist ein regelbasiertes, reproduzierbares Trading-System für
Nasdaq-100-Futures (MNQ/NQ). Es verbindet Backtesting, Multi-Timeframe-Analyse,
Risikomanagement, ein Desktop-Dashboard und Paper-Orderausführung über
NinjaTrader 8 in einer gemeinsamen Pipeline.

> **Aktueller Stand (August 2026):** Die NinjaTrader-2-Way-Bridge überträgt
> historische und geschlossene Bars, Ticks sowie Order-, Execution-, Positions-
> und Kontomeldungen. Der vollständige Orderweg wurde mit NinjaTrader 8.1.8.2
> und `Sim101` nachgewiesen. Die frühere IBKR-Anbindung wurde entfernt.

## Wichtig vorab

- **Kein Echtgeld:** `execution.live_trading_enabled` bleibt `false`.
- **Nur NinjaTrader-Simulation:** Das AddOn akzeptiert Orders ausschließlich,
  wenn `Account.Provider == Provider.Simulator` gilt. Dafür existiert kein
  Konfigurations- oder Umgehungsschalter.
- **Kein nachgewiesener Edge:** Die bisherigen Backtests belegen keine robuste
  Profitabilität. TradeX ist kein Versprechen zukünftiger Rendite.
- **Keine KI-Entscheidungen:** Strategie, Risiko und Orderfreigabe sind
  deterministisch, konfigurierbar, protokolliert und backtestbar.
- **Laufende Kerzen sind nur Anzeige:** Analysiert werden ausschließlich
  geschlossene Bars.

## Funktionsumfang

| Bereich | Stand |
|---|---|
| Multi-Timeframe-Analyse | Swings, Struktur, FVG, Liquidität, Sweeps, Displacement und HTF-Bias |
| Strategien | ICT-Pflichtkette und Opening Range über eine gemeinsame Registry |
| Risiko | Positionsgröße, Tagesverlust, Handelsfenster, Stop-/Zielprüfung und Kill Switch |
| Backtesting | Gleicher Analyse- und Entscheidungsweg wie im Betrieb, pessimistische Fills und Statistik |
| Marktdaten | Dukascopy-Proxy, Demo-Daten, Replay und echte NinjaTrader-Bars/Ticks |
| Dashboard | Desktop- und separate mobile Ansicht, Chart, Analyse, Betrieb und Status über FastAPI/React |
| NinjaTrader-Bridge | Historie, Bars, Ticks und bidirektionaler Paper-Order-Lifecycle |
| Paper-Ausführung | Entry, Stop und Ziel als echte NinjaTrader-Simulationsorders mit OCO und Aufräumweg |
| Echtgeldhandel | Strukturell gesperrt |

## Architektur

```text
NinjaTrader 8 AddOn
  ├─ Bars, Historie und Ticks ───────────────┐
  └─ Konto, Orders, Fills und Positionen ◄───┼──────────────┐
                                             │              │
                                      NinjaTraderFeed   NinjaTraderBroker
                                             │              │
                                             ▼              │
                                     MarketContext          │
                                             │              │
                                      Strategy Registry     │
                                             │              │
                                         RiskEngine         │
                                             │              │
                                       BrokerExecutor ──────┘
                                             │
                                      FastAPI + SQLite
                                             │
                                      React-Dashboard
```

Der zentrale Weg lautet:

```text
geschlossene Bar → MarketContext → Strategie → Risiko → Executor → Broker
```

Backtest, Replay und NinjaTrader-Betrieb verwenden denselben Analyse- und
Entscheidungsweg. Nur Datenquelle und Füllquelle werden ausgetauscht.

### Vier Invarianten

1. Nur geschlossene Bars gelangen in die Analyse.
2. Detektoren sind reine, reproduzierbare Zustandsübergänge ohne I/O, Uhr oder
   Zufall.
3. `MarketContext.on_base_bar()` ist der einzige Analysepfad.
4. Schwellenwerte stehen in `config/default.yaml`; unbekannte Werte brechen
   die Konfigurationsprüfung ab.

## Voraussetzungen

- Windows
- Python 3.12 oder neuer
- Node.js und npm für das Dashboard
- Für echte MNQ/NQ-Daten und Paperorders: NinjaTrader 8 mit installiertem
  TradeX-AddOn und einem aktiven Simulationskonto

## Installation

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
cd ui
npm.cmd install
npm.cmd run build
cd ..
```

Ohne NinjaTrader können Oberfläche, Analyse und Backtest mit synthetischen
Demodaten oder dem kostenlosen Dukascopy-Proxy genutzt werden:

```powershell
.\.venv\Scripts\python.exe scripts\generate_demo_data.py --days 45
.\.venv\Scripts\python.exe scripts\fetch_dukascopy.py --symbol MNQ_PROXY --from 2023-01-01
```

`MNQ_DEMO` enthält **keine Marktdaten**. `MNQ_PROXY` ist ein Nasdaq-100-
Index-CFD und **kein echter MNQ-Future**. Beide Quellen eignen sich zur
technischen Prüfung, aber nicht als Beleg für eine profitable Futures-Strategie.

## Start

Der kanonische Windows-Startweg ist:

```powershell
.\start_tradex.bat
```

Die Startdatei prüft Umgebung, UI-Build, Daten, NinjaTrader-Bridge, Broker und
Sicherheitskette. Eine gehaltene Dateisperre verhindert zwei gleichzeitige
TradeX-Sitzungen mit getrennten Risikobüchern.

```powershell
.\start_tradex.bat --check    # nur Vorprüfung
.\start_tradex.bat --server   # Engine ohne Desktop-Fenster
```

`TradeX.bat` bleibt für vorhandene Verknüpfungen bestehen und leitet auf
`start_tradex.bat` weiter.

## NinjaTrader-2-Way-Bridge

Das AddOn [`bridge_nt8/TradeXBridge.cs`](bridge_nt8/TradeXBridge.cs) läuft
innerhalb von NinjaTrader und lauscht ausschließlich auf `127.0.0.1:39473`.
Das zeilenbasierte JSON-Protokoll benötigt keine externen C#-Bibliotheken.

```text
NinjaTrader → TradeX: bar, tick, status, heartbeat, account,
                     order_update, execution, position, order_rejected
TradeX → NinjaTrader: subscribe, unsubscribe, history, account_query,
                     order_submit, order_cancel, flatten
```

Wichtige Betriebsbedingungen:

- Das Wurzelsymbol wird über `nt8_symbol` in
  `config/instruments.yaml` auf den aktuellen NinjaTrader-Kontrakt abgebildet.
- Beim Contract Roll müssen `MNQ` und `NQ` dort aktualisiert werden.
- Der Simulationsmotor benötigt ein aktives Marktdaten-Abonnement.
- Ticks werden für die Anzeige zusammengefasst; Bars und Orderereignisse nie.
- Stop und Ziel werden als echte OCO-Klammerorders in NinjaTrader angelegt.
- Ein `flatten` storniert zuerst offene Orders und stellt danach die Position
  glatt.

Die vollständige Installation, das Protokoll, die Kontosperre und der
nachgewiesene End-to-End-Ablauf stehen in
[`bridge_nt8/README.md`](bridge_nt8/README.md).

### Sicherer Verbindungstest

```powershell
.\.venv\Scripts\python.exe scripts\nt8_paper_order.py
```

Ohne Freigabeschalter verbindet sich das Skript nur, prüft Bridge und Konto und
sendet **keine Order**.

Das tatsächliche Paper-Testskript verlangt bewusst zwei Bestätigungen:

```powershell
.\.venv\Scripts\python.exe scripts\nt8_paper_order.py --yes-send-paper-order
```

Danach muss zusätzlich `JA` eingegeben werden. Der `finally`-Pfad storniert
offene Orders und stellt die Testposition glatt. Das Skript ist ausschließlich
für ein laufendes NinjaTrader-Simulationskonto gedacht.

## Sicherheitsmodell

Eine Paperorder passiert alle folgenden Stufen fail-closed:

```text
Paper-Modus
  → live_trading_enabled == false
  → broker.enabled == true
  → lokale Sperrvariablen erlauben Orders
  → Bridge verbunden
  → Account.Provider == Provider.Simulator
  → Konto entspricht der Auswahl
  → RiskEngine erlaubt den Trade
  → Marktdaten sind frisch
  → Rate-Limit ist frei
  → order_key ist unbekannt
```

Die Simulationseigenschaft wird sowohl im Python-Guard als auch im
NinjaTrader-AddOn geprüft. `broker.nt8.allowed_accounts` kann Konten zusätzlich
ausschließen, aber kein Nicht-Simulationskonto freischalten.

`execution.mode: paper_auto` und `broker.enabled: true` bedeuten daher nicht
„Live-Handel“. Live-Modi werden mit `BROKER_LIVE_BLOCKED` abgewiesen.

## Analyse und Strategien

TradeX erkennt quantitativ definierte Marktmerkmale:

- Swing Highs/Lows und BOS/MSS
- Fair Value Gaps mit Größen- und ATR-Filter
- Displacement über Range, Body-Anteil und Ausbruch
- Swing-, Equal-, Session-, Vortages- und Vorwochen-Liquidität
- Liquidity Sweeps mit zeitlich begrenzter Rückeroberung
- gewichteten 4H-/1H-Bias

Die ICT-Pflichtkette ist als Zustandsmaschine implementiert:

```text
HTF Bias → Liquidity Sweep → Displacement → FVG → Retracement → MSS → Entry
   4H/1H  └──────────── Setup-Ebene 5m ────────────┘  └─ 1m ─┘
```

Fehlt ein Glied oder passt seine Richtung bzw. Reihenfolge nicht, entsteht kein
Trade. Strategien schlagen Setups vor; das gemeinsame Risikobuch entscheidet
über Positionsgröße und Freigabe.

## Backtesting

```powershell
.\.venv\Scripts\python.exe scripts\run_backtest.py --symbol MNQ_PROXY --save
.\.venv\Scripts\python.exe scripts\run_backtest.py --symbol MNQ_PROXY,MES_PROXY --save
.\.venv\Scripts\python.exe scripts\run_backtest.py --symbol MNQ_PROXY --muster
```

Der Backtest verwendet denselben Analyse- und Strategiepfad wie Replay und
NinjaTrader-Betrieb. Die Ausführung ist bewusst pessimistisch modelliert:
Folgebar-Einstieg, Schlupf, Gebühren, `stop_first` bei mehrdeutigen OHLC-Bars,
Zeitstop und Verwerfen verspäteter Signale über Datenlücken.

Berichte enthalten unter anderem:

- Netto-Ergebnisse in R
- Stichproben- und Vertrauenshinweise
- In-/Out-of-Sample-Vergleich
- Einzeltrades und `config_hash`
- Musterstatistik mit Mehrfachtest-Korrektur

Die vorhandenen Messungen weisen keinen belastbaren Edge nach. Parameter werden
nicht automatisch optimiert und Strategie-Defaults nicht aufgrund kurzer oder
synthetischer Daten angepasst.

## Marktdaten

| Symbol | Quelle | Verwendung | Einschränkung |
|---|---|---|---|
| `MNQ`, `NQ` | NinjaTrader | echte Futures-Bars, Ticks und Paperorders | NinjaTrader und aktueller `nt8_symbol` erforderlich |
| `MNQ_PROXY`, `MES_PROXY` | Dukascopy | kostenlose Historie und Backtests | Index-CFD statt Future; Volumen ist keine Kontraktzahl |
| `MNQ_DEMO` | Generator | UI- und Funktionstest | synthetisch, keinerlei Marktaussage |

Der Dukascopy-Importer ist fortsetzbar und behandelt Drosselung mit
Retry/Backoff. Wegen HTTP-503-Drosselung dürfen keine parallelen Importläufe
gestartet werden.

## Entwicklung

```powershell
.\.venv\Scripts\python.exe -m ruff check tradex tests scripts
.\.venv\Scripts\python.exe -m pytest tests\ -q
cd ui
npm.cmd run typecheck
npm.cmd test
```

UI-Entwicklung mit Hot Reload:

```powershell
# Terminal 1
.\.venv\Scripts\python.exe -m tradex.shell --server

# Terminal 2
cd ui
npm.cmd run dev
```

## Projektstruktur

```text
tradex/domain/       Domänenobjekte und Instrumente, kein I/O
tradex/data/         Provider, Parquet, Sessions, Aggregation und Integrität
tradex/analysis/     MarketContext und Detektoren
tradex/strategy/     Strategien, Portfolio und Registry
tradex/risk/         Sizing, Grenzen, Konsistenz und gemeinsames Risikobuch
tradex/backtest/     Executor, Runner, Kennzahlen, Statistik und Berichte
tradex/live/         Replay-/NT8-Feeds, Sitzungen, Beobachtung und Kill Switch
tradex/broker/       Brokerunabhängige Pipeline und NinjaTrader-Adapter
tradex/api/          FastAPI und DTOs als einziger UI-Vertrag
bridge_nt8/          NinjaScript-AddOn und Bridge-Spezifikation
ui/                  React, TypeScript und lightweight-charts
scripts/             Datenabruf, Backtests, Preflight und Handprüfungen
tests/               deterministische Unit-, Vertrags- und Integrationstests
```

## Grenzen und offene Punkte

- Echtgeldhandel bleibt gesperrt.
- Sicherer externer/mobile Fernzugriff mit Authentifizierung und HTTPS ist noch
  nicht festgelegt.
- `Sim101` berücksichtigt ohne NinjaTrader-Commission-Template keine Gebühren;
  Paperfills können deshalb optimistischer als der Backtest sein.
- Gebühren, die erst nach einer Füllmeldung eintreffen, verwenden im
  `BrokerExecutor` den dokumentierten Schätzpfad.
- Der aktuell konfigurierte Futures-Kontrakt muss bei jedem Roll manuell geprüft
  und aktualisiert werden.
- Strategie und Ausführung funktionieren technisch; eine profitable Strategie
  ist damit nicht bewiesen.

## Haftungsausschluss

Dieses Repository dient Entwicklung, Forschung, Backtesting und Papertrading.
Es ist keine Anlageberatung und keine Aufforderung zum Handel. Futures sind
risikoreich. Nutze den Code nicht mit Echtgeld, solange die Live-Sperren nicht
in einem getrennten, ausdrücklich geprüften Projektabschnitt neu bewertet und
freigegeben wurden.
