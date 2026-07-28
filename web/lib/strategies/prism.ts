// 棱镜 (Prism) — Adaptive Multi-Factor Strategy
//
// Core thesis: no single factor dominates in all market regimes. Like a prism
// splitting white light into its spectral components, this strategy decomposes
// market behavior into distinct factors and dynamically adjusts their weights
// based on the detected regime.
//
// Regime Detection:
//   - Trending: high directional consistency, ADX-like measure > threshold
//   - Ranging: low directional consistency, mean-reversion profitable
//   - Volatile: high realized vol relative to history, defensive posture
//
// Factor Rotation:
//   - Trending → emphasize momentum (60%), reduce mean-reversion (10%)
//   - Ranging  → emphasize mean-reversion (45%), reduce momentum (20%)
//   - Volatile → emphasize quality/defense (40%), reduce all offense
//
// Additional innovations:
//   - Volatility targeting: scale exposure inversely to recent vol
//   - Market breadth: use cross-sectional advance/decline as regime input
//   - Cross-sectional rank normalization for all factors
//   - Hurst exponent proxy for persistence detection
import type { Signal, SymbolSnapshot } from "../strategyTypes";
import type { Scorer } from "../backtest";

export interface PrismScorerOptions {
  minCloses?: number;
  /** Lookback for regime detection. */
  regimeLookback?: number;
  /** Minimum score to emit a buy signal. */
  minScoreToBuy?: number;
  /** Maximum 1-day return to still allow buying. */
  maxOneDayChasePct?: number;
  /** Target annualized volatility for position scaling (0-1). */
  targetVol?: number;
}

function avg(xs: number[]): number {
  if (xs.length === 0) return 0;
  return xs.reduce((a, b) => a + b, 0) / xs.length;
}

function std(xs: number[]): number {
  if (xs.length < 2) return 0;
  const m = avg(xs);
  return Math.sqrt(xs.reduce((s, x) => s + (x - m) ** 2, 0) / (xs.length - 1));
}

function rankNormalize(values: (number | null)[], fallback = 0.5): number[] {
  const valid = values
    .map((v, i) => ({ v, i }))
    .filter((x): x is { v: number; i: number } => x.v != null);
  if (valid.length === 0) return values.map(() => fallback);
  valid.sort((a, b) => a.v - b.v);
  const ranks = new Array<number>(values.length).fill(fallback);
  let start = 0;
  for (let i = 1; i <= valid.length; i++) {
    if (i === valid.length || valid[i].v !== valid[start].v) {
      const avgRank = (start + i - 1) / 2;
      const normalized = avgRank / Math.max(valid.length - 1, 1);
      for (let j = start; j < i; j++) ranks[valid[j].i] = normalized;
      start = i;
    }
  }
  return ranks;
}

type Regime = "trending" | "ranging" | "volatile";

/** Detect market regime from cross-sectional returns.
 *  Uses average absolute return vs average signed return (directional consistency)
 *  and realized volatility level. */
function detectRegime(allReturns: number[][], lookback: number): Regime {
  // allReturns[i] = array of daily returns for stock i
  if (allReturns.length === 0) return "ranging";
  // Compute cross-sectional averages
  const absReturns: number[] = [];
  const signedReturns: number[] = [];
  const recentVols: number[] = [];

  for (const returns of allReturns) {
    const recent = returns.slice(-lookback);
    if (recent.length < 5) continue;
    const absAvg = avg(recent.map(Math.abs));
    const signedAvg = avg(recent);
    absReturns.push(absAvg);
    signedReturns.push(Math.abs(signedAvg));
    recentVols.push(std(recent));
  }
  if (absReturns.length === 0) return "ranging";

  // Directional consistency: |avg return| / avg |return|
  // High = trending, Low = ranging
  const avgAbs = avg(absReturns);
  const avgSigned = avg(signedReturns);
  const directionalConsistency = avgAbs > 0 ? avgSigned / avgAbs : 0;

  // Volatility level: current vs longer history
  const avgVol = avg(recentVols);
  const volThreshold = 0.025; // ~2.5% daily vol = elevated

  if (avgVol > volThreshold) return "volatile";
  if (directionalConsistency > 0.35) return "trending";
  return "ranging";
}

/** Hurst exponent proxy via variance ratio test.
 *  H > 0.5 = persistent (trending), H < 0.5 = mean-reverting. */
function hurstProxy(returns: number[], lookback: number): number {
  const r = returns.slice(-lookback);
  if (r.length < 20) return 0.5;
  // Variance ratio: Var(k-period returns) / (k * Var(1-period returns))
  const k = 5;
  const var1 = std(r.slice(1).map((_, i) => r[i + 1])) ** 2;
  if (var1 === 0) return 0.5;
  const kReturns: number[] = [];
  for (let i = k; i < r.length; i += k) {
    let sum = 0;
    for (let j = i - k; j < i; j++) sum += r[j];
    kReturns.push(sum);
  }
  if (kReturns.length < 3) return 0.5;
  const varK = std(kReturns) ** 2;
  const vr = varK / (k * var1);
  // Convert variance ratio to Hurst proxy: H ≈ 0.5 + log(VR) / (2*log(k))
  return Math.max(0, Math.min(1, 0.5 + Math.log(Math.max(vr, 0.01)) / (2 * Math.log(k))));
}

/** Factor weights per regime. */
const REGIME_WEIGHTS: Record<Regime, {
  momentum: number;
  meanReversion: number;
  quality: number;
  volume: number;
  breadth: number;
}> = {
  trending: { momentum: 0.45, meanReversion: 0.10, quality: 0.15, volume: 0.20, breadth: 0.10 },
  ranging: { momentum: 0.20, meanReversion: 0.35, quality: 0.20, volume: 0.15, breadth: 0.10 },
  volatile: { momentum: 0.15, meanReversion: 0.15, quality: 0.40, volume: 0.15, breadth: 0.15 },
};

export function prismScorer(options: PrismScorerOptions = {}): Scorer {
  const {
    minCloses = 30,
    regimeLookback = 20,
    minScoreToBuy = 0.55,
    maxOneDayChasePct = 5,
    targetVol = 0.20,
  } = options;

  return async (snapshots: SymbolSnapshot[], { asOf }): Promise<Signal[]> => {
    // Phase 1: compute per-stock returns for regime detection
    const allReturns: number[][] = [];
    interface StockData {
      symbol: string;
      returns: number[];
      momentum10: number | null;
      momentum20: number | null;
      meanReversion: number | null;
      quality: number | null;
      volumeSignal: number | null;
      oneDayReturn: number;
      realizedVol: number;
      hurst: number;
    }
    const stocks: StockData[] = [];

    for (const s of snapshots) {
      const closes = s.closes;
      const volumes = s.volumes ?? [];
      if (closes.length < minCloses) continue;

      // Daily returns
      const returns: number[] = [];
      for (let i = 1; i < closes.length; i++) {
        returns.push(closes[i] / closes[i - 1] - 1);
      }
      allReturns.push(returns);

      const current = closes[closes.length - 1];
      const previous = closes[closes.length - 2];

      // Momentum factors
      const momentum10 = closes.length > 11 ? current / closes[closes.length - 11] - 1 : null;
      const momentum20 = closes.length > 21 ? current / closes[closes.length - 21] - 1 : null;

      // Mean-reversion: distance from 20-day MA (negative = oversold = buy signal in ranging)
      const ma20 = avg(closes.slice(-20));
      const meanReversion = ma20 > 0 ? -(current / ma20 - 1) : null; // inverted: oversold = positive

      // Quality: low volatility + positive earnings proxy (low drawdown)
      const recentReturns = returns.slice(-20);
      const vol20 = std(recentReturns);
      const maxDD20 = (() => {
        let peak = closes[closes.length - 21];
        let maxDd = 0;
        for (let i = closes.length - 20; i < closes.length; i++) {
          peak = Math.max(peak, closes[i]);
          maxDd = Math.min(maxDd, closes[i] / peak - 1);
        }
        return maxDd;
      })();
      // Quality score: lower vol and smaller drawdown = higher quality
      const quality = vol20 > 0 ? -vol20 * 10 + maxDD20 * 2 : null;

      // Volume signal: volume trend (5d avg / 20d avg)
      let volumeSignal: number | null = null;
      if (volumes.length >= 25) {
        const vol5 = avg(volumes.slice(-5));
        const vol20 = avg(volumes.slice(-20));
        volumeSignal = vol20 > 0 ? Math.log(vol5 / vol20) : null;
      }

      // Hurst exponent for this stock
      const hurst = hurstProxy(returns, 40);

      stocks.push({
        symbol: s.symbol,
        returns,
        momentum10,
        momentum20,
        meanReversion,
        quality,
        volumeSignal,
        oneDayReturn: current / previous - 1,
        realizedVol: vol20,
        hurst,
      });
    }
    if (stocks.length === 0) return [];

    // Phase 2: detect market regime
    const regime = detectRegime(allReturns, regimeLookback);
    const weights = REGIME_WEIGHTS[regime];

    // Phase 3: market breadth (% of stocks above their 20-day MA)
    const closesBySymbol = new Map(snapshots.map((snap) => [snap.symbol, snap.closes]));
    const aboveMA = stocks.filter((s) => {
      const closes = closesBySymbol.get(s.symbol);
      if (!closes || closes.length < 20) return false;
      return closes[closes.length - 1] > avg(closes.slice(-20));
    }).length;
    const breadth = stocks.length > 0 ? aboveMA / stocks.length : 0.5;
    // Breadth score: >0.5 is bullish, <0.5 bearish
    const breadthScore = Math.max(0, Math.min(1, breadth));

    // Phase 4: rank-normalize factors
    const momScores = rankNormalize(stocks.map((s) =>
      s.momentum20 != null && s.momentum10 != null
        ? 0.6 * s.momentum20 + 0.4 * s.momentum10
        : null,
    ));
    const mrScores = rankNormalize(stocks.map((s) => s.meanReversion));
    const qualScores = rankNormalize(stocks.map((s) => s.quality));
    const volScores = rankNormalize(stocks.map((s) => s.volumeSignal));

    // Phase 5: compute composite scores with regime-adaptive weights
    const signals: Signal[] = stocks.map((s, i) => {
      const composite =
        weights.momentum * momScores[i] +
        weights.meanReversion * mrScores[i] +
        weights.quality * qualScores[i] +
        weights.volume * volScores[i] +
        weights.breadth * breadthScore;

      // Volatility targeting: scale confidence by vol ratio
      const annualizedVol = s.realizedVol * Math.sqrt(244);
      const volScalar = annualizedVol > 0
        ? Math.min(1.2, Math.max(0.5, targetVol / annualizedVol))
        : 1;
      const adjustedScore = composite * volScalar;

      // Hurst-aware filter: in ranging regime, prefer low-Hurst (mean-reverting) stocks
      const hurstFilter = regime === "ranging" ? (s.hurst < 0.55 ? 1 : 0.85) : 1;
      const finalScore = adjustedScore * hurstFilter;

      // Risk filters
      const chaseBlocked = s.oneDayReturn > maxOneDayChasePct / 100;
      const volTooHigh = s.realizedVol > 0.06; // >6% daily vol = extreme
      const actionable =
        finalScore >= minScoreToBuy &&
        !chaseBlocked &&
        !volTooHigh;

      const regimeLabel = regime === "trending" ? "趋势" : regime === "ranging" ? "震荡" : "高波";
      const action = actionable ? "buy" : volTooHigh || (regime === "volatile" && finalScore < 0.4) ? "sell" : "hold";
      const reason = chaseBlocked
        ? "追高过滤"
        : volTooHigh
          ? "波动率过高"
          : `状态:${regimeLabel} H:${s.hurst.toFixed(2)} 宽度:${(breadth * 100).toFixed(0)}%`;

      return {
        symbol: s.symbol,
        action,
        confidence: Math.max(0, Math.min(1, finalScore)),
        size: action === "buy" ? 1 : 0,
        rationale: `棱镜: ${reason}`,
      };
    });

    return signals.sort((a, b) => b.confidence - a.confidence);
  };
}
