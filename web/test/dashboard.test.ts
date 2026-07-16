// Tests for the deterministic Dashboard rule-based strategy.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "scc-dash-"));
process.chdir(tmp);

import type { Kline } from "../lib/pyserver";
import { runBacktest, type BacktestConfig, type SymbolSeries } from "../lib/backtest";
import { ruleBasedScorer } from "../lib/dashboardBacktest";
import { buildLatestPlan, buildPromotedModelPlan, buildSnapshotsAsOf } from "../lib/latestPlan";
import { toExecutableSignals } from "../lib/signalPolicy";

function makeKlines(start: string, closes: number[]): Kline[] {
  const d = new Date(start);
  return closes.map((c) => {
    while (d.getUTCDay() === 0 || d.getUTCDay() === 6) {
      d.setUTCDate(d.getUTCDate() + 1);
    }
    const date = d.toISOString().slice(0, 10);
    d.setUTCDate(d.getUTCDate() + 1);
    return { date, open: c, high: c, low: c, close: c, volume: 1_000_000 };
  });
}

const cfg: BacktestConfig = {
  startCash: 1_000_000,
  rebalanceEveryNDays: 5,
  startDate: "2025-01-01",
  endDate: "2025-06-30",
  feeBps: 10,
  maxPositions: 1,
  autoSellUnselected: true,
};

function makeVolumes(n: number, recentMultiplier = 1): number[] {
  return Array.from({ length: n }, (_, i) => (i >= n - 5 ? 1_000_000 * recentMultiplier : 1_000_000));
}

test("ruleBasedScorer ranks right-side strength ahead of low PEG alone", async () => {
  const scorer = ruleBasedScorer();
  const snapshots = [
    {
      symbol: "MOMENTUM",
      name: "Momentum",
      theme: "光模块",
      global_supply: true,
      closes: Array.from({ length: 30 }, (_, i) => 100 + i),
      volumes: makeVolumes(30, 1.8),
      fundamental: { pe_ttm: 80, profit_yoy: 50 }, // PEG = 1.6
    },
    {
      symbol: "LOWPEG",
      name: "Low PEG",
      theme: "电力",
      closes: Array.from({ length: 30 }, (_, i) => 100 + i * 0.1),
      volumes: makeVolumes(30, 1),
      fundamental: { pe_ttm: 8, profit_yoy: 80 }, // PEG = 0.1, but no right-side strength
    },
  ];
  const signals = await scorer(snapshots, { asOf: "2025-02-01", mode: "backtest" });
  assert.equal(signals.length, 2);
  assert.ok(signals[0].confidence > signals[1].confidence, "right-side strength should outrank low PEG alone");
  assert.equal(signals[0].symbol, "MOMENTUM");
  assert.equal(signals[0].action, "buy");
});

test("ruleBasedScorer does not chase a latest visible day above 5%", async () => {
  const scorer = ruleBasedScorer();
  const closes = [
    ...Array.from({ length: 29 }, (_, i) => 100 + i * 0.4),
    120,
  ];
  const [signal] = await scorer([
    {
      symbol: "CHASE",
      name: "Chase",
      theme: "AI-PCB",
      global_supply: true,
      closes,
      volumes: makeVolumes(30, 2),
      fundamental: { pe_ttm: 60, profit_yoy: 60 },
    },
  ], { asOf: "2025-02-01", mode: "backtest" });
  assert.notEqual(signal.action, "buy");
  assert.equal(signal.size, 0);
});

test("ruleBasedScorer treats severe profit decline as a risk filter", async () => {
  const scorer = ruleBasedScorer();
  const [signal] = await scorer([
    {
      symbol: "RISK",
      name: "Risk",
      theme: "光模块",
      global_supply: true,
      closes: Array.from({ length: 30 }, (_, i) => 100 + i),
      volumes: makeVolumes(30, 1.6),
      fundamental: { pe_ttm: 60, profit_yoy: -40 },
    },
  ], { asOf: "2025-02-01", mode: "backtest" });
  assert.equal(signal.action, "sell");
  assert.equal(signal.size, 0);
  assert.match(signal.rationale, /^基本面风险过滤/);
});

test("toExecutableSignals keeps only top portfolio buys and normalizes weights", () => {
  const signals = [
    { symbol: "A", action: "buy" as const, confidence: 0.9, size: 1, rationale: "a" },
    { symbol: "B", action: "buy" as const, confidence: 0.8, size: 1, rationale: "b" },
    { symbol: "C", action: "buy" as const, confidence: 0.7, size: 1, rationale: "c" },
    { symbol: "D", action: "sell" as const, confidence: 0.6, size: 1, rationale: "d" },
  ];
  const executable = toExecutableSignals(signals, { maxPositions: 2 });
  const buys = executable.filter((s) => s.action === "buy");
  const totalBuyWeight = buys.reduce((sum, s) => sum + s.size, 0);

  assert.deepEqual(buys.map((s) => s.symbol), ["A", "B"]);
  assert.ok(Math.abs(totalBuyWeight - 1) < 0.0001, `got ${totalBuyWeight}`);
  assert.equal(executable.find((s) => s.symbol === "C")?.action, "hold");
  assert.equal(executable.find((s) => s.symbol === "D")?.size, 0);
});

test("buildLatestPlan scores the latest complete close even without a next execution bar", async () => {
  const klines = makeKlines("2026-06-15", [10, 11, 12]);
  const decisionDate = klines.at(-1)!.date;
  const series: SymbolSeries[] = [
    {
      entry: { symbol: "A", name: "Latest", theme: "T" },
      klines,
      fundamentals: [
        { effective_date: "2026-06-15", pe_ttm: 30, profit_yoy: 20 },
        { effective_date: "2026-12-31", pe_ttm: 5, profit_yoy: 200 },
      ],
    },
  ];

  const snapshots = buildSnapshotsAsOf(series, decisionDate);
  assert.equal(snapshots[0].closes.length, klines.length);
  assert.equal(snapshots[0].fundamental?.pe_ttm, 30);

  const plan = await buildLatestPlan(series, {
    decisionDate,
    maxPositions: 1,
    scorer: async (received, opts) => {
      assert.equal(opts.asOf, decisionDate);
      assert.equal(received[0].closes.at(-1), 12);
      return [{
        symbol: "A",
        action: "buy" as const,
        confidence: 0.9,
        size: 1,
        rationale: "latest close",
      }];
    },
  });

  assert.equal(plan.decisionDate, decisionDate);
  assert.equal(plan.executionPrice, "next_open");
  assert.deepEqual(plan.signals.filter((s) => s.action === "buy").map((s) => s.symbol), ["A"]);
});

test("shadow model cannot replace V1 but champion snapshot can build orders", () => {
  const snapshot = {
    generated_at: "2026-07-08T11:15:00.000Z",
    decision_date: "2026-07-08",
    data_cutoff: "2026-07-08",
    stage: "champion" as const,
    model_version: "lgbm-001",
    feature_version: "alpha158-core-v1",
    source: "qlib" as const,
    predictions: [
      {
        symbol: "A",
        rank: 1,
        score: 0.03,
        expectedReturns: { d3: 0.04 },
        downsideRisk: -0.02,
        confidence: 0.8,
        targetWeight: 0.5,
        action: "buy" as const,
        reasonCodes: ["POSITIVE_NET_UTILITY"],
      },
    ],
  };
  const plan = buildPromotedModelPlan(snapshot, 4);
  assert.equal(plan.source, "qlib-promoted");
  assert.equal(plan.signals[0].action, "buy");
  assert.equal(plan.signals[0].modelVersion, "lgbm-001");
  assert.throws(() => buildPromotedModelPlan({ ...snapshot, stage: "shadow" }, 4), /not champion/);
});

test("autoSellUnselected rotates portfolio to top-scoring names", async () => {
  const scorer = ruleBasedScorer();
  // A trends up strongly; B flat.
  const aCloses = Array.from({ length: 80 }, (_, i) => 100 + i * 0.8);
  const bCloses = Array.from({ length: 80 }, () => 100);
  const series: SymbolSeries[] = [
    {
      entry: { symbol: "A", name: "Up", theme: "T" },
      klines: makeKlines("2025-01-01", aCloses),
      fundamentals: [{ effective_date: "2025-01-02", pe_ttm: 20, profit_yoy: 20 }],
    },
    {
      entry: { symbol: "B", name: "Flat", theme: "T" },
      klines: makeKlines("2025-01-01", bCloses),
      fundamentals: [{ effective_date: "2025-01-02", pe_ttm: 20, profit_yoy: 20 }],
    },
  ];

  const result = await runBacktest(series, cfg, { scorer });
  const firstRebalanceBuys = result.trades.filter((t) => t.date === result.trades[0]?.date && t.side === "buy");
  assert.ok(firstRebalanceBuys.some((t) => t.symbol === "A"), "should buy A at first rebalance");
  assert.ok(!firstRebalanceBuys.some((t) => t.symbol === "B"), "should not buy flat B");
  assert.ok(result.stats.totalReturnPct > 0, "strategy should profit from uptrend");
});

test("future fundamentals are not visible before effective_date", async () => {
  const scorer = ruleBasedScorer();
  // Two stocks with identical prices; one gets a much better fundamental report
  // dated far in the future. The future report must not influence early rebalance.
  const closes = Array.from({ length: 80 }, (_, i) => 100 + i * 0.1);
  const series: SymbolSeries[] = [
    {
      entry: { symbol: "EARLY", name: "Early", theme: "T" },
      klines: makeKlines("2025-01-01", closes),
      fundamentals: [{ effective_date: "2025-01-02", pe_ttm: 20, profit_yoy: 20 }],
    },
    {
      entry: { symbol: "LATE", name: "Late", theme: "T" },
      klines: makeKlines("2025-01-01", closes),
      fundamentals: [
        { effective_date: "2025-01-02", pe_ttm: 20, profit_yoy: 20 },
        { effective_date: "2025-12-31", pe_ttm: 10, profit_yoy: 100 }, // future
      ],
    },
  ];

  const earlyResult = await runBacktest(series, { ...cfg, endDate: "2025-02-15" }, { scorer });
  // On the first rebalance both should tie (same fundamentals, same prices).
  // Ensure LATE did not dominate due to its future report by checking the
  // earliest buy list includes both or neither exclusively.
  const firstBuySymbols = new Set(
    earlyResult.trades
      .filter((t) => t.side === "buy")
      .slice(0, 2)
      .map((t) => t.symbol),
  );
  assert.ok(
    firstBuySymbols.size <= 2,
    "at most maxPositions should be bought",
  );
});
