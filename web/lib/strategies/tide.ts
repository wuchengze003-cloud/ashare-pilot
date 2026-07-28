// 潮汐 (Tide) — Capital Flow Microstructure Strategy
//
// Core thesis: institutional capital leaves footprints in volume-price
// relationships before they manifest in price trends. By decomposing volume
// into directional flow, detecting accumulation/distribution phases, and
// tracking "smart money" proxies, we can front-run momentum shifts.
//
// Factors:
//   1. Capital Flow Momentum (30%): volume-weighted directional return
//   2. OBV Trend (25%): On-Balance Volume slope as net pressure gauge
//   3. Volume-Price Divergence (20%): detects exhaustion / stealth accumulation
//   4. Accumulation Phase (15%): volume expansion + price compression = 吸筹
//   5. Large Order Proxy (10%): abnormal volume spikes with direction
//
// Data: uses only daily OHLCV (available in backtest). In live mode, can be
// augmented with Tushare moneyflow / top_list via pyserver.
import type { Signal, SymbolSnapshot } from "../strategyTypes";
import type { Scorer } from "../backtest";

export interface TideScorerOptions {
  minCloses?: number;
  /** Lookback for capital flow momentum calculation. */
  flowLookback?: number;
  /** Lookback for OBV slope calculation. */
  obvLookback?: number;
  /** Volume spike threshold (multiple of 20-day average). */
  spikeThreshold?: number;
  /** Minimum score to emit a buy signal. */
  minScoreToBuy?: number;
  /** Maximum 1-day return to still allow buying (anti-chase). */
  maxOneDayChasePct?: number;
}

function avg(xs: number[]): number {
  if (xs.length === 0) return 0;
  return xs.reduce((a, b) => a + b, 0) / xs.length;
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

/** Capital Flow Momentum: sum of (daily_return * volume_ratio) over lookback.
 *  Positive = net capital inflow; negative = net outflow. */
function capitalFlowMomentum(closes: number[], volumes: number[], lookback: number): number | null {
  if (closes.length < lookback + 1 || volumes.length < lookback + 1) return null;
  const volBase = avg(volumes.slice(-lookback - 20, -lookback));
  if (volBase <= 0) return null;
  let flow = 0;
  for (let i = closes.length - lookback; i < closes.length; i++) {
    const ret = closes[i] / closes[i - 1] - 1;
    const volRatio = volumes[i] / volBase;
    flow += ret * volRatio;
  }
  return flow;
}

/** OBV slope: linear regression slope of On-Balance Volume over lookback. */
function obvSlope(closes: number[], volumes: number[], lookback: number): number | null {
  if (closes.length < lookback + 1 || volumes.length < lookback + 1) return null;
  // Compute OBV series for the lookback window
  const obv: number[] = [0];
  const start = closes.length - lookback - 1;
  for (let i = start + 1; i < closes.length; i++) {
    const dir = closes[i] > closes[i - 1] ? 1 : closes[i] < closes[i - 1] ? -1 : 0;
    obv.push(obv[obv.length - 1] + dir * volumes[i]);
  }
  // Normalize OBV by average volume to make it comparable across stocks
  const avgVol = avg(volumes.slice(-lookback));
  if (avgVol <= 0) return null;
  const normObv = obv.map((v) => v / avgVol);
  // Linear regression slope
  const n = normObv.length;
  const xMean = (n - 1) / 2;
  const yMean = avg(normObv);
  let num = 0, den = 0;
  for (let i = 0; i < n; i++) {
    num += (i - xMean) * (normObv[i] - yMean);
    den += (i - xMean) ** 2;
  }
  return den > 0 ? num / den : 0;
}

/** Volume-Price Divergence: price making new highs/lows but volume disagreeing.
 *  Returns positive when volume confirms price, negative on divergence. */
function volumePriceDivergence(closes: number[], volumes: number[], lookback: number): number | null {
  if (closes.length < lookback || volumes.length < lookback) return null;
  const recentCloses = closes.slice(-lookback);
  const recentVols = volumes.slice(-lookback);
  const half = Math.floor(lookback / 2);
  // Price trend: compare second half avg to first half avg
  const priceFirst = avg(recentCloses.slice(0, half));
  const priceSecond = avg(recentCloses.slice(half));
  const priceUp = priceSecond > priceFirst;
  // Volume trend
  const volFirst = avg(recentVols.slice(0, half));
  const volSecond = avg(recentVols.slice(half));
  const volUp = volSecond > volFirst;
  if (priceUp && volUp) return 1;       // Healthy: price up + volume up
  if (!priceUp && !volUp) return 0.6;   // Quiet decline, less dangerous
  if (priceUp && !volUp) return -0.5;   // Bearish divergence: price up, volume fading
  return -0.8;                           // Most bearish: price down, volume up (distribution)
}

/** Accumulation detection: volume expanding while price range compresses.
 *  High score = likely institutional accumulation (吸筹). */
function accumulationScore(closes: number[], volumes: number[], lookback: number): number | null {
  if (closes.length < lookback || volumes.length < lookback) return null;
  const recentCloses = closes.slice(-lookback);
  const recentVols = volumes.slice(-lookback);
  const half = Math.floor(lookback / 2);
  // Volume expansion: second half vs first half
  const volExpansion = avg(recentVols.slice(half)) / Math.max(avg(recentVols.slice(0, half)), 1);
  // Price compression: range of second half vs first half
  const range1 = Math.max(...recentCloses.slice(0, half)) - Math.min(...recentCloses.slice(0, half));
  const range2 = Math.max(...recentCloses.slice(half)) - Math.min(...recentCloses.slice(half));
  const priceCompression = range1 > 0 ? 1 - Math.min(range2 / range1, 2) / 2 : 0;
  // Accumulation = volume up + price range tight
  const score = (volExpansion - 1) * 0.6 + priceCompression * 0.4;
  return Math.max(-1, Math.min(1, score));
}

/** Large order proxy: count of volume-spike days with positive direction. */
function largeOrderScore(closes: number[], volumes: number[], lookback: number, spikeThreshold: number): number | null {
  if (closes.length < lookback + 1 || volumes.length < lookback + 1) return null;
  const volBase = avg(volumes.slice(-lookback - 20, -lookback));
  if (volBase <= 0) return null;
  let positiveSpikes = 0;
  let negativeSpikes = 0;
  for (let i = closes.length - lookback; i < closes.length; i++) {
    if (volumes[i] > volBase * spikeThreshold) {
      if (closes[i] > closes[i - 1]) positiveSpikes++;
      else negativeSpikes++;
    }
  }
  const total = positiveSpikes + negativeSpikes;
  if (total === 0) return 0;
  return (positiveSpikes - negativeSpikes) / total;
}

export function tideScorer(options: TideScorerOptions = {}): Scorer {
  const {
    minCloses = 30,
    flowLookback = 10,
    obvLookback = 15,
    spikeThreshold = 2.0,
    minScoreToBuy = 0.56,
    maxOneDayChasePct = 5,
  } = options;

  return async (snapshots: SymbolSnapshot[], { asOf }): Promise<Signal[]> => {
    interface Scored {
      symbol: string;
      flowMom: number | null;
      obv: number | null;
      divergence: number | null;
      accumulation: number | null;
      largeOrder: number | null;
      oneDayReturn: number;
      ma5: number;
      ma20: number;
      momentum20: number;
    }
    const scored: Scored[] = [];

    for (const s of snapshots) {
      const closes = s.closes;
      const volumes = s.volumes ?? [];
      if (closes.length < minCloses || volumes.length < minCloses) continue;

      const current = closes[closes.length - 1];
      const previous = closes[closes.length - 2];
      scored.push({
        symbol: s.symbol,
        flowMom: capitalFlowMomentum(closes, volumes, flowLookback),
        obv: obvSlope(closes, volumes, obvLookback),
        divergence: volumePriceDivergence(closes, volumes, 20),
        accumulation: accumulationScore(closes, volumes, 20),
        largeOrder: largeOrderScore(closes, volumes, 15, spikeThreshold),
        oneDayReturn: current / previous - 1,
        ma5: avg(closes.slice(-5)),
        ma20: avg(closes.slice(-20)),
        momentum20: current / closes[closes.length - 21] - 1,
      });
    }
    if (scored.length === 0) return [];

    // Rank-normalize each factor
    const flowScores = rankNormalize(scored.map((s) => s.flowMom));
    const obvScores = rankNormalize(scored.map((s) => s.obv));
    const divScores = rankNormalize(scored.map((s) => s.divergence));
    const accScores = rankNormalize(scored.map((s) => s.accumulation));
    const loScores = rankNormalize(scored.map((s) => s.largeOrder));

    const signals: Signal[] = scored.map((s, i) => {
      // Weighted composite: flow 30% + OBV 25% + divergence 20% + accumulation 15% + large order 10%
      const composite =
        0.30 * flowScores[i] +
        0.25 * obvScores[i] +
        0.20 * divScores[i] +
        0.15 * accScores[i] +
        0.10 * loScores[i];

      // Trend filter: don't buy against major trend
      const trendBroken = s.ma5 < s.ma20 * 0.97 && s.momentum20 < -0.05;
      const chaseBlocked = s.oneDayReturn > maxOneDayChasePct / 100;
      // Require at least mild uptrend or accumulation signal
      const hasFlowSupport = (s.flowMom ?? 0) > 0 || (s.accumulation ?? 0) > 0.2;
      const actionable =
        composite >= minScoreToBuy &&
        !trendBroken &&
        !chaseBlocked &&
        hasFlowSupport;

      const flowText = s.flowMom != null ? (s.flowMom * 100).toFixed(2) : "-";
      const obvText = s.obv != null ? s.obv.toFixed(3) : "-";
      const action = actionable ? "buy" : trendBroken ? "sell" : "hold";
      const reason = trendBroken
        ? "趋势破坏"
        : chaseBlocked
          ? "追高过滤"
          : !hasFlowSupport
            ? "资金流不支持"
            : `资金流入${flowText} OBV${obvText}`;

      return {
        symbol: s.symbol,
        action,
        confidence: Math.max(0, Math.min(1, composite)),
        size: action === "buy" ? 1 : 0,
        rationale: `潮汐: ${reason}`,
      };
    });

    return signals.sort((a, b) => b.confidence - a.confidence);
  };
}
