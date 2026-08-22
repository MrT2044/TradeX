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
  StrategyState,
} from './api/types';
import { TradeChart, type ChartToggles } from './chart/TradeChart';
import { de } from './i18n/de';
import { AnalysisPanel } from './panels/AnalysisPanel';
import { BacktestPanel } from './panels/BacktestPanel';
import { ReplayControls } from './panels/ReplayControls';
import { StatusBar } from './panels/StatusBar';
import { StrategyPanel } from './panels/StrategyPanel';
import { SystemPanel } from './panels/SystemPanel';

const TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h'];
const ENTRY_TIMEFRAME = '1m';
const PLAY_INTERVAL_MS = 400;

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
  const [error, setError] = useState<string | null>(null);

  const symbols = useMemo(
    () => Array.from(new Set(coverage.map((item) => item.symbol))).sort(),
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

        const available = Array.from(new Set(coverageData.map((c) => c.symbol)));
        // Bevorzugt das konfigurierte Standardinstrument, sonst das erste
        // Symbol, fuer das ueberhaupt Daten vorliegen.
        const preferred = available.includes(healthData.symbol)
          ? healthData.symbol
          : available[0];
        if (preferred) setSymbol(preferred);
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
        // Ergebnis des vorigen Instruments verwerfen - sonst stuende beim
        // Wechsel eine fremde Statistik unter dem neuen Symbol.
        setBacktest(null);
        // feedAll=false: die Daten liegen bereit, die Analyse startet bei 0.
        // So kann man von Anfang an mitverfolgen, wann welcher Detektor anspringt.
        const loaded = await api.load(symbol, { feedAll: false });
        if (cancelled) return;
        setTotal(loaded.base_bars);
        setCursor(loaded.cursor);
        setIntegrity(loaded.integrity);

        // Einen Teil sofort durchlaufen lassen, damit nicht ein leeres Chart
        // dasteht - aber nicht alles, damit noch etwas zum Zusehen bleibt.
        const warmup = Math.min(loaded.base_bars, Math.floor(loaded.base_bars * 0.6));
        if (warmup > 0) {
          const stepped = await api.step(symbol, warmup);
          if (cancelled) return;
          setCursor(stepped.cursor);
        }
        await refreshView(symbol, timeframe);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (!cancelled) setBusy(false);
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
        selected={symbol}
        onSelect={setSymbol}
        busy={busy}
      />

      {error && (
        <div className="banner banner--error">
          <strong>{de.common.error}:</strong> {error}
          <button type="button" onClick={() => setError(null)}>
            ×
          </button>
        </div>
      )}

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
          <SystemPanel health={health} integrity={integrity} logs={logs} />
        </aside>
      </main>
    </div>
  );
}
