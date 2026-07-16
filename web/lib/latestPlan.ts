import type { FundamentalSnapshot, Scorer, SymbolSeries } from "./backtest";
import { toExecutableSignals } from "./signalPolicy";
import type { Signal, SymbolSnapshot } from "./strategyTypes";
import type { ShadowModelSnapshot } from "./mlShadow";
import { isStrategyEntryAsOf, resolveEntryAsOf } from "./universe";

export interface LatestPlan {
  decisionDate: string;
  executionPrice: "next_open";
  source: "dashboard-latest-close" | "qlib-promoted";
  scoreModel: "dashboard-rule" | "qlib-model";
  maxPositions: number;
  minScoreToBuy?: number;
  signals: Signal[];
  /** Research output only. It cannot alter V1 orders while stage=shadow. */
  shadowModel?: ShadowModelSnapshot;
  /** Promoted model that generated the executable plan. */
  championModel?: ShadowModelSnapshot;
}

export function buildPromotedModelPlan(
  snapshot: ShadowModelSnapshot,
  maxPositions: number,
): LatestPlan {
  if (snapshot.stage !== "champion") {
    throw new Error(`model ${snapshot.model_version} is not champion`);
  }
  if (snapshot.data_cutoff > snapshot.decision_date) {
    throw new Error("champion prediction uses future data");
  }
  return {
    decisionDate: snapshot.decision_date,
    executionPrice: "next_open",
    source: "qlib-promoted",
    scoreModel: "qlib-model",
    maxPositions,
    championModel: snapshot,
    signals: snapshot.predictions
      .filter((prediction): prediction is typeof prediction & { action: "buy" | "hold" | "sell" } =>
        prediction.action !== "cash")
      .map((prediction) => ({
        symbol: prediction.symbol,
        action: prediction.action,
        confidence: prediction.confidence,
        size: prediction.action === "buy" ? prediction.targetWeight : 0,
        rationale: `${prediction.reasonCodes.join("/")} 3日预期${((prediction.expectedReturns.d3 ?? 0) * 100).toFixed(1)}%`,
        modelVersion: snapshot.model_version,
        expectedReturns: prediction.expectedReturns,
        downsideRisk: prediction.downsideRisk,
        reasonCodes: prediction.reasonCodes,
      })),
  };
}

function latestFundamentalAsOf(
  fundamentals: FundamentalSnapshot[] | undefined,
  date: string,
): SymbolSnapshot["fundamental"] | undefined {
  if (!fundamentals || fundamentals.length === 0) return undefined;
  const available = fundamentals.filter((f) => f.effective_date && f.effective_date <= date);
  if (available.length === 0) return undefined;
  available.sort((a, b) => (a.effective_date! < b.effective_date! ? -1 : 1));
  const latest = available[available.length - 1];
  return {
    pe_ttm: latest.pe_ttm,
    pb: latest.pb,
    market_cap: latest.market_cap,
    profit_yoy: latest.profit_yoy,
  };
}

export function buildSnapshotsAsOf(
  series: SymbolSeries[],
  decisionDate: string,
): SymbolSnapshot[] {
  return series
    .filter((s) => isStrategyEntryAsOf(s.entry, decisionDate))
    .map((s) => {
      const entry = resolveEntryAsOf(s.entry, decisionDate);
      const upto = s.klines.filter((k) => k.date <= decisionDate);
      return {
        symbol: entry.symbol,
        name: entry.name,
        theme: entry.theme,
        note: entry.note,
        closes: upto.map((k) => k.close),
        volumes: upto.map((k) => k.volume),
        global_supply: entry.global_supply ?? null,
        fundamental: latestFundamentalAsOf(s.fundamentals, decisionDate),
      };
    })
    .filter((s) => s.closes.length > 0);
}

export async function buildLatestPlan(
  series: SymbolSeries[],
  opts: {
    decisionDate: string;
    scorer: Scorer;
    maxPositions: number;
    minScoreToBuy?: number;
  },
): Promise<LatestPlan> {
  const snapshots = buildSnapshotsAsOf(series, opts.decisionDate);
  const rawSignals = await opts.scorer(snapshots, {
    asOf: opts.decisionDate,
    mode: "backtest",
  });

  return {
    decisionDate: opts.decisionDate,
    executionPrice: "next_open",
    source: "dashboard-latest-close",
    scoreModel: "dashboard-rule",
    maxPositions: opts.maxPositions,
    minScoreToBuy: opts.minScoreToBuy,
    signals: toExecutableSignals(rawSignals, { maxPositions: opts.maxPositions }),
  };
}
