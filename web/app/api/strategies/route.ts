import { NextResponse } from "next/server";
import { readProductionGate } from "@/lib/productionGate";
import {
  candidateGateLabel,
  productionCandidateName,
  productionFamilyName,
} from "@/lib/productionPresentation";

export const dynamic = "force-dynamic";

export async function GET() {
  const gate = readProductionGate();
  return NextResponse.json({
    production: {
      status: gate.status,
      champion_id: gate.champion_id,
      message: gate.message,
      contract_sha256: gate.contract_sha256,
      contract_version: gate.contract_version,
      feature_version: gate.feature_version,
      panel_start: gate.panel_start,
      panel_end: gate.panel_end,
    },
    strategies: gate.candidates.map((candidate) => ({
      id: candidate.candidate_id,
      name: productionCandidateName(candidate.candidate_id),
      family: candidate.family,
      family_name: productionFamilyName(candidate.family),
      signal_frequency: candidate.signal_frequency,
      lifecycle:
        candidate.candidate_id === gate.champion_id
          ? "production-champion"
          : "research-candidate",
      executable:
        gate.status === "active" &&
        candidate.candidate_id === gate.champion_id,
      gates_passed: candidate.daily_gates_passed,
      failed_gates: candidate.failed_gate_codes.map((code) => ({
        code,
        label: candidateGateLabel(code),
      })),
      metrics: {
        oos_annualized_return_pct: candidate.oos_annualized_return_pct,
        oos_sharpe: candidate.oos_sharpe,
        oos_max_drawdown_pct: candidate.oos_max_drawdown_pct,
        oos_calmar: candidate.oos_calmar,
        frozen_total_return_pct: candidate.frozen_total_return_pct,
        frozen_sharpe: candidate.frozen_sharpe,
        frozen_max_drawdown_pct: candidate.frozen_max_drawdown_pct,
        oos_upside_capture_pct: candidate.oos_upside_capture_pct,
        positive_oos_fold_share_pct: candidate.positive_oos_fold_share_pct,
        bootstrap_probability_pct: candidate.bootstrap_probability_pct,
        oos_closed_trades: candidate.oos_closed_trades,
      },
    })),
  });
}
