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
import {
  PRODUCTION_GATE_FILE,
  PRODUCTION_SIGNALS_FILE,
  buildCashOnlyProductionSignals,
  deriveProductionGateFromFiles,
} from "../lib/productionGate";
import { toCostConfig, roundTripBps } from "../lib/costConfig";
import { loadTradingConstraints } from "../lib/tradingConstraints";
import {
  buildSignalHistorySnapshot,
  readSignalHistorySnapshots,
  writeSignalHistorySnapshot,
} from "../lib/signalHistory";
import {
  assertProductionRuntimeArtifacts,
  assertRuntimeArtifacts,
  readProductionRuntimeValidationInput,
  readRuntimeValidationInput,
} from "../lib/runtimeValidation";
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
import type { EnhancementDataStatus } from "../lib/strategies/tide";

interface StrategyScorerData extends Record<string, unknown> {
  moneyflowData?: Record<string, MoneyflowRow[]>;
  marginData?: Record<string, MarginRow[]>;
  moneyflowStatus?: EnhancementDataStatus;
  moneyflowCoverage?: number;
  marginCoverage?: number;
  indexData?: IndexDailyRow[];
  marketBreadthData?: MarketBreadth[];
  regimeDataStatus?: EnhancementDataStatus;
}

function dateMinusCalendarDays(value: string, days: number): string {
  const parsed = new Date(`${value}T00:00:00Z`);
  parsed.setUTCDate(parsed.getUTCDate() - days);
  return parsed.toISOString().slice(0, 10);
}

function coverageStatus(covered: number, total: number): EnhancementDataStatus {
  const ratio = total > 0 ? covered / total : 0;
  if (ratio >= 0.9) return "available";
  if (ratio >= 0.5) return "partial";
  return "unavailable";
}

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
  range: { startDate: string; endDate: string },
): Promise<StrategyScorerData> {
  const opts: StrategyScorerData = {};
  const historyRange = {
    startDate: dateMinusCalendarDays(range.startDate, 120),
    endDate: range.endDate,
  };

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
              fetchMoneyflow(sym, historyRange),
              fetchMarginDetail(sym, historyRange),
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

      const moneyflowCount = Object.keys(moneyflowData).length;
      const marginCount = Object.keys(marginData).length;
      opts.moneyflowData = moneyflowData;
      opts.marginData = marginData;
      opts.moneyflowStatus = coverageStatus(moneyflowCount, symbols.length);
      opts.moneyflowCoverage = symbols.length > 0 ? moneyflowCount / symbols.length : 0;
      opts.marginCoverage = symbols.length > 0 ? marginCount / symbols.length : 0;
      console.log(
        `[tide] Moneyflow: ${moneyflowCount}/${symbols.length} symbols (${opts.moneyflowStatus}), ` +
        `Margin: ${marginCount}/${symbols.length} symbols`,
      );
    } catch (err) {
      opts.moneyflowData = {};
      opts.marginData = {};
      opts.moneyflowStatus = "unavailable";
      opts.moneyflowCoverage = 0;
      opts.marginCoverage = 0;
      console.log(
        `[tide] Enhancement data fetch failed (strategy suspended): ` +
        `${err instanceof Error ? err.message : String(err)}`,
      );
    }
  } else if (strategyId === "prism") {
    try {
      console.log("[prism] Fetching index daily + market breadth from pyserver...");
      const [indexResult, breadthResult] = await Promise.allSettled([
        fetchIndexDaily("000300.SH", historyRange),
        fetchMarketBreadth(),
      ]);
      if (indexResult.status === "fulfilled") {
        opts.indexData = indexResult.value.rows as IndexDailyRow[];
        opts.regimeDataStatus = indexResult.value.rows.length >= 20
          ? "available"
          : indexResult.value.rows.length >= 5
            ? "partial"
            : "unavailable";
        console.log(`[prism] Index daily: ${indexResult.value.rows.length} bars (CSI300)`);
      } else {
        opts.indexData = [];
        opts.regimeDataStatus = "unavailable";
        console.log("[prism] Index daily fetch failed, strategy suspended");
      }
      if (breadthResult.status === "fulfilled") {
        opts.marketBreadthData = [breadthResult.value as MarketBreadth];
        console.log(
          `[prism] Market breadth: advance_ratio=${breadthResult.value.advance_ratio.toFixed(2)}, ` +
          `new_high_20=${breadthResult.value.new_high_20}, new_low_20=${breadthResult.value.new_low_20}`,
        );
      } else {
        console.log("[prism] Market breadth fetch failed, using cross-sectional fallback");
      }
    } catch (err) {
      opts.indexData = [];
      opts.marketBreadthData = [];
      opts.regimeDataStatus = "unavailable";
      console.log(
        `[prism] Enhancement data fetch failed (strategy suspended): ` +
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
const productionConstraints = loadTradingConstraints();
const decisionEveryNDays = Number(process.env.DASHBOARD_DECISION_EVERY ?? process.env.DASHBOARD_REBALANCE ?? 1);
const maxPositions = Math.min(
  Number(process.env.DASHBOARD_MAX_POSITIONS ?? productionConstraints.maxPositions),
  productionConstraints.maxPositions,
);
const minHoldBars = Number(
  process.env.DASHBOARD_MIN_HOLD_BARS ?? productionConstraints.minHoldingBars,
);
const rebalanceThresholdPct = Number(
  process.env.DASHBOARD_REBALANCE_THRESHOLD_PCT ??
    productionConstraints.rebalanceThresholdPct,
);
const configuredMinScoreToBuy = process.env.DASHBOARD_MIN_SCORE_TO_BUY;
const shouldOptimize = process.env.DASHBOARD_OPTIMIZE === "1";
const shouldWriteDiagnosticHistory =
  process.env.DASHBOARD_WRITE_DIAGNOSTIC_HISTORY === "1";
const strategyId = process.env.DASHBOARD_STRATEGY ?? "momentum-v1";
const activeStrategy = getStrategy(strategyId) ?? getDefaultStrategy();
const minScoreFor = (strategy: typeof activeStrategy) => Number(
  configuredMinScoreToBuy ?? strategy.defaultMinScore,
);
const minScoreToBuy = minScoreFor(activeStrategy);
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
      positions[t.symbol].cost += t.netCashFlowYuan !== undefined
        ? -t.netCashFlowYuan
        : t.shares * t.price;
    } else {
      const pos = positions[t.symbol];
      if (pos.shares > 0) {
        const avgCost = pos.cost / pos.shares;
        const sold = Math.min(t.shares, pos.shares);
        const netProceeds = t.netCashFlowYuan !== undefined
          ? t.netCashFlowYuan * (sold / t.shares)
          : sold * t.price;
        realized[theme] += netProceeds - sold * avgCost;
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
  // A research winner is not deployable until an exact production inference
  // adapter is registered here. The current race has no approved champion.
  const productionGate = deriveProductionGateFromFiles(undefined, {
    deployableChampionIds: [],
  });
  writeRuntimeJson(PRODUCTION_GATE_FILE, productionGate);

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
    startCash: productionConstraints.initialCapitalYuan,
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
  const historicalSignalsFor = (id: string) => Object.fromEntries(
    readSignalHistorySnapshots(Number.POSITIVE_INFINITY, id)
      .filter((snapshot) => snapshot.signal_date < endDate)
      .map((snapshot) => [snapshot.signal_date, snapshot.signals]),
  );
  const historicalSignalsByDate = historicalSignalsFor(activeStrategy.id);
  // Fetch strategy-specific enhancement data (moneyflow/margin for Tide, index/breadth for Prism)
  const allSymbols = series.map((s) => s.entry.symbol);
  const activeScorerData = await fetchStrategyScorerData(strategyId, allSymbols, { startDate, endDate });
  const activeScorer = activeStrategy.createScorer({ minScoreToBuy, ...activeScorerData });
  const runOptions = {
    scorer: activeScorer,
    historicalSignalsByDate,
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
    strategy_status: strategyId === "tide" && activeScorerData.moneyflowStatus === "unavailable"
      ? "suspended"
      : strategyId === "tide" && activeScorerData.moneyflowStatus === "partial"
        ? "degraded"
        : strategyId === "prism" && activeScorerData.regimeDataStatus === "unavailable"
          ? "suspended"
          : strategyId === "prism" && activeScorerData.regimeDataStatus === "partial"
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
  if (shouldWriteDiagnosticHistory) {
    writeSignalHistorySnapshot(
      buildSignalHistorySnapshot(latestPlan, series, activeStrategy.id),
      activeStrategy.id,
      { writeFlat: false },
    );
  }
  if (productionGate.status !== "cash-only") {
    throw new Error(
      `production champion ${productionGate.champion_id ?? "unknown"} passed research gates but has no connected inference adapter`,
    );
  }
  writeRuntimeJson(
    PRODUCTION_SIGNALS_FILE,
    buildCashOnlyProductionSignals(
      productionGate,
      latestPlan.decisionDate,
      latestCompleteDate,
      snapshotBasis,
    ),
  );
  writeRuntimeJson("meta.json", {
    generated_at: new Date().toISOString(),
    universe_count: activeUniverse.length,
  });

  // --- Multi-strategy: run remaining strategies and write to per-strategy dirs ---
  const runAllStrategies = process.env.DASHBOARD_ALL_STRATEGIES === "1";
  const strategySummaries: Array<{
    id: string;
    name: string;
    minScoreToBuy: number;
    stats: BacktestResult["stats"];
  }> = [
    { id: activeStrategy.id, name: activeStrategy.name, minScoreToBuy, stats: result.stats },
  ];

  if (runAllStrategies) {
    for (const strategy of STRATEGIES) {
      if (strategy.id === activeStrategy.id) continue;
      console.log(`\n--- Running strategy: ${strategy.name} (${strategy.codename}) [${strategy.id}] ---`);
      const strategyMinScoreToBuy = minScoreFor(strategy);
      const stratScorerData = await fetchStrategyScorerData(
        strategy.id,
        allSymbols,
        { startDate, endDate },
      );
      const stratScorer = strategy.createScorer({
        minScoreToBuy: strategyMinScoreToBuy,
        ...stratScorerData,
      });
      const stratResult = await runBacktest(series, cfg, {
        scorer: stratScorer,
        historicalSignalsByDate: historicalSignalsFor(strategy.id),
      });
      const stratBenchmarkCurve = computeBenchmarkCurve(benchmark, stratResult.config);
      const stratLastBar = stratResult.equityCurve[stratResult.equityCurve.length - 1];
      const stratPlan = await buildLatestPlan(series, {
        decisionDate: stratLastBar.date,
        scorer: strategy.createScorer({
          minScoreToBuy: strategyMinScoreToBuy,
          ...stratScorerData,
        }),
        maxPositions: stratResult.config.maxPositions,
        minScoreToBuy: strategyMinScoreToBuy,
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
          minScoreToBuy: strategyMinScoreToBuy,
        },
        optimizationWarnings: [],
        optimizationCandidates: [],
        researchStatus: null,
        strategy_status: strategy.id === "tide" && stratScorerData.moneyflowStatus === "unavailable"
          ? "suspended"
          : strategy.id === "tide" && stratScorerData.moneyflowStatus === "partial"
            ? "degraded"
            : strategy.id === "prism" && stratScorerData.regimeDataStatus === "unavailable"
              ? "suspended"
              : strategy.id === "prism" && stratScorerData.regimeDataStatus === "partial"
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
      if (shouldWriteDiagnosticHistory) {
        writeSignalHistorySnapshot(
          buildSignalHistorySnapshot(stratPlan, series, strategy.id),
          strategy.id,
          { writeFlat: false },
        );
      }
      strategySummaries.push({
        id: strategy.id,
        name: strategy.name,
        minScoreToBuy: strategyMinScoreToBuy,
        stats: stratResult.stats,
      });
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
    const params = {
      minScoreToBuy: s.minScoreToBuy,
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
    production_gate: {
      status: productionGate.status,
      champion_id: productionGate.champion_id,
      contract_sha256: productionGate.contract_sha256,
      minute_coverage_pct: productionGate.minute_coverage_pct,
      reason_codes: productionGate.reason_codes,
    },
  });

  assertRuntimeArtifacts(readRuntimeValidationInput());
  assertProductionRuntimeArtifacts(readProductionRuntimeValidationInput());
  console.log("Wrote runtime backtest/signals/history/meta to web/data/runtime");
  console.log(
    `Production gate: ${productionGate.status}, champion=${productionGate.champion_id ?? "none"}, ` +
      `reasons=${productionGate.reason_codes.join(",") || "none"}`,
  );
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
