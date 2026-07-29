import fs from "node:fs";
import { repoConfigPath } from "./repoConfig";

export interface TradingConstraints {
  schema: string;
  allowedBoards: string[];
  priceLimitFractions: Record<string, number>;
  symbolPrefixes: Record<string, string[]>;
  excludeLimitUp: boolean;
  limitSlack: number;
  maxOneDayReturnToBuy: number;
  initialCapitalYuan: number;
  maxDrawdownPct: number;
  maxSinglePositionPct: number;
  maxSingleThemePct: number;
  maxPositions: number;
  decisionTiming: string;
  executionPrice: "next_open";
  supportedSignalFrequencies: string[];
  intradayExecutionPrice: "next_bar_open";
  tPlusOne: boolean;
  lotSize: number;
  maxOrderBarAmountPct: number;
  minHoldingBars: number;
  rebalanceThresholdPct: number;
}

let cached: TradingConstraints | null = null;

function finiteNumber(value: unknown, label: string, minimum = 0): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < minimum) {
    throw new Error(`invalid trading constraint ${label}: ${String(value)}`);
  }
  return parsed;
}

function positiveInteger(value: unknown, label: string): number {
  const parsed = finiteNumber(value, label, 1);
  if (!Number.isInteger(parsed)) {
    throw new Error(`invalid trading constraint ${label}: expected integer`);
  }
  return parsed;
}

export function loadTradingConstraints(): TradingConstraints {
  if (cached) return cached;
  const file = repoConfigPath("trading-constraints.json");
  const raw = JSON.parse(fs.readFileSync(file, "utf-8")) as Record<string, any>;
  const boards = raw.boards ?? {};
  const buy = raw.buy_constraints ?? {};
  const risk = raw.risk_management ?? {};
  const execution = raw.execution ?? {};
  const priceLimitFractions = Object.fromEntries(
    Object.entries(boards.price_limit_fractions ?? {}).map(([key, value]) => [
      key,
      finiteNumber(value, `boards.price_limit_fractions.${key}`),
    ]),
  );
  for (const required of ["main", "main_st", "star", "chinext", "bse"]) {
    if (!(required in priceLimitFractions)) {
      throw new Error(`missing trading constraint boards.price_limit_fractions.${required}`);
    }
  }
  const executionPrice = String(execution.execution_price ?? "");
  const intradayExecutionPrice = String(execution.intraday_execution_price ?? "");
  if (executionPrice !== "next_open" || intradayExecutionPrice !== "next_bar_open") {
    throw new Error("unsupported execution price in trading-constraints.json");
  }
  cached = {
    schema: String(raw.$schema ?? ""),
    allowedBoards: [...(boards.allowed ?? [])].map(String),
    priceLimitFractions,
    symbolPrefixes: Object.fromEntries(
      Object.entries(boards.symbol_prefixes ?? {}).map(([key, value]) => [
        key,
        Array.isArray(value) ? value.map(String) : [],
      ]),
    ),
    excludeLimitUp: Boolean(buy.exclude_limit_up),
    limitSlack: finiteNumber(buy.limit_slack, "buy_constraints.limit_slack"),
    maxOneDayReturnToBuy: finiteNumber(
      buy.max_one_day_return_to_buy,
      "buy_constraints.max_one_day_return_to_buy",
    ),
    initialCapitalYuan: finiteNumber(
      risk.initial_capital_yuan,
      "risk_management.initial_capital_yuan",
      1,
    ),
    maxDrawdownPct: finiteNumber(risk.max_drawdown_pct, "risk_management.max_drawdown_pct"),
    maxSinglePositionPct: finiteNumber(
      risk.max_single_position_pct,
      "risk_management.max_single_position_pct",
    ),
    maxSingleThemePct: finiteNumber(
      risk.max_single_theme_pct,
      "risk_management.max_single_theme_pct",
    ),
    maxPositions: positiveInteger(risk.max_positions, "risk_management.max_positions"),
    decisionTiming: String(execution.decision_timing ?? ""),
    executionPrice,
    supportedSignalFrequencies: [...(execution.supported_signal_frequencies ?? [])].map(String),
    intradayExecutionPrice,
    tPlusOne: Boolean(execution.t_plus_1),
    lotSize: positiveInteger(execution.lot_size, "execution.lot_size"),
    maxOrderBarAmountPct: finiteNumber(
      execution.max_order_bar_amount_pct,
      "execution.max_order_bar_amount_pct",
    ),
    minHoldingBars: positiveInteger(
      execution.min_holding_bars,
      "execution.min_holding_bars",
    ),
    rebalanceThresholdPct: finiteNumber(
      execution.rebalance_threshold_pct,
      "execution.rebalance_threshold_pct",
    ),
  };
  if (!cached.schema.endsWith("/v2")) {
    throw new Error(`unsupported trading constraints schema: ${cached.schema}`);
  }
  if (!cached.tPlusOne || !cached.supportedSignalFrequencies.includes("5min")) {
    throw new Error("production constraints must enable T+1 and 5min signal support");
  }
  return cached;
}

export function boardForSymbol(symbol: string): "main" | "star" | "chinext" | "bse" {
  const cfg = loadTradingConstraints();
  const code = symbol.replace(/^(sh|sz|bj)/i, "").replace(/\.(sh|sz|bj)$/i, "");
  for (const board of ["star", "chinext", "bse"] as const) {
    if ((cfg.symbolPrefixes[board] ?? []).some((prefix) => code.startsWith(prefix))) {
      return board;
    }
  }
  return "main";
}

export function configuredPriceLimitFraction(symbol: string, name: string): number {
  const cfg = loadTradingConstraints();
  const board = boardForSymbol(symbol);
  if (board === "main" && /ST/i.test(name)) return cfg.priceLimitFractions.main_st;
  return cfg.priceLimitFractions[board];
}
