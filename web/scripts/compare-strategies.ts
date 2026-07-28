// Multi-strategy comparison: runs all registered strategies on the same data
// and outputs a side-by-side performance table.
//
// Usage:
//   cd web && npx tsx scripts/compare-strategies.ts
//
// Env overrides:
//   DASHBOARD_START=2026-02-24  DASHBOARD_END=2026-07-25
import fs from "node:fs";
import path from "node:path";
import {
  loadStrategyEntries,
} from "../lib/universe";
import { runBacktest, type BacktestConfig } from "../lib/backtest";
import { STRATEGIES } from "../lib/strategyRegistry";
import { toCostConfig, roundTripBps } from "../lib/costConfig";
import {
  buildSymbolSeries,
  buildSymbolSeriesFromPyserverCache,
} from "../lib/dashboardData";

function shanghaiToday(): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}

async function main() {
  const startDate = process.env.DASHBOARD_START ?? "2026-02-24";
  const endDate = process.env.DASHBOARD_END ?? shanghaiToday();
  const cacheDir = path.resolve(process.cwd(), process.env.DASHBOARD_CACHE ?? ".cache/datasource");
  const pyserverCacheDb = path.resolve(process.cwd(), process.env.PYSERVER_CACHE_DB ?? "../pyserver/cache.db");

  const universe = loadStrategyEntries();
  console.log(`Universe: ${universe.length} entries`);
  console.log(`Window: ${startDate} → ${endDate}`);
  console.log(`Strategies: ${STRATEGIES.map((s) => s.name).join(", ")}\n`);

  const { series } = fs.existsSync(cacheDir)
    ? buildSymbolSeries(universe, cacheDir)
    : buildSymbolSeriesFromPyserverCache(universe, pyserverCacheDb);
  console.log(`Price series: ${series.length}\n`);

  if (series.length === 0) {
    console.error("No price series available. Run pyserver or populate cache first.");
    process.exit(1);
  }

  const unifiedCost = toCostConfig();
  const baseCfg: BacktestConfig = {
    startCash: 1_000_000,
    rebalanceEveryNDays: 1,
    decisionEveryNDays: 1,
    executionPrice: "next_open",
    startDate,
    endDate,
    feeBps: roundTripBps() / 2,  // legacy per-side field, derived from cost model
    costConfig: unifiedCost,
    maxPositions: 4,
    autoSellUnselected: true,
    minHoldBars: 5,
    rebalanceThresholdPct: 5,
    sharpeTarget: 3,
    optimizationWindow: "post_cny_2026",
  };

  interface StrategyResult {
    name: string;
    codename: string;
    totalReturnPct: number;
    cagrPct: number;
    maxDrawdownPct: number;
    sharpe: number;
    trades: number;
    roundTrips: number;
    roundTripWinRatePct: number;
    avgRoundTripPnlPct: number;
  }

  const results: StrategyResult[] = [];

  for (const strategy of STRATEGIES) {
    console.log(`Running ${strategy.name} (${strategy.codename})...`);
    const scorer = strategy.createScorer({ minScoreToBuy: strategy.defaultMinScore });
    const result = await runBacktest(series, baseCfg, { scorer });
    results.push({
      name: strategy.name,
      codename: strategy.codename,
      totalReturnPct: result.stats.totalReturnPct,
      cagrPct: result.stats.cagrPct,
      maxDrawdownPct: result.stats.maxDrawdownPct,
      sharpe: result.stats.sharpe,
      trades: result.stats.trades,
      roundTrips: result.stats.roundTrips ?? 0,
      roundTripWinRatePct: result.stats.roundTripWinRatePct ?? 0,
      avgRoundTripPnlPct: result.stats.avgRoundTripPnlPct ?? 0,
    });
    console.log(`  Sharpe: ${result.stats.sharpe.toFixed(2)}  Return: ${result.stats.totalReturnPct.toFixed(1)}%  MaxDD: ${result.stats.maxDrawdownPct.toFixed(1)}%`);
  }

  // Print comparison table
  console.log("\n" + "=".repeat(100));
  console.log("策略对比 (Strategy Comparison)");
  console.log("=".repeat(100));
  const header = [
    "策略",
    "总收益%",
    "年化%",
    "最大回撤%",
    "夏普",
    "订单数",
    "完整交易",
    "交易胜率%",
    "平均盈亏%",
  ].join("\t");
  console.log(header);
  console.log("-".repeat(100));
  for (const r of results) {
    console.log(
      [
        `${r.name}(${r.codename})`,
        r.totalReturnPct.toFixed(2),
        r.cagrPct.toFixed(2),
        r.maxDrawdownPct.toFixed(2),
        r.sharpe.toFixed(2),
        r.trades.toString(),
        r.roundTrips.toString(),
        r.roundTripWinRatePct.toFixed(1),
        r.avgRoundTripPnlPct.toFixed(2),
      ].join("\t"),
    );
  }
  console.log("=".repeat(100));

  // Write JSON output for frontend consumption
  const output = {
    generated_at: new Date().toISOString(),
    window: { startDate, endDate },
    strategies: results,
  };
  const outDir = path.resolve(process.cwd(), "data/runtime");
  fs.mkdirSync(outDir, { recursive: true });
  fs.writeFileSync(
    path.join(outDir, "strategy-comparison.json"),
    JSON.stringify(output, null, 2),
  );
  console.log(`\nWrote comparison to data/runtime/strategy-comparison.json`);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
