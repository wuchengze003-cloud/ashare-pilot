import type { BacktestResult } from "./backtest";
import type { LatestPlan } from "./latestPlan";
import { readRuntimeJson } from "./runtimeData";
import { readSignalHistorySnapshots, type SignalHistorySnapshot } from "./signalHistory";
import type { Signal } from "./strategyTypes";

export interface RuntimeSignalsSnapshot {
  signal_date?: string;
  latest_complete_date?: string;
  signals?: Signal[];
  universe_count?: number;
}

export interface RuntimeMetaSnapshot {
  generated_at?: string;
  universe_count?: number;
}

export interface RuntimeBacktestSnapshot extends BacktestResult {
  latestDate?: string;
  latestPlan?: LatestPlan;
  latestHoldings?: BacktestResult["equityCurve"][number]["positions"];
}

export interface RuntimeValidationIssue {
  code: string;
  message: string;
}

export interface RuntimeValidationInput {
  backtest: RuntimeBacktestSnapshot | null;
  signals: RuntimeSignalsSnapshot | null;
  histories: SignalHistorySnapshot[];
  meta?: RuntimeMetaSnapshot | null;
}

function nearlyEqual(a: number | undefined, b: number | undefined, tolerance = 1e-6): boolean {
  if (a === undefined || b === undefined) return a === b;
  return Math.abs(a - b) <= tolerance;
}

function signalMap(signals: Signal[] | undefined): Map<string, Signal> {
  return new Map((signals ?? []).map((s) => [s.symbol, s]));
}

function compareSignals(
  issues: RuntimeValidationIssue[],
  label: string,
  expected: Signal[] | undefined,
  actual: Signal[] | undefined,
): void {
  const expectedMap = signalMap(expected);
  const actualMap = signalMap(actual);
  if (expectedMap.size !== actualMap.size) {
    issues.push({
      code: "SIGNAL_COUNT_MISMATCH",
      message: `${label}: signal count mismatch expected=${expectedMap.size} actual=${actualMap.size}`,
    });
  }
  for (const [symbol, exp] of expectedMap) {
    const got = actualMap.get(symbol);
    if (!got) {
      issues.push({ code: "SIGNAL_MISSING", message: `${label}: missing signal ${symbol}` });
      continue;
    }
    if (exp.action !== got.action) {
      issues.push({
        code: "SIGNAL_ACTION_MISMATCH",
        message: `${label}: ${symbol} action expected=${exp.action} actual=${got.action}`,
      });
    }
    if (!nearlyEqual(exp.size, got.size)) {
      issues.push({
        code: "SIGNAL_SIZE_MISMATCH",
        message: `${label}: ${symbol} size expected=${exp.size} actual=${got.size}`,
      });
    }
    if (!nearlyEqual(exp.confidence, got.confidence)) {
      issues.push({
        code: "SIGNAL_CONFIDENCE_MISMATCH",
        message: `${label}: ${symbol} confidence expected=${exp.confidence} actual=${got.confidence}`,
      });
    }
    if ((exp.rationale ?? "") !== (got.rationale ?? "")) {
      issues.push({
        code: "SIGNAL_REASON_MISMATCH",
        message: `${label}: ${symbol} rationale expected=${exp.rationale} actual=${got.rationale}`,
      });
    }
  }
}

function compareHoldings(
  issues: RuntimeValidationIssue[],
  expected: BacktestResult["equityCurve"][number]["positions"] | undefined,
  actual: BacktestResult["equityCurve"][number]["positions"] | undefined,
): void {
  const expectedKeys = Object.keys(expected ?? {}).sort();
  const actualKeys = Object.keys(actual ?? {}).sort();
  if (expectedKeys.join(",") !== actualKeys.join(",")) {
    issues.push({
      code: "LATEST_HOLDINGS_MISMATCH",
      message: `latestHoldings symbols mismatch expected=${expectedKeys.join(",")} actual=${actualKeys.join(",")}`,
    });
    return;
  }
  for (const symbol of expectedKeys) {
    const exp = expected![symbol];
    const got = actual![symbol];
    if (exp.shares !== got.shares || !nearlyEqual(exp.price, got.price)) {
      issues.push({
        code: "LATEST_HOLDINGS_MISMATCH",
        message: `${symbol} latestHoldings mismatch expected=${JSON.stringify(exp)} actual=${JSON.stringify(got)}`,
      });
    }
  }
}

export function validateRuntimeArtifacts(input: RuntimeValidationInput): RuntimeValidationIssue[] {
  const issues: RuntimeValidationIssue[] = [];
  const { backtest, signals, histories, meta } = input;
  if (!backtest) {
    issues.push({ code: "MISSING_BACKTEST", message: "runtime backtest.json is missing" });
    return issues;
  }
  if (!signals) {
    issues.push({ code: "MISSING_SIGNALS", message: "runtime signals.json is missing" });
  }
  const latestBar = backtest.equityCurve?.at(-1);
  const latestDate = backtest.latestDate ?? latestBar?.date;
  const latestPlan = backtest.latestPlan;
  if (!latestDate || !latestBar) {
    issues.push({ code: "MISSING_LATEST_BAR", message: "backtest has no latest equity bar" });
  }
  if (!latestPlan) {
    issues.push({ code: "MISSING_LATEST_PLAN", message: "backtest.latestPlan is missing" });
  }
  if (latestDate && latestPlan && latestPlan.decisionDate !== latestDate) {
    issues.push({
      code: "LATEST_PLAN_DATE_MISMATCH",
      message: `latestPlan.decisionDate=${latestPlan.decisionDate} latestDate=${latestDate}`,
    });
  }
  if (latestBar && latestDate && latestBar.date !== latestDate) {
    issues.push({
      code: "LATEST_BAR_DATE_MISMATCH",
      message: `latest equity bar date=${latestBar.date} latestDate=${latestDate}`,
    });
  }
  compareHoldings(issues, latestBar?.positions, backtest.latestHoldings);

  if (signals && latestPlan) {
    if (signals.signal_date !== latestPlan.decisionDate) {
      issues.push({
        code: "SIGNALS_DATE_MISMATCH",
        message: `signals.signal_date=${signals.signal_date} latestPlan.decisionDate=${latestPlan.decisionDate}`,
      });
    }
    if (signals.latest_complete_date !== latestPlan.decisionDate) {
      issues.push({
        code: "SIGNALS_COMPLETE_DATE_MISMATCH",
        message: `signals.latest_complete_date=${signals.latest_complete_date} latestPlan.decisionDate=${latestPlan.decisionDate}`,
      });
    }
    compareSignals(issues, "signals.json vs latestPlan", latestPlan.signals, signals.signals);
  }
  if (
    meta?.universe_count !== undefined &&
    signals?.universe_count !== undefined &&
    meta.universe_count !== signals.universe_count
  ) {
    issues.push({
      code: "UNIVERSE_COUNT_MISMATCH",
      message: `meta.universe_count=${meta.universe_count} signals.universe_count=${signals.universe_count}`,
    });
  }

  const historiesByDate = new Map<string, SignalHistorySnapshot>();
  for (const history of histories) {
    if (historiesByDate.has(history.signal_date)) {
      issues.push({ code: "DUPLICATE_HISTORY", message: `duplicate signal history ${history.signal_date}` });
    }
    historiesByDate.set(history.signal_date, history);
  }

  if (latestPlan) {
    const latestHistory = historiesByDate.get(latestPlan.decisionDate);
    if (!latestHistory) {
      issues.push({
        code: "MISSING_LATEST_HISTORY",
        message: `missing signals-history/${latestPlan.decisionDate}.json`,
      });
    } else {
      compareSignals(issues, `history ${latestPlan.decisionDate} vs latestPlan`, latestPlan.signals, latestHistory.signals);
    }
  }

  const backtestSignalsByDate = backtest.signalsByDate ?? {};
  for (const history of histories) {
    if (!latestDate || history.signal_date >= latestDate) continue;
    if (history.signal_date < backtest.config.startDate) continue;
    const simulatedSignals = backtestSignalsByDate[history.signal_date];
    if (!simulatedSignals) {
      issues.push({
        code: "MISSING_BACKTEST_SIGNALS",
        message: `backtest.signalsByDate is missing archived date ${history.signal_date}`,
      });
      continue;
    }
    compareSignals(
      issues,
      `history ${history.signal_date} vs backtest.signalsByDate`,
      history.signals,
      simulatedSignals,
    );
  }

  for (const trade of backtest.trades ?? []) {
    if (trade.side !== "buy" || !trade.decisionDate) continue;
    const history = historiesByDate.get(trade.decisionDate);
    if (!history) continue;
    const archived = history.signals.find((s) => s.symbol === trade.symbol);
    if (!archived || archived.action !== "buy" || archived.size <= 0) {
      issues.push({
        code: "BUY_WITHOUT_ARCHIVED_SIGNAL",
        message: `${trade.symbol} bought on ${trade.tradeDate} from ${trade.decisionDate}, but archived signal was ${
          archived ? `${archived.action} size=${archived.size}` : "missing"
        }`,
      });
      continue;
    }
    if ((trade.reason ?? "") !== (archived.rationale ?? "")) {
      issues.push({
        code: "BUY_REASON_MISMATCH",
        message: `${trade.symbol} buy reason on ${trade.tradeDate} does not match archived signal`,
      });
    }
    if (trade.targetWeightAfter !== undefined && !nearlyEqual(trade.targetWeightAfter, archived.size, 1e-4)) {
      issues.push({
        code: "BUY_TARGET_WEIGHT_MISMATCH",
        message: `${trade.symbol} buy targetWeightAfter=${trade.targetWeightAfter} archived size=${archived.size}`,
      });
    }
  }

  return issues;
}

export function readRuntimeValidationInput(): RuntimeValidationInput {
  return {
    backtest: readRuntimeJson<RuntimeBacktestSnapshot>("backtest.json"),
    signals: readRuntimeJson<RuntimeSignalsSnapshot>("signals.json"),
    meta: readRuntimeJson<RuntimeMetaSnapshot>("meta.json"),
    histories: readSignalHistorySnapshots(Number.POSITIVE_INFINITY),
  };
}

export function assertRuntimeArtifacts(input: RuntimeValidationInput): void {
  const issues = validateRuntimeArtifacts(input);
  if (issues.length > 0) {
    throw new Error(
      `Runtime data validation failed:\n${issues.map((issue) => `- ${issue.code}: ${issue.message}`).join("\n")}`,
    );
  }
}
