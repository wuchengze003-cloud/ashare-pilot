import { readRuntimeJson } from "./runtimeData";

export type ModelStage = "shadow" | "champion" | "disabled";

export interface ShadowPrediction {
  symbol: string;
  rank: number;
  score: number;
  expectedReturns: Partial<Record<"d1" | "d3" | "d5" | "d10", number>>;
  downsideRisk: number;
  confidence: number;
  targetWeight: number;
  action: "buy" | "hold" | "sell" | "cash";
  reasonCodes: string[];
  featureContributions?: Record<string, number>;
}

export interface ShadowModelSnapshot {
  generated_at: string;
  decision_date: string;
  data_cutoff: string;
  stage: ModelStage;
  model_version: string;
  feature_version: string;
  source: "qlib";
  predictions: ShadowPrediction[];
  quality?: {
    data_quality_passed: boolean;
    drift_passed: boolean;
    warnings: string[];
  };
  shadow_account?: {
    cash: number;
    positions: Record<string, number>;
    equity_curve: Array<{
      date: string;
      equity: number;
      cash: number;
      positions: number;
    }>;
  } | null;
}

export function readModelSnapshot(
  kind: "champion" | "challenger",
): ShadowModelSnapshot | null {
  return readRuntimeJson<ShadowModelSnapshot>(`ml/${kind}-predictions.json`);
}

export function modelSnapshotForDate(
  decisionDate: string,
  kind: "champion" | "challenger",
  snapshot = readModelSnapshot(kind),
): ShadowModelSnapshot | null {
  if (!snapshot || snapshot.decision_date !== decisionDate) return null;
  if (kind === "champion" && snapshot.stage !== "champion") return null;
  if (kind === "challenger" && snapshot.stage !== "shadow") return null;
  return snapshot;
}
