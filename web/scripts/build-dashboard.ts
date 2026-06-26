// Build ignored runtime dashboard snapshots.
//
// Daily entry:
//   cd web && npm run dashboard:update
//
// Env overrides:
//   DASHBOARD_START=2026-02-24  DASHBOARD_END=2026-06-12
//   DASHBOARD_INTRADAY=1        # explicitly mark a pre-close run as intraday
//   DASHBOARD_DECISION_EVERY=1  DASHBOARD_MAX_POSITIONS=4
//   DASHBOARD_MIN_HOLD_BARS=5   DASHBOARD_REBALANCE_THRESHOLD_PCT=5
//   DASHBOARD_MIN_SCORE_TO_BUY=0.54
//   DASHBOARD_OPTIMIZE=1        # diagnostic only; daily runs keep fixed params by default
//   DASHBOARD_CACHE=.cache/datasource
import fs from "node:fs";
import path from "node:path";
import { loadEntries } from "../lib/universe";
import { runBacktest, type BacktestConfig, type BacktestResult } from "../lib/backtest";
import { ruleBasedScorer } from "../lib/dashboardBacktest";
import { optimizeBacktest, type OptimizationResult } from "../lib/backtestOptimization";
import { buildLatestPlan, type LatestPlan } from "../lib/latestPlan";
import { writeRuntimeJson } from "../lib/runtimeData";
import {
  buildSignalHistorySnapshot,
  readSignalHistorySnapshots,
  writeSignalHistorySnapshot,
} from "../lib/signalHistory";
import { assertRuntimeArtifacts, readRuntimeValidationInput } from "../lib/runtimeValidation";
import {
  buildSymbolSeries,
  buildSymbolSeriesFromPyserverCache,
  type PriceRow,
} from "../lib/dashboardData";

function shanghaiNowParts() {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    hour12: false,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).formatToParts(new Date());
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "00";
  return {
    date: `${get("year")}-${get("month")}-${get("day")}`,
    hour: Number(get("hour")),
    minute: Number(get("minute")),
  };
}

function beforeShanghaiClose(): boolean {
  const now = shanghaiNowParts();
  return now.hour < 15;
}

function latestAvailableDate(dates: string[], requestedEndDate: string): string {
  const today = shanghaiNowParts().date;
  const filtered = dates
    .filter((d) => d <= requestedEndDate)
    .filter((d) => (beforeShanghaiClose() ? d < today : true))
    .sort();
  return filtered.at(-1) ?? requestedEndDate;
}

const today = shanghaiNowParts().date;
const explicitEndDate = Boolean(process.env.DASHBOARD_END);
const startDate = process.env.DASHBOARD_START ?? "2026-02-24";
const requestedEndDate = process.env.DASHBOARD_END ?? today;
const intradayOverride = process.env.DASHBOARD_INTRADAY;
const isIntradaySnapshot =
  intradayOverride === "1" ||
  (intradayOverride !== "0" && explicitEndDate && requestedEndDate === today && beforeShanghaiClose());
const snapshotBasis = isIntradaySnapshot ? "intraday-midday" : "latest-complete-close";
const snapshotLabel = isIntradaySnapshot ? "午盘快照" : "完整收盘";
const decisionEveryNDays = Number(process.env.DASHBOARD_DECISION_EVERY ?? process.env.DASHBOARD_REBALANCE ?? 1);
const maxPositions = Number(process.env.DASHBOARD_MAX_POSITIONS ?? 4);
const minHoldBars = Number(process.env.DASHBOARD_MIN_HOLD_BARS ?? 5);
const rebalanceThresholdPct = Number(process.env.DASHBOARD_REBALANCE_THRESHOLD_PCT ?? 5);
const minScoreToBuy = Number(process.env.DASHBOARD_MIN_SCORE_TO_BUY ?? 0.54);
const shouldOptimize = process.env.DASHBOARD_OPTIMIZE === "1";
const cacheDir = path.resolve(process.cwd(), process.env.DASHBOARD_CACHE ?? ".cache/datasource");
const pyserverCacheDb = path.resolve(process.cwd(), process.env.PYSERVER_CACHE_DB ?? "../pyserver/cache.db");

interface DashboardOutput {
  generated_at: string;
  snapshot_basis?: "latest-complete-close" | "intraday-midday";
  snapshot_label?: string;
  config: BacktestConfig;
  stats: BacktestResult["stats"];
  equityCurve: BacktestResult["equityCurve"];
  benchmarkCurve: Array<{ date: string; equity: number }>;
  trades: BacktestResult["trades"];
  themePerformance: Array<{
    theme: string;
    returnPct: number;
    realizedPct: number;
    unrealizedPct: number;
    allocationDays: number;
    avgWeightPct: number;
  }>;
  signalsByDate: BacktestResult["signalsByDate"];
  latestPlan: LatestPlan;
  latestHoldings: BacktestResult["equityCurve"][number]["positions"];
  latestDate: string;
  meetsSharpeTarget?: boolean;
  primaryWindow?: string;
  validationStats?: OptimizationResult["validationStats"];
  optimizedParams?: OptimizationResult["optimizedParams"];
  optimizationWarnings?: string[];
  optimizationCandidates?: OptimizationResult["candidates"];
}

function computeBenchmarkCurve(benchmark: PriceRow[], cfg: BacktestConfig) {
  const sorted = [...benchmark].sort((a, b) => (a.date < b.date ? -1 : 1));
  const inWindow = sorted.filter((r) => r.date >= cfg.startDate && r.date <= cfg.endDate);
  if (inWindow.length === 0) return [];
  const startCash = cfg.startCash;
  const startPrice = inWindow[0].close;
  const shares = startCash / startPrice;
  return inWindow.map((r) => ({ date: r.date, equity: shares * r.close }));
}

function computeThemePerformance(
  result: BacktestResult,
  series: Array<{ entry: { symbol: string; theme: string } }>,
): DashboardOutput["themePerformance"] {
  const themeMap = new Map(series.map((s) => [s.entry.symbol, s.entry.theme]));
  const realized: Record<string, number> = {};
  const unrealized: Record<string, number> = {};
  const allocationDays: Record<string, number> = {};
  const weights: Record<string, number> = {};

  // Track cost basis per symbol using FIFO so we can compute both realized and
  // unrealized P&L per theme.
  const positions: Record<string, { shares: number; cost: number }> = {};
  for (const t of result.trades) {
    const theme = themeMap.get(t.symbol) ?? "未分类";
    realized[theme] ??= 0;
    positions[t.symbol] ??= { shares: 0, cost: 0 };
    if (t.side === "buy") {
      positions[t.symbol].shares += t.shares;
      positions[t.symbol].cost += t.shares * t.price;
    } else {
      const pos = positions[t.symbol];
      if (pos.shares > 0) {
        const avgCost = pos.cost / pos.shares;
        const sold = Math.min(t.shares, pos.shares);
        realized[theme] += sold * (t.price - avgCost);
        pos.cost -= sold * avgCost;
        pos.shares -= sold;
      }
    }
  }

  // Unrealized P&L for positions still open at the end of the backtest.
  const lastBar = result.equityCurve[result.equityCurve.length - 1];
  for (const [sym, pos] of Object.entries(positions)) {
    if (pos.shares <= 0) continue;
    const theme = themeMap.get(sym) ?? "未分类";
    const latestPrice = lastBar.positions[sym]?.price;
    if (latestPrice === undefined) continue;
    const avgCost = pos.cost / pos.shares;
    unrealized[theme] ??= 0;
    unrealized[theme] += pos.shares * (latestPrice - avgCost);
  }

  // Average allocation weight per theme.
  for (const bar of result.equityCurve) {
    const totalEquity = bar.equity || 1;
    for (const [sym, pos] of Object.entries(bar.positions)) {
      const theme = themeMap.get(sym) ?? "未分类";
      allocationDays[theme] ??= 0;
      weights[theme] ??= 0;
      allocationDays[theme] += 1;
      weights[theme] += (pos.shares * pos.price) / totalEquity;
    }
  }

  const allThemes = new Set([
    ...Object.keys(realized),
    ...Object.keys(unrealized),
    ...Object.keys(allocationDays),
  ]);
  return [...allThemes].map((theme) => {
    const r = realized[theme] ?? 0;
    const u = unrealized[theme] ?? 0;
    return {
      theme,
      returnPct: (r + u) / (result.config.startCash || 1) * 100,
      realizedPct: r / (result.config.startCash || 1) * 100,
      unrealizedPct: u / (result.config.startCash || 1) * 100,
      allocationDays: allocationDays[theme] ?? 0,
      avgWeightPct: allocationDays[theme] ? (weights[theme] / allocationDays[theme]) * 100 : 0,
    };
  });
}

async function main() {
  const universe = loadEntries();
  console.log(`Loaded ${universe.length} universe entries`);

  const source = fs.existsSync(cacheDir)
    ? `data-source CSVs at ${cacheDir}`
    : `pyserver SQLite cache at ${pyserverCacheDb}`;
  const { series, benchmark } = fs.existsSync(cacheDir)
    ? buildSymbolSeries(universe, cacheDir)
    : buildSymbolSeriesFromPyserverCache(universe, pyserverCacheDb);
  console.log(`Built ${series.length} price series, benchmark ${benchmark.length} bars from ${source}`);

  if (series.length === 0) {
    console.error("No usable price series found. Check cached CSVs or pyserver cache.");
    process.exit(1);
  }
  const allDates = [...new Set(series.flatMap((s) => s.klines.map((k) => k.date)))].sort();
  const endDate = explicitEndDate ? requestedEndDate : latestAvailableDate(allDates, requestedEndDate);

  const cfg: BacktestConfig = {
    startCash: 1_000_000,
    rebalanceEveryNDays: decisionEveryNDays,
    decisionEveryNDays,
    executionPrice: "next_open",
    startDate,
    endDate,
    feeBps: 10,
    maxPositions,
    autoSellUnselected: true,
    minHoldBars,
    rebalanceThresholdPct,
    sharpeTarget: 3,
    optimizationWindow: "post_cny_2026",
  };
  const historicalSignalsByDate = Object.fromEntries(
    readSignalHistorySnapshots(Number.POSITIVE_INFINITY)
      .filter((snapshot) => snapshot.signal_date < endDate)
      .map((snapshot) => [snapshot.signal_date, snapshot.signals]),
  );
  const runOptions = {
    scorer: ruleBasedScorer({ minScoreToBuy }),
    historicalSignalsByDate,
  };

  const optimized = shouldOptimize
    ? await optimizeBacktest(series, cfg)
    : {
      result: await runBacktest(series, cfg, runOptions),
      optimization: null,
    };
  const result = optimized.result;
  const benchmarkCurve = computeBenchmarkCurve(benchmark, result.config);

  const lastBar = result.equityCurve[result.equityCurve.length - 1];
  const effectiveMinScoreToBuy = optimized.optimization?.optimizedParams.minScoreToBuy ?? minScoreToBuy;
  const latestPlan = await buildLatestPlan(series, {
    decisionDate: lastBar.date,
    scorer: ruleBasedScorer({ minScoreToBuy: effectiveMinScoreToBuy }),
    maxPositions: result.config.maxPositions,
    minScoreToBuy: effectiveMinScoreToBuy,
  });
  const output: DashboardOutput = {
    generated_at: new Date().toISOString(),
    snapshot_basis: snapshotBasis,
    snapshot_label: snapshotLabel,
    config: result.config,
    stats: result.stats,
    equityCurve: result.equityCurve,
    benchmarkCurve,
    trades: result.trades,
    themePerformance: computeThemePerformance(result, series),
    signalsByDate: result.signalsByDate,
    latestPlan,
    latestHoldings: lastBar.positions,
    latestDate: lastBar.date,
    meetsSharpeTarget: result.stats.sharpe >= (result.config.sharpeTarget ?? 3),
    primaryWindow: "post_cny_2026",
    validationStats: optimized.optimization?.validationStats,
    optimizedParams: optimized.optimization?.optimizedParams ?? {
      maxPositions: result.config.maxPositions,
      minHoldBars: result.config.minHoldBars ?? 0,
      rebalanceThresholdPct: result.config.rebalanceThresholdPct ?? 0,
      minScoreToBuy: effectiveMinScoreToBuy,
    },
    optimizationWarnings: optimized.optimization?.warnings ?? [],
    optimizationCandidates: optimized.optimization?.candidates ?? [],
  };

  writeRuntimeJson("backtest.json", output);
  const signalsOutput = {
    generated_at: new Date().toISOString(),
    source: latestPlan.source,
    score_model: latestPlan.scoreModel,
    signal_date: latestPlan.decisionDate,
    latest_complete_date: latestPlan.decisionDate,
    signal_basis: snapshotBasis,
    snapshot_label: snapshotLabel,
    max_positions: latestPlan.maxPositions,
    optimized_params: optimized.optimization?.optimizedParams ?? {
      maxPositions: result.config.maxPositions,
      minHoldBars: result.config.minHoldBars ?? 0,
      rebalanceThresholdPct: result.config.rebalanceThresholdPct ?? 0,
      minScoreToBuy: effectiveMinScoreToBuy,
    },
    universe_count: universe.length,
    scored_count: latestPlan.signals.length,
    skipped_count: Math.max(0, universe.length - latestPlan.signals.length),
    fundamentals: [],
    signals: latestPlan.signals,
  };
  writeRuntimeJson("signals.json", signalsOutput);
  writeSignalHistorySnapshot(buildSignalHistorySnapshot(latestPlan, series));
  writeRuntimeJson("meta.json", {
    generated_at: new Date().toISOString(),
    universe_count: universe.length,
  });
  assertRuntimeArtifacts(readRuntimeValidationInput());
  console.log("Wrote runtime backtest/signals/history/meta to web/data/runtime");
  console.log(
    `Latest plan: ${latestPlan.decisionDate} ${snapshotLabel} -> next open, ` +
      `${latestPlan.signals.filter((s) => s.action === "buy").length} buys`,
  );
  console.log(
    `Return: ${result.stats.totalReturnPct.toFixed(2)}%  ` +
      `CAGR: ${result.stats.cagrPct.toFixed(2)}%  ` +
      `MaxDD: ${result.stats.maxDrawdownPct.toFixed(2)}%  ` +
      `Sharpe: ${result.stats.sharpe.toFixed(2)}  ` +
      `Trades: ${result.stats.trades}`,
  );
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
