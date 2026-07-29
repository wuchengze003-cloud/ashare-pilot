import {
  PRODUCTION_GATE_FILE,
  PRODUCTION_SIGNALS_FILE,
  buildCashOnlyProductionSignals,
  deriveProductionGateFromFiles,
} from "../lib/productionGate";
import { readRuntimeJson, writeRuntimeJson } from "../lib/runtimeData";
import {
  assertProductionRuntimeArtifacts,
  readProductionRuntimeValidationInput,
} from "../lib/runtimeValidation";

interface RuntimeManifest {
  data_date?: string;
  latest_complete_date?: string;
  snapshot_basis?: "latest-complete-close" | "intraday-midday";
  production_gate?: {
    status: string;
    champion_id: string | null;
    contract_sha256: string | null;
    minute_coverage_pct: number | null;
    reason_codes: string[];
  };
  [key: string]: unknown;
}

const manifest = readRuntimeJson<RuntimeManifest>("manifest.json");
const signalDate = manifest?.data_date ?? manifest?.latest_complete_date;
const latestCompleteDate = manifest?.latest_complete_date ?? manifest?.data_date;
if (!signalDate || !latestCompleteDate) {
  throw new Error("manifest.json must contain data_date and latest_complete_date");
}

const gate = deriveProductionGateFromFiles(undefined, {
  deployableChampionIds: [],
});
writeRuntimeJson(PRODUCTION_GATE_FILE, gate);
if (gate.status !== "cash-only") {
  throw new Error(
    `production champion ${gate.champion_id ?? "unknown"} requires an exact inference adapter before activation`,
  );
}
writeRuntimeJson(
  PRODUCTION_SIGNALS_FILE,
  buildCashOnlyProductionSignals(
    gate,
    signalDate,
    latestCompleteDate,
    manifest?.snapshot_basis ?? "latest-complete-close",
  ),
);
writeRuntimeJson("manifest.json", {
  ...manifest,
  production_gate: {
    status: gate.status,
    champion_id: gate.champion_id,
    contract_sha256: gate.contract_sha256,
    minute_coverage_pct: gate.minute_coverage_pct,
    reason_codes: gate.reason_codes,
  },
});
assertProductionRuntimeArtifacts(readProductionRuntimeValidationInput());
console.log(
  `Production gate synchronized: status=${gate.status}, champion=${gate.champion_id ?? "none"}, signal_date=${signalDate}`,
);
