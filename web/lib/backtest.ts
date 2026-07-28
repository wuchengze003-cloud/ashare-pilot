// Bar-by-bar backtest engine. The default trading contract now mirrors the
// intended live workflow: decide after day D close, execute on D+1 open, then
// mark the portfolio at D+1 close. Signals never see D+1 prices.
import type { Kline } from "./pyserver";
import type { SymbolSnapshot, Signal } from "./strategyTypes";
import { ruleBasedScorer } from "./dashboardBacktest";
import {
  isStrategyEntryAsOf,
  resolveEntryAsOf,
  type UniverseEntry,
} from "./universe";

export interface CostConfig {
  /** Commission on buy side, in basis points. Default 2.5bp. */
  buyCommissionBps?: number;
  /** Commission on sell side, in basis points. Default 2.5bp. */
  sellCommissionBps?: number;
  /** A-share stamp duty on sell side only, in basis points. Default 5bp (0.05%). */
  stampDutyBps?: number;
  /** Slippage per side, in basis points. Default 3bp. */
  slippageBps?: number;
}

export interface BacktestConfig {
  startCash: number;
  /** Backward-compatible alias. New callers should set decisionEveryNDays. */
  rebalanceEveryNDays: number;
  /** How often to make a close-after-decision. Defaults to rebalanceEveryNDays, then 1. */
  decisionEveryNDays?: number;
  /** Current supported realistic execution model: D close decision -> D+1 open trade. */
  executionPrice?: "next_open";
  startDate: string;         // YYYY-MM-DD
  endDate: string;
  /** Legacy per-side fee in basis points. Applied on BOTH buy and sell.
   *  When costConfig is provided, this field is ignored. */
  feeBps: number;
  /** Granular A-share cost model. Overrides feeBps when present. */
  costConfig?: CostConfig;
  maxPositions: number;
  /** When true, any held position not selected by a buy signal at rebalance is sold.
   *  Useful for ranking-based strategies where the portfolio should exactly mirror
   *  the top-scoring names each period. Defaults to false. */
  autoSellUnselected?: boolean;
  /** Minimum number of bars a position must be held before it can be sold.
   *  Helps reduce whipsaw turnover in ranking-based strategies. */
  minHoldBars?: number;
  /** Position-level drift threshold (in percent). If a held top-K position's
   *  current weight is within this threshold of its target weight, no trade is
   *  made for that symbol. Similarly, unselected positions below this weight
   *  are kept as residuals instead of sold. */
  rebalanceThresholdPct?: number;
  sharpeTarget?: number;
  optimizationWindow?: "post_cny_2026" | "jan_2026" | string;
}

export interface FundamentalSnapshot {
  pe_ttm?: number | null;
  pb?: number | null;
  market_cap?: number | null;
  revenue_yoy?: number | null;
  profit_yoy?: number | null;
  /** The date this fundamental snapshot becomes known (e.g. announcement date). */
  effective_date?: string;
}

export interface PortfolioBar {
  date: string;
  equity: number;
  cash: number;
  positions: Record<string, { shares: number; price: number }>;
}

export type TradeSide = "buy" | "sell" | "reduce";

export interface Trade {
  /** Backward-compatible alias for tradeDate. */
  date: string;
  decisionDate: string;
  tradeDate: string;
  priceField: "open";
  symbol: string;
  side: TradeSide;
  shares: number;
  price: number;
  reason: string;
  targetWeightBefore: number;
  targetWeightAfter: number;
  pnlPct?: number | null;
}

export interface RoundTripEpisode {
  symbol: string;
  entryDate: string;
  exitDate: string;
  shares: number;
  avgEntryPrice: number;
  avgExitPrice: number;
  pnlPct: number; // net of all fees
  bars: number;
}

export interface BacktestResult {
  config: BacktestConfig;
  equityCurve: PortfolioBar[];
  trades: Trade[];
  signalsByDate: Record<string, Signal[]>;
  stats: {
    totalReturnPct: number;
    cagrPct: number;
    maxDrawdownPct: number;
    sharpe: number;
    /** Total order count (buys + sells + reductions). */
    trades: number;
    /** Legacy win rate (per sell action). Kept for backward compat. */
    winRatePct: number;
    turnoverPct: number;
    /** Complete round-trip episodes (open → fully close). */
    roundTrips?: number;
    /** Profitable round-trip episodes / total round-trips. */
    roundTripWinRatePct?: number;
    /** Average P&L per round-trip episode, net of fees. */
    avgRoundTripPnlPct?: number;
  };
  episodes?: RoundTripEpisode[];
}

export interface SymbolSeries {
  entry: UniverseEntry;
  klines: Kline[];
  /** Point-in-time fundamentals. The engine picks the latest entry whose
   *  effective_date <= rebalance date, so no future financial reports leak
   *  into historical decisions. */
  fundamentals?: FundamentalSnapshot[];
}

// Union of all series' dates. An intersection would let a single suspended
// (停牌) or late-listed symbol delete whole stretches of the portfolio clock —
// months of marks vanish and CAGR/Sharpe are computed over a warped calendar.
// Symbols without a bar on a given date simply can't trade that day.
function unionTradingDates(series: SymbolSeries[]): string[] {
  const all = new Set<string>();
  series.forEach((s) => s.klines.forEach((k) => all.add(k.date)));
  return [...all].sort();
}

function indexByDate(klines: Kline[]) {
  const m = new Map<string, Kline>();
  for (const k of klines) m.set(k.date, k);
  return m;
}

function latestFundamentalAsOf(
  fundamentals: FundamentalSnapshot[] | undefined,
  date: string,
): Omit<FundamentalSnapshot, "effective_date"> | undefined {
  if (!fundamentals || fundamentals.length === 0) return undefined;
  const available = fundamentals.filter(
    (f) => f.effective_date && f.effective_date <= date,
  );
  if (available.length === 0) return undefined;
  available.sort((a, b) => (a.effective_date! < b.effective_date! ? -1 : 1));
  const latest = available[available.length - 1];
  const { effective_date: _, ...rest } = latest;
  return rest;
}

// A-share daily price-limit (涨跌停) thresholds by board, as a fraction of the
// prior close. Main board ±10% (ST ±5%), 科创板/创业板 ±20%, 北交所 ±30%.
export function priceLimitFraction(symbol: string, name: string): number {
  const code = symbol.replace(/^(sh|sz|bj)/i, "").replace(/\.(sh|sz|bj)$/i, "");
  if (/^(688|300|301)/.test(code)) return 0.2; // 科创板 / 创业板
  if (/^(4|8|92)/.test(code)) return 0.3; // 北交所
  return /ST/i.test(name) ? 0.05 : 0.1; // 主板（ST 减半）
}

// Klines are 前复权 (qfq) adjusted, which preserves daily returns, so a 涨/跌停
// lock still shows up as a move at the board limit. The 0.3pp slack absorbs the
// exchange's 0.01-yuan rounding of the limit price.
const LIMIT_SLACK = 0.003;

export type Progress =
  | { phase: "signals"; done: number; total: number }
  | { phase: "simulating"; done: number; total: number };

export type Scorer = (
  snapshots: SymbolSnapshot[],
  opts: { asOf: string; mode: "backtest" },
) => Promise<Signal[]>;

const DEFAULT_SIGNAL_CONCURRENCY = 6;

function signalConcurrency(): number {
  const n = Number(process.env.BACKTEST_SIGNAL_CONCURRENCY ?? DEFAULT_SIGNAL_CONCURRENCY);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : DEFAULT_SIGNAL_CONCURRENCY;
}

export interface RunBacktestOptions {
  onProgress?: (p: Progress) => void;
  /** Override the default Dashboard rule scorer — used by tests to inject deterministic signals. */
  scorer?: Scorer;
  /**
   * Immutable signals that were already shown to the user for a decision date.
   * Daily simulated portfolios must execute these archived plans instead of
   * recomputing old signals with today's universe metadata or rule text.
   */
  historicalSignalsByDate?: Record<string, Signal[]>;
}

export async function runBacktest(
  series: SymbolSeries[],
  cfg: BacktestConfig,
  optsOrOnProgress?: RunBacktestOptions | ((p: Progress) => void),
): Promise<BacktestResult> {
  const opts: RunBacktestOptions = typeof optsOrOnProgress === "function"
    ? { onProgress: optsOrOnProgress }
    : (optsOrOnProgress ?? {});
  const onProgress = opts.onProgress;
  const scorer: Scorer = opts.scorer ?? ruleBasedScorer();
  const historicalSignalsByDate = opts.historicalSignalsByDate ?? {};
  const decisionEveryNDays = Math.max(
    1,
    Math.floor(cfg.decisionEveryNDays ?? cfg.rebalanceEveryNDays ?? 1),
  );
  const normalizedCfg: BacktestConfig = {
    ...cfg,
    rebalanceEveryNDays: decisionEveryNDays,
    decisionEveryNDays,
    executionPrice: "next_open",
    sharpeTarget: cfg.sharpeTarget ?? 3,
  };
  const dates = unionTradingDates(series).filter(
    (d) => d >= cfg.startDate && d <= cfg.endDate,
  );
  if (dates.length < 5) {
    throw new Error(`Not enough aligned trading days (${dates.length}) in window`);
  }

  const byDate = series.map((s) => indexByDate(s.klines));
  const symbols = series.map((s) => s.entry.symbol);
  const symbolIndex = new Map(symbols.map((s, j) => [s, j] as const));

  // Prior-close lookup built from the FULL series so the first in-window bar
  // still resolves a previous close for limit detection.
  const prevCloseByDate = series.map((s) => {
    const sorted = [...s.klines].sort((a, b) => (a.date < b.date ? -1 : 1));
    const m = new Map<string, number>();
    for (let k = 1; k < sorted.length; k++) m.set(sorted[k].date, sorted[k - 1].close);
    return m;
  });
  const limitFrac = series.map((s) => priceLimitFraction(s.entry.symbol, s.entry.name));
  const dayReturn = (j: number, date: string, close: number): number | null => {
    const prev = prevCloseByDate[j].get(date);
    if (prev === undefined || prev <= 0) return null;
    return close / prev - 1;
  };
  const atLimitUp = (j: number, date: string, close: number): boolean => {
    const r = dayReturn(j, date, close);
    return r !== null && r >= limitFrac[j] - LIMIT_SLACK;
  };
  const atLimitDown = (j: number, date: string, close: number): boolean => {
    const r = dayReturn(j, date, close);
    return r !== null && r <= -(limitFrac[j] - LIMIT_SLACK);
  };

  const t0 = Date.now();
  // Pre-fetch ALL decision signals in parallel. Signals at decision date D
  // depend on closes <= D and execute at D+1 open.
  // Cached entries return instantly; uncached fire concurrently (bounded).
  const decisionDates = dates.filter((_, i) => i < dates.length - 1 && i % decisionEveryNDays === 0);
  const signalsByDate: Record<string, Signal[]> = {};
  const CONCURRENCY = signalConcurrency();
  let signalsDone = 0;
  onProgress?.({ phase: "signals", done: 0, total: decisionDates.length });
  for (let i = 0; i < decisionDates.length; i += CONCURRENCY) {
    const slice = decisionDates.slice(i, i + CONCURRENCY);
    const results = await Promise.all(
      slice.map(async (d) => {
        const archivedSignals = historicalSignalsByDate[d];
        if (archivedSignals) {
          signalsDone++;
          onProgress?.({ phase: "signals", done: signalsDone, total: decisionDates.length });
          return [d, archivedSignals.map((s) => ({ ...s }))] as const;
        }
        const snapshots: SymbolSnapshot[] = series
          .filter((s) => isStrategyEntryAsOf(s.entry, d))
          .map((s) => {
            const entry = resolveEntryAsOf(s.entry, d);
            // Include D close: the decision is made after close, then executed
            // on the next trading day's open. Fundamentals are point-in-time:
            // the latest snapshot whose effective_date <= d.
            const upto = s.klines.filter((k) => k.date <= d);
            return {
              symbol: entry.symbol,
              name: entry.name,
              theme: entry.theme,
              note: entry.note,
              closes: upto.map((k) => k.close),
              volumes: upto.map((k) => k.volume),
              global_supply: entry.global_supply ?? null,
              fundamental: latestFundamentalAsOf(s.fundamentals, d),
            };
          })
          .filter((s) => s.closes.length > 0); // not yet listed as of d
        const sigs = await scorer(snapshots, { asOf: d, mode: "backtest" });
        signalsDone++;
        onProgress?.({ phase: "signals", done: signalsDone, total: decisionDates.length });
        return [d, sigs] as const;
      }),
    );
    for (const [d, sigs] of results) signalsByDate[d] = sigs;
  }
  console.log(
    `[backtest] fetched ${decisionDates.length} decision signals in ${
      ((Date.now() - t0) / 1000).toFixed(1)
    }s (concurrency=${CONCURRENCY})`,
  );

  let cash = cfg.startCash;
  const shares: Record<string, number> = Object.fromEntries(symbols.map((s) => [s, 0]));
  const avgCost: Record<string, number> = Object.fromEntries(symbols.map((s) => [s, 0]));
  // Sells/reductions blocked by a 跌停 lock or 停牌; retried each bar until tradable.
  const pendingSell: Record<string, {
    decisionDate: string;
    side: "sell" | "reduce";
    shares: number | "all";
    reason: string;
    hardExit: boolean;
    targetWeightAfter: number;
  } | undefined> = {};
  // Track the bar index at which a position was opened, for minHoldBars.
  const lastBuyBar: Record<string, number> = {};
  // Last traded close per symbol, for marking positions on days it has no bar.
  const lastPrice: Record<string, number> = {};
  const equityCurve: PortfolioBar[] = [];
  const trades: Trade[] = [];
  // Cost model: granular A-share costs when costConfig is provided,
  // otherwise fall back to legacy symmetric feeBps.
  const cc = cfg.costConfig;
  const buyFee = cc
    ? ((cc.buyCommissionBps ?? 2.5) + (cc.slippageBps ?? 3)) / 10_000
    : cfg.feeBps / 10_000;
  const sellFee = cc
    ? ((cc.sellCommissionBps ?? 2.5) + (cc.stampDutyBps ?? 5) + (cc.slippageBps ?? 3)) / 10_000
    : cfg.feeBps / 10_000;
  let realizedTrades = 0;
  let winningTrades = 0;
  let tradedValue = 0;

  const portfolioValue = (prices: Record<string, number>): number =>
    cash +
    symbols.reduce(
      (sum, sym) => sum + (shares[sym] ?? 0) * (prices[sym] ?? lastPrice[sym] ?? 0),
      0,
    );

  const isHardExit = (sig: Signal): boolean => sig.action === "sell";
  const heldBars = (sym: string, i: number): number =>
    lastBuyBar[sym] === undefined ? Number.POSITIVE_INFINITY : i - lastBuyBar[sym];
  const canOrdinarySell = (sym: string, i: number): boolean =>
    !normalizedCfg.minHoldBars || heldBars(sym, i) >= normalizedCfg.minHoldBars;

  const recordSellWin = (sym: string, price: number) => {
    if ((avgCost[sym] ?? 0) <= 0) return null;
    const pnlPct = (price / avgCost[sym] - 1) * 100;
    realizedTrades++;
    if (pnlPct > 0) winningTrades++;
    return pnlPct;
  };

  const pushTrade = (
    date: string,
    decisionDate: string,
    sym: string,
    side: TradeSide,
    sh: number,
    price: number,
    reason: string,
    targetWeightBefore: number,
    targetWeightAfter: number,
    pnlPct?: number | null,
  ) => {
    tradedValue += sh * price;
    trades.push({
      date,
      decisionDate,
      tradeDate: date,
      priceField: "open",
      symbol: sym,
      side,
      shares: sh,
      price,
      reason,
      targetWeightBefore,
      targetWeightAfter,
      pnlPct,
    });
  };

  const progressEvery = Math.max(1, Math.floor(dates.length / 20));
  onProgress?.({ phase: "simulating", done: 0, total: dates.length });
  for (let i = 0; i < dates.length; i++) {
    const date = dates[i];
    if (i % progressEvery === 0 || i === dates.length - 1) {
      onProgress?.({ phase: "simulating", done: i + 1, total: dates.length });
    }
    // Symbols without a bar today (停牌, not yet listed) are untradable; held
    // positions mark at their last traded close.
    const tradePrices: Record<string, number> = {};
    const closePrices: Record<string, number> = {};
    for (let j = 0; j < symbols.length; j++) {
      const k = byDate[j].get(date);
      if (k) {
        tradePrices[symbols[j]] = k.open;
        closePrices[symbols[j]] = k.close;
      }
    }

    const decisionDate = i > 0 && (i - 1) % decisionEveryNDays === 0 ? dates[i - 1] : null;
    const signals = decisionDate ? signalsByDate[decisionDate] ?? [] : [];
    const driftThreshold = (normalizedCfg.rebalanceThresholdPct ?? 0) / 100;

    const rankedBuys = signals
      .filter((s) => {
        if (s.action !== "buy" || s.size <= 0) return false;
        if (tradePrices[s.symbol] === undefined) return false;
        const j = symbolIndex.get(s.symbol);
        return !(j !== undefined && atLimitUp(j, date, tradePrices[s.symbol]));
      })
      .sort((a, b) => b.confidence * b.size - a.confidence * a.size);
    const preliminaryBuys = rankedBuys.slice(0, normalizedCfg.maxPositions);
    const preliminaryBuySymbols = new Set(preliminaryBuys.map((s) => s.symbol));
    const lockedUnselectedCount = normalizedCfg.minHoldBars
      ? symbols.filter(
          (sym) =>
            (shares[sym] ?? 0) > 0 &&
            !preliminaryBuySymbols.has(sym) &&
            !canOrdinarySell(sym, i),
        ).length
      : 0;
    const topBuys = rankedBuys.slice(0, Math.max(0, normalizedCfg.maxPositions - lockedUnselectedCount));
    const explicitTargetWeightSum = topBuys.reduce((sum, s) => sum + s.size, 0);
    const hasExplicitTargetWeights =
      explicitTargetWeightSum > 0 && explicitTargetWeightSum <= 1 + 1e-6;
    const targetTotal = topBuys.reduce((sum, s) => sum + s.size * s.confidence, 0) || 1;
    const targetWeights = new Map(
      topBuys.map((s) => [
        s.symbol,
        hasExplicitTargetWeights ? s.size : (s.size * s.confidence) / targetTotal,
      ] as const),
    );
    const hardSellSymbols = new Set(signals.filter(isHardExit).map((s) => s.symbol));

    // A fresh selected buy cancels a stale deferred ordinary sell/reduction.
    for (const sym of targetWeights.keys()) {
      if (pendingSell[sym] && !hardSellSymbols.has(sym)) pendingSell[sym] = undefined;
    }

    const executeSellOrder = (
      sym: string,
      requestedShares: number | "all",
      side: "sell" | "reduce",
      reason: string,
      fromDecisionDate: string,
      hardExit: boolean,
      targetWeightAfter: number,
      allowPending: boolean,
    ) => {
      const held = shares[sym] ?? 0;
      if (held <= 0) {
        pendingSell[sym] = undefined;
        return false;
      }
      if (!hardExit && !canOrdinarySell(sym, i)) return false;
      const j = symbolIndex.get(sym);
      const px = tradePrices[sym];
      const enqueue = () => {
        if (!allowPending) return;
        pendingSell[sym] = {
          decisionDate: fromDecisionDate,
          side,
          shares: requestedShares,
          reason,
          hardExit,
          targetWeightAfter,
        };
      };
      if (px === undefined) {
        enqueue();
        return false;
      }
      if (j !== undefined && atLimitDown(j, date, px)) {
        enqueue();
        return false;
      }
      const beforeEquity = portfolioValue(tradePrices);
      const targetWeightBefore = beforeEquity > 0 ? (held * px) / beforeEquity : 0;
      const sh = requestedShares === "all" ? held : Math.min(held, requestedShares);
      if (sh <= 0) return false;
      cash += sh * px * (1 - sellFee);
      const pnlPct = recordSellWin(sym, px);
      shares[sym] = Math.max(0, held - sh);
      if (shares[sym] === 0) avgCost[sym] = 0;
      pushTrade(
        date,
        fromDecisionDate,
        sym,
        shares[sym] > 0 ? "reduce" : side,
        sh,
        px,
        reason,
        targetWeightBefore,
        shares[sym] > 0 ? targetWeightAfter : 0,
        pnlPct,
      );
      pendingSell[sym] = undefined;
      return true;
    };

    // Retry deferred sells/reductions first; these are prior decisions waiting
    // for the first tradable open.
    for (const sym of symbols) {
      const pending = pendingSell[sym];
      if (!pending) continue;
      executeSellOrder(
        sym,
        pending.shares,
        pending.side,
        pending.reason,
        pending.decisionDate,
        pending.hardExit,
        pending.targetWeightAfter,
        true,
      );
    }

    if (decisionDate) {
      // Hard exits first: trend/risk sells bypass minHoldBars.
      for (const sig of signals) {
        if (!isHardExit(sig)) continue;
        executeSellOrder(
          sig.symbol,
          "all",
          "sell",
          sig.rationale || "硬退出",
          decisionDate,
          true,
          0,
          true,
        );
      }

      if (normalizedCfg.autoSellUnselected) {
        for (const sym of symbols) {
          if ((shares[sym] ?? 0) <= 0) continue;
          if (targetWeights.has(sym) || hardSellSymbols.has(sym)) continue;
          const px = tradePrices[sym] ?? lastPrice[sym];
          const preEquity = portfolioValue(tradePrices);
          if (px && driftThreshold > 0 && ((shares[sym] ?? 0) * px) / preEquity <= driftThreshold) {
            continue;
          }
          executeSellOrder(
            sym,
            "all",
            "sell",
            "跌出明日目标组合",
            decisionDate,
            false,
            0,
            true,
          );
        }
      }

      // Reduce selected names that are above target weight. This is ordinary
      // rotation and respects minHoldBars.
      for (const [sym, targetWeight] of targetWeights) {
        const held = shares[sym] ?? 0;
        const px = tradePrices[sym];
        if (held <= 0 || !px) continue;
        const preEquity = portfolioValue(tradePrices);
        const currentWeight = preEquity > 0 ? (held * px) / preEquity : 0;
        if (currentWeight <= targetWeight + driftThreshold) continue;
        const excessValue = (currentWeight - targetWeight) * preEquity;
        const sh = Math.floor(excessValue / px / 100) * 100;
        executeSellOrder(
          sym,
          sh,
          "reduce",
          "高于目标仓位，减仓再平衡",
          decisionDate,
          false,
          targetWeight,
          true,
        );
      }

      // Buy/increase selected names after sells have freed cash.
      const postSellEquity = portfolioValue(tradePrices);
      for (const sig of topBuys) {
        const targetWeight = targetWeights.get(sig.symbol) ?? 0;
        const px = tradePrices[sig.symbol];
        if (!px) continue;
        const j = symbolIndex.get(sig.symbol);
        if (j !== undefined && atLimitUp(j, date, px)) continue;
        const held = shares[sig.symbol] ?? 0;
        const currentValue = held * px;
        const currentWeight = postSellEquity > 0 ? currentValue / postSellEquity : 0;
        if (currentWeight >= targetWeight - driftThreshold) continue;
        const alloc = Math.max(0, targetWeight * postSellEquity - currentValue);
        const sh = Math.floor(alloc / (px * (1 + buyFee)) / 100) * 100;
        if (sh <= 0) continue;
        const cost = sh * px * (1 + buyFee);
        if (cost > cash) continue;
        cash -= cost;
        const oldShares = shares[sig.symbol] ?? 0;
        const oldCost = (avgCost[sig.symbol] ?? 0) * oldShares;
        shares[sig.symbol] = oldShares + sh;
        avgCost[sig.symbol] = (oldCost + sh * px) / shares[sig.symbol];
        if (oldShares === 0) lastBuyBar[sig.symbol] = i;
        pushTrade(
          date,
          decisionDate,
          sig.symbol,
          "buy",
          sh,
          px,
          sig.rationale || "进入明日目标组合",
          currentWeight,
          targetWeight,
          null,
        );
        pendingSell[sig.symbol] = undefined;
      }
    }

    // Mark-to-market (suspended names mark at last traded close)
    let equity = cash;
    const positions: PortfolioBar["positions"] = {};
    for (const sym of symbols) {
      if (shares[sym] > 0) {
        const px = closePrices[sym] ?? lastPrice[sym];
        equity += shares[sym] * px;
        positions[sym] = { shares: shares[sym], price: px };
      }
    }
    equityCurve.push({ date, equity, cash, positions });
    for (const sym of symbols) {
      if (closePrices[sym] !== undefined) lastPrice[sym] = closePrices[sym];
    }
  }

  // Stats
  const equities = equityCurve.map((b) => b.equity);
  const start = equities[0];
  const end = equities[equities.length - 1];
  const totalReturnPct = (end / start - 1) * 100;
  // Calendar span, not bar count: bar count undercounts elapsed time whenever
  // the union calendar has gaps (suspensions, holidays), overstating CAGR.
  const spanMs = Date.parse(equityCurve.at(-1)!.date) - Date.parse(equityCurve[0].date);
  const years = Math.max(spanMs / (365.25 * 24 * 3600 * 1000), 1 / 252);
  const cagrPct = (Math.pow(end / start, 1 / years) - 1) * 100;

  let peak = start;
  let maxDD = 0;
  for (const e of equities) {
    peak = Math.max(peak, e);
    maxDD = Math.min(maxDD, e / peak - 1);
  }

  const rets: number[] = [];
  for (let i = 1; i < equities.length; i++) {
    rets.push(equities[i] / equities[i - 1] - 1);
  }
  const mean = rets.reduce((a, b) => a + b, 0) / (rets.length || 1);
  const variance =
    rets.reduce((a, b) => a + (b - mean) ** 2, 0) / (rets.length || 1);
  const std = Math.sqrt(variance);
  const sharpe = std > 0 ? (mean / std) * Math.sqrt(252) : 0;
  const averageEquity = equities.reduce((sum, e) => sum + e, 0) / (equities.length || 1);
  const turnoverPct = averageEquity > 0 ? (tradedValue / averageEquity) * 100 : 0;
  const winRatePct = realizedTrades > 0 ? (winningTrades / realizedTrades) * 100 : 0;

  // Round-trip episode computation: one episode = open position → fully close.
  const episodes = computeRoundTripEpisodes(trades, buyFee, sellFee);
  const roundTrips = episodes.length;
  const roundTripWins = episodes.filter((ep) => ep.pnlPct > 0).length;
  const roundTripWinRatePct = roundTrips > 0 ? (roundTripWins / roundTrips) * 100 : 0;
  const avgRoundTripPnlPct = roundTrips > 0
    ? episodes.reduce((sum, ep) => sum + ep.pnlPct, 0) / roundTrips
    : 0;

  return {
    config: normalizedCfg,
    equityCurve,
    trades,
    signalsByDate,
    stats: {
      totalReturnPct,
      cagrPct,
      maxDrawdownPct: maxDD * 100,
      sharpe,
      trades: trades.length,
      winRatePct,
      turnoverPct,
      roundTrips,
      roundTripWinRatePct,
      avgRoundTripPnlPct,
    },
    episodes,
  };
}

/** Reconstruct complete position episodes from the trade log.
 *  An episode starts at the first buy and ends when the position is fully closed.
 *  Partial reductions are accumulated into the episode's exit cash flows. */
function computeRoundTripEpisodes(
  trades: Trade[],
  buyFee: number,
  sellFee: number,
): RoundTripEpisode[] {
  const episodes: RoundTripEpisode[] = [];
  // Open episodes keyed by symbol
  const open = new Map<string, {
    entryDate: string;
    totalShares: number;
    totalCost: number; // shares * price * (1 + buyFee)
    exitShares: number;
    exitProceeds: number; // shares * price * (1 - sellFee)
  }>();

  for (const t of trades) {
    if (t.side === "buy") {
      const existing = open.get(t.symbol);
      if (existing) {
        existing.totalShares += t.shares;
        existing.totalCost += t.shares * t.price * (1 + buyFee);
      } else {
        open.set(t.symbol, {
          entryDate: t.tradeDate,
          totalShares: t.shares,
          totalCost: t.shares * t.price * (1 + buyFee),
          exitShares: 0,
          exitProceeds: 0,
        });
      }
    } else {
      // sell or reduce
      const ep = open.get(t.symbol);
      if (!ep) continue;
      ep.exitShares += t.shares;
      ep.exitProceeds += t.shares * t.price * (1 - sellFee);
      if (ep.exitShares >= ep.totalShares) {
        // Position fully closed — record episode
        const avgEntry = ep.totalCost / ep.totalShares;
        const avgExit = ep.exitProceeds / ep.exitShares;
        const bars = Math.max(1, Math.round(
          (new Date(t.tradeDate).getTime() - new Date(ep.entryDate).getTime()) / 86_400_000,
        ));
        episodes.push({
          symbol: t.symbol,
          entryDate: ep.entryDate,
          exitDate: t.tradeDate,
          shares: ep.totalShares,
          avgEntryPrice: avgEntry / (1 + buyFee), // raw price
          avgExitPrice: avgExit / (1 - sellFee),   // raw price
          pnlPct: (ep.exitProceeds / ep.totalCost - 1) * 100,
          bars,
        });
        open.delete(t.symbol);
      }
    }
  }
  return episodes;
}
