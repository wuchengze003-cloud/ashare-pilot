import type { BacktestResult } from "./backtest";
import type { LatestPlan } from "./latestPlan";
import {
  PRODUCTION_GATE_FILE,
  PRODUCTION_GATE_SCHEMA_VERSION,
  PRODUCTION_SIGNALS_FILE,
  PRODUCTION_SIGNALS_SCHEMA_VERSION,
  type ProductionGateSnapshot,
  type ProductionSignalsSnapshot,
} from "./productionGate";
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
  strategy?: { id: string };
  snapshot_basis?: "latest-complete-close" | "intraday-midday";
  snapshot_label?: string;
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

export interface ProductionRuntimeValidationInput {
  gate: ProductionGateSnapshot | null;
  signals: ProductionSignalsSnapshot | null;
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
  for (const [kind, model] of [
    ["SHADOW", latestPlan?.shadowModel],
    ["CHAMPION", latestPlan?.championModel],
  ] as const) {
    if (!model || !latestPlan) continue;
    if (model.decision_date !== latestPlan.decisionDate) {
      issues.push({
        code: `${kind}_MODEL_DATE_MISMATCH`,
        message: `${kind.toLowerCase()}.decision_date=${model.decision_date} latestPlan.decisionDate=${latestPlan.decisionDate}`,
      });
    }
    if (!model.model_version.trim() || !model.feature_version.trim()) {
      issues.push({
        code: `${kind}_MODEL_VERSION_MISSING`,
        message: `${kind.toLowerCase()} model_version and feature_version are required`,
      });
    }
    if (model.data_cutoff > model.decision_date) {
      issues.push({
        code: `${kind}_MODEL_FUTURE_DATA`,
        message: `${kind.toLowerCase()}.data_cutoff=${model.data_cutoff} exceeds decision_date=${model.decision_date}`,
      });
    }
    const symbols = model.predictions.map((prediction) => prediction.symbol);
    if (new Set(symbols).size !== symbols.length) {
      issues.push({
        code: `${kind}_MODEL_DUPLICATE_SYMBOL`,
        message: `${kind.toLowerCase()} predictions contain duplicate symbols`,
      });
    }
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
    const expectedCompleteDate = backtest.snapshot_basis === "intraday-midday"
      ? backtest.equityCurve
          .map((bar) => bar.date)
          .filter((date) => date < latestPlan.decisionDate)
          .sort()
          .at(-1)
      : latestPlan.decisionDate;
    if (signals.latest_complete_date !== expectedCompleteDate) {
      issues.push({
        code: "SIGNALS_COMPLETE_DATE_MISMATCH",
        message: `signals.latest_complete_date=${signals.latest_complete_date} expected=${expectedCompleteDate}`,
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
  const expectedStrategyId = backtest.strategy?.id ?? "momentum-v1";
  for (const history of histories) {
    if (history.strategy_id !== expectedStrategyId) {
      issues.push({
        code: "HISTORY_STRATEGY_MISMATCH",
        message: `history ${history.signal_date} strategy=${history.strategy_id} expected=${expectedStrategyId}`,
      });
      continue;
    }
    if (historiesByDate.has(history.signal_date)) {
      issues.push({ code: "DUPLICATE_HISTORY", message: `duplicate signal history ${history.signal_date}` });
    }
    historiesByDate.set(history.signal_date, history);
  }

  if (latestPlan) {
    const latestHistory = historiesByDate.get(latestPlan.decisionDate);
    if (latestHistory) {
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

export function validateProductionRuntimeArtifacts(
  input: ProductionRuntimeValidationInput,
): RuntimeValidationIssue[] {
  const issues: RuntimeValidationIssue[] = [];
  const { gate, signals } = input;
  if (!gate) {
    issues.push({
      code: "MISSING_PRODUCTION_GATE",
      message: `${PRODUCTION_GATE_FILE} is missing`,
    });
    return issues;
  }
  if (gate.schema_version !== PRODUCTION_GATE_SCHEMA_VERSION) {
    issues.push({
      code: "INVALID_PRODUCTION_GATE_SCHEMA",
      message: `production gate schema=${String(gate.schema_version)}`,
    });
  }
  if (gate.status !== "active" && gate.status !== "cash-only") {
    issues.push({
      code: "INVALID_PRODUCTION_GATE_STATUS",
      message: `production gate status=${String(gate.status)}`,
    });
  }
  if (!signals) {
    issues.push({
      code: "MISSING_PRODUCTION_SIGNALS",
      message: `${PRODUCTION_SIGNALS_FILE} is missing`,
    });
    return issues;
  }
  if (signals.schema_version !== PRODUCTION_SIGNALS_SCHEMA_VERSION) {
    issues.push({
      code: "INVALID_PRODUCTION_SIGNALS_SCHEMA",
      message: `production signals schema=${String(signals.schema_version)}`,
    });
  }
  if (signals.status !== gate.status) {
    issues.push({
      code: "PRODUCTION_STATUS_MISMATCH",
      message: `gate=${gate.status} signals=${signals.status}`,
    });
  }
  if (signals.champion_id !== gate.champion_id) {
    issues.push({
      code: "PRODUCTION_CHAMPION_MISMATCH",
      message: `gate=${gate.champion_id ?? "none"} signals=${signals.champion_id ?? "none"}`,
    });
  }
  if (signals.gate_generated_at !== gate.generated_at) {
    issues.push({
      code: "PRODUCTION_GATE_GENERATION_MISMATCH",
      message: `gate=${gate.generated_at} signals=${signals.gate_generated_at}`,
    });
  }
  if (signals.contract_sha256 !== gate.contract_sha256) {
    issues.push({
      code: "PRODUCTION_CONTRACT_MISMATCH",
      message: `gate=${gate.contract_sha256 ?? "none"} signals=${signals.contract_sha256 ?? "none"}`,
    });
  }
  if (
    signals.reason_codes.length !== gate.reason_codes.length ||
    signals.reason_codes.some((code, index) => code !== gate.reason_codes[index])
  ) {
    issues.push({
      code: "PRODUCTION_REASON_CODES_MISMATCH",
      message: `gate=${gate.reason_codes.join(",")} signals=${signals.reason_codes.join(",")}`,
    });
  }
  if (!signals.signal_date || !signals.latest_complete_date) {
    issues.push({
      code: "PRODUCTION_SIGNAL_DATE_MISSING",
      message: "production signal_date and latest_complete_date are required",
    });
  }
  if (
    signals.signal_basis !== "latest-complete-close" &&
    signals.signal_basis !== "intraday-midday"
  ) {
    issues.push({
      code: "INVALID_PRODUCTION_SIGNAL_BASIS",
      message: `production signal_basis=${String(signals.signal_basis)}`,
    });
  }
  if (gate.status === "cash-only") {
    if (gate.champion_id !== null || signals.champion_id !== null) {
      issues.push({
        code: "CASH_ONLY_HAS_CHAMPION",
        message: "cash-only production state cannot name a champion",
      });
    }
    if (signals.signals.length > 0) {
      issues.push({
        code: "CASH_ONLY_HAS_SIGNALS",
        message: `cash-only production state contains ${signals.signals.length} signals`,
      });
    }
    if (gate.reason_codes.length === 0 || signals.reason_codes.length === 0) {
      issues.push({
        code: "CASH_ONLY_REASON_MISSING",
        message: "cash-only production state must explain why opening signals are blocked",
      });
    }
  } else {
    if (!gate.champion_id || !signals.champion_id) {
      issues.push({
        code: "ACTIVE_CHAMPION_MISSING",
        message: "active production state requires one champion",
      });
    }
    if (gate.reason_codes.length > 0 || signals.reason_codes.length > 0) {
      issues.push({
        code: "ACTIVE_GATE_HAS_FAILURES",
        message: "active production state cannot contain failure reason codes",
      });
    }
  }
  return issues;
}

export function readRuntimeValidationInput(): RuntimeValidationInput {
  const backtest = readRuntimeJson<RuntimeBacktestSnapshot>("backtest.json");
  const strategyId = backtest?.strategy?.id ?? "momentum-v1";
  return {
    backtest,
    signals: readRuntimeJson<RuntimeSignalsSnapshot>("signals.json"),
    meta: readRuntimeJson<RuntimeMetaSnapshot>("meta.json"),
    histories: readSignalHistorySnapshots(Number.POSITIVE_INFINITY, strategyId),
  };
}

export function readProductionRuntimeValidationInput(): ProductionRuntimeValidationInput {
  let gate: ProductionGateSnapshot | null = null;
  let signals: ProductionSignalsSnapshot | null = null;
  try {
    gate = readRuntimeJson<ProductionGateSnapshot>(PRODUCTION_GATE_FILE);
  } catch {
    gate = null;
  }
  try {
    signals = readRuntimeJson<ProductionSignalsSnapshot>(PRODUCTION_SIGNALS_FILE);
  } catch {
    signals = null;
  }
  return { gate, signals };
}

export function assertRuntimeArtifacts(input: RuntimeValidationInput): void {
  const issues = validateRuntimeArtifacts(input);
  if (issues.length > 0) {
    throw new Error(
      `Runtime data validation failed:\n${issues.map((issue) => `- ${issue.code}: ${issue.message}`).join("\n")}`,
    );
  }
}

export function assertProductionRuntimeArtifacts(
  input: ProductionRuntimeValidationInput,
): void {
  const issues = validateProductionRuntimeArtifacts(input);
  if (issues.length > 0) {
    throw new Error(
      `Production runtime validation failed:\n${issues.map((issue) => `- ${issue.code}: ${issue.message}`).join("\n")}`,
    );
  }
}
