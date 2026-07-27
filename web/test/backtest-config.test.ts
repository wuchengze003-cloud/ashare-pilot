import { test } from "node:test";
import assert from "node:assert/strict";
import { parseBacktestConfigBody } from "../lib/backtestConfig";

test("valid minimal body gets documented defaults", () => {
  const r = parseBacktestConfigBody({ startDate: "2026-01-01", endDate: "2026-07-01" });
  assert.equal(r.ok, true);
  if (!r.ok) return;
  assert.equal(r.cfg.startCash, 1_000_000);
  assert.equal(r.cfg.feeBps, 10);
  assert.equal(r.cfg.maxPositions, 5);
  assert.equal(r.cfg.decisionEveryNDays, 1);
  assert.equal(r.cfg.minHoldBars, 5);
  assert.equal(r.cfg.autoSellUnselected, true);
  assert.equal(r.cfg.executionPrice, "next_open");
});

test("decisionEveryNDays overrides the legacy rebalanceEveryNDays alias", () => {
  const r = parseBacktestConfigBody({
    startDate: "2026-01-01",
    endDate: "2026-07-01",
    rebalanceEveryNDays: 3,
    decisionEveryNDays: 7,
  });
  assert.equal(r.ok, true);
  if (r.ok) {
    assert.equal(r.cfg.decisionEveryNDays, 7);
    assert.equal(r.cfg.rebalanceEveryNDays, 7);
  }
});

test("rejects non-object bodies", () => {
  for (const body of [null, undefined, 42, "x", [1, 2]]) {
    const r = parseBacktestConfigBody(body);
    assert.equal(r.ok, false, `body=${JSON.stringify(body)}`);
  }
});

test("rejects missing, malformed, and impossible dates", () => {
  assert.equal(parseBacktestConfigBody({ endDate: "2026-07-01" }).ok, false);
  assert.equal(parseBacktestConfigBody({ startDate: "2026-01-01" }).ok, false);
  assert.equal(
    parseBacktestConfigBody({ startDate: "2026-01-01", endDate: "2026-07-01".slice(0, 7) }).ok,
    false,
  );
  assert.equal(
    parseBacktestConfigBody({ startDate: "2026-02-30", endDate: "2026-07-01" }).ok,
    false,
  );
});

test("rejects startDate after endDate", () => {
  const r = parseBacktestConfigBody({ startDate: "2026-07-01", endDate: "2026-01-01" });
  assert.equal(r.ok, false);
  if (!r.ok) assert.match(r.error, /on or before/);
});

test("rejects out-of-range or non-integer numeric knobs", () => {
  const base = { startDate: "2026-01-01", endDate: "2026-07-01" };
  assert.equal(parseBacktestConfigBody({ ...base, startCash: 0 }).ok, false);
  assert.equal(parseBacktestConfigBody({ ...base, startCash: -5 }).ok, false);
  assert.equal(parseBacktestConfigBody({ ...base, feeBps: -1 }).ok, false);
  assert.equal(parseBacktestConfigBody({ ...base, maxPositions: 0 }).ok, false);
  assert.equal(parseBacktestConfigBody({ ...base, maxPositions: 2.5 }).ok, false);
  assert.equal(parseBacktestConfigBody({ ...base, maxPositions: "lots" }).ok, false);
  assert.equal(parseBacktestConfigBody({ ...base, decisionEveryNDays: 0 }).ok, false);
  assert.equal(parseBacktestConfigBody({ ...base, minHoldBars: -1 }).ok, false);
  assert.equal(parseBacktestConfigBody({ ...base, rebalanceThresholdPct: 101 }).ok, false);
  assert.equal(parseBacktestConfigBody({ ...base, autoSellUnselected: "yes" }).ok, false);
});

test("accepts a fully specified valid body", () => {
  const r = parseBacktestConfigBody({
    startDate: "2025-01-01",
    endDate: "2026-07-01",
    startCash: 500_000,
    feeBps: 15,
    maxPositions: 8,
    rebalanceEveryNDays: 5,
    minHoldBars: 3,
    rebalanceThresholdPct: 4,
    sharpeTarget: 2.5,
    autoSellUnselected: false,
    optimizationWindow: "jan_2026",
  });
  assert.equal(r.ok, true);
  if (r.ok) {
    assert.equal(r.cfg.maxPositions, 8);
    assert.equal(r.cfg.decisionEveryNDays, 5);
    assert.equal(r.cfg.autoSellUnselected, false);
    assert.equal(r.cfg.optimizationWindow, "jan_2026");
  }
});
