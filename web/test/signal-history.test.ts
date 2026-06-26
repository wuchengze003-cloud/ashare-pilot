import { test } from "node:test";
import assert from "node:assert/strict";

import { buildSignalHistorySnapshot } from "../lib/signalHistory";
import type { LatestPlan } from "../lib/latestPlan";
import type { SymbolSeries } from "../lib/backtest";

const series: SymbolSeries[] = [
  {
    entry: { symbol: "A", name: "Alpha", theme: "光模块" },
    klines: [
      { date: "2026-06-24", open: 9, high: 11, low: 9, close: 10, volume: 1000 },
      { date: "2026-06-25", open: 11, high: 13, low: 10, close: 12, volume: 2000 },
      { date: "2026-06-26", open: 50, high: 60, low: 49, close: 55, volume: 3000 },
    ],
  },
];

test("signal history stores the decision-day close and never a future price", () => {
  const plan: LatestPlan = {
    decisionDate: "2026-06-25",
    executionPrice: "next_open",
    source: "dashboard-latest-close",
    scoreModel: "dashboard-rule",
    maxPositions: 4,
    minScoreToBuy: 0.65,
    signals: [
      { symbol: "A", action: "buy", confidence: 0.9, size: 0.25, rationale: "test buy" },
      { symbol: "MISSING", action: "buy", confidence: 0.8, size: 0.25, rationale: "test missing" },
    ],
  };

  const snapshot = buildSignalHistorySnapshot(plan, series);
  const alpha = snapshot.signals.find((signal) => signal.symbol === "A");
  const missing = snapshot.signals.find((signal) => signal.symbol === "MISSING");

  assert.equal(snapshot.signal_date, "2026-06-25");
  assert.equal(alpha?.signalPrice, 12);
  assert.equal(alpha?.signalPriceDate, "2026-06-25");
  assert.equal(alpha?.name, "Alpha");
  assert.equal(alpha?.theme, "光模块");
  assert.equal(missing?.signalPrice, null);
  assert.equal(missing?.signalPriceDate, null);
});
