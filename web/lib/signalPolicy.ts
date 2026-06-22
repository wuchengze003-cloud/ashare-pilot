import type { Signal } from "./strategyTypes";

export interface ExecutableSignalOptions {
  maxPositions: number;
}

function roundWeight(v: number): number {
  return Number(v.toFixed(4));
}

export function toExecutableSignals(
  signals: Signal[],
  { maxPositions }: ExecutableSignalOptions,
): Signal[] {
  const rankedBuys = signals
    .filter((s) => s.action === "buy" && s.size > 0)
    .sort((a, b) => b.confidence * b.size - a.confidence * a.size);
  const selectedBuys = rankedBuys.slice(0, Math.max(0, maxPositions));
  const selected = new Set(selectedBuys.map((s) => s.symbol));
  const selectedWeight = selectedBuys.reduce((sum, s) => sum + s.confidence * s.size, 0);
  const equalWeight = selectedBuys.length > 0 ? 1 / selectedBuys.length : 0;

  return signals
    .map((s) => {
      if (s.action !== "buy") {
        return { ...s, size: 0 };
      }
      if (!selected.has(s.symbol)) {
        return {
          ...s,
          action: "hold" as const,
          size: 0,
          rationale: `评分未进入组合前${maxPositions}，不交易`,
        };
      }
      const weight = selectedWeight > 0
        ? (s.confidence * s.size) / selectedWeight
        : equalWeight;
      return {
        ...s,
        size: roundWeight(weight),
      };
    })
    .sort((a, b) => {
      const order = { buy: 0, sell: 1, hold: 2 };
      const ao = order[a.action];
      const bo = order[b.action];
      if (ao !== bo) return ao - bo;
      return b.confidence - a.confidence;
    });
}

export function latestSignalDate<T>(
  signalsByDate: Record<string, T[]> | undefined,
): string | null {
  const dates = Object.keys(signalsByDate ?? {}).sort();
  return dates.at(-1) ?? null;
}
