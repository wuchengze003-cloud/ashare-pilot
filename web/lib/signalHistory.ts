import type { LatestPlan } from "./latestPlan";
import type { SymbolSeries } from "./backtest";
import { listRuntimeJson, readRuntimeJson, writeRuntimeJson } from "./runtimeData";

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
  strategy_id: string;
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
  strategyId = "momentum-v1",
): SignalHistorySnapshot {
  const bySymbol = new Map(series.map((s) => [s.entry.symbol, s]));
  return {
    strategy_id: strategyId,
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

export function writeSignalHistorySnapshot(
  snapshot: SignalHistorySnapshot,
  strategyId = "momentum-v1",
  options: { writeFlat?: boolean } = {},
): void {
  if (snapshot.strategy_id !== strategyId) {
    throw new Error(
      `signal history strategy mismatch: snapshot=${snapshot.strategy_id} path=${strategyId}`,
    );
  }
  const strategyPath = `strategies/${strategyId}/history/${snapshot.signal_date}.json`;
  const existing = readRuntimeJson<Partial<SignalHistorySnapshot>>(strategyPath);
  if (existing) {
    const normalizedExisting = {
      ...existing,
      strategy_id: existing.strategy_id ?? strategyId,
    };
    const { generated_at: _existingGeneratedAt, ...existingContent } = normalizedExisting;
    const { generated_at: _newGeneratedAt, ...newContent } = snapshot;
    if (JSON.stringify(existingContent) !== JSON.stringify(newContent)) {
      throw new Error(
        `immutable signal history conflict: strategy=${strategyId} date=${snapshot.signal_date}`,
      );
    }
  }
  if (!existing || existing.strategy_id !== strategyId) {
    writeRuntimeJson(strategyPath, snapshot);
  }
  // The flat path is the active runtime contract. Per-strategy comparison runs
  // must not overwrite it with non-active strategy plans for the same date.
  if (options.writeFlat ?? true) {
    writeRuntimeJson(`signals-history/${snapshot.signal_date}.json`, snapshot);
  }
}

export function readSignalHistorySnapshots(limit = 20, strategyId?: string): SignalHistorySnapshot[] {
  const dir = strategyId ? `strategies/${strategyId}/history` : "signals-history";
  return listRuntimeJson<SignalHistorySnapshot>(dir)
    .map((item) => item.data)
    .filter((snapshot) => !strategyId || snapshot.strategy_id === strategyId)
    .sort((a, b) => (a.signal_date < b.signal_date ? 1 : -1))
    .slice(0, limit);
}
