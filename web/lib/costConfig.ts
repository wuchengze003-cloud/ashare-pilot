// Unified A-share transaction cost model.
// Reads from config/cost-model.json so TypeScript and Python share the same
// economic contract.  The file is resolved relative to the repo root, which
// is two levels up from web/lib/.
import fs from "node:fs";
import path from "node:path";

export interface CostModel {
  buyCommissionBps: number;
  sellCommissionBps: number;
  stampDutyBps: number;
  baseSlippageBps: number;
  minimumCommissionYuan: number;
  impactModel: string;
}

export interface CostConfig {
  buyCommissionBps: number;
  sellCommissionBps: number;
  stampDutyBps: number;
  slippageBps: number;
}

let cached: CostModel | null = null;

function repoRoot(): string {
  // web/lib/costConfig.ts → web/lib → web → repo root
  return path.resolve(__dirname, "..", "..");
}

export function loadCostModel(): CostModel {
  if (cached) return cached;
  const filePath = path.join(repoRoot(), "config", "cost-model.json");
  const raw = JSON.parse(fs.readFileSync(filePath, "utf-8")) as Record<string, unknown>;
  cached = {
    buyCommissionBps: Number(raw.buy_commission_bps),
    sellCommissionBps: Number(raw.sell_commission_bps),
    stampDutyBps: Number(raw.stamp_duty_bps),
    baseSlippageBps: Number(raw.base_slippage_bps),
    minimumCommissionYuan: Number(raw.minimum_commission_yuan),
    impactModel: String(raw.impact_model ?? "sqrt-volume"),
  };
  return cached;
}

/** Convert the unified model to the CostConfig shape used by BacktestConfig. */
export function toCostConfig(model: CostModel = loadCostModel()): CostConfig {
  return {
    buyCommissionBps: model.buyCommissionBps,
    sellCommissionBps: model.sellCommissionBps,
    stampDutyBps: model.stampDutyBps,
    slippageBps: model.baseSlippageBps,
  };
}

/** Round-trip cost in bps (buy + sell). */
export function roundTripBps(model: CostModel = loadCostModel()): number {
  return model.buyCommissionBps + model.baseSlippageBps +
    model.sellCommissionBps + model.stampDutyBps + model.baseSlippageBps;
}
