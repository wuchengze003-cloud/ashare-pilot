// Unified A-share transaction cost model.
// Reads from config/cost-model.json so TypeScript and Python share the same
// economic contract.  The file is resolved relative to the repo root, which
// is two levels up from web/lib/.
import fs from "node:fs";
import { repoConfigPath } from "./repoConfig";

export interface CostModel {
  buyCommissionBps: number;
  sellCommissionBps: number;
  stampDutyBps: number;
  baseSlippageBps: number;
  minimumCommissionYuan: number;
  impactModel: string;
  impactCoefficient: number;
  impactVolatilityLookback: number;
  maxImpactBps: number;
  missingTurnoverPenaltyBps: number;
}

export interface CostConfig {
  buyCommissionBps: number;
  sellCommissionBps: number;
  stampDutyBps: number;
  slippageBps: number;
  minimumCommissionYuan?: number;
  impactModel?: string;
  impactCoefficient?: number;
  impactVolatilityLookback?: number;
  maxImpactBps?: number;
  missingTurnoverPenaltyBps?: number;
}

let cached: CostModel | null = null;

export function loadCostModel(): CostModel {
  if (cached) return cached;
  const filePath = repoConfigPath("cost-model.json");
  const raw = JSON.parse(fs.readFileSync(filePath, "utf-8")) as Record<string, unknown>;
  const parsed: CostModel = {
    buyCommissionBps: Number(raw.buy_commission_bps),
    sellCommissionBps: Number(raw.sell_commission_bps),
    stampDutyBps: Number(raw.stamp_duty_bps),
    baseSlippageBps: Number(raw.base_slippage_bps),
    minimumCommissionYuan: Number(raw.minimum_commission_yuan),
    impactModel: String(raw.impact_model ?? "sqrt-volume"),
    impactCoefficient: Number(raw.impact_coefficient ?? 0),
    impactVolatilityLookback: Number(raw.impact_volatility_lookback ?? 20),
    maxImpactBps: Number(raw.max_impact_bps ?? 0),
    missingTurnoverPenaltyBps: Number(raw.missing_turnover_penalty_bps ?? 0),
  };
  for (const [key, value] of Object.entries(parsed)) {
    if (key === "impactModel") continue;
    if (!Number.isFinite(value) || Number(value) < 0) {
      throw new Error(`invalid cost model ${key}: ${String(value)}`);
    }
  }
  if (!Number.isInteger(parsed.impactVolatilityLookback) || parsed.impactVolatilityLookback < 2) {
    throw new Error("impactVolatilityLookback must be an integer >= 2");
  }
  if (parsed.impactModel !== "square-root-participation") {
    throw new Error(`unsupported impact model: ${parsed.impactModel}`);
  }
  cached = parsed;
  return cached;
}

/** Convert the unified model to the CostConfig shape used by BacktestConfig. */
export function toCostConfig(model: CostModel = loadCostModel()): CostConfig {
  return {
    buyCommissionBps: model.buyCommissionBps,
    sellCommissionBps: model.sellCommissionBps,
    stampDutyBps: model.stampDutyBps,
    slippageBps: model.baseSlippageBps,
    minimumCommissionYuan: model.minimumCommissionYuan,
    impactModel: model.impactModel,
    impactCoefficient: model.impactCoefficient,
    impactVolatilityLookback: model.impactVolatilityLookback,
    maxImpactBps: model.maxImpactBps,
    missingTurnoverPenaltyBps: model.missingTurnoverPenaltyBps,
  };
}

/** Round-trip cost in bps (buy + sell). */
export function roundTripBps(model: CostModel = loadCostModel()): number {
  return model.buyCommissionBps + model.baseSlippageBps +
    model.sellCommissionBps + model.stampDutyBps + model.baseSlippageBps;
}
