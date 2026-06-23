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
    source: "dashboard-latest-close";
    scoreModel: "dashboard-rule";
    maxPositions: number;
    minScoreToBuy?: number;
    signals: Array<{
      symbol: string;
      action: "buy" | "hold" | "sell";
      confidence: number;
      size: number;
      rationale: string;
    }>;
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
}
