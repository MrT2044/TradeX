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

// ------------------------------------------------------------ Strategie (Ph. 3)
export type SetupStage =
  | 'swept'
  | 'displaced'
  | 'retraced'
  | 'confirmed'
  | 'invalidated'
  | 'expired';

export interface Setup {
  id: number;
  direction: Direction;
  stage: SetupStage;
  created_ts: number;
  sweep_price: number;
  sweep_kind: LiquidityKind;
  invalidation_price: number;
  checklist: Record<string, boolean>;
  missing: string[];
  fvg_top: number | null;
  fvg_bottom: number | null;
  retracement_extreme: number | null;
  displacement_strength: number | null;
}

export interface TradeSignal {
  setup_id: number;
  symbol: string;
  strategy: string;
  side: 'LONG' | 'SHORT';
  entry: number;
  stop: number;
  target: number;
  stop_ticks: number;
  rr: number;
  quantity: number;
  risk_amount: number;
  reward_amount: number;
  entry_ts: number;
  stop_anchor: string;
  target_source: string;
}

export interface StrategyDecision {
  ts: number;
  symbol: string;
  timeframe: string;
  setup_id: number;
  direction: Direction;
  decision: 'LONG' | 'SHORT' | 'NO_TRADE';
  stage: SetupStage;
  htf_bias: BiasValue;
  strategy: string;
  checklist: Record<string, boolean>;
  missing: string[];
  blocking_reason: string;
  reasons: Reason[];
  signal: TradeSignal | null;
}

// ------------------------------------------------------------- Backtest (Ph. 4)
export type ExitReason = 'stop' | 'target' | 'time' | 'end_of_data';

/**
 * `profit_factor`, `payoff_ratio` und `sqn` sind null, wenn sie sich aus der
 * Stichprobe nicht bestimmen lassen - etwa ohne einen einzigen Verlusttrade.
 * Das ist NICHT dasselbe wie 0 und darf im UI auch nicht so aussehen.
 */
export interface Metrics {
  trades: number;
  wins: number;
  losses: number;
  scratches: number;
  win_rate: number;
  gross_profit: number;
  gross_loss: number;
  commission: number;
  net_pnl: number;
  profit_factor: number | null;
  expectancy_r: number;
  expectancy_usd: number;
  avg_win_r: number;
  avg_loss_r: number;
  payoff_ratio: number | null;
  best_r: number;
  worst_r: number;
  stdev_r: number;
  sqn: number | null;
  max_drawdown_usd: number;
  max_drawdown_pct: number;
  max_drawdown_r: number;
  max_consecutive_wins: number;
  max_consecutive_losses: number;
  avg_bars_held: number;
  avg_mae_r: number;
  avg_mfe_r: number;
  avg_planned_rr: number;
  start_equity: number;
  final_equity: number;
  return_pct: number;
  first_ts: number;
  last_ts: number;
  unresolved: number;
}

export interface EquityPoint {
  ts: number;
  trade_number: number;
  equity: number;
  drawdown: number;
}

export interface SimulatedTrade {
  setup_id: number;
  /** Welche Strategie den Trade vorgeschlagen hat. */
  strategy: string;
  side: 'LONG' | 'SHORT';
  session: string;
  quantity: number;
  planned_entry: number;
  entry_price: number;
  exit_price: number;
  stop: number;
  target: number;
  planned_rr: number;
  entry_ts: number;
  exit_ts: number;
  bars_held: number;
  exit_reason: ExitReason;
  pnl: number;
  commission: number;
  r_multiple: number;
  mae_r: number;
  mfe_r: number;
  stop_anchor: string;
  target_source: string;
  htf_bias: BiasValue | '';
}

export interface BacktestReport {
  symbol: string;
  instrument_name: string;
  base_timeframe: string;
  bars: number;
  first_ts: number;
  last_ts: number;
  backtest_version: string;
  /** Was der Leser wissen muss, BEVOR er die Zahlen deutet. */
  warnings: string[];
  is_significant: boolean;
  min_trades: number;
  overall: Metrics;
  in_sample: Metrics;
  out_of_sample: Metrics;
  /** Welche Strategie hat das Ergebnis getragen, welche nur Gebuehren produziert? */
  by_strategy: Record<string, Metrics>;
  /** Bei mehreren Instrumenten: traegt die Regel ueberall oder nur auf einem? */
  by_symbol: Record<string, Metrics>;
  by_session: Record<string, Metrics>;
  by_direction: Record<string, Metrics>;
  by_exit: Record<string, Metrics>;
  by_stop_anchor: Record<string, Metrics>;
  by_target_source: Record<string, Metrics>;
  exit_counts: Record<string, number>;
  rejections: Record<string, number>;
  signals: number;
  unfilled: number;
  /** Signale ueber einer Datenluecke - verworfen statt Stunden spaeter gefuellt. */
  stale: number;
  trades_total: number;
  equity: EquityPoint[];
  trades: SimulatedTrade[];
  assumptions: Record<string, unknown>;
}

export interface BacktestRun {
  id: number;
  ts_utc: string;
  symbol: string;
  base_timeframe: string;
  first_ts: number;
  last_ts: number;
  bars: number;
  config_hash: string;
  strategy_version: string;
  backtest_version: string;
  trades: number;
  wins: number;
  losses: number;
  net_pnl: number;
  expectancy_r: number;
  profit_factor: number | null;
  max_drawdown_pct: number;
  notes: string;
}

export interface StrategyState {
  symbol: string;
  enabled: boolean;
  setup_timeframe: string;
  confirmation_timeframe: string;
  stop_anchor: string;
  min_rr: number;
  active_setups: Setup[];
  recent_decisions: StrategyDecision[];
  last_signal: TradeSignal | null;
  decisions_total: number;
  trades_total: number;
  no_trades_total: number;
  rejection_counts: Record<string, number>;
}
