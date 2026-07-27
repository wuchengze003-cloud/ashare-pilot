import { runBacktest, type BacktestConfig, type BacktestResult, type SymbolSeries } from "./backtest";
import { ruleBasedScorer } from "./dashboardBacktest";

export interface OptimizationCandidate {
  config: BacktestConfig;
  stats: BacktestResult["stats"];
}

export interface OptimizationResult {
  best: OptimizationCandidate;
  candidates: OptimizationCandidate[];
  validationStats: {
    jan_2026: BacktestResult["stats"];
  };
  optimizedParams: {
    maxPositions: number;
    minHoldBars: number;
    rebalanceThresholdPct: number;
    minScoreToBuy: number;
  };
  warnings: string[];
}

function rankCandidates(a: OptimizationCandidate, b: OptimizationCandidate): number {
  const sharpeDiff = b.stats.sharpe - a.stats.sharpe;
  if (Math.abs(sharpeDiff) > 0.01) return sharpeDiff;
  const drawdownDiff = b.stats.maxDrawdownPct - a.stats.maxDrawdownPct;
  if (Math.abs(drawdownDiff) > 0.1) return drawdownDiff;
  const tradeDiff = a.stats.trades - b.stats.trades;
  if (tradeDiff !== 0) return tradeDiff;
  return b.stats.totalReturnPct - a.stats.totalReturnPct;
}

function warningsFor(result: BacktestResult, sharpeTarget: number): string[] {
  const warnings: string[] = [];
  if (result.stats.sharpe < sharpeTarget) warnings.push(`夏普未达 ${sharpeTarget}`);
  if (result.stats.trades > 260) warnings.push("交易次数偏高");
  if (result.stats.maxDrawdownPct < -25) warnings.push("最大回撤偏高");
  if (result.stats.turnoverPct > 4000) warnings.push("换手率偏高");
  return warnings;
}

export async function optimizeBacktest(
  series: SymbolSeries[],
  baseConfig: BacktestConfig,
): Promise<{ result: BacktestResult; optimization: OptimizationResult }> {
  const maxPositionsSet = [4, 5, 6];
  const minHoldBarsSet = [3, 5, 7, 10];
  const thresholdSet = [0, 5, 10];
  const minScoreSet = [0.54, 0.58, 0.62];
  const candidates: OptimizationCandidate[] = [];
  const resultByKey = new Map<string, BacktestResult>();

  for (const maxPositions of maxPositionsSet) {
    for (const minHoldBars of minHoldBarsSet) {
      for (const rebalanceThresholdPct of thresholdSet) {
        for (const minScoreToBuy of minScoreSet) {
          const config: BacktestConfig = {
            ...baseConfig,
            maxPositions,
            minHoldBars,
            rebalanceThresholdPct,
          };
          const result = await runBacktest(series, config, {
            scorer: ruleBasedScorer({ minScoreToBuy }),
          });
          const key = JSON.stringify({ maxPositions, minHoldBars, rebalanceThresholdPct, minScoreToBuy });
          resultByKey.set(key, result);
          candidates.push({ config: result.config, stats: result.stats });
        }
      }
    }
  }

  candidates.sort(rankCandidates);
  const best = candidates[0];
  const optimizedParams = {
    maxPositions: best.config.maxPositions,
    minHoldBars: best.config.minHoldBars ?? 0,
    rebalanceThresholdPct: best.config.rebalanceThresholdPct ?? 0,
    minScoreToBuy: minScoreSet[0],
  };
  const bestKeyPrefix = {
    maxPositions: optimizedParams.maxPositions,
    minHoldBars: optimizedParams.minHoldBars,
    rebalanceThresholdPct: optimizedParams.rebalanceThresholdPct,
  };
  let bestResult: BacktestResult | null = null;
  for (const minScoreToBuy of minScoreSet) {
    const key = JSON.stringify({ ...bestKeyPrefix, minScoreToBuy });
    const result = resultByKey.get(key);
    if (result && result.stats.sharpe === best.stats.sharpe) {
      bestResult = result;
      optimizedParams.minScoreToBuy = minScoreToBuy;
      break;
    }
  }
  if (!bestResult) {
    for (const [key, result] of resultByKey) {
      if (result.stats.sharpe === best.stats.sharpe) {
        bestResult = result;
        optimizedParams.minScoreToBuy = JSON.parse(key).minScoreToBuy;
        break;
      }
    }
  }
  if (!bestResult) throw new Error("optimizer failed to recover best result");

  const janConfig: BacktestConfig = {
    ...bestResult.config,
    startDate: "2026-01-02",
    optimizationWindow: "jan_2026",
  };
  const janValidation = await runBacktest(series, janConfig, {
    scorer: ruleBasedScorer({ minScoreToBuy: optimizedParams.minScoreToBuy }),
  });

  const optimization: OptimizationResult = {
    best,
    candidates: candidates.slice(0, 10),
    validationStats: {
      jan_2026: janValidation.stats,
    },
    optimizedParams,
    warnings: warningsFor(bestResult, bestResult.config.sharpeTarget ?? 3),
  };

  return { result: bestResult, optimization };
}
