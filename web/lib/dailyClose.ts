import type { RuntimeBacktestSnapshot, RuntimeMetaSnapshot, RuntimeSignalsSnapshot } from "./runtimeValidation";
import {
  validateProductionRuntimeArtifacts,
  type ProductionRuntimeValidationInput,
} from "./runtimeValidation";
import type {
  ProductionGateSnapshot,
  ProductionGateStatus,
  ProductionSignalsSnapshot,
} from "./productionGate";

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
  production: ProductionRuntimeValidationInput;
  analystItems: AnalystCoverageItem[];
}

export interface DailyCloseValidationIssue {
  code: string;
  message: string;
}

export interface SignalsEndpointBody {
  status?: ProductionGateStatus;
  champion_id?: string | null;
  signal_date?: string | null;
  latest_complete_date?: string | null;
  signals?: unknown[];
}

export interface DailyCloseProductionReceipt {
  gate_generated_at: string;
  signals_generated_at: string;
  contract_sha256: string | null;
  status: ProductionGateStatus;
  champion_id: string | null;
  signal_date: string;
  latest_complete_date: string;
  signal_count: number;
}

export function buildDailyCloseProductionReceipt(
  input: ProductionRuntimeValidationInput,
): DailyCloseProductionReceipt | null {
  const { gate, signals } = input;
  if (!gate || !signals) return null;
  return {
    gate_generated_at: gate.generated_at,
    signals_generated_at: signals.generated_at,
    contract_sha256: gate.contract_sha256,
    status: gate.status,
    champion_id: gate.champion_id,
    signal_date: signals.signal_date,
    latest_complete_date: signals.latest_complete_date,
    signal_count: signals.signals.length,
  };
}

export function dailyCloseReceiptMatchesProduction(
  receipt: DailyCloseProductionReceipt | null | undefined,
  gate: ProductionGateSnapshot,
  signals: ProductionSignalsSnapshot | null,
): boolean {
  return Boolean(
    receipt &&
      signals &&
      receipt.gate_generated_at === gate.generated_at &&
      receipt.signals_generated_at === signals.generated_at &&
      receipt.contract_sha256 === gate.contract_sha256 &&
      receipt.status === gate.status &&
      receipt.champion_id === gate.champion_id &&
      receipt.signal_date === signals.signal_date &&
      receipt.latest_complete_date === signals.latest_complete_date &&
      receipt.signal_count === signals.signals.length,
  );
}

export function isShortHistoryStrategySeries(
  entry: { strategy_from?: string },
  expectedDate: string,
  coverage: { latestDate?: string; uniqueDates: number },
  minBars = 30,
): boolean {
  return Boolean(
    entry.strategy_from &&
      entry.strategy_from <= expectedDate &&
      coverage.latestDate === expectedDate &&
      coverage.uniqueDates > 0 &&
      coverage.uniqueDates < minBars,
  );
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

export function validateSignalsEndpointBody(
  body: SignalsEndpointBody,
  expectedDate: string,
): DailyCloseValidationIssue[] {
  const issues: DailyCloseValidationIssue[] = [];
  if (
    body.signal_date !== expectedDate ||
    body.latest_complete_date !== expectedDate
  ) {
    issues.push({
      code: "PRODUCTION_ENDPOINT_DATE_MISMATCH",
      message: `signal=${body.signal_date ?? "missing"}, complete=${body.latest_complete_date ?? "missing"}, expected=${expectedDate}`,
    });
  }
  if (body.status !== "active" && body.status !== "cash-only") {
    issues.push({
      code: "PRODUCTION_ENDPOINT_STATUS_INVALID",
      message: `status=${body.status ?? "missing"}`,
    });
  } else if (body.status === "cash-only") {
    if (body.champion_id != null) {
      issues.push({
        code: "PRODUCTION_ENDPOINT_CASH_HAS_CHAMPION",
        message: `cash-only endpoint champion=${body.champion_id}`,
      });
    }
    if ((body.signals?.length ?? 0) > 0) {
      issues.push({
        code: "PRODUCTION_ENDPOINT_CASH_HAS_SIGNALS",
        message: `cash-only endpoint signals=${body.signals?.length ?? 0}`,
      });
    }
  } else {
    if (!body.champion_id) {
      issues.push({
        code: "PRODUCTION_ENDPOINT_CHAMPION_MISSING",
        message: "active endpoint has no champion",
      });
    }
  }
  return issues;
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

  issues.push(
    ...validateProductionRuntimeArtifacts(input.production).map((issue) => ({
      code: issue.code,
      message: issue.message,
    })),
  );
  const productionSignals: ProductionSignalsSnapshot | null =
    input.production.signals;
  if (
    productionSignals &&
    (
      productionSignals.signal_date !== input.expectedDate ||
      productionSignals.latest_complete_date !== input.expectedDate
    )
  ) {
    issues.push({
      code: "PRODUCTION_SIGNAL_DATE_MISMATCH",
      message: `signal=${productionSignals.signal_date}, complete=${productionSignals.latest_complete_date}, expected=${input.expectedDate}`,
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
