/** Zugriff auf die Engine-Schnittstelle. */

import type {
  BarsResponse,
  ContextSnapshot,
  Coverage,
  Health,
  Instrument,
  LoadResponse,
  LogEntry,
  Overlays,
  StepResponse,
  StrategyState,
} from './types';

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = 'ApiError';
  }
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
      const body = (await response.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
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

  step: (symbol: string, count: number) =>
    request<StepResponse>('/step', {
      method: 'POST',
      body: JSON.stringify({ symbol, count }),
    }),

  reset: (symbol: string) =>
    request<StepResponse>(`/reset?symbol=${encodeURIComponent(symbol)}`, { method: 'POST' }),
};
