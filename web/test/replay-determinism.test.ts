// Replay-determinism guard (merge blocker 1).
//
// Re-running a backtest or scorer with as_of = T1 must produce identical
// signals, orders, positions and equity whether or not the input series
// contains data after T1 — including a corporate action (price halved,
// volume doubled) that arrives later. If a future change lets latest data
// leak past the as_of cut, this test fails.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";

// Cache backend writes under cwd/.cache; sandbox it.
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "scc-replay-"));
process.chdir(tmp);

import type { Kline, MoneyflowRow } from "../lib/pyserver";
import { runBacktest, type BacktestConfig, type SymbolSeries } from "../lib/backtest";
import { ruleBasedScorer } from "../lib/dashboardBacktest";
import { tideScorer } from "../lib/strategies/tide";

const TOTAL_DAYS = 120;
const T1_INDEX = 99;
const TRUNCATED_DAYS = 110;
const SPLIT_INDEX = 112; // corporate action strictly after T1
const EQUITY_TOLERANCE = 1e-9;

function makeKlines(numDays: number, base: number, step: number): Kline[] {
  const d = new Date("2025-01-01T00:00:00Z");
  const rows: Kline[] = [];
  for (let i = 0; i < numDays; i++) {
    while (d.getUTCDay() === 0 || d.getUTCDay() === 6) d.setUTCDate(d.getUTCDate() + 1);
    const date = d.toISOString().slice(0, 10);
    d.setUTCDate(d.getUTCDate() + 1);
    const split = i >= SPLIT_INDEX;
    const price = (base + i * step) * (split ? 0.5 : 1);
    rows.push({
      date,
      open: price,
      high: price * 1.01,
      low: price * 0.99,
      close: price,
      volume: 1_000_000 * (split ? 2 : 1),
    });
  }
  return rows;
}

function makeSeries(numDays: number): SymbolSeries[] {
  return [
    { entry: { symbol: "A", name: "Up", theme: "T" }, klines: makeKlines(numDays, 100, 0.5) },
    { entry: { symbol: "B", name: "Flat", theme: "T" }, klines: makeKlines(numDays, 100, 0.05) },
  ];
}

function asOfDate(): string {
  // The date of the T1 bar in the untruncated series (identical prefix).
  return makeKlines(TOTAL_DAYS, 100, 0.5)[T1_INDEX].date;
}

const cfg: BacktestConfig = {
  startCash: 1_000_000,
  rebalanceEveryNDays: 5,
  startDate: "2025-01-01",
  endDate: asOfDate(),
  feeBps: 0,
  maxPositions: 5,
};

test("runBacktest replay: trades identical when later data (incl. corporate action) exists", async () => {
  const early = await runBacktest(makeSeries(TRUNCATED_DAYS), cfg);
  const late = await runBacktest(makeSeries(TOTAL_DAYS), cfg);

  const strip = (t: (typeof early.trades)[number]) => ({
    symbol: t.symbol,
    side: t.side,
    shares: t.shares,
    decisionDate: t.decisionDate,
    tradeDate: t.tradeDate,
  });
  assert.deepEqual(
    early.trades.map(strip),
    late.trades.map(strip),
    "discrete orders (symbol/side/shares/dates) must be identical under replay",
  );
});

test("runBacktest replay: signals identical when later data exists", async () => {
  const early = await runBacktest(makeSeries(TRUNCATED_DAYS), cfg);
  const late = await runBacktest(makeSeries(TOTAL_DAYS), cfg);
  assert.deepEqual(
    early.signalsByDate,
    late.signalsByDate,
    "discrete signals must be identical under replay",
  );
});

test("runBacktest replay: equity curve identical within tolerance", async () => {
  const early = await runBacktest(makeSeries(TRUNCATED_DAYS), cfg);
  const late = await runBacktest(makeSeries(TOTAL_DAYS), cfg);
  assert.equal(early.equityCurve.length, late.equityCurve.length);
  for (let i = 0; i < early.equityCurve.length; i++) {
    const a = early.equityCurve[i];
    const b = late.equityCurve[i];
    assert.equal(a.date, b.date);
    const scale = Math.max(Math.abs(a.equity), 1);
    assert.ok(
      Math.abs(a.equity - b.equity) / scale <= EQUITY_TOLERANCE,
      `equity diverged at ${a.date}: ${a.equity} vs ${b.equity}`,
    );
    assert.deepEqual(a.positions, b.positions, `positions diverged at ${a.date}`);
  }
});

test("ruleBasedScorer sees only bars at or before asOf", async () => {
  const full = makeSeries(TOTAL_DAYS);
  const truncated = makeSeries(TRUNCATED_DAYS);
  const asOf = asOfDate();
  const scorer = ruleBasedScorer();

  const snapshotsFrom = (series: SymbolSeries[]) =>
    series.map((s) => {
      const upto = s.klines.filter((k) => k.date <= asOf);
      return {
        symbol: s.entry.symbol,
        name: s.entry.name,
        theme: s.entry.theme,
        closes: upto.map((k) => k.close),
        volumes: upto.map((k) => k.volume),
      };
    });

  const sigEarly = await scorer(snapshotsFrom(truncated), { asOf, mode: "backtest" });
  const sigLate = await scorer(snapshotsFrom(full), { asOf, mode: "backtest" });
  assert.deepEqual(sigEarly, sigLate, "scorer signals must not depend on data after asOf");
});

test("tideScorer moneyflow replay: rows after asOf never reach the scorer", async () => {
  const asOf = asOfDate();
  const klines = makeKlines(TOTAL_DAYS, 100, 0.5);
  const makeFlow = (numDays: number): MoneyflowRow[] =>
    klines.slice(0, numDays).map((k, i) => ({
      trade_date: k.date.replaceAll("-", ""),
      net_mf_amount: 100 + i,
      buy_lg_amount: 500 + i,
      sell_lg_amount: 400 - i,
      buy_elg_amount: 50 + i,
      sell_elg_amount: 40 - i,
    }));

  const snapshots = [
    {
      symbol: "A",
      name: "Up",
      theme: "T",
      closes: klines.slice(0, T1_INDEX + 1).map((k) => k.close),
      volumes: klines.slice(0, T1_INDEX + 1).map((k) => k.volume),
    },
  ];
  const early = await tideScorer({
    moneyflowData: { A: makeFlow(TRUNCATED_DAYS) },
    moneyflowStatus: "available",
  })(snapshots, { asOf, mode: "backtest" });
  const late = await tideScorer({
    moneyflowData: { A: makeFlow(TOTAL_DAYS) },
    moneyflowStatus: "available",
  })(snapshots, { asOf, mode: "backtest" });
  assert.deepEqual(early, late, "tide signals must not depend on moneyflow rows after asOf");
});
