import { test } from "node:test";
import assert from "node:assert/strict";

import { validateRuntimeArtifacts, type RuntimeBacktestSnapshot } from "../lib/runtimeValidation";
import type { SignalHistorySnapshot } from "../lib/signalHistory";
import type { Signal } from "../lib/strategyTypes";

const archivedSignals: Signal[] = [
  { symbol: "A", action: "buy", confidence: 0.8, size: 0.6, rationale: "archived A" },
  { symbol: "B", action: "hold", confidence: 0.7, size: 0, rationale: "not top 1" },
];

const latestSignals: Signal[] = [
  { symbol: "A", action: "hold", confidence: 0.6, size: 0, rationale: "keep" },
  { symbol: "B", action: "buy", confidence: 0.9, size: 1, rationale: "latest B" },
];

function history(date: string, signals: Signal[]): SignalHistorySnapshot {
  return {
    generated_at: `${date}T08:00:00.000Z`,
    signal_date: date,
    execution_price: "next_open",
    source: "dashboard-latest-close",
    score_model: "dashboard-rule",
    max_positions: 1,
    signals,
  };
}

function backtest(overrides: Partial<RuntimeBacktestSnapshot> = {}): RuntimeBacktestSnapshot {
  return {
    config: {
      startCash: 1_000_000,
      rebalanceEveryNDays: 1,
      decisionEveryNDays: 1,
      executionPrice: "next_open",
      startDate: "2026-06-24",
      endDate: "2026-06-26",
      feeBps: 0,
      maxPositions: 1,
    },
    equityCurve: [
      { date: "2026-06-24", equity: 1_000_000, cash: 1_000_000, positions: {} },
      { date: "2026-06-25", equity: 1_000_000, cash: 400_000, positions: { A: { shares: 6000, price: 100 } } },
      { date: "2026-06-26", equity: 1_010_000, cash: 400_000, positions: { A: { shares: 6000, price: 101 } } },
    ],
    trades: [
      {
        date: "2026-06-25",
        decisionDate: "2026-06-24",
        tradeDate: "2026-06-25",
        priceField: "open",
        symbol: "A",
        side: "buy",
        shares: 6000,
        price: 100,
        reason: "archived A",
        targetWeightBefore: 0,
        targetWeightAfter: 0.6,
        pnlPct: null,
      },
    ],
    signalsByDate: {
      "2026-06-24": archivedSignals,
    },
    stats: {
      totalReturnPct: 1,
      cagrPct: 1,
      maxDrawdownPct: 0,
      sharpe: 3,
      trades: 1,
      winRatePct: 0,
      turnoverPct: 60,
    },
    latestDate: "2026-06-26",
    latestPlan: {
      decisionDate: "2026-06-26",
      executionPrice: "next_open",
      source: "dashboard-latest-close",
      scoreModel: "dashboard-rule",
      maxPositions: 1,
      signals: latestSignals,
    },
    latestHoldings: { A: { shares: 6000, price: 101 } },
    ...overrides,
  };
}

test("runtime validation accepts archived signals that match simulated trades", () => {
  const issues = validateRuntimeArtifacts({
    backtest: backtest(),
    signals: {
      signal_date: "2026-06-26",
      latest_complete_date: "2026-06-26",
      universe_count: 2,
      signals: latestSignals,
    },
    meta: { universe_count: 2 },
    histories: [history("2026-06-24", archivedSignals), history("2026-06-26", latestSignals)],
  });

  assert.deepEqual(issues, []);
});

test("runtime validation keeps intraday signal date separate from latest complete date", () => {
  const issues = validateRuntimeArtifacts({
    backtest: backtest({ snapshot_basis: "intraday-midday" }),
    signals: {
      signal_date: "2026-06-26",
      latest_complete_date: "2026-06-25",
      universe_count: 2,
      signals: latestSignals,
    },
    meta: { universe_count: 2 },
    histories: [history("2026-06-24", archivedSignals), history("2026-06-26", latestSignals)],
  });

  assert.deepEqual(issues, []);
});

test("runtime validation rejects historical signal drift", () => {
  const driftedSignals: Signal[] = [
    { symbol: "A", action: "hold", confidence: 0.8, size: 0, rationale: "drifted" },
    { symbol: "B", action: "buy", confidence: 0.7, size: 1, rationale: "recomputed B" },
  ];
  const issues = validateRuntimeArtifacts({
    backtest: backtest({ signalsByDate: { "2026-06-24": driftedSignals } }),
    signals: {
      signal_date: "2026-06-26",
      latest_complete_date: "2026-06-26",
      universe_count: 2,
      signals: latestSignals,
    },
    meta: { universe_count: 2 },
    histories: [history("2026-06-24", archivedSignals), history("2026-06-26", latestSignals)],
  });

  assert.ok(issues.some((issue) => issue.code === "SIGNAL_ACTION_MISMATCH"));
});

test("runtime validation rejects buys that were not in the archived plan", () => {
  const issues = validateRuntimeArtifacts({
    backtest: backtest({
      trades: [
        {
          date: "2026-06-25",
          decisionDate: "2026-06-24",
          tradeDate: "2026-06-25",
          priceField: "open",
          symbol: "B",
          side: "buy",
          shares: 10_000,
          price: 100,
          reason: "recomputed B",
          targetWeightBefore: 0,
          targetWeightAfter: 1,
          pnlPct: null,
        },
      ],
    }),
    signals: {
      signal_date: "2026-06-26",
      latest_complete_date: "2026-06-26",
      universe_count: 2,
      signals: latestSignals,
    },
    meta: { universe_count: 2 },
    histories: [history("2026-06-24", archivedSignals), history("2026-06-26", latestSignals)],
  });

  assert.ok(issues.some((issue) => issue.code === "BUY_WITHOUT_ARCHIVED_SIGNAL"));
});

test("runtime validation rejects shadow predictions with future data", () => {
  const base = backtest();
  const issues = validateRuntimeArtifacts({
    backtest: backtest({
      latestPlan: {
        ...base.latestPlan!,
        shadowModel: {
          generated_at: "2026-06-26T10:00:00.000Z",
          decision_date: "2026-06-26",
          data_cutoff: "2026-06-27",
          stage: "shadow",
          model_version: "lgbm-001",
          feature_version: "alpha-v1",
          source: "qlib",
          predictions: [],
        },
      },
    }),
    signals: {
      signal_date: "2026-06-26",
      latest_complete_date: "2026-06-26",
      universe_count: 2,
      signals: latestSignals,
    },
    meta: { universe_count: 2 },
    histories: [history("2026-06-24", archivedSignals), history("2026-06-26", latestSignals)],
  });

  assert.ok(issues.some((issue) => issue.code === "SHADOW_MODEL_FUTURE_DATA"));
});
