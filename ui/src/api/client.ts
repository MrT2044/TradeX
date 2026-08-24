/** Zugriff auf die Engine-Schnittstelle. */

import type {
  BacktestReport,
  BacktestRun,
  BarsResponse,
  ContextSnapshot,
  Coverage,
  Health,
  HistoryResponse,
  WatchState,
  Instrument,
  LoadResponse,
  LogEntry,
  Overlays,
  SessionRun,
  SessionStatus,
  SimulatedTrade,
  StepResponse,
  StrategyState,
} from './types';

/** Grenze von `/step` je Anfrage - muss zu `StepRequest.count` in
 *  `tradex/api/routes/analysis.py` passen. */
const MAX_STEP = 100_000;

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

/** Aus dem `detail`-Feld einer Fehlerantwort einen lesbaren Satz machen.
 *
 * Eine Meldung, die den Fehler verschweigt, ist schlimmer als keine: sie sieht
 * aus, als haette man hingesehen. Deshalb wird hier jede Form ausgepackt, die
 * FastAPI liefern kann - Text, Liste von Pruefergebnissen, einzelnes Objekt -
 * und im schlimmsten Fall das JSON selbst gezeigt. Unlesbar ist immer noch
 * besser als nichtssagend.
 */
function beschreibeFehler(detail: unknown): string {
  if (!detail) return '';
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return detail.map(beschreibeFehler).filter(Boolean).join('; ');
  if (typeof detail === 'object') {
    const eintrag = detail as { msg?: unknown; loc?: unknown };
    if (typeof eintrag.msg === 'string') {
      const feld = Array.isArray(eintrag.loc) ? eintrag.loc.join('.') : '';
      return feld ? `${feld}: ${eintrag.msg}` : eintrag.msg;
    }
    try {
      return JSON.stringify(detail);
    } catch {
      return '';
    }
  }
  return String(detail);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      headers: { 'Content-Type': 'application/json' },
      ...init,
    });
  } catch {
    throw new ApiError('Keine Verbindung zur Engine.', 0);
  }

  if (!response.ok) {
    // Die Engine liefert bei Bedienfehlern (404) einen erklaerenden Text mit,
    // der direkt anzeigbar ist. Nur bei echten Fehlern auf den Status zurueckfallen.
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      // `detail` ist NICHT immer ein Text: bei einer abgelehnten Anfrage (422)
      // liefert FastAPI eine Liste von Objekten. Die landete ungeprueft in der
      // Meldung und stand dann als "[object Object]" auf dem Bildschirm - also
      // genau dort, wo die Ursache haette stehen sollen.
      detail = beschreibeFehler(body.detail) || detail;
    } catch {
      /* Antwort war kein JSON - Status genuegt */
    }
    throw new ApiError(detail, response.status);
  }
  return (await response.json()) as T;
}

export const api = {
  health: () => request<Health>('/health'),
  instruments: () => request<Instrument[]>('/instruments'),
  coverage: () => request<Coverage[]>('/coverage'),
  logs: (limit = 200) => request<LogEntry[]>(`/logs?limit=${limit}`),

  load: (symbol: string, options: { maxBars?: number; feedAll?: boolean } = {}) =>
    request<LoadResponse>('/load', {
      method: 'POST',
      body: JSON.stringify({
        symbol,
        max_bars: options.maxBars ?? 200000,
        feed_all: options.feedAll ?? true,
      }),
    }),

  bars: (symbol: string, timeframe: string, limit = 1500) =>
    request<BarsResponse>(
      `/bars?symbol=${encodeURIComponent(symbol)}&timeframe=${timeframe}&limit=${limit}`,
    ),

  analysis: (symbol: string) =>
    request<ContextSnapshot>(`/analysis?symbol=${encodeURIComponent(symbol)}`),

  strategy: (symbol: string, limit = 30) =>
    request<StrategyState>(
      `/strategy?symbol=${encodeURIComponent(symbol)}&limit=${limit}`,
    ),

  overlays: (symbol: string, timeframe: string) =>
    request<Overlays>(
      `/overlays?symbol=${encodeURIComponent(symbol)}&timeframe=${timeframe}`,
    ),

  /** Bars analysieren - notfalls in mehreren Anfragen.
   *
   * `/load` nimmt bis zu 400.000 Bars an, `/step` aber nur 100.000 je Anfrage.
   * Diese Grenze ist richtig: eine einzelne Anfrage soll nicht unbegrenzt lange
   * rechnen. Falsch war, dass die Oberflaeche sie nicht kannte - der Warmlauf
   * rechnet 60% der geladenen Bars aus und schickte bei vollem Datenbestand
   * 120.000, worauf die Engine mit 422 ablehnte. Aufgeteilt wird deshalb hier,
   * an der einen Stelle, die jeder Aufrufer benutzt: "ans Ende springen" lief
   * sonst in denselben Fehler.
   */
  step: async (symbol: string, count: number): Promise<StepResponse> => {
    let offen = Math.max(1, Math.trunc(count));
    let antwort = await request<StepResponse>('/step', {
      method: 'POST',
      body: JSON.stringify({ symbol, count: Math.min(offen, MAX_STEP) }),
    });
    offen -= MAX_STEP;
    // `exhausted` bricht ab, sobald keine Bars mehr da sind - sonst liefe die
    // Schleife bei einer zu grossen Anforderung ins Leere weiter.
    while (offen > 0 && !antwort.exhausted) {
      antwort = await request<StepResponse>('/step', {
        method: 'POST',
        body: JSON.stringify({ symbol, count: Math.min(offen, MAX_STEP) }),
      });
      offen -= MAX_STEP;
    }
    return antwort;
  },

  /** Historie aus NinjaTrader nachladen. Dauert Sekunden - Aufrufer muss die
   *  Oberflaeche als beschaeftigt markieren. */
  importNt8History: (symbol: string, days = 0) =>
    request<HistoryResponse>('/history/nt8', {
      method: 'POST',
      body: JSON.stringify({ symbol, days }),
    }),

  /** Zustand der Marktbeobachtung samt letztem Kurs. Bewusst schlank - die
   *  Oberflaeche fragt das mehrmals je Sekunde ab. */
  watch: () => request<WatchState>('/watch'),

  watchStart: (symbol: string) =>
    request<WatchState>('/watch/start', {
      method: 'POST',
      body: JSON.stringify({ symbol }),
    }),

  watchStop: () => request<WatchState>('/watch/stop', { method: 'POST' }),

  reset: (symbol: string) =>
    request<StepResponse>(`/reset?symbol=${encodeURIComponent(symbol)}`, { method: 'POST' }),

  /**
   * Rechnet synchron durch - je nach Zeitraum dauert das Sekunden bis Minuten.
   * Der Aufrufer muss die Oberflaeche solange als beschaeftigt markieren.
   */
  backtest: (symbol: string, options: { maxBars?: number; save?: boolean } = {}) =>
    request<BacktestReport>('/backtest', {
      method: 'POST',
      body: JSON.stringify({
        symbol,
        max_bars: options.maxBars ?? 400000,
        save: options.save ?? true,
      }),
    }),

  lastBacktest: (symbol: string) =>
    request<BacktestReport>(`/backtest?symbol=${encodeURIComponent(symbol)}`),

  backtestRuns: (symbol: string, limit = 10) =>
    request<BacktestRun[]>(
      `/backtest/runs?symbol=${encodeURIComponent(symbol)}&limit=${limit}`,
    ),

  // --- Laufender Betrieb (Phase 7) ---------------------------------------
  session: () => request<SessionStatus>('/session'),

  sessionStart: (symbols: string[], options: { feed?: string; speed?: number } = {}) =>
    request<SessionStatus>('/session/start', {
      method: 'POST',
      body: JSON.stringify({
        symbols,
        feed: options.feed ?? 'replay',
        speed: options.speed ?? 3600,
      }),
    }),

  /**
   * Kill Switch (Spec 24). Wirkt sofort und wartet auf nichts: keine neuen
   * Positionen mehr. Offene Positionen laufen WEITER zu ihrem Stop - deshalb
   * ist das hier nicht dasselbe wie `sessionStop`.
   */
  sessionHalt: () => request<SessionStatus>('/session/halt', { method: 'POST' }),
  sessionResume: () => request<SessionStatus>('/session/resume', { method: 'POST' }),
  sessionStop: () => request<SessionStatus>('/session/stop', { method: 'POST' }),

  sessionTrades: (limit = 50) =>
    request<SimulatedTrade[]>(`/session/trades?limit=${limit}`),

  sessionRuns: (limit = 10) => request<SessionRun[]>(`/sessions?limit=${limit}`),
};
