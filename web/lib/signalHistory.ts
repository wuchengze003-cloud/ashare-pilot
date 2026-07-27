import type { LatestPlan } from "./latestPlan";
import type { SymbolSeries } from "./backtest";
import { listRuntimeJson, writeRuntimeJson } from "./runtimeData";

export interface ArchivedSignal {
  symbol: string;
  name?: string | null;
  theme?: string;
  action: "buy" | "hold" | "sell";
  confidence: number;
  size: number;
  rationale: string;
  signalPrice?: number | null;
  signalPriceDate?: string | null;
}

export interface SignalHistorySnapshot {
  generated_at: string;
  signal_date: string;
  execution_price: LatestPlan["executionPrice"];
  source: LatestPlan["source"];
  score_model: LatestPlan["scoreModel"];
  max_positions: number;
  min_score_to_buy?: number;
  signals: ArchivedSignal[];
}

function latestCloseAtOrBefore(series: SymbolSeries, date: string) {
  const row = [...series.klines]
    .filter((k) => k.date <= date)
    .sort((a, b) => (a.date < b.date ? -1 : 1))
    .at(-1);
  return row ? { price: row.close, date: row.date } : { price: null, date: null };
}

export function buildSignalHistorySnapshot(
  plan: LatestPlan,
  series: SymbolSeries[],
): SignalHistorySnapshot {
  const bySymbol = new Map(series.map((s) => [s.entry.symbol, s]));
  return {
    generated_at: new Date().toISOString(),
    signal_date: plan.decisionDate,
    execution_price: plan.executionPrice,
    source: plan.source,
    score_model: plan.scoreModel,
    max_positions: plan.maxPositions,
    min_score_to_buy: plan.minScoreToBuy,
    signals: plan.signals.map((signal) => {
      const symbolSeries = bySymbol.get(signal.symbol);
      const price = symbolSeries
        ? latestCloseAtOrBefore(symbolSeries, plan.decisionDate)
        : { price: null, date: null };
      return {
        symbol: signal.symbol,
        name: symbolSeries?.entry.name ?? null,
        theme: symbolSeries?.entry.theme,
        action: signal.action,
        confidence: signal.confidence,
        size: signal.size,
        rationale: signal.rationale,
        signalPrice: price.price,
        signalPriceDate: price.date,
      };
    }),
  };
}

export function writeSignalHistorySnapshot(snapshot: SignalHistorySnapshot): void {
  writeRuntimeJson(`signals-history/${snapshot.signal_date}.json`, snapshot);
}

export function readSignalHistorySnapshots(limit = 20): SignalHistorySnapshot[] {
  return listRuntimeJson<SignalHistorySnapshot>("signals-history")
    .map((item) => item.data)
    .sort((a, b) => (a.signal_date < b.signal_date ? 1 : -1))
    .slice(0, limit);
}
