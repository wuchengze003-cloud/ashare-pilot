import { test } from "node:test";
import assert from "node:assert/strict";

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  buildSignalHistorySnapshot,
  readSignalHistorySnapshots,
  writeSignalHistorySnapshot,
} from "../lib/signalHistory";
import { readRuntimeJson } from "../lib/runtimeData";
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

test("signal history stores the decision-day close and never a future price", () => {
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

test("per-strategy history writes do not overwrite flat active history", () => {
  const oldRuntimeDir = process.env.RUNTIME_DATA_DIR;
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "signal-history-"));
  process.env.RUNTIME_DATA_DIR = tempDir;
  try {
    const active = buildSignalHistorySnapshot(
      {
        decisionDate: "2026-06-25",
        executionPrice: "next_open",
        source: "dashboard-latest-close",
        scoreModel: "dashboard-rule",
        maxPositions: 4,
        signals: [{ symbol: "A", action: "buy", confidence: 0.9, size: 0.25, rationale: "active" }],
      },
      series,
    );
    const comparison = {
      ...active,
      strategy_id: "prism",
      signals: [{ symbol: "A", action: "sell" as const, confidence: 0.1, size: 0, rationale: "comparison" }],
    };

    writeSignalHistorySnapshot(active, "momentum-v1");
    writeSignalHistorySnapshot(comparison, "prism", { writeFlat: false });

    const flat = readRuntimeJson<typeof active>("signals-history/2026-06-25.json");
    const prism = readRuntimeJson<typeof comparison>("strategies/prism/history/2026-06-25.json");
    assert.equal(flat?.strategy_id, "momentum-v1");
    assert.equal(prism?.strategy_id, "prism");
    assert.equal(flat?.signals[0]?.rationale, "active");
    assert.equal(prism?.signals[0]?.rationale, "comparison");
  } finally {
    if (oldRuntimeDir === undefined) {
      delete process.env.RUNTIME_DATA_DIR;
    } else {
      process.env.RUNTIME_DATA_DIR = oldRuntimeDir;
    }
    fs.rmSync(tempDir, { recursive: true, force: true });
  }
});

test("strategy history reads only self-identifying snapshots from its own directory", () => {
  const oldRuntimeDir = process.env.RUNTIME_DATA_DIR;
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "signal-history-read-"));
  process.env.RUNTIME_DATA_DIR = tempDir;
  try {
    const momentum = buildSignalHistorySnapshot(plan, series, "momentum-v1");
    const prism = buildSignalHistorySnapshot(plan, series, "prism");
    writeSignalHistorySnapshot(momentum, "momentum-v1", { writeFlat: false });
    writeSignalHistorySnapshot(prism, "prism", { writeFlat: false });

    assert.deepEqual(
      readSignalHistorySnapshots(Number.POSITIVE_INFINITY, "momentum-v1").map((row) => row.strategy_id),
      ["momentum-v1"],
    );
    assert.deepEqual(
      readSignalHistorySnapshots(Number.POSITIVE_INFINITY, "prism").map((row) => row.strategy_id),
      ["prism"],
    );
    assert.throws(
      () => writeSignalHistorySnapshot(momentum, "prism", { writeFlat: false }),
      /strategy mismatch/,
    );
  } finally {
    if (oldRuntimeDir === undefined) {
      delete process.env.RUNTIME_DATA_DIR;
    } else {
      process.env.RUNTIME_DATA_DIR = oldRuntimeDir;
    }
    fs.rmSync(tempDir, { recursive: true, force: true });
  }
});

test("published per-strategy history is immutable", () => {
  const oldRuntimeDir = process.env.RUNTIME_DATA_DIR;
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "signal-history-immutable-"));
  process.env.RUNTIME_DATA_DIR = tempDir;
  try {
    const first = buildSignalHistorySnapshot(plan, series, "momentum-v1");
    writeSignalHistorySnapshot(first, "momentum-v1", { writeFlat: false });
    writeSignalHistorySnapshot(
      { ...first, generated_at: "2099-01-01T00:00:00.000Z" },
      "momentum-v1",
      { writeFlat: false },
    );

    const changed = {
      ...first,
      signals: first.signals.map((signal) => (
        signal.symbol === "A" ? { ...signal, action: "sell" as const, size: 0 } : signal
      )),
    };
    assert.throws(
      () => writeSignalHistorySnapshot(changed, "momentum-v1", { writeFlat: false }),
      /immutable signal history conflict/,
    );
    const stored = readSignalHistorySnapshots(1, "momentum-v1")[0];
    assert.equal(stored.signals.find((signal) => signal.symbol === "A")?.action, "buy");
  } finally {
    if (oldRuntimeDir === undefined) {
      delete process.env.RUNTIME_DATA_DIR;
    } else {
      process.env.RUNTIME_DATA_DIR = oldRuntimeDir;
    }
    fs.rmSync(tempDir, { recursive: true, force: true });
  }
});
