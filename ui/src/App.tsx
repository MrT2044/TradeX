import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { ApiError, api } from './api/client';
import type {
  BacktestReport,
  BarsResponse,
  ContextSnapshot,
  Coverage,
  Health,
  Instrument,
  Integrity,
  LogEntry,
  Overlays,
  SessionStatus,
  SimulatedTrade,
  StrategyState,
} from './api/types';
import { useIsMobile } from './api/useIsMobile';
import { useSessionStream } from './api/useSessionStream';
import { TradeChart, type ChartToggles } from './chart/TradeChart';
import { MobileDashboard } from './panels/MobileDashboard';
import { de } from './i18n/de';
import { AnalysisPanel } from './panels/AnalysisPanel';
import { BacktestPanel } from './panels/BacktestPanel';
import { ErrorBanner } from './panels/ErrorBanner';
import { ReplayControls } from './panels/ReplayControls';
import { SessionPanel } from './panels/SessionPanel';
import { StatusBar } from './panels/StatusBar';
import { StrategyPanel } from './panels/StrategyPanel';
import { SystemPanel } from './panels/SystemPanel';

const TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h'];
const ENTRY_TIMEFRAME = '1m';
const PLAY_INTERVAL_MS = 400;

/** Wie viele Basis-Bars beim Symbolwechsel geladen UND analysiert werden.
 *
 *  Der Chart soll ein Chart sein: hinschauen, schieben, zoomen. Dazu muessen
 *  Bars und Muster denselben Bereich abdecken - ein Chart, der weiter reicht
 *  als die Analyse, zeigt Kerzen ohne die Kursluecken und Sweeps, die dort
 *  tatsaechlich liegen, und das ist schlimmer als ein kuerzerer Chart.
 *
 *  Deshalb eine feste, ueberschaubare Menge statt des ganzen Bestands: 30.000
 *  Minutenbars sind rund drei Wochen und kosten gemessene ~15 Sekunden. Die
 *  vollen 200.000 waeren ~105 Sekunden gewesen - bei jedem Symbolwechsel. Fuer
 *  laengere Zeitraeume ist der Backtest zustaendig, nicht der Chart. */
const HISTORY_BARS = 30_000;

/** Portionsgroesse des Warmlaufs. Klein genug, dass die Anzeige sichtbar
 *  vorankommt, gross genug, dass der Verwaltungsaufwand nicht ins Gewicht
 *  faellt. */
const WARMUP_CHUNK = 5_000;

/** Wie oft der Chart im Echtzeitbetrieb nachgefuehrt wird.
 *
 *  Die Basis ist die Minutenbar - haeufiger nachzufragen brachte nichts ausser
 *  Last. Fuenf Sekunden sind fein genug, dass die laufende Bar sichtbar
 *  waechst, und grob genug, dass die Abfrage nicht ins Gewicht faellt. */
const LIVE_REFRESH_MS = 5_000;

const DEFAULT_TOGGLES: ChartToggles = {
  fvg: true,
  liquidity: true,
  swings: false,
  structure: true,
  sweeps: true,
};

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [coverage, setCoverage] = useState<Coverage[]>([]);
  const [symbol, setSymbol] = useState<string>('');
  const [timeframe, setTimeframe] = useState<string>('5m');

  const [bars, setBars] = useState<BarsResponse | null>(null);
  const [overlays, setOverlays] = useState<Overlays | null>(null);
  const [snapshot, setSnapshot] = useState<ContextSnapshot | null>(null);
  const [strategy, setStrategy] = useState<StrategyState | null>(null);
  const [integrity, setIntegrity] = useState<Integrity | null>(null);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [backtest, setBacktest] = useState<BacktestReport | null>(null);
  const [backtesting, setBacktesting] = useState(false);

  const [cursor, setCursor] = useState(0);
  const [total, setTotal] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [stepSize, setStepSize] = useState(15);

  const [toggles, setToggles] = useState<ChartToggles>(DEFAULT_TOGGLES);
  const [busy, setBusy] = useState(false);
  /** Fortschritt des Warmlaufs - null, wenn gerade keiner laeuft. */
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [session, setSession] = useState<SessionStatus | null>(null);
  const [sessionTrades, setSessionTrades] = useState<SimulatedTrade[]>([]);
  const [sessionBusy, setSessionBusy] = useState(false);

  /** Alle konfigurierten Instrumente - nicht nur die mit gespeicherten Daten.
   *
   *  Vorher stand hier die Abdeckung des Speichers, weshalb MNQ und NQ gar
   *  nicht zur Auswahl standen: fuer die echten Futures liegt nichts auf der
   *  Platte, ihre Bars kommen live von NinjaTrader. Eine Liste, die genau die
   *  Instrumente verschweigt, die man im Betrieb handelt, ist die falsche
   *  Liste. Welche Historie haben, sagt die Auswahl selbst an (siehe
   *  `withData`). */
  const symbols = useMemo(
    () => instruments.map((item) => item.symbol).sort(),
    [instruments],
  );
  const symbolsWithData = useMemo(
    () => new Set(coverage.map((item) => item.symbol)),
    [coverage],
  );
  const instrument = useMemo(
    () => instruments.find((item) => item.symbol === symbol) ?? null,
    [instruments, symbol],
  );

  // --- Stammdaten einmalig laden ------------------------------------------
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [healthData, instrumentData, coverageData] = await Promise.all([
          api.health(),
          api.instruments(),
          api.coverage(),
        ]);
        if (cancelled) return;
        setHealth(healthData);
        setInstruments(instrumentData);
        setCoverage(coverageData);

        // KEINE Vorauswahl. Vorher lud der Start von selbst ein Instrument -
        // fuenfzehn Sekunden Rechenzeit fuer etwas, das man vielleicht gar
        // nicht sehen wollte, und beim Hinsehen weiss man nicht sofort, was
        // da eigentlich steht. Gewaehlt wird bewusst.
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // --- Chartdaten und Analyse fuer den aktuellen Stand holen ---------------
  const refreshView = useCallback(
    async (targetSymbol: string, targetTimeframe: string) => {
      const [barsData, overlayData, snapshotData, strategyData, logData] = await Promise.all([
        api.bars(targetSymbol, targetTimeframe),
        api.overlays(targetSymbol, targetTimeframe),
        api.analysis(targetSymbol),
        api.strategy(targetSymbol),
        api.logs(120),
      ]);
      setBars(barsData);
      setOverlays(overlayData);
      setSnapshot(snapshotData);
      setStrategy(strategyData);
      setLogs(logData);
    },
    [],
  );

  // --- Symbolwechsel: laden, aber noch nicht analysieren -------------------
  useEffect(() => {
    if (!symbol) return;
    let cancelled = false;

    (async () => {
      setBusy(true);
      setPlaying(false);
      setError(null);
      try {
        // ALLES vom vorigen Instrument verwerfen, bevor das neue laedt. Ein
        // stehengebliebenes Chart unter einem bereits umgeschalteten
        // Symbolnamen ist die gefaehrlichste Anzeige, die dieses Programm
        // haben kann: sie ist nicht falsch beschriftet, sie zeigt schlicht
        // etwas anderes als das, was oben steht. Lieber leer als verwechselbar.
        setBacktest(null);
        setBars(null);
        setOverlays(null);
        setSnapshot(null);
        setStrategy(null);
        setIntegrity(null);
        setCursor(0);
        setTotal(0);

        // MNQ und NQ haben keine gespeicherte Historie - ihre Bars kommen
        // live von NinjaTrader. Fuer sie wird gar nicht erst geladen: der
        // Versuch endete in "Keine 1m-Daten im lokalen Speicher", und das ist
        // eine Fehlermeldung fuer einen Zustand, der voellig in Ordnung ist.
        // Eine Meldung, die bei normalem Verhalten erscheint, bringt einem bei,
        // Meldungen zu ignorieren - und dann wird auch die echte uebersehen.
        if (!symbolsWithDataRef.current.has(symbol)) {
          if (!cancelled) await refreshView(symbol, timeframe).catch(() => undefined);
          return;
        }

        // feedAll=false und danach portionsweise selbst durchlaufen: nur so
        // laesst sich der Fortschritt anzeigen. `feedAll: true` rechnet im
        // Server durch und meldet sich erst, wenn es fertig ist - fuenfzehn
        // Sekunden ohne ein Lebenszeichen sehen aus wie ein Absturz.
        const loaded = await api.load(symbol, { maxBars: HISTORY_BARS, feedAll: false });
        if (cancelled) return;
        setTotal(loaded.base_bars);
        setCursor(loaded.cursor);
        setIntegrity(loaded.integrity);

        // Den GANZEN geladenen Bereich analysieren, damit Chart und Muster
        // denselben Zeitraum abdecken.
        let done = loaded.cursor;
        while (done < loaded.base_bars) {
          const stepped = await api.step(symbol, Math.min(WARMUP_CHUNK, loaded.base_bars - done));
          if (cancelled) return;
          done = stepped.cursor;
          setCursor(done);
          setProgress({ done, total: loaded.base_bars });
          if (stepped.exhausted) break;
        }
        setProgress(null);
        await refreshView(symbol, timeframe);
      } catch (err) {
        if (cancelled) return;
        // Fuer die echten Futures (MNQ, NQ) liegt keine Historie auf der
        // Platte - deren Bars kommen live von NinjaTrader. Laeuft dafuer
        // gerade eine Sitzung, ist das kein Fehler, sondern der Normalfall:
        // der Chart faengt leer an und fuellt sich Bar fuer Bar.
        if (sessionRef.current?.active && sessionRef.current.symbols.includes(symbol)) {
          await refreshView(symbol, timeframe).catch(() => undefined);
        } else {
          setError(err instanceof Error ? err.message : String(err));
        }
      } finally {
        if (!cancelled) {
          setBusy(false);
          setProgress(null);
        }
      }
    })();

    return () => {
      cancelled = true;
    };
    // timeframe bewusst NICHT in den Abhaengigkeiten: ein Wechsel der Zeitebene
    // darf die laufende Analyse nicht neu starten.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol, refreshView]);

  // --- Zeitebene wechseln: nur die Ansicht neu holen -----------------------
  useEffect(() => {
    if (!symbol || busy) return;
    let cancelled = false;
    (async () => {
      try {
        await refreshView(symbol, timeframe);
      } catch (err) {
        if (!cancelled && !(err instanceof ApiError && err.status === 404)) {
          setError(err instanceof Error ? err.message : String(err));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [timeframe]);

  // --- Laufender Betrieb (Phase 7) -----------------------------------------
  // Kommt ueber einen Ereignisstrom herein, unabhaengig von allem anderen: der
  // Betrieb laeuft weiter, auch wenn niemand im Chart blaettert, und er muss
  // sichtbar bleiben, waehrend die uebrige Oberflaeche auf einen Backtest
  // wartet - deshalb ohne `busy`-Sperre.
  const live = useSessionStream();

  useEffect(() => {
    if (live.status) setSession(live.status);
  }, [live.status]);

  // Trades werden nicht mitgestroemt: sie aendern sich selten, sind aber je
  // Eintrag umfangreich. Sie werden nachgeholt, wenn ihre Zahl steigt.
  const tradesSeen = useRef(-1);
  useEffect(() => {
    const closed = live.status?.trades_closed ?? 0;
    if (closed === tradesSeen.current) return;
    tradesSeen.current = closed;
    if (!live.status?.active || closed === 0) {
      setSessionTrades([]);
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const list = await api.sessionTrades(20);
        if (!cancelled) setSessionTrades(list);
      } catch {
        /* Der Betrieb ist nicht die Hauptansicht - ein Aussetzer darf hier
           keine Fehlermeldung ueber den ganzen Bildschirm werfen. */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [live.status]);

  const sessionAction = useCallback(
    async (action: () => Promise<SessionStatus>) => {
      setSessionBusy(true);
      try {
        setSession(await action());
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setSessionBusy(false);
      }
    },
    [],
  );

  // --- Wiedergabe ----------------------------------------------------------
  const advance = useCallback(
    async (count: number) => {
      if (!symbol) return;
      setBusy(true);
      try {
        const stepped = await api.step(symbol, count);
        setCursor(stepped.cursor);
        if (stepped.exhausted) setPlaying(false);
        await refreshView(symbol, timeframe);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
        setPlaying(false);
      } finally {
        setBusy(false);
      }
    },
    [symbol, timeframe, refreshView],
  );

  const playingRef = useRef(playing);
  playingRef.current = playing;

  // Der Ladevorgang muss den Betriebszustand lesen koennen, ohne ihn in seine
  // Abhaengigkeiten zu nehmen - sonst startet er bei jeder Zustandsmeldung neu.
  const sessionRef = useRef(session);
  sessionRef.current = session;

  const symbolsWithDataRef = useRef(symbolsWithData);
  symbolsWithDataRef.current = symbolsWithData;

  useEffect(() => {
    if (!playing) return;
    let cancelled = false;
    const timer = window.setInterval(() => {
      if (cancelled || !playingRef.current) return;
      void advance(stepSize);
    }, PLAY_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [playing, stepSize, advance]);

  // --- Echtzeit: der Chart folgt der laufenden Sitzung ---------------------
  // Laeuft eine Sitzung fuer dieses Symbol, liefert `/api/bars` deren Bars -
  // die Auswahl trifft der Server (`TradexService.chart_context`). Hier muss
  // nur regelmaessig nachgefragt werden. Der Zustandsstrom (SSE) meldet
  // Zaehler, keine Kursreihen; die waeren fuer einen Dauerstrom zu gross und
  // wuerden bei jeder Bar das ganze Chart neu schicken.
  const liveActive = Boolean(
    session?.active && session.running && symbol && session.symbols.includes(symbol),
  );

  useEffect(() => {
    if (!liveActive || !symbol) return;
    let cancelled = false;
    const holen = async () => {
      try {
        const [barsData, overlayData] = await Promise.all([
          api.bars(symbol, timeframe),
          api.overlays(symbol, timeframe),
        ]);
        if (cancelled) return;
        setBars(barsData);
        setOverlays(overlayData);
      } catch {
        /* Ein Aussetzer ist kein Grund, das Nachfuehren aufzugeben - die
           naechste Runde kann wieder klappen. Der Verbindungszustand steht
           ohnehin schon in der Betriebsanzeige. */
      }
    };
    void holen();
    const timer = window.setInterval(() => void holen(), LIVE_REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [liveActive, symbol, timeframe]);

  const handleReset = useCallback(async () => {
    if (!symbol) return;
    setPlaying(false);
    setBusy(true);
    try {
      const reset = await api.reset(symbol);
      setCursor(reset.cursor);
      await refreshView(symbol, timeframe);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [symbol, timeframe, refreshView]);

  // --- Backtest -------------------------------------------------------------
  const runBacktest = useCallback(async () => {
    if (!symbol) return;
    // Der Lauf rechnet synchron durch und blockiert die Antwort. Die
    // Wiedergabe wird angehalten, damit nicht parallel Schritte hineinlaufen.
    setPlaying(false);
    setBacktesting(true);
    setError(null);
    try {
      setBacktest(await api.backtest(symbol));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBacktesting(false);
    }
  }, [symbol]);

  // --- Handy: eigene Ansicht, nicht dasselbe Layout schmalgerechnet -------
  // Steht VOR allen anderen Rueckgaben: die Ueberwachung soll auch dann
  // funktionieren, wenn kein Datenbestand vorliegt - der laufende Betrieb ist
  // davon unabhaengig.
  const isMobile = useIsMobile();
  if (isMobile) {
    return (
      <div className="app app--mobile">
        <MobileDashboard
          status={session}
          trades={sessionTrades}
          mode={live.mode}
          ageSeconds={live.ageSeconds}
        />
      </div>
    );
  }

  // --- Keine Daten vorhanden ----------------------------------------------
  if (!error && symbols.length === 0 && health) {
    return (
      <div className="app app--empty">
        <StatusBar
          health={health}
          instrument={null}
          snapshot={null}
          coverage={[]}
          symbols={[]}
          withData={new Set()}
          selected=""
          onSelect={() => undefined}
          busy={false}
        />
        <div className="empty">
          <h2>{de.data.noData}</h2>
          <p>{de.data.noDataHint}</p>
          <pre>{de.data.noDataCommand}</pre>
          <p>{de.data.realDataHint}</p>
          <pre>{de.data.realDataCommand}</pre>
        </div>
      </div>
    );
  }

  return (
    <div className="app">
      <StatusBar
        health={health}
        instrument={instrument}
        snapshot={snapshot}
        coverage={coverage}
        symbols={symbols}
        withData={symbolsWithData}
        selected={symbol}
        onSelect={setSymbol}
        busy={busy}
      />

      {error && <ErrorBanner message={error} onDismiss={() => setError(null)} />}

      <main className="layout">
        <div className="layout__main">
          <div className="chart-toolbar">
            <div className="chart-toolbar__group">
              {TIMEFRAMES.map((tf) => (
                <button
                  key={tf}
                  type="button"
                  className={tf === timeframe ? 'chip chip--active' : 'chip'}
                  onClick={() => setTimeframe(tf)}
                  disabled={busy}
                >
                  {tf}
                </button>
              ))}
            </div>

            <div className="chart-toolbar__group">
              {(
                [
                  ['fvg', de.chart.showFvg],
                  ['liquidity', de.chart.showLiquidity],
                  ['structure', de.chart.showStructure],
                  ['sweeps', de.chart.showSweeps],
                  ['swings', de.chart.showSwings],
                ] as [keyof ChartToggles, string][]
              ).map(([key, label]) => (
                <label key={key} className="toggle">
                  <input
                    type="checkbox"
                    checked={toggles[key]}
                    onChange={(event) =>
                      setToggles((prev) => ({ ...prev, [key]: event.target.checked }))
                    }
                  />
                  <span>{label}</span>
                </label>
              ))}
            </div>
          </div>

          <div className="chart-wrap">
            <TradeChart
              bars={bars}
              overlays={overlays}
              toggles={toggles}
              priceDecimals={instrument?.price_decimals ?? 2}
            />
            {liveActive && (
              <div className="chart-live">
                {de.chart.live}
                {/* Der laufende Kurs aus den Ticks. Er steht neben der Marke
                    und nicht im Chart: gezeichnet wird auf geschlossenen
                    Bars, und eine Linie, die zwischen den Kerzen zappelt,
                    liesse offen, was davon ausgewertet wurde. */}
                {typeof session?.last_prices?.[symbol] === 'number' && (
                  <span className="chart-live__price">
                    {session.last_prices[symbol].toFixed(instrument?.price_decimals ?? 2)}
                  </span>
                )}
              </div>
            )}
            {!symbol && (
              <div className="chart-loading">
                <div className="chart-loading__text">{de.chart.chooseSymbol}</div>
              </div>
            )}
            {/* Kein Fehler, sondern eine Ansage: fuer dieses Instrument gibt es
                nichts zu laden, die Bars entstehen erst im Betrieb. */}
            {symbol && !symbolsWithData.has(symbol) && !liveActive && !busy && (
              <div className="chart-loading">
                <div className="chart-loading__text">{de.chart.liveOnlyHint}</div>
              </div>
            )}
            {progress && (
              <div className="chart-loading">
                <div className="chart-loading__text">
                  {de.chart.analysing(progress.done, progress.total)}
                </div>
                <div className="chart-loading__bar">
                  <div
                    className="chart-loading__fill"
                    style={{ width: `${(progress.done / progress.total) * 100}%` }}
                  />
                </div>
              </div>
            )}
          </div>

          <ReplayControls
            cursor={cursor}
            total={total}
            playing={playing}
            busy={busy}
            stepSize={stepSize}
            onStepSize={setStepSize}
            onStep={() => void advance(stepSize)}
            onPlayPause={() => setPlaying((value) => !value)}
            onReset={() => void handleReset()}
            onToEnd={() => void advance(total - cursor)}
          />
        </div>

        <aside className="layout__side">
          <AnalysisPanel snapshot={snapshot} entryTimeframe={ENTRY_TIMEFRAME} />
          <StrategyPanel strategy={strategy} />
          <BacktestPanel
            report={backtest}
            busy={backtesting || busy}
            onRun={() => void runBacktest()}
          />
          <SessionPanel
            status={session}
            trades={sessionTrades}
            busy={sessionBusy}
            symbol={symbol}
            onStart={(feed) => void sessionAction(() => api.sessionStart([symbol], { feed }))}
            onHalt={() => void sessionAction(api.sessionHalt)}
            onResume={() => void sessionAction(api.sessionResume)}
            onStop={() => void sessionAction(api.sessionStop)}
          />
          <SystemPanel health={health} integrity={integrity} logs={logs} />
        </aside>
      </main>
    </div>
  );
}
