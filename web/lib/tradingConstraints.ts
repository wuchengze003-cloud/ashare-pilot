// Unified trading-constraints reader.
// Reads from config/trading-constraints.json so Web and Research share the
// same board rules, buy constraints, and risk parameters.
import fs from "node:fs";
import path from "node:path";

interface TradingConstraintsFile {
  boards: {
    allowed: string[];
    price_limit_fractions: Record<string, number>;
    symbol_prefixes: Record<string, string[]>;
  };
  buy_constraints: {
    exclude_limit_up: boolean;
    limit_slack: number;
    max_one_day_return_to_buy: number;
  };
  risk_management: {
    initial_capital_yuan: number;
    max_drawdown_pct: number;
    max_single_position_pct: number;
    max_single_theme_pct: number;
    max_positions: number;
  };
  execution: {
    decision_timing: string;
    execution_price: string;
    min_holding_bars: number;
    rebalance_threshold_pct: number;
  };
}

function resolveConfigPath(): string {
  // web/lib/tradingConstraints.ts → web → repo root → config/trading-constraints.json
  return path.resolve(__dirname, "..", "..", "config", "trading-constraints.json");
}

let cached: TradingConstraintsFile | null = null;

export function loadTradingConstraints(): TradingConstraintsFile {
  if (cached) return cached;
  const file = resolveConfigPath();
  cached = JSON.parse(fs.readFileSync(file, "utf-8")) as TradingConstraintsFile;
  return cached;
}

/** A-share daily price-limit fraction by board, mirroring priceLimitFraction()
 *  in backtest.ts but driven by the unified config file. */
export function priceLimitFractionFromConfig(symbol: string, name: string): number {
  const cfg = loadTradingConstraints();
  const code = symbol.replace(/^(sh|sz|bj)/i, "").replace(/\.(sh|sz|bj)$/i, "");
  const prefixes = cfg.boards.symbol_prefixes;
  if (prefixes.star.some((p) => code.startsWith(p))) return cfg.boards.price_limit_fractions.star;
  if (prefixes.chinext.some((p) => code.startsWith(p))) return cfg.boards.price_limit_fractions.chinext;
  if (prefixes.bse.some((p) => code.startsWith(p))) return cfg.boards.price_limit_fractions.bse;
  return /ST/i.test(name)
    ? cfg.boards.price_limit_fractions.main_st
    : cfg.boards.price_limit_fractions.main;
}

export function limitSlack(): number {
  return loadTradingConstraints().buy_constraints.limit_slack;
}

export function maxOneDayReturnToBuy(): number {
  return loadTradingConstraints().buy_constraints.max_one_day_return_to_buy;
}

export function riskParams() {
  return loadTradingConstraints().risk_management;
}
