import type { RuntimeBacktestSnapshot, RuntimeMetaSnapshot, RuntimeSignalsSnapshot } from "./runtimeValidation";

export interface MarketSeriesCoverage {
  symbol: string;
  latestDate?: string;
}

export interface AnalystCoverageItem {
  symbol: string;
  current_price?: number | null;
  current_price_as_of?: string | null;
}

export interface DailyCloseValidationInput {
  expectedDate: string;
  expectedUniverseCount: number;
  expectedSymbols?: string[];
  benchmarkLatestDate?: string;
  series: MarketSeriesCoverage[];
  allowedMissingSeriesSymbols?: string[];
  allowedStaleSymbols?: string[];
  backtest: RuntimeBacktestSnapshot | null;
  signals: RuntimeSignalsSnapshot | null;
  meta: RuntimeMetaSnapshot | null;
  analystItems: AnalystCoverageItem[];
}

export interface DailyCloseValidationIssue {
  code: string;
  message: string;
}

export function shanghaiDateTimeParts(now = new Date()): {
  date: string;
  hour: number;
  minute: number;
} {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    hour12: false,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).formatToParts(now);
  const get = (type: string) => parts.find((part) => part.type === type)?.value ?? "00";
  return {
    date: `${get("year")}-${get("month")}-${get("day")}`,
    hour: Number(get("hour")),
    minute: Number(get("minute")),
  };
}

export function parseSymbolList(value?: string): string[] {
  return [...new Set((value ?? "").split(",").map((item) => item.trim()).filter(Boolean))];
}

export function validateDailyCloseData(input: DailyCloseValidationInput): DailyCloseValidationIssue[] {
  const issues: DailyCloseValidationIssue[] = [];
  const allowedStale = new Set(input.allowedStaleSymbols ?? []);
  const allowedMissingSeries = new Set(input.allowedMissingSeriesSymbols ?? []);

  if (input.benchmarkLatestDate !== input.expectedDate) {
    issues.push({
      code: "BENCHMARK_DATE_MISMATCH",
      message: `benchmark latest=${input.benchmarkLatestDate ?? "missing"}, expected=${input.expectedDate}`,
    });
  }

  if (input.expectedSymbols) {
    const availableSymbols = new Set(input.series.map((item) => item.symbol));
    const missingSymbols = input.expectedSymbols.filter(
      (symbol) => !availableSymbols.has(symbol) && !allowedMissingSeries.has(symbol),
    );
    if (missingSymbols.length > 0) {
      issues.push({
        code: "MISSING_PRICE_SERIES",
        message: missingSymbols.join(","),
      });
    }
  } else if (input.series.length !== input.expectedUniverseCount - allowedMissingSeries.size) {
    issues.push({
      code: "SERIES_COUNT_MISMATCH",
      message: `price series=${input.series.length}, expected=${input.expectedUniverseCount - allowedMissingSeries.size}`,
    });
  }
  const staleSeries = input.series.filter(
    (item) => item.latestDate !== input.expectedDate && !allowedStale.has(item.symbol),
  );
  if (staleSeries.length > 0) {
    issues.push({
      code: "STALE_PRICE_SERIES",
      message: staleSeries
        .map((item) => `${item.symbol}:${item.latestDate ?? "missing"}`)
        .join(","),
    });
  }

  const latestDate = input.backtest?.latestDate ?? input.backtest?.equityCurve?.at(-1)?.date;
  if (latestDate !== input.expectedDate) {
    issues.push({
      code: "BACKTEST_DATE_MISMATCH",
      message: `backtest latest=${latestDate ?? "missing"}, expected=${input.expectedDate}`,
    });
  }
  if (input.backtest?.snapshot_basis !== "latest-complete-close") {
    issues.push({
      code: "INVALID_SNAPSHOT_BASIS",
      message: `snapshot_basis=${input.backtest?.snapshot_basis ?? "missing"}`,
    });
  }
  if (input.backtest?.latestPlan?.decisionDate !== input.expectedDate) {
    issues.push({
      code: "PLAN_DATE_MISMATCH",
      message: `latestPlan=${input.backtest?.latestPlan?.decisionDate ?? "missing"}, expected=${input.expectedDate}`,
    });
  }
  if (input.signals?.signal_date !== input.expectedDate) {
    issues.push({
      code: "SIGNAL_DATE_MISMATCH",
      message: `signals=${input.signals?.signal_date ?? "missing"}, expected=${input.expectedDate}`,
    });
  }
  if (input.meta?.universe_count !== input.expectedUniverseCount) {
    issues.push({
      code: "META_UNIVERSE_COUNT_MISMATCH",
      message: `meta universe=${input.meta?.universe_count ?? "missing"}, expected=${input.expectedUniverseCount}`,
    });
  }

  if (input.analystItems.length !== input.expectedUniverseCount) {
    issues.push({
      code: "ANALYST_COUNT_MISMATCH",
      message: `analyst items=${input.analystItems.length}, expected=${input.expectedUniverseCount}`,
    });
  }
  const incompleteAnalyst = input.analystItems.filter(
    (item) =>
      item.current_price == null ||
      !item.current_price_as_of ||
      !item.current_price_as_of.startsWith(input.expectedDate),
  );
  if (incompleteAnalyst.length > 0) {
    issues.push({
      code: "INCOMPLETE_ANALYST_SNAPSHOT",
      message: incompleteAnalyst.map((item) => item.symbol).join(","),
    });
  }

  return issues;
}
