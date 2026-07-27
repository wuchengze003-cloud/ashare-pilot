export interface DashboardModelSnapshot {
  generated_at: string;
  decision_date: string;
  data_cutoff: string;
  stage: "shadow" | "champion" | "disabled";
  model_version: string;
  feature_version: string;
  source: "qlib";
  predictions: Array<{
    symbol: string;
    rank: number;
    score: number;
    expectedReturns: Partial<Record<"d1" | "d3" | "d5" | "d10", number>>;
    downsideRisk: number;
    confidence: number;
    targetWeight: number;
    action: "buy" | "hold" | "sell" | "cash";
    reasonCodes: string[];
    featureContributions?: Record<string, number>;
  }>;
  quality?: {
    data_quality_passed: boolean;
    drift_passed: boolean;
    warnings: string[];
  };
  shadow_account?: {
    cash: number;
    positions: Record<string, number>;
    equity_curve: Array<{
      date: string;
      equity: number;
      cash: number;
      positions: number;
    }>;
  } | null;
}

export interface DashboardData {
  generated_at: string;
  snapshot_basis?: "latest-complete-close" | "intraday-midday";
  snapshot_label?: string;
  config: {
    startCash: number;
    rebalanceEveryNDays: number;
    decisionEveryNDays?: number;
    executionPrice?: "next_open";
    startDate: string;
    endDate: string;
    feeBps: number;
    maxPositions: number;
    minHoldBars?: number;
    rebalanceThresholdPct?: number;
    sharpeTarget?: number;
    optimizationWindow?: string;
  };
  stats: {
    totalReturnPct: number;
    cagrPct: number;
    maxDrawdownPct: number;
    sharpe: number;
    trades: number;
    winRatePct?: number;
    turnoverPct?: number;
  };
  equityCurve: Array<{
    date: string;
    equity: number;
    cash: number;
    positions: Record<string, { shares: number; price: number }>;
  }>;
  benchmarkCurve: Array<{ date: string; equity: number }>;
  trades: Array<{
    date: string;
    decisionDate?: string;
    tradeDate?: string;
    priceField?: "open";
    symbol: string;
    side: "buy" | "sell" | "reduce";
    shares: number;
    price: number;
    reason?: string;
    targetWeightBefore?: number;
    targetWeightAfter?: number;
    pnlPct?: number | null;
  }>;
  themePerformance: Array<{
    theme: string;
    returnPct: number;
    realizedPct: number;
    unrealizedPct: number;
    allocationDays: number;
    avgWeightPct: number;
  }>;
  latestHoldings: Record<string, { shares: number; price: number }>;
  latestDate: string;
  latestPlan?: {
    decisionDate: string;
    executionPrice: "next_open";
    source: "dashboard-latest-close" | "qlib-promoted";
    scoreModel: "dashboard-rule" | "qlib-model";
    maxPositions: number;
    minScoreToBuy?: number;
    signals: Array<{
      symbol: string;
      action: "buy" | "hold" | "sell";
      confidence: number;
      size: number;
      rationale: string;
      modelVersion?: string;
      expectedReturns?: Partial<Record<"d1" | "d3" | "d5" | "d10", number>>;
      downsideRisk?: number;
      reasonCodes?: string[];
    }>;
    shadowModel?: DashboardModelSnapshot;
    championModel?: DashboardModelSnapshot;
  };
  meetsSharpeTarget?: boolean;
  primaryWindow?: string;
  validationStats?: {
    jan_2026?: DashboardData["stats"];
  };
  optimizedParams?: {
    maxPositions: number;
    minHoldBars: number;
    rebalanceThresholdPct: number;
    minScoreToBuy: number;
  };
  optimizationWarnings?: string[];
  researchStatus?: {
    generated_at?: string;
    status?: string;
    production_strategy?: "v1-rule" | "ml-champion";
    active_model?: string | null;
    challenger_models?: string[];
    activation_pending?: string | null;
    promotion_assessments?: Array<{
      generated_at?: string;
      model_version?: string;
      status?: "shadow-not-ready" | "eligible" | "promoted";
      passed?: boolean;
      metrics?: {
        primary_sharpe?: number;
        oos_sharpe?: number;
        max_drawdown_pct?: number;
        average_hold_bars?: number;
        turnover_pct?: number;
        bootstrap_win_probability?: number;
        shadow_trading_days?: number;
        closed_trades?: number;
        oos_folds?: number;
        data_quality_passed?: boolean;
        drift_passed?: boolean;
      };
      failures?: Array<{
        code?: string;
        actual?: string | number | boolean;
        required?: string | number | boolean;
      }>;
    }>;
    qlib_benchmark?: {
      passed?: boolean;
      data_cutoff?: string;
      release?: string;
      promotable?: false;
      results?: {
        linear?: {
          fold_rank_ic?: number[];
          median_rank_ic?: number;
          holdout_rank_ic?: number;
        } | null;
        lightgbm?: {
          fold_rank_ic?: number[];
          median_rank_ic?: number;
          holdout_rank_ic?: number;
        } | null;
      };
    } | null;
    tushare_production?: {
      passed?: boolean;
      error?: string;
      recorded_at?: string;
      data_cutoff?: string | null;
      trading_days?: number;
      failures?: string[];
    } | null;
    model_health?: {
      as_of?: string;
      model_version?: string;
      consecutive_underperform_days?: number;
      current_drawdown_pct?: number;
      data_quality_passed?: boolean;
      drift_passed?: boolean;
    } | null;
    outcome_feedback?: {
      as_of?: string;
      inserted?: number;
      summary?: {
        outcomes?: number;
        groups?: Array<{
          model_version?: string;
          horizon_bars?: number;
          observations?: number;
          hit_rate?: number;
          net_hit_rate?: number;
          mean_net_return?: number;
          mean_excess_return?: number;
          mean_mfe?: number;
          mean_mae?: number;
          mean_opportunity_cost?: number;
          mean_absolute_calibration_error?: number;
        }>;
      };
    } | null;
  } | null;
}
