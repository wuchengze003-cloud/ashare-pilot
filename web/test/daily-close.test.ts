import assert from "node:assert/strict";
import test from "node:test";
import { parseSymbolList, validateDailyCloseData } from "../lib/dailyClose";

function validInput() {
  return {
    expectedDate: "2026-06-29",
    expectedUniverseCount: 2,
    benchmarkLatestDate: "2026-06-29",
    series: [
      { symbol: "000001", latestDate: "2026-06-29" },
      { symbol: "600000", latestDate: "2026-06-29" },
    ],
    backtest: {
      snapshot_basis: "latest-complete-close" as const,
      latestDate: "2026-06-29",
      latestPlan: { decisionDate: "2026-06-29", signals: [] },
      equityCurve: [{ date: "2026-06-29", equity: 1, cash: 1, positions: {} }],
    } as never,
    signals: { signal_date: "2026-06-29", latest_complete_date: "2026-06-29", signals: [] },
    meta: { universe_count: 2 },
    analystItems: [
      { symbol: "000001", current_price: 10, current_price_as_of: "2026-06-29T15:01:00" },
      { symbol: "600000", current_price: 11, current_price_as_of: "2026-06-29T15:01:00" },
    ],
  };
}

test("daily close validation accepts a complete same-day snapshot", () => {
  assert.deepEqual(validateDailyCloseData(validInput()), []);
});

test("daily close validation rejects stale market and signal data", () => {
  const input = validInput();
  input.benchmarkLatestDate = "2026-06-26";
  input.series[1].latestDate = "2026-06-26";
  input.signals.signal_date = "2026-06-26";
  const codes = validateDailyCloseData(input).map((issue) => issue.code);
  assert.ok(codes.includes("BENCHMARK_DATE_MISMATCH"));
  assert.ok(codes.includes("STALE_PRICE_SERIES"));
  assert.ok(codes.includes("SIGNAL_DATE_MISMATCH"));
});

test("configured suspended symbols may keep an older last bar", () => {
  const input = validInput();
  input.series[1].latestDate = "2026-06-26";
  assert.deepEqual(validateDailyCloseData({ ...input, allowedStaleSymbols: ["600000"] }), []);
});

test("symbol allowlist parsing is trimmed and deduplicated", () => {
  assert.deepEqual(parseSymbolList(" 600000,000001,600000,,"), ["600000", "000001"]);
});
