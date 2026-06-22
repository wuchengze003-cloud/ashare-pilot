// Deterministic, rule-based implementation of the right-side AI-infrastructure
// strategy described in lib/deepseek.ts. The Dashboard path uses this scorer so
// the backtest is reproducible and does not depend on LLM sampling.
//
// The scorer intentionally uses only point-in-time fields available at the
// rebalance date: historical closes/volumes, theme labels, global-supply-chain
// tags, and already-effective fundamentals. Fundamentals are a risk filter, not
// a source of alpha, because the normal backtest path often has price-only data.
import type { Signal, SymbolSnapshot } from "./strategyTypes";
import type { Scorer } from "./backtest";

export interface RuleBasedScorerOptions {
  /** Minimum number of historical closes required to score a symbol. */
  minCloses?: number;
  /** Lookback window for medium-term momentum calculations. */
  momentumDays?: number;
  /** Lookback window for short-term momentum calculations. */
  shortMomentumDays?: number;
  /** Price momentum/setup weight (0-1). */
  priceWeight?: number;
  /** Theme strength and chain-position weight (0-1). */
  themeWeight?: number;
  /** Volume confirmation weight (0-1). */
  volumeWeight?: number;
  /** Trend-shape weight (0-1). */
  trendWeight?: number;
  /** Do not buy if the latest visible daily return is above this threshold. */
  maxOneDayChasePct?: number;
  /** Do not buy if the latest visible five-day return is above this threshold. */
  maxFiveDayExtensionPct?: number;
  /** Minimum final score required for a buy signal. */
  minScoreToBuy?: number;
}

interface ScoredStock {
  symbol: string;
  name?: string | null;
  theme?: string;
  momentum20: number;
  momentum5: number;
  oneDayReturn: number;
  ma5: number;
  ma10: number;
  ma20: number;
  trendRaw: number;
  volumeRaw: number | null;
  setup: "突破确认" | "回踩转强" | "主题强势" | "趋势持有" | "风险过滤";
  peg: number | null;
  riskBlocked: boolean;
  globalSupply: boolean;
  chokeholdRaw: number;
}

function rankNormalize(values: (number | null)[], fallback = 0.5): number[] {
  const valid = values
    .map((v, i) => ({ v, i }))
    .filter((x): x is { v: number; i: number } => x.v != null);
  if (valid.length === 0) return values.map(() => fallback);
  valid.sort((a, b) => a.v - b.v);
  const ranks = new Array<number>(values.length).fill(fallback);
  // Average-rank ties so identical values receive identical scores.
  let start = 0;
  for (let i = 1; i <= valid.length; i++) {
    if (i === valid.length || valid[i].v !== valid[start].v) {
      const avgRank = (start + i - 1) / 2;
      const normalized = avgRank / Math.max(valid.length - 1, 1);
      for (let j = start; j < i; j++) {
        ranks[valid[j].i] = normalized;
      }
      start = i;
    }
  }
  return ranks;
}

function avg(xs: number[]): number {
  return xs.reduce((a, b) => a + b, 0) / xs.length;
}

function themeTierScore(theme?: string): number {
  const t = theme ?? "";
  if (/光模块|AI-PCB|PCB|覆铜板/.test(t)) return 1;
  if (/AI服务器|液冷|IDC|云|电力设备/.test(t)) return 0.82;
  if (/存储|HBM|半导体设备|晶圆代工|半导体材料/.test(t)) return 0.68;
  if (/功率器件|电力/.test(t)) return 0.55;
  return 0.5;
}

function supplyChainChokeScore(s: SymbolSnapshot): number {
  const text = `${s.theme ?? ""} ${s.name ?? ""} ${s.note ?? ""}`;
  if (/光模块|光通信|光芯片|光纤|光器件|激光器|铌酸锂|InP/i.test(text)) return 1;
  if (/AI-PCB|PCB|覆铜板|CCL|PTFE|高速板/i.test(text)) return 1;
  if (/液冷|温控|散热|冷板|精密空调/i.test(text)) return 0.9;
  if (/变压器|电源|UPS|HVDC|配电|电力设备|服务器电源/i.test(text)) return 0.86;
  if (/特气|WF6|钨|镓|锗|碲|锑|铋|氟|稀土|镝|铽|钇|铁氧体|碳化硅|金刚石|半导体材料/i.test(text)) {
    return 0.82;
  }
  if (/MLCC|电容|电感|被动元件|磁性/i.test(text)) return 0.78;
  if (/HBM|存储|封测|先进封装/i.test(text)) return 0.68;
  if (/AI服务器|IDC|云|晶圆代工|算力/i.test(text)) return 0.6;
  return 0.5;
}

function volumeRatio5v20(volumes?: number[]): number | null {
  if (!volumes || volumes.length < 25) return null;
  const tail = volumes.slice(-25);
  const recent = avg(tail.slice(-5));
  const base = avg(tail.slice(0, 20));
  if (!Number.isFinite(recent) || !Number.isFinite(base) || base <= 0) return null;
  return Math.log(recent / base);
}

function riskBlockedByFundamentals(s: SymbolSnapshot): boolean {
  const pe = s.fundamental?.pe_ttm;
  const profitYoy = s.fundamental?.profit_yoy;
  if (profitYoy != null && profitYoy < -30) return true;
  if (pe != null && pe > 250 && (profitYoy == null || profitYoy < 20)) return true;
  return false;
}

/** Build a scorer that ranks the universe by the right-side rule. */
export function ruleBasedScorer(options: RuleBasedScorerOptions = {}): Scorer {
  const {
    minCloses = 25,
    momentumDays = 20,
    shortMomentumDays = 5,
    priceWeight = 0.35,
    themeWeight = 0.3,
    volumeWeight = 0.2,
    trendWeight = 0.15,
    maxOneDayChasePct = 5,
    maxFiveDayExtensionPct = 18,
    minScoreToBuy = 0.58,
  } = options;
  const totalWeight = priceWeight + themeWeight + volumeWeight + trendWeight;

  return async (snapshots: SymbolSnapshot[], { asOf: _asOf }): Promise<Signal[]> => {
    const scored: ScoredStock[] = [];
    for (const s of snapshots) {
      const closes = s.closes;
      if (closes.length < Math.max(minCloses, momentumDays + 1, shortMomentumDays + 1, 20)) {
        continue;
      }
      const current = closes[closes.length - 1];
      const previous = closes[closes.length - 2];
      const momentum20 = current / closes[closes.length - 1 - momentumDays] - 1;
      const momentum5 = current / closes[closes.length - 1 - shortMomentumDays] - 1;
      const oneDayReturn = current / previous - 1;
      const ma5 = avg(closes.slice(-5));
      const ma10 = avg(closes.slice(-10));
      const ma20 = avg(closes.slice(-20));
      const priorHigh20 = Math.max(...closes.slice(-21, -1));

      const pe = s.fundamental?.pe_ttm;
      const profitYoy = s.fundamental?.profit_yoy;
      let peg: number | null = null;
      if (pe != null && pe > 0 && profitYoy != null && profitYoy > 0) {
        peg = pe / profitYoy;
      }

      const trendRaw =
        (current > ma5 ? 0.25 : 0) +
        (ma5 > ma10 ? 0.25 : 0) +
        (ma10 > ma20 ? 0.25 : 0) +
        (current > priorHigh20 ? 0.25 : 0);
      const volRaw = volumeRatio5v20(s.volumes);
      const breakout = current > priorHigh20 && (volRaw == null || volRaw > 0);
      const pullbackTurn = current >= ma10 && current <= ma5 * 1.035 && ma5 >= ma10 && momentum5 > 0;
      const strongThemeCandidate = current > ma5 && momentum20 > 0 && momentum5 > 0;

      scored.push({
        symbol: s.symbol,
        name: s.name,
        theme: s.theme,
        momentum20,
        momentum5,
        oneDayReturn,
        ma5,
        ma10,
        ma20,
        trendRaw,
        volumeRaw: volRaw,
        setup: breakout
          ? "突破确认"
          : pullbackTurn
            ? "回踩转强"
            : strongThemeCandidate
              ? "主题强势"
              : current >= ma10
                ? "趋势持有"
                : "风险过滤",
        peg,
        riskBlocked: riskBlockedByFundamentals(s),
        globalSupply: s.global_supply === true,
        chokeholdRaw: supplyChainChokeScore(s),
      });
    }
    if (scored.length === 0) return [];

    // Theme-level momentum: average 20-day return of members.
    const themeSum = new Map<string, number>();
    const themeCount = new Map<string, number>();
    for (const s of scored) {
      const theme = s.theme || "未分类";
      themeSum.set(theme, (themeSum.get(theme) ?? 0) + s.momentum20);
      themeCount.set(theme, (themeCount.get(theme) ?? 0) + 1);
    }
    const themeAvg = new Map<string, number>();
    for (const [theme, sum] of themeSum) {
      themeAvg.set(theme, sum / (themeCount.get(theme) ?? 1));
    }

    // Normalize each component by rank so weights are regime-invariant.
    const mediumMomentumScores = rankNormalize(scored.map((s) => s.momentum20));
    const shortMomentumScores = rankNormalize(scored.map((s) => s.momentum5));
    const pScores = scored.map((_, i) => 0.65 * mediumMomentumScores[i] + 0.35 * shortMomentumScores[i]);
    const themeMomentumScores = rankNormalize(
      scored.map((s) => themeAvg.get(s.theme || "未分类") ?? 0),
    );
    const tScores = scored.map(
      (s, i) =>
        0.5 * themeMomentumScores[i] +
        0.2 * themeTierScore(s.theme) +
        0.2 * s.chokeholdRaw +
        0.1 * (s.globalSupply ? 1 : 0.5),
    );
    const vScores = rankNormalize(scored.map((s) => s.volumeRaw));

    const signals: Signal[] = scored.map((s, i) => {
      const total =
        (priceWeight * pScores[i] +
          themeWeight * tScores[i] +
          volumeWeight * vScores[i] +
          trendWeight * s.trendRaw) /
        totalWeight;
      const chaseBlocked =
        s.oneDayReturn > maxOneDayChasePct / 100 ||
        s.momentum5 > maxFiveDayExtensionPct / 100;
      const trendBroken = s.ma10 > 0 && s.ma20 > 0 && (s.ma10 < s.ma20 * 0.985 || s.momentum20 < -0.08);
      const validBuySetup = s.setup === "突破确认" || s.setup === "回踩转强" || s.setup === "主题强势";
      const actionable =
        total >= minScoreToBuy &&
        !chaseBlocked &&
        !trendBroken &&
        !s.riskBlocked &&
        validBuySetup;
      const pegText = s.peg != null ? s.peg.toFixed(2) : "-";
      const momText = `${(s.momentum20 * 100).toFixed(1)}%`;
      const volText = s.volumeRaw == null ? "-" : Math.exp(s.volumeRaw).toFixed(2);
      const action = actionable ? "buy" : s.riskBlocked || trendBroken ? "sell" : "hold";
      const reasonSetup = s.riskBlocked
        ? "基本面风险过滤"
        : trendBroken
          ? "趋势破坏"
          : chaseBlocked
            ? "追高过滤"
            : s.setup;
      return {
        symbol: s.symbol,
        action,
        confidence: Math.max(0, Math.min(1, total)),
        size: action === "buy" ? 1 : 0,
        rationale: `${reasonSetup} 动量${momText} 量${volText} PEG${pegText}`,
      };
    });

    return signals.sort((a, b) => b.confidence - a.confidence);
  };
}
