import assert from "node:assert/strict";
import test from "node:test";
import {
  buildDailyCloseProductionReceipt,
  dailyCloseReceiptMatchesProduction,
  isShortHistoryStrategySeries,
  parseSymbolList,
  validateDailyCloseData,
  validateSignalsEndpointBody,
  type DailyCloseValidationInput,
} from "../lib/dailyClose";

function validInput(): DailyCloseValidationInput {
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
    production: {
      gate: {
        schema_version: 2,
        generated_at: "2026-06-29T08:00:00.000Z",
        status: "cash-only",
        champion_id: null,
        contract_sha256: "contract",
        contract_version: "3.0.0",
        feature_version: "5",
        panel_start: "2018-04-02",
        panel_end: "2026-07-27",
        complete_daily_trading_days: 2077,
        daily_status: "daily-complete",
        minute_status: "blocked_minute_data_coverage",
        minute_coverage_pct: 3.69,
        required_symbol_days: 100,
        available_symbol_days: 4,
        reason_codes: ["MINUTE_DATA_INCOMPLETE"],
        message: "cash only",
        candidates: [],
      },
      signals: {
        schema_version: 2,
        generated_at: "2026-06-29T08:00:00.000Z",
        gate_generated_at: "2026-06-29T08:00:00.000Z",
        contract_sha256: "contract",
        status: "cash-only",
        champion_id: null,
        signal_date: "2026-06-29",
        latest_complete_date: "2026-06-29",
        signal_basis: "latest-complete-close",
        reason_codes: ["MINUTE_DATA_INCOMPLETE"],
        signals: [],
      },
    },
    analystItems: [
      { symbol: "000001", current_price: 10, current_price_as_of: "2026-06-29T15:01:00" },
      { symbol: "600000", current_price: 11, current_price_as_of: "2026-06-29T15:01:00" },
    ],
  };
}

test("daily close validation accepts a complete same-day snapshot", () => {
  assert.deepEqual(validateDailyCloseData(validInput()), []);
});

test("daily close validation can allow short-history new listings", () => {
  const input = validInput();
  input.expectedUniverseCount = 3;
  input.expectedSymbols = ["000001", "600000", "688825"];
  input.meta!.universe_count = 3;
  input.analystItems.push({
    symbol: "688825",
    current_price: 49,
    current_price_as_of: "2026-06-29T15:01:00",
  });

  assert.deepEqual(validateDailyCloseData(input).map((issue) => issue.code), ["MISSING_PRICE_SERIES"]);
  assert.deepEqual(
    validateDailyCloseData({ ...input, allowedMissingSeriesSymbols: ["688825"] }),
    [],
  );
});

test("daily close validation rejects stale market and signal data", () => {
  const input = validInput();
  input.benchmarkLatestDate = "2026-06-26";
  input.series[1].latestDate = "2026-06-26";
  input.signals!.signal_date = "2026-06-26";
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

test("short-history strategy series requires current cached data", () => {
  assert.equal(
    isShortHistoryStrategySeries(
      { strategy_from: "2026-07-27" },
      "2026-07-28",
      { latestDate: "2026-07-28", uniqueDates: 2 },
    ),
    true,
  );
  assert.equal(
    isShortHistoryStrategySeries(
      { strategy_from: "2026-07-27" },
      "2026-07-28",
      { latestDate: "2026-07-27", uniqueDates: 2 },
    ),
    false,
  );
  assert.equal(
    isShortHistoryStrategySeries(
      {},
      "2026-07-28",
      { latestDate: "2026-07-28", uniqueDates: 2 },
    ),
    false,
  );
  assert.equal(
    isShortHistoryStrategySeries(
      { strategy_from: "2026-07-27" },
      "2026-07-28",
      { latestDate: "2026-07-28", uniqueDates: 30 },
    ),
    false,
  );
});

test("symbol allowlist parsing is trimmed and deduplicated", () => {
  assert.deepEqual(parseSymbolList(" 600000,000001,600000,,"), ["600000", "000001"]);
});

test("daily close validation rejects production signals from the wrong date", () => {
  const input = validInput();
  input.production.signals!.signal_date = "2026-06-26";
  const codes = validateDailyCloseData(input).map((issue) => issue.code);
  assert.ok(codes.includes("PRODUCTION_SIGNAL_DATE_MISMATCH"));
});

test("signals endpoint validation accepts cash-only only when it is empty", () => {
  assert.deepEqual(
    validateSignalsEndpointBody({
      status: "cash-only",
      champion_id: null,
      signal_date: "2026-06-29",
      latest_complete_date: "2026-06-29",
      signals: [],
    }, "2026-06-29"),
    [],
  );

  const codes = validateSignalsEndpointBody({
    status: "cash-only",
    champion_id: null,
    signal_date: "2026-06-29",
    latest_complete_date: "2026-06-29",
    signals: [{}],
  }, "2026-06-29").map((issue) => issue.code);
  assert.ok(codes.includes("PRODUCTION_ENDPOINT_CASH_HAS_SIGNALS"));
});

test("active production endpoint may validly publish no trades", () => {
  assert.deepEqual(
    validateSignalsEndpointBody({
      status: "active",
      champion_id: "prism-v3",
      signal_date: "2026-06-29",
      latest_complete_date: "2026-06-29",
      signals: [],
    }, "2026-06-29"),
    [],
  );
});

test("daily-close receipt is bound to the exact production snapshots", () => {
  const input = validInput().production;
  const receipt = buildDailyCloseProductionReceipt(input);
  assert.ok(receipt);
  assert.equal(
    dailyCloseReceiptMatchesProduction(receipt, input.gate!, input.signals),
    true,
  );

  input.signals!.generated_at = "2026-06-29T08:01:00.000Z";
  assert.equal(
    dailyCloseReceiptMatchesProduction(receipt, input.gate!, input.signals),
    false,
  );
});
