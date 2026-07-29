import fs from "node:fs";
import crypto from "node:crypto";
import path from "node:path";
import type { Signal } from "./strategyTypes";
import { readRuntimeJson } from "./runtimeData";

export const PRODUCTION_GATE_SCHEMA_VERSION = 2;
export const PRODUCTION_SIGNALS_SCHEMA_VERSION = 2;
export const PRODUCTION_GATE_FILE = "production-gate.json";
export const PRODUCTION_SIGNALS_FILE = "production-signals.json";

export type ProductionGateStatus = "active" | "cash-only";

export interface ProductionCandidateSummary {
  candidate_id: string;
  family: string;
  signal_frequency: "1d" | "1d+5min" | null;
  daily_gates_passed: boolean;
  oos_sharpe: number | null;
  frozen_sharpe: number | null;
  oos_annualized_return_pct: number | null;
  frozen_total_return_pct: number | null;
  oos_max_drawdown_pct: number | null;
  frozen_max_drawdown_pct: number | null;
  oos_calmar: number | null;
  oos_upside_capture_pct: number | null;
  positive_oos_fold_share_pct: number | null;
  bootstrap_probability_pct: number | null;
  oos_closed_trades: number | null;
  failed_gate_codes: string[];
}

export interface ProductionGateSnapshot {
  schema_version: 2;
  generated_at: string;
  status: ProductionGateStatus;
  champion_id: string | null;
  contract_sha256: string | null;
  contract_version: string | null;
  feature_version: string | null;
  panel_start: string | null;
  panel_end: string | null;
  complete_daily_trading_days: number | null;
  daily_status: string | null;
  minute_status: string | null;
  minute_coverage_pct: number | null;
  required_symbol_days: number | null;
  available_symbol_days: number | null;
  reason_codes: string[];
  message: string;
  candidates: ProductionCandidateSummary[];
}

export interface ProductionSignalsSnapshot {
  schema_version: 2;
  generated_at: string;
  gate_generated_at: string;
  contract_sha256: string | null;
  status: ProductionGateStatus;
  champion_id: string | null;
  signal_date: string;
  latest_complete_date: string;
  signal_basis: "latest-complete-close" | "intraday-midday";
  reason_codes: string[];
  signals: Signal[];
}

interface DailyMetrics {
  sharpe?: unknown;
  total_return_pct?: unknown;
  annualized_return_pct?: unknown;
  max_drawdown_pct?: unknown;
  calmar?: unknown;
  closed_trades?: unknown;
}

interface DailyCandidate {
  candidate_id?: unknown;
  family?: unknown;
  signal_frequency?: unknown;
  daily_gates_passed?: unknown;
  oos?: DailyMetrics;
  frozen?: DailyMetrics;
  oos_upside_capture?: unknown;
  positive_oos_fold_share?: unknown;
  bootstrap_probability_sharpe_positive?: unknown;
  daily_gate_results?: unknown;
}

export interface DailyRaceReport {
  schema_version?: unknown;
  status?: unknown;
  contract_sha256?: unknown;
  contract_version?: unknown;
  feature_version?: unknown;
  panel_start?: unknown;
  panel_end?: unknown;
  complete_daily_trading_days?: unknown;
  candidates?: unknown;
  minute_candidates?: unknown;
  selected_daily_candidate?: unknown;
  production_champion?: unknown;
}

interface MinuteCandidate {
  candidate_id?: unknown;
  integrated_gates_passed?: unknown;
}

export interface MinuteRaceReport {
  schema_version?: unknown;
  status?: unknown;
  contract_sha256?: unknown;
  minute_data_sha256?: unknown;
  required_symbol_days?: unknown;
  available_symbol_days?: unknown;
  minute_coverage_pct?: unknown;
  missing_symbol_days?: unknown;
  candidates?: unknown;
  production_champion?: unknown;
}

export interface ProductionGateOptions {
  generatedAt?: string;
  deployableChampionIds?: readonly string[];
  expectedContractSha256?: string | null;
}

export interface ProductionSignalsApiPayload {
  generated_at: string;
  source: "production-gate";
  status: ProductionGateStatus;
  champion_id: string | null;
  stale: boolean;
  signal_date: string | null;
  latest_complete_date: string | null;
  signal_basis: ProductionSignalsSnapshot["signal_basis"] | null;
  reason_codes: string[];
  message: string;
  counts: Record<"buy" | "hold" | "sell", number>;
  gross_buy_weight: number;
  signals: Signal[];
}

function finite(value: unknown): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function signalFrequency(value: unknown): "1d" | "1d+5min" | null {
  return value === "1d" || value === "1d+5min" ? value : null;
}

function failedGateCodes(value: unknown): string[] {
  if (!value || typeof value !== "object" || Array.isArray(value)) return [];
  return Object.entries(value)
    .filter(([, passed]) => passed === false)
    .map(([code]) => code)
    .sort();
}

function candidateSummaries(report: DailyRaceReport | null): ProductionCandidateSummary[] {
  if (!Array.isArray(report?.candidates)) return [];
  return report.candidates.flatMap((raw) => {
    const candidate = raw as DailyCandidate;
    const candidateId = stringValue(candidate.candidate_id);
    const family = stringValue(candidate.family);
    if (!candidateId || !family) return [];
    const upside = finite(candidate.oos_upside_capture);
    const positiveFoldShare = finite(candidate.positive_oos_fold_share);
    const bootstrapProbability = finite(
      candidate.bootstrap_probability_sharpe_positive,
    );
    return [{
      candidate_id: candidateId,
      family,
      signal_frequency: signalFrequency(candidate.signal_frequency),
      daily_gates_passed: candidate.daily_gates_passed === true,
      oos_sharpe: finite(candidate.oos?.sharpe),
      frozen_sharpe: finite(candidate.frozen?.sharpe),
      oos_annualized_return_pct: finite(candidate.oos?.annualized_return_pct),
      frozen_total_return_pct: finite(candidate.frozen?.total_return_pct),
      oos_max_drawdown_pct: finite(candidate.oos?.max_drawdown_pct),
      frozen_max_drawdown_pct: finite(candidate.frozen?.max_drawdown_pct),
      oos_calmar: finite(candidate.oos?.calmar),
      oos_upside_capture_pct: upside == null ? null : upside * 100,
      positive_oos_fold_share_pct:
        positiveFoldShare == null ? null : positiveFoldShare * 100,
      bootstrap_probability_pct:
        bootstrapProbability == null ? null : bootstrapProbability * 100,
      oos_closed_trades: finite(candidate.oos?.closed_trades),
      failed_gate_codes: failedGateCodes(candidate.daily_gate_results),
    }];
  });
}

function cashOnlyGate(
  generatedAt: string,
  reasonCodes: string[],
  daily: DailyRaceReport | null,
  minute: MinuteRaceReport | null,
): ProductionGateSnapshot {
  return {
    schema_version: PRODUCTION_GATE_SCHEMA_VERSION,
    generated_at: generatedAt,
    status: "cash-only",
    champion_id: null,
    contract_sha256:
      stringValue(minute?.contract_sha256) ??
      stringValue(daily?.contract_sha256),
    contract_version: stringValue(daily?.contract_version),
    feature_version: stringValue(daily?.feature_version),
    panel_start: stringValue(daily?.panel_start),
    panel_end: stringValue(daily?.panel_end),
    complete_daily_trading_days: finite(daily?.complete_daily_trading_days),
    daily_status: stringValue(daily?.status),
    minute_status: stringValue(minute?.status),
    minute_coverage_pct: finite(minute?.minute_coverage_pct),
    required_symbol_days: finite(minute?.required_symbol_days),
    available_symbol_days: finite(minute?.available_symbol_days),
    reason_codes: [...new Set(reasonCodes)],
    message: "严格生产门禁未通过，当前不发布开仓信号。",
    candidates: candidateSummaries(daily),
  };
}

export function deriveProductionGate(
  daily: DailyRaceReport | null,
  minute: MinuteRaceReport | null,
  options: ProductionGateOptions = {},
): ProductionGateSnapshot {
  const generatedAt = options.generatedAt ?? new Date().toISOString();
  const deployable = new Set(options.deployableChampionIds ?? []);
  const reasons: string[] = [];
  const dailyCandidates = candidateSummaries(daily);
  const dailySchema = finite(daily?.schema_version);
  const declaredMinuteCandidates = Array.isArray(daily?.minute_candidates)
    ? daily.minute_candidates.flatMap((value) => {
      const candidateId = stringValue(value);
      return candidateId ? [candidateId] : [];
    })
    : null;
  const dailyOnlyRace = Boolean(
    dailySchema === 3 &&
      declaredMinuteCandidates &&
      declaredMinuteCandidates.length === 0,
  );

  if (!daily) {
    reasons.push("DAILY_REPORT_MISSING");
  } else if (
    (dailySchema !== 2 && dailySchema !== 3) ||
    dailyCandidates.length !== 3 ||
    new Set(dailyCandidates.map((candidate) => candidate.candidate_id)).size !== 3 ||
    (dailySchema === 3 &&
      dailyCandidates.some((candidate) => candidate.signal_frequency == null))
  ) {
    reasons.push("DAILY_REPORT_INVALID");
  }

  if (!dailyOnlyRace) {
    if (!minute) {
      reasons.push("MINUTE_REPORT_MISSING");
    } else if (minute.schema_version !== 2) {
      reasons.push("MINUTE_REPORT_INVALID");
    }
  }

  const dailyContract = stringValue(daily?.contract_sha256);
  const minuteContract = stringValue(minute?.contract_sha256);
  const expectedContract = stringValue(options.expectedContractSha256);
  if (dailyOnlyRace) {
    if (!dailyContract) reasons.push("RACE_CONTRACT_MISSING");
  } else {
    if ((daily || minute) && (!dailyContract || !minuteContract)) {
      reasons.push("RACE_CONTRACT_MISSING");
    } else if (dailyContract !== minuteContract) {
      reasons.push("RACE_CONTRACT_MISMATCH");
    }
  }
  if (
    expectedContract &&
    dailyContract &&
    dailyContract !== expectedContract
  ) {
    reasons.push("RACE_CONTRACT_STALE");
  }

  const minuteCoverage = finite(minute?.minute_coverage_pct);
  const missingSymbolDays = finite(minute?.missing_symbol_days);
  if (
    !dailyOnlyRace &&
    minute &&
    (minuteCoverage !== 100 ||
      missingSymbolDays !== 0 ||
      !stringValue(minute.minute_data_sha256))
  ) {
    reasons.push("MINUTE_DATA_INCOMPLETE");
  }

  const champion = dailyOnlyRace
    ? stringValue(daily?.production_champion)
    : stringValue(minute?.production_champion);
  if (dailyOnlyRace) {
    if (daily?.status !== "production_champion_selected") {
      reasons.push("NO_PRODUCTION_CHAMPION");
    }
    if (daily?.status === "production_champion_selected" && !champion) {
      reasons.push("CHAMPION_ID_MISSING");
    }
  } else {
    if (minute && minute.status !== "production_champion_selected") {
      reasons.push("NO_PRODUCTION_CHAMPION");
    }
    if (minute?.status === "production_champion_selected" && !champion) {
      reasons.push("CHAMPION_ID_MISSING");
    }
  }

  const minuteCandidates = Array.isArray(minute?.candidates)
    ? minute.candidates as MinuteCandidate[]
    : [];
  const championEvidence = champion
    ? minuteCandidates.find((candidate) => candidate.candidate_id === champion)
    : undefined;
  const dailyChampionEvidence = champion
    ? dailyCandidates.find((candidate) => candidate.candidate_id === champion)
    : undefined;
  if (champion && dailyChampionEvidence?.daily_gates_passed !== true) {
    reasons.push("CHAMPION_DAILY_GATE_MISMATCH");
  }
  if (
    champion &&
    dailyOnlyRace &&
    dailyChampionEvidence?.signal_frequency !== "1d"
  ) {
    reasons.push("CHAMPION_FREQUENCY_MISMATCH");
  }
  if (
    champion &&
    !dailyOnlyRace &&
    championEvidence?.integrated_gates_passed !== true
  ) {
    reasons.push("CHAMPION_GATE_MISMATCH");
  }
  if (champion && !deployable.has(champion)) {
    reasons.push("CHAMPION_IMPLEMENTATION_MISSING");
  }

  if (reasons.length > 0 || !champion) {
    return cashOnlyGate(
      generatedAt,
      reasons,
      daily,
      dailyOnlyRace ? null : minute,
    );
  }

  return {
    schema_version: PRODUCTION_GATE_SCHEMA_VERSION,
    generated_at: generatedAt,
    status: "active",
    champion_id: champion,
    contract_sha256: dailyOnlyRace ? dailyContract : minuteContract,
    contract_version: stringValue(daily?.contract_version),
    feature_version: stringValue(daily?.feature_version),
    panel_start: stringValue(daily?.panel_start),
    panel_end: stringValue(daily?.panel_end),
    complete_daily_trading_days: finite(daily?.complete_daily_trading_days),
    daily_status: stringValue(daily?.status),
    minute_status: dailyOnlyRace ? null : stringValue(minute?.status),
    minute_coverage_pct: dailyOnlyRace ? null : minuteCoverage,
    required_symbol_days: dailyOnlyRace
      ? null
      : finite(minute?.required_symbol_days),
    available_symbol_days: dailyOnlyRace
      ? null
      : finite(minute?.available_symbol_days),
    reason_codes: [],
    message: `唯一上线策略为 ${champion}，生产信号仅由该策略生成。`,
    candidates: dailyCandidates,
  };
}

function readJson<T>(file: string): T | null {
  try {
    if (!fs.existsSync(file)) return null;
    return JSON.parse(fs.readFileSync(file, "utf-8")) as T;
  } catch {
    return null;
  }
}

export function defaultResearchRaceDir(): string {
  return path.resolve(process.cwd(), "..", "research", "runtime", "strategy-race");
}

export function defaultProductionRaceConfig(): string {
  return path.resolve(
    process.cwd(),
    "..",
    "research",
    "config",
    "production-race-v2.json",
  );
}

export function computeProductionRaceContractSha256(
  configFile = defaultProductionRaceConfig(),
): string | null {
  const repositoryRoot = path.resolve(process.cwd(), "..");
  const files = [
    configFile,
    path.join(repositoryRoot, "config", "cost-model.json"),
    path.join(repositoryRoot, "config", "trading-constraints.json"),
    path.join(repositoryRoot, "research", "ashare_research", "race_config.py"),
    path.join(repositoryRoot, "research", "ashare_research", "strategy_factors.py"),
    path.join(repositoryRoot, "research", "ashare_research", "strategy_race.py"),
    path.join(repositoryRoot, "research", "ashare_research", "portfolio.py"),
    path.join(repositoryRoot, "research", "ashare_research", "features.py"),
    path.join(repositoryRoot, "research", "ashare_research", "cost_config.py"),
    path.join(
      repositoryRoot,
      "research",
      "ashare_research",
      "trading_constraints.py",
    ),
  ];
  if (files.some((file) => !fs.existsSync(file))) return null;
  const digest = crypto.createHash("sha256");
  for (const file of files) {
    digest.update(path.basename(file));
    digest.update(fs.readFileSync(file));
  }
  return digest.digest("hex");
}

export function deriveProductionGateFromFiles(
  raceDir = defaultResearchRaceDir(),
  options: ProductionGateOptions = {},
): ProductionGateSnapshot {
  const daily = readJson<DailyRaceReport>(path.join(raceDir, "daily-report.json"));
  const minute = readJson<MinuteRaceReport>(path.join(raceDir, "final-report.json"));
  return deriveProductionGate(daily, minute, {
    ...options,
    expectedContractSha256:
      options.expectedContractSha256 ??
      computeProductionRaceContractSha256() ??
      "__current_contract_unavailable__",
  });
}

function isProductionGateSnapshot(value: unknown): value is ProductionGateSnapshot {
  if (!value || typeof value !== "object") return false;
  const gate = value as Partial<ProductionGateSnapshot>;
  return (
    gate.schema_version === PRODUCTION_GATE_SCHEMA_VERSION &&
    (gate.status === "active" || gate.status === "cash-only") &&
    Array.isArray(gate.reason_codes) &&
    Array.isArray(gate.candidates) &&
    typeof gate.generated_at === "string" &&
    typeof gate.message === "string"
  );
}

export function readProductionGate(): ProductionGateSnapshot {
  let raw: unknown = null;
  try {
    raw = readRuntimeJson<unknown>(PRODUCTION_GATE_FILE);
  } catch {
    // Invalid runtime JSON is handled by the same fail-closed result.
  }
  if (isProductionGateSnapshot(raw)) return raw;
  return cashOnlyGate(
    new Date().toISOString(),
    [raw == null ? "PRODUCTION_GATE_MISSING" : "PRODUCTION_GATE_INVALID"],
    null,
    null,
  );
}

export function readProductionSignals(): ProductionSignalsSnapshot | null {
  try {
    const value = readRuntimeJson<ProductionSignalsSnapshot>(PRODUCTION_SIGNALS_FILE);
    if (
      !value ||
      value.schema_version !== PRODUCTION_SIGNALS_SCHEMA_VERSION ||
      (value.status !== "active" && value.status !== "cash-only") ||
      typeof value.gate_generated_at !== "string" ||
      !Object.hasOwn(value, "contract_sha256") ||
      !Array.isArray(value.signals)
    ) {
      return null;
    }
    return value;
  } catch {
    return null;
  }
}

export function buildCashOnlyProductionSignals(
  gate: ProductionGateSnapshot,
  signalDate: string,
  latestCompleteDate: string,
  signalBasis: ProductionSignalsSnapshot["signal_basis"],
): ProductionSignalsSnapshot {
  if (gate.status !== "cash-only" || gate.champion_id !== null) {
    throw new Error("cash-only production signals require a cash-only gate");
  }
  return {
    schema_version: PRODUCTION_SIGNALS_SCHEMA_VERSION,
    generated_at: new Date().toISOString(),
    gate_generated_at: gate.generated_at,
    contract_sha256: gate.contract_sha256,
    status: "cash-only",
    champion_id: null,
    signal_date: signalDate,
    latest_complete_date: latestCompleteDate,
    signal_basis: signalBasis,
    reason_codes: gate.reason_codes,
    signals: [],
  };
}

function zeroCounts(): Record<"buy" | "hold" | "sell", number> {
  return { buy: 0, hold: 0, sell: 0 };
}

// A signal snapshot is only servable while its decision date stays close to
// the current Asia/Shanghai calendar date. The longest A-share closure
// (Spring Festival) spans at most ~11 calendar days between sessions, so a
// 15-day default tolerates every holiday while still failing closed within
// roughly two weeks if the daily-close pipeline stops refreshing data.
export const PRODUCTION_SIGNALS_MAX_AGE_DAYS_DEFAULT = 15;

export function productionSignalsMaxAgeDays(): number {
  const raw = process.env.PRODUCTION_SIGNALS_MAX_AGE_DAYS;
  if (!raw) return PRODUCTION_SIGNALS_MAX_AGE_DAYS_DEFAULT;
  const parsed = Number(raw);
  if (!Number.isFinite(parsed) || parsed < 0) {
    return PRODUCTION_SIGNALS_MAX_AGE_DAYS_DEFAULT;
  }
  return Math.floor(parsed);
}

function shanghaiDateString(now: Date): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(now);
  const get = (type: string) =>
    parts.find((part) => part.type === type)?.value ?? "00";
  return `${get("year")}-${get("month")}-${get("day")}`;
}

export function isProductionSignalSnapshotStale(
  snapshot: Pick<ProductionSignalsSnapshot, "signal_date">,
  now: Date = new Date(),
  maxAgeDays: number = productionSignalsMaxAgeDays(),
): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(snapshot.signal_date)) return true;
  const signalTime = Date.parse(`${snapshot.signal_date}T00:00:00Z`);
  const todayTime = Date.parse(`${shanghaiDateString(now)}T00:00:00Z`);
  if (!Number.isFinite(signalTime) || !Number.isFinite(todayTime)) return true;
  return todayTime - signalTime > maxAgeDays * 86_400_000;
}

export function buildProductionSignalsApiPayload(
  gate: ProductionGateSnapshot,
  snapshot: ProductionSignalsSnapshot | null,
  requestedAsOf?: string | null,
  options: { now?: Date; maxAgeDays?: number } = {},
): ProductionSignalsApiPayload {
  const snapshotMatchesGate = Boolean(
    snapshot &&
      snapshot.status === gate.status &&
      snapshot.champion_id === gate.champion_id &&
      snapshot.gate_generated_at === gate.generated_at &&
      snapshot.contract_sha256 === gate.contract_sha256 &&
      snapshot.reason_codes.length === gate.reason_codes.length &&
      snapshot.reason_codes.every((code, index) => code === gate.reason_codes[index]),
  );
  const requestedDateMatches = !requestedAsOf || snapshot?.signal_date === requestedAsOf;
  const signalsStale = Boolean(
    snapshot &&
      isProductionSignalSnapshotStale(
        snapshot,
        options.now ?? new Date(),
        options.maxAgeDays ?? productionSignalsMaxAgeDays(),
      ),
  );
  const canServeActive = Boolean(
    gate.status === "active" &&
      gate.champion_id &&
      snapshotMatchesGate &&
      requestedDateMatches &&
      !signalsStale,
  );

  if (!canServeActive) {
    const reasonCodes = [...gate.reason_codes];
    if (!snapshotMatchesGate) reasonCodes.push("PRODUCTION_SIGNALS_MISSING_OR_MISMATCHED");
    if (!requestedDateMatches) reasonCodes.push("REQUESTED_AS_OF_UNAVAILABLE");
    if (signalsStale) reasonCodes.push("PRODUCTION_SIGNALS_STALE");
    return {
      generated_at: new Date().toISOString(),
      source: "production-gate",
      status: "cash-only",
      champion_id: null,
      stale: !snapshotMatchesGate || !requestedDateMatches || signalsStale,
      signal_date: snapshot?.signal_date ?? null,
      latest_complete_date: snapshot?.latest_complete_date ?? null,
      signal_basis: snapshot?.signal_basis ?? null,
      reason_codes: [...new Set(reasonCodes)],
      message: gate.message,
      counts: zeroCounts(),
      gross_buy_weight: 0,
      signals: [],
    };
  }

  const signals = snapshot!.signals;
  const counts = signals.reduce((acc, signal) => {
    acc[signal.action] += 1;
    return acc;
  }, zeroCounts());
  return {
    generated_at: new Date().toISOString(),
    source: "production-gate",
    status: "active",
    champion_id: gate.champion_id,
    stale: false,
    signal_date: snapshot!.signal_date,
    latest_complete_date: snapshot!.latest_complete_date,
    signal_basis: snapshot!.signal_basis,
    reason_codes: [],
    message: gate.message,
    counts,
    gross_buy_weight: Number(
      signals
        .filter((signal) => signal.action === "buy")
        .reduce((sum, signal) => sum + signal.size, 0)
        .toFixed(4),
    ),
    signals,
  };
}
