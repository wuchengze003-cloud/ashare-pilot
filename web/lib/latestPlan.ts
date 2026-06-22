import type { FundamentalSnapshot, Scorer, SymbolSeries } from "./backtest";
import { toExecutableSignals } from "./signalPolicy";
import type { Signal, SymbolSnapshot } from "./strategyTypes";

export interface LatestPlan {
  decisionDate: string;
  executionPrice: "next_open";
  source: "dashboard-latest-close";
  scoreModel: "dashboard-rule";
  maxPositions: number;
  minScoreToBuy?: number;
  signals: Signal[];
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
    .map((s) => {
      const upto = s.klines.filter((k) => k.date <= decisionDate);
      return {
        symbol: s.entry.symbol,
        name: s.entry.name,
        theme: s.entry.theme,
        note: s.entry.note,
        closes: upto.map((k) => k.close),
        volumes: upto.map((k) => k.volume),
        global_supply: s.entry.global_supply ?? null,
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
