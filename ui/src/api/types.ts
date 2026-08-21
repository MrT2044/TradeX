/**
 * Typen der Engine-Schnittstelle.
 *
 * Spiegelbild von `tradex/api/schemas.py`. Aendert sich dort etwas, muss es hier
 * nachgezogen werden - das ist der bewusste Preis der strikten Trennung von
 * Engine und Oberflaeche (Spec 27).
 */

export type Direction = 'bullish' | 'bearish';
export type BiasValue = 'bullish' | 'bearish' | 'neutral';
export type FvgState = 'open' | 'touched' | 'mitigated' | 'expired';
export type LiquiditySide = 'buy_side' | 'sell_side';
export type LiquidityKind = 'swing' | 'equal' | 'session' | 'prior_day' | 'prior_week';
export type StructureState = 'bullish' | 'bearish' | 'range';

export interface Bar {
  ts: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  roll_boundary: boolean;
}

export interface BarsResponse {
  symbol: string;
  timeframe: string;
  bars: Bar[];
  /** Die noch laufende Bar. Wird gezeichnet, aber nie ausgewertet. */
  forming: Bar | null;
}

export interface Reason {
  code: string;
  ok: boolean;
  params: Record<string, unknown>;
}

export interface Swing {
  index: number;
  ts: number;
  price: number;
  type: 'swing_high' | 'swing_low';
  strength: number;
  confirmed_at_index: number;
}

export interface Fvg {
  id: number;
  direction: Direction;
  created_index: number;
  created_ts: number;
  bottom: number;
  top: number;
  size_ticks: number;
  state: FvgState;
  max_fill: number;
  touched_index: number | null;
  mitigated_index: number | null;
  /** Zeitpunkt, an dem die Zone erledigt wurde; `null` solange sie aktiv ist. */
  closed_ts: number | null;
}

export interface LiquidityPool {
  id: number;
  price: number;
  side: LiquiditySide;
  kind: LiquidityKind;
  label: string;
  strength: number;
  state: 'untapped' | 'swept';
  created_ts: number;
  tapped_index: number | null;
}

export interface Sweep {
  pool_id: number;
  pool_kind: LiquidityKind;
  pool_price: number;
  side: LiquiditySide;
  direction: Direction;
  penetration_ts: number;
  reclaim_ts: number;
  depth_ticks: number;
  bars_to_reclaim: number;
}

export interface StructureEvent {
  index: number;
  ts: number;
  type: 'bos_bullish' | 'bos_bearish' | 'mss_bullish' | 'mss_bearish';
  broken_price: number;
  break_price: number;
  previous_state: StructureState;
  new_state: StructureState;
}

export interface Displacement {
  index: number;
  ts: number;
  direction: Direction;
  range: number;
  body_ratio: number;
  range_atr_mult: number;
  volume_ratio: number | null;
  volume_confirmed: boolean;
  strength: number;
}

export interface Overlays {
  symbol: string;
  timeframe: string;
  swings: Swing[];
  fvgs: Fvg[];
  pools: LiquidityPool[];
  sweeps: Sweep[];
  structure_events: StructureEvent[];
  displacements: Displacement[];
}

export interface TimeframeBias {
  timeframe: string;
  score: number;
  structure_score: number;
  fvg_score: number;
  liquidity_score: number;
  structure_state: StructureState;
  active_bullish_fvgs: number;
  active_bearish_fvgs: number;
  nearest_buy_side: number | null;
  nearest_sell_side: number | null;
}

export interface Bias {
  bias: BiasValue;
  score: number;
  per_timeframe: TimeframeBias[];
  reasons: Reason[];
}

export interface TimeframeSnapshot {
  timeframe: string;
  bar_count: number;
  ready: boolean;
  session: string;
  atr: number | null;
  volume_avg: number | null;
  last_bar: Bar | null;
  structure_state: StructureState;
  last_structure_event: StructureEvent | null;
  last_mss: StructureEvent | null;
  swings: Swing[];
  active_fvgs: Fvg[];
  untapped_pools: LiquidityPool[];
  recent_sweeps: Sweep[];
  last_displacement: Displacement | null;
}

export interface ContextSnapshot {
  symbol: string;
  last_ts: number;
  session: string;
  bias: Bias;
  timeframes: Record<string, TimeframeSnapshot>;
}

export interface Instrument {
  symbol: string;
  name: string;
  exchange: string;
  currency: string;
  tick_size: number;
  tick_value: number;
  point_value: number;
  price_decimals: number;
}

export interface Coverage {
  symbol: string;
  timeframe: string;
  first_ts: number;
  last_ts: number;
  bar_count: number;
}

export interface Provider {
  name: string;
  status: string;
  detail: string;
  historical_bars: boolean;
  live_bars: boolean;
  live_ticks: boolean;
  bid_ask: boolean;
  market_depth: boolean;
  volume: boolean;
  order_execution: boolean;
  notes: string;
}

export interface Health {
  ok: boolean;
  mode: string;
  live_trading_enabled: boolean;
  symbol: string;
  config_hash: string;
  strategy_version: string;
  providers: Provider[];
  warnings: string[];
}

export interface IntegrityGap {
  start_ts: number;
  end_ts: number;
  missing_bars: number;
}

export interface Integrity {
  symbol: string;
  timeframe: string;
  bar_count: number;
  is_clean: boolean;
  missing_bars: number;
  gaps: IntegrityGap[];
  invalid_bars: number[];
  duplicate_timestamps: number;
}

export interface LoadResponse {
  symbol: string;
  base_timeframe: string;
  base_bars: number;
  cursor: number;
  progress: number;
  integrity: Integrity | null;
  warnings: string[];
}

export interface StepResponse {
  symbol: string;
  cursor: number;
  base_bars: number;
  progress: number;
  exhausted: boolean;
  new_swings: number;
  new_fvgs: number;
  new_sweeps: number;
  new_structure_events: number;
  new_displacements: number;
}

export interface LogEntry {
  timestamp: string;
  level: string;
  event: string;
  fields: Record<string, unknown>;
}
