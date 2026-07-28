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
import {
  activeEntriesAsOf,
  loadStrategyEntries,
  resolveEntryAsOf,
  type UniverseEntry,
} from "../lib/universe";
import { runBacktest, type BacktestConfig, type BacktestResult } from "../lib/backtest";
import { ruleBasedScorer } from "../lib/dashboardBacktest";
import { optimizeBacktest, type OptimizationResult } from "../lib/backtestOptimization";
import { getStrategy, getDefaultStrategy, STRATEGIES } from "../lib/strategyRegistry";
import { buildLatestPlan, buildPromotedModelPlan, type LatestPlan } from "../lib/latestPlan";
import { modelSnapshotForDate } from "../lib/mlShadow";
import { readRuntimeJson, writeRuntimeJson, writeStrategyJson, runtimeStrategyDir } from "../lib/runtimeData";
import { toCostConfig, roundTripBps } from "../lib/costConfig";
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
import {
  fetchMoneyflow,
  fetchMarginDetail,
  fetchIndexDaily,
  fetchMarketBreadth,
  type MoneyflowRow,
  type MarginRow,
  type IndexDailyRow,
  type MarketBreadth,
} from "../lib/pyserver";

/**
 * Fetch strategy-specific enhancement data from pyserver.
 * - Tide: real moneyflow + margin detail for all universe symbols
 * - Prism: index daily (CSI300) + market breadth for regime detection
 * - Momentum-V1: no additional data needed
 *
 * Returns an object that can be spread into createScorer() options.
 * All fetches are best-effort: if pyserver is unavailable, the strategy
 * gracefully falls back to V1 behavior.
 */
async function fetchStrategyScorerData(
  strategyId: string,
  symbols: string[],
): Promise<Record<string, unknown>> {
  const opts: Record<string, unknown> = {};

  if (strategyId === "tide") {
    try {
      console.log("[tide] Fetching moneyflow + margin data from pyserver...");
      const moneyflowData: Record<string, MoneyflowRow[]> = {};
      const marginData: Record<string, MarginRow[]> = {};

      // Fetch with limited concurrency to avoid overwhelming pyserver
      const CONCURRENCY = 10;
      for (let i = 0; i < symbols.length; i += CONCURRENCY) {
        const batch = symbols.slice(i, i + CONCURRENCY);
        const results = await Promise.allSettled(
          batch.map(async (sym) => {
            const [mf, mg] = await Promise.allSettled([
              fetchMoneyflow(sym, 20),
              fetchMarginDetail(sym, 20),
            ]);
            return { sym, mf, mg };
          }),
        );
        for (const r of results) {
          if (r.status === "fulfilled") {
            const { sym, mf, mg } = r.value;
            if (mf.status === "fulfilled" && mf.value.rows.length > 0) {
              moneyflowData[sym] = mf.value.rows;
            }
            if (mg.status === "fulfilled" && mg.value.rows.length > 0) {
              marginData[sym] = mg.value.rows;
            }
          }
        }
      }

      if (Object.keys(moneyflowData).length > 0) opts.moneyflowData = moneyflowData;
      if (Object.keys(marginData).length > 0) opts.marginData = marginData;
      console.log(
        `[tide] Moneyflow: ${Object.keys(moneyflowData).length}/${symbols.length} symbols, ` +
        `Margin: ${Object.keys(marginData).length}/${symbols.length} symbols`,
      );
    } catch (err) {
      console.log(
        `[tide] Enhancement data fetch failed (will use V1 fallback): ` +
        `${err instanceof Error ? err.message : String(err)}`,
      );
    }
  } else if (strategyId === "prism") {
    try {
      console.log("[prism] Fetching index daily + market breadth from pyserver...");
      const [indexResult, breadthResult] = await Promise.allSettled([
        fetchIndexDaily("000300.SH", 60),
        fetchMarketBreadth(),
      ]);
      if (indexResult.status === "fulfilled") {
        opts.indexData = indexResult.value.rows as IndexDailyRow[];
        console.log(`[prism] Index daily: ${indexResult.value.rows.length} bars (CSI300)`);
      } else {
        console.log("[prism] Index daily fetch failed, using cross-sectional fallback");
      }
      if (breadthResult.status === "fulfilled") {
        opts.marketBreadth = breadthResult.value as MarketBreadth;
        console.log(
          `[prism] Market breadth: advance_ratio=${breadthResult.value.advance_ratio.toFixed(2)}, ` +
          `new_high_20=${breadthResult.value.new_high_20}, new_low_20=${breadthResult.value.new_low_20}`,
        );
      } else {
        console.log("[prism] Market breadth fetch failed, using cross-sectional fallback");
      }
    } catch (err) {
      console.log(
        `[prism] Enhancement data fetch failed (will use V1 fallback): ` +
        `${err instanceof Error ? err.message : String(err)}`,
      );
    }
  }

  return opts;
}

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
const strategyId = process.env.DASHBOARD_STRATEGY ?? "momentum-v1";
const activeStrategy = getStrategy(strategyId) ?? getDefaultStrategy();
const cacheDir = path.resolve(process.cwd(), process.env.DASHBOARD_CACHE ?? ".cache/datasource");
const pyserverCacheDb = path.resolve(process.cwd(), process.env.PYSERVER_CACHE_DB ?? "../pyserver/cache.db");

interface DashboardOutput {
  generated_at: string;
  snapshot_basis?: "latest-complete-close" | "intraday-midday";
  snapshot_label?: string;
  strategy?: { id: string; name: string; codename: string; description: string };
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
  researchStatus?: Record<string, unknown> | null;
  strategy_status?: string;
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
  series: Array<{ entry: UniverseEntry }>,
): DashboardOutput["themePerformance"] {
  const entryMap = new Map(series.map((s) => [s.entry.symbol, s.entry]));
  const themeAt = (symbol: string, date: string) => {
    const entry = entryMap.get(symbol);
    return entry ? resolveEntryAsOf(entry, date).theme : "未分类";
  };
  const realized: Record<string, number> = {};
  const unrealized: Record<string, number> = {};
  const allocationDays: Record<string, number> = {};
  const weights: Record<string, number> = {};

  // Track cost basis per symbol using FIFO so we can compute both realized and
  // unrealized P&L per theme.
  const positions: Record<string, { shares: number; cost: number }> = {};
  for (const t of result.trades) {
    const theme = themeAt(t.symbol, t.decisionDate ?? t.tradeDate ?? t.date);
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
    const theme = themeAt(sym, lastBar.date);
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
      const theme = themeAt(sym, bar.date);
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
  const universe = loadStrategyEntries();
  console.log(`Loaded ${universe.length} universe entries`);
  console.log(`Strategy: ${activeStrategy.name} (${activeStrategy.codename}) [${activeStrategy.id}]`);

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
  const latestCompleteDate = isIntradaySnapshot
    ? [...new Set(benchmark.map((row) => row.date))]
        .filter((date) => date < today && date <= requestedEndDate)
        .sort()
        .at(-1) ?? latestAvailableDate(allDates, requestedEndDate)
    : endDate;
  const activeUniverse = activeEntriesAsOf(universe, endDate);

  const unifiedCost = toCostConfig();
  const cfg: BacktestConfig = {
    startCash: 1_000_000,
    rebalanceEveryNDays: decisionEveryNDays,
    decisionEveryNDays,
    executionPrice: "next_open",
    startDate,
    endDate,
    feeBps: roundTripBps() / 2,  // legacy per-side field, kept for backward compat
    costConfig: unifiedCost,
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
  // Fetch strategy-specific enhancement data (moneyflow/margin for Tide, index/breadth for Prism)
  const allSymbols = series.map((s) => s.entry.symbol);
  const activeScorerData = await fetchStrategyScorerData(strategyId, allSymbols);
  const activeScorer = activeStrategy.createScorer({ minScoreToBuy, ...activeScorerData });
  const runOptions = {
    scorer: activeScorer,
    historicalSignalsByDate: strategyId === "momentum-v1" ? historicalSignalsByDate : {},
  };

  const optimized = shouldOptimize
    ? await optimizeBacktest(series, cfg, (opts) => activeStrategy.createScorer({ ...activeScorerData, ...opts }))
    : {
      result: await runBacktest(series, cfg, runOptions),
      optimization: null,
    };
  const result = optimized.result;
  const benchmarkCurve = computeBenchmarkCurve(benchmark, result.config);

  const lastBar = result.equityCurve[result.equityCurve.length - 1];
  const effectiveMinScoreToBuy = optimized.optimization?.optimizedParams.minScoreToBuy ?? minScoreToBuy;
  const baseLatestPlan = await buildLatestPlan(series, {
    decisionDate: lastBar.date,
    scorer: activeStrategy.createScorer({ minScoreToBuy: effectiveMinScoreToBuy, ...activeScorerData }),
    maxPositions: result.config.maxPositions,
    minScoreToBuy: effectiveMinScoreToBuy,
  });
  const championModel = modelSnapshotForDate(lastBar.date, "champion");
  const shadowModel = modelSnapshotForDate(lastBar.date, "challenger");
  const executablePlan = championModel
    ? buildPromotedModelPlan(championModel, result.config.maxPositions)
    : baseLatestPlan;
  const latestPlan: LatestPlan = shadowModel
    ? { ...executablePlan, shadowModel }
    : executablePlan;
  const output: DashboardOutput = {
    generated_at: new Date().toISOString(),
    snapshot_basis: snapshotBasis,
    snapshot_label: snapshotLabel,
    strategy: { id: activeStrategy.id, name: activeStrategy.name, codename: activeStrategy.codename, description: activeStrategy.description },
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
    researchStatus: readRuntimeJson<Record<string, unknown>>("ml/status.json"),
    strategy_status: strategyId === "tide" && activeScorerData.moneyflowData === undefined
      ? "suspended"
      : strategyId === "prism" && activeScorerData.indexData === undefined
        ? "degraded"
        : "active",
  };

  writeRuntimeJson("backtest.json", output);
  // Also write to per-strategy namespace
  writeStrategyJson(activeStrategy.id, "backtest.json", output);
  const signalsOutput = {
    generated_at: new Date().toISOString(),
    source: latestPlan.source,
    score_model: latestPlan.scoreModel,
    signal_date: latestPlan.decisionDate,
    latest_complete_date: latestCompleteDate,
    signal_basis: snapshotBasis,
    snapshot_label: snapshotLabel,
    max_positions: latestPlan.maxPositions,
    optimized_params: optimized.optimization?.optimizedParams ?? {
      maxPositions: result.config.maxPositions,
      minHoldBars: result.config.minHoldBars ?? 0,
      rebalanceThresholdPct: result.config.rebalanceThresholdPct ?? 0,
      minScoreToBuy: effectiveMinScoreToBuy,
    },
    universe_count: activeUniverse.length,
    scored_count: latestPlan.signals.length,
    skipped_count: Math.max(0, activeUniverse.length - latestPlan.signals.length),
    shadow_model: shadowModel
      ? {
        stage: shadowModel.stage,
        model_version: shadowModel.model_version,
        feature_version: shadowModel.feature_version,
        data_cutoff: shadowModel.data_cutoff,
      }
      : null,
    champion_model: championModel
      ? {
        stage: championModel.stage,
        model_version: championModel.model_version,
        feature_version: championModel.feature_version,
        data_cutoff: championModel.data_cutoff,
      }
      : null,
    fundamentals: [],
    signals: latestPlan.signals,
  };
  writeRuntimeJson("signals.json", signalsOutput);
  writeStrategyJson(activeStrategy.id, "signals.json", signalsOutput);
  writeSignalHistorySnapshot(buildSignalHistorySnapshot(latestPlan, series), activeStrategy.id);
  writeRuntimeJson("meta.json", {
    generated_at: new Date().toISOString(),
    universe_count: activeUniverse.length,
  });

  // --- Multi-strategy: run remaining strategies and write to per-strategy dirs ---
  const runAllStrategies = process.env.DASHBOARD_ALL_STRATEGIES === "1";
  const strategySummaries: Array<{ id: string; name: string; stats: BacktestResult["stats"] }> = [
    { id: activeStrategy.id, name: activeStrategy.name, stats: result.stats },
  ];

  if (runAllStrategies) {
    for (const strategy of STRATEGIES) {
      if (strategy.id === activeStrategy.id) continue;
      console.log(`\n--- Running strategy: ${strategy.name} (${strategy.codename}) [${strategy.id}] ---`);
      const stratScorerData = await fetchStrategyScorerData(strategy.id, allSymbols);
      const stratScorer = strategy.createScorer({ minScoreToBuy, ...stratScorerData });
      const stratResult = await runBacktest(series, cfg, {
        scorer: stratScorer,
        historicalSignalsByDate: strategy.id === "momentum-v1" ? historicalSignalsByDate : {},
      });
      const stratBenchmarkCurve = computeBenchmarkCurve(benchmark, stratResult.config);
      const stratLastBar = stratResult.equityCurve[stratResult.equityCurve.length - 1];
      const stratPlan = await buildLatestPlan(series, {
        decisionDate: stratLastBar.date,
        scorer: strategy.createScorer({ minScoreToBuy, ...stratScorerData }),
        maxPositions: stratResult.config.maxPositions,
        minScoreToBuy,
      });
      const stratOutput: DashboardOutput = {
        generated_at: new Date().toISOString(),
        snapshot_basis: snapshotBasis,
        snapshot_label: snapshotLabel,
        strategy: { id: strategy.id, name: strategy.name, codename: strategy.codename, description: strategy.description },
        config: stratResult.config,
        stats: stratResult.stats,
        equityCurve: stratResult.equityCurve,
        benchmarkCurve: stratBenchmarkCurve,
        trades: stratResult.trades,
        themePerformance: computeThemePerformance(stratResult, series),
        signalsByDate: stratResult.signalsByDate,
        latestPlan: stratPlan,
        latestHoldings: stratLastBar.positions,
        latestDate: stratLastBar.date,
        meetsSharpeTarget: stratResult.stats.sharpe >= (stratResult.config.sharpeTarget ?? 3),
        primaryWindow: "post_cny_2026",
        optimizedParams: {
          maxPositions: stratResult.config.maxPositions,
          minHoldBars: stratResult.config.minHoldBars ?? 0,
          rebalanceThresholdPct: stratResult.config.rebalanceThresholdPct ?? 0,
          minScoreToBuy,
        },
        optimizationWarnings: [],
        optimizationCandidates: [],
        researchStatus: null,
        strategy_status: strategy.id === "tide" && stratScorerData.moneyflowData === undefined
          ? "suspended"
          : strategy.id === "prism" && stratScorerData.indexData === undefined
            ? "degraded"
            : "active",
      };
      writeStrategyJson(strategy.id, "backtest.json", stratOutput);
      const stratSignals = {
        generated_at: new Date().toISOString(),
        source: stratPlan.source,
        score_model: stratPlan.scoreModel,
        signal_date: stratPlan.decisionDate,
        latest_complete_date: latestCompleteDate,
        signal_basis: snapshotBasis,
        snapshot_label: snapshotLabel,
        max_positions: stratPlan.maxPositions,
        universe_count: activeUniverse.length,
        scored_count: stratPlan.signals.length,
        signals: stratPlan.signals,
      };
      writeStrategyJson(strategy.id, "signals.json", stratSignals);
      writeSignalHistorySnapshot(buildSignalHistorySnapshot(stratPlan, series), strategy.id);
      strategySummaries.push({ id: strategy.id, name: strategy.name, stats: stratResult.stats });
      console.log(
        `  Return: ${stratResult.stats.totalReturnPct.toFixed(2)}%  ` +
          `Sharpe: ${stratResult.stats.sharpe.toFixed(2)}  ` +
          `Trades: ${stratResult.stats.trades}`,
      );
    }

    // Write strategy comparison
    writeRuntimeJson("strategy-comparison.json", {
      generated_at: new Date().toISOString(),
      data_date: endDate,
      strategies: strategySummaries.map((s) => ({
        id: s.id,
        name: s.name,
        totalReturnPct: s.stats.totalReturnPct,
        cagrPct: s.stats.cagrPct,
        maxDrawdownPct: s.stats.maxDrawdownPct,
        sharpe: s.stats.sharpe,
        trades: s.stats.trades,
        winRatePct: s.stats.winRatePct,
        turnoverPct: s.stats.turnoverPct,
      })),
    });
    console.log("\nWrote strategy-comparison.json");
  }

  // --- Generate manifest.json ---
  const { execSync } = await import("node:child_process");
  const crypto = await import("node:crypto");
  let gitSha = "unknown";
  try { gitSha = execSync("git rev-parse HEAD", { encoding: "utf-8" }).trim(); } catch { /* not in git */ }
  let universeSha = "unknown";
  try {
    const universeContent = fs.readFileSync(path.resolve(process.cwd(), "data/universe.json"), "utf-8");
    universeSha = crypto.createHash("sha256").update(universeContent).digest("hex").slice(0, 16);
  } catch { /* universe not found */ }

  // Compute per-strategy file SHA-256 and parameter versions
  const manifestStrategies = strategySummaries.map((s) => {
    const stratMeta = getStrategy(s.id);
    const params = {
      minScoreToBuy: stratMeta?.defaultMinScore ?? minScoreToBuy,
      maxPositions,
      minHoldBars,
      rebalanceThresholdPct,
    };
    const fileHashes: Record<string, string> = {};
    for (const fileName of ["backtest.json", "signals.json"]) {
      try {
        const filePath = path.join(runtimeStrategyDir(s.id), fileName);
        const content = fs.readFileSync(filePath, "utf-8");
        fileHashes[fileName] = crypto.createHash("sha256").update(content).digest("hex").slice(0, 16);
      } catch { /* file not written yet */ }
    }
    return { id: s.id, name: s.name, params, file_sha256: fileHashes };
  });

  writeRuntimeJson("manifest.json", {
    generated_at: new Date().toISOString(),
    git_sha: gitSha,
    universe_sha: universeSha,
    data_date: endDate,
    latest_complete_date: latestCompleteDate,
    snapshot_basis: snapshotBasis,
    strategies: manifestStrategies,
    cost_model: "config/cost-model.json",
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
