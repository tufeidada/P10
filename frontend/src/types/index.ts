export interface RegimeData {
  trade_date: string;
  regime_mode: 'offense' | 'cautious_offense' | 'defense' | 'risk_off';
  trend_score: number | null;
  volatility_score: number | null;
  breadth_score: number | null;
  liquidity_score: number | null;
  max_position_pct: number;
  signal_threshold_adj: number;
  dimension_weights: {
    technical: number;
    fundamental: number;
    flow: number;
    sentiment: number;
  };
}

export interface StockSummary {
  symbol: string;
  name: string;
  market: string;
  industry: string | null;
  composite_score: number | null;
  technical_score: number | null;
  fundamental_score: number | null;
  flow_score: number | null;
  direction: 'bullish' | 'neutral' | 'bearish' | null;
  confidence: number | null;
  judgment_date: string | null;
  rule_signal_strength: string | null;
  llm_direction: string | null;
  llm_signal_strength: string | null;
  latest_signal: {
    signal_type: string;
    strength: string;
    signal_time: string;
  } | null;
  latest_close: number | null;
  latest_pct_chg: number | null;
  latest_bar_date: string | null;
}

export interface FactorBreakdown {
  score: number;
  weight: number;
  contribution: number;
  score_missing: boolean;
}

export interface FactorContributions {
  baseline: number;
  factors: {
    technical: FactorBreakdown;
    fundamental: FactorBreakdown;
    flow: FactorBreakdown;
    sentiment: FactorBreakdown;
  };
  composite_stored: number;
  composite_recomputed: number;
  residual: number;
  weights_source: string;
}

export interface JudgmentDetail {
  id: number;
  symbol: string;
  judgment_date: string;
  timeframe: string;
  technical_score: number | null;
  fundamental_score: number | null;
  flow_score: number | null;
  sentiment_score: number | null;
  composite_score: number;
  direction: 'bullish' | 'neutral' | 'bearish';
  confidence: number;
  logic_text: string | null;
  suggested_action: string | null;
  entry_zone_low: number | null;
  entry_zone_high: number | null;
  stop_loss: number | null;
  target_price: number | null;
  signal_sources: Record<string, unknown>;
  regime_at_time: Record<string, unknown>;
  rule_signal_strength: string | null;
  llm_direction: string | null;
  llm_signal_strength: string | null;
  llm_reasoning: string | null;
  llm_risks: string | null;
  llm_extra_advice: string | null;
  llm_vote_consensus: number | null;
  llm_vote_total_calls: number | null;
  factor_contributions?: FactorContributions | null;
}

export interface JudgmentHistory {
  id: number;
  judgment_date: string;
  direction: string;
  confidence: number;
  composite_score: number;
  technical_score: number | null;
  fundamental_score: number | null;
  flow_score: number | null;
  sentiment_score: number | null;
  is_correct: boolean | null;
  actual_ret_10d: number | null;
}

export interface Bar {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface BarsData {
  symbol: string;
  bars: Bar[];
  moving_averages: {
    ma5: (number | null)[];
    ma20: (number | null)[];
    ma60: (number | null)[];
    ma150: (number | null)[];
  };
}

export interface Signal {
  id: number;
  symbol: string;
  signal_type: 'buy' | 'sell';
  strength: string;
  trigger_rule: string;
  price_at_signal: number;
  signal_time: string;
}

export interface HealthData {
  status: string;
  sources: Record<
    string,
    {
      latest_date: string | null;
      status: 'ok' | 'warning' | 'error';
    }
  >;
}

export interface CandidateListResponse {
  stocks: StockSummary[];
  total: number;
}

export interface SignalListResponse {
  signals: Signal[];
  total: number;
}

export interface RegimeResponse {
  cn: RegimeData | null;
  us: RegimeData | null;
}

export interface ReviewReport {
  id: number;
  report_type: string;
  report_date: string;
  market: string;
  total_judgments: number | null;
  accuracy_short: number | null;
  accuracy_mid: number | null;
  alpha_vs_benchmark: number | null;
  summary_text: string | null;
  key_findings: Record<string, unknown> | null;
  full_report_md: string | null;
  created_at: string;
}

export interface SignalQualityRule {
  rule_name: string;
  market: string;
  regime_mode: string | null;
  total_signals: number;
  correct_signals: number;
  accuracy: number | null;
  avg_return: number | null;
  ic_value: number | null;
  ir_value: number | null;
  period_end: string;
}

export interface Experience {
  id: number;
  category: string;
  market: string;
  status: string;
  content_text: string;
  discovery_date: string;
  applied_count: number;
  last_validated: string | null;
}

export interface DataQualitySource {
  latest_date: string | null;
  status: 'ok' | 'warning' | 'error';
  row_count?: number;
}

export interface DataQualityData {
  status: string;
  sources: Record<string, DataQualitySource>;
  last_checked: string;
}

export interface QualityData {
  rules: SignalQualityRule[];
}

export interface ExperienceData {
  experiences: Experience[];
}

export interface AccuracyRow {
  direction: string;
  total: number;
  evaluated: number;
  accuracy: number | null;
  avg_ret_10d: number | null;
  avg_vote_consensus?: number | null;
}

export interface DivergenceStats {
  total: number;
  llm_more_aggressive: number;
  llm_more_conservative: number;
  fully_aligned: number;
  llm_aggressive_ratio: number | null;
  llm_conservative_ratio: number | null;
  aligned_ratio: number | null;
}

export interface DivergenceTrendPoint {
  date: string;
  total: number;
  llm_aggressive: number;
  llm_conservative: number;
  aligned: number;
}

export interface QualityTrackingData {
  rule_accuracy: AccuracyRow[];
  llm_accuracy: AccuracyRow[];
  divergence: DivergenceStats;
  divergence_trend: DivergenceTrendPoint[];
  alpha: {
    high_conviction_count: number;
    high_conviction_avg_ret: number | null;
    overall_avg_ret: number | null;
    evaluated: number;
  };
}
