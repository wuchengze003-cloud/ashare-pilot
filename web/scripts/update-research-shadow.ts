// Optional deterministic shadow-model inference. Training and optimization are
// deliberately excluded from this daily path.
import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { writeRuntimeJson } from "../lib/runtimeData";

const webRoot = path.resolve(__dirname, "..");
const repoRoot = path.resolve(webRoot, "..");
const researchRoot = path.join(repoRoot, "research");
const researchRuntime = path.resolve(
  researchRoot,
  process.env.RESEARCH_RUNTIME_DIR ?? "runtime",
);
const registryPath = path.join(researchRuntime, "registry", "active_model.json");
const modelSnapshotPaths = [
  path.join(webRoot, "data", "runtime", "ml", "champion-predictions.json"),
  path.join(webRoot, "data", "runtime", "ml", "challenger-predictions.json"),
];

interface RegistryState {
  active?: {
    model_version: string;
    artifact_uri: string;
  } | null;
  candidates?: string[];
}

interface ModelReference {
  model_version: string;
  artifact_uri: string;
}

function clearModelSnapshots() {
  for (const file of modelSnapshotPaths) {
    if (fs.existsSync(file)) fs.unlinkSync(file);
  }
}

function readJson(file: string): Record<string, unknown> | null {
  if (!fs.existsSync(file)) return null;
  return JSON.parse(fs.readFileSync(file, "utf-8")) as Record<string, unknown>;
}

function researchSummary(registry?: RegistryState, assessedVersions: string[] = []) {
  const qlibSource = readJson(path.join(researchRuntime, "qlib", "cn_data", "source.json"));
  const qlibHealth = readJson(path.join(researchRuntime, "qlib", "cn_data", "health.json"));
  const linearBenchmark = readJson(
    path.join(researchRuntime, "benchmarks", "alpha158-linear-csi500", "result.json"),
  );
  const lightgbmBenchmark = readJson(
    path.join(researchRuntime, "benchmarks", "alpha158-lightgbm-csi500", "result.json"),
  );
  const tushareError = readJson(
    path.join(researchRuntime, "data", "meta", "last-sync-error.json"),
  );
  const tushareCoverage = readJson(
    path.join(researchRuntime, "data", "meta", "coverage.json"),
  );
  const modelHealth = readJson(path.join(researchRuntime, "monitor", "latest.json"));
  const outcomeFeedback = readJson(path.join(researchRuntime, "outcomes", "latest.json"));
  const assessmentVersions = [...new Set([
    ...(registry?.candidates ?? []),
    ...(registry?.active?.model_version ? [registry.active.model_version] : []),
    ...assessedVersions,
  ])];
  const assessments = assessmentVersions
    .map((version) => readJson(
      path.join(researchRuntime, "evaluations", version, "promotion-assessment.json"),
    ))
    .filter((value): value is Record<string, unknown> => value != null);
  return {
    production_strategy: registry?.active ? "ml-champion" : "v1-rule",
    active_model: registry?.active?.model_version ?? null,
    challenger_models: registry?.candidates ?? [],
    promotion_assessments: assessments,
    model_health: modelHealth,
    outcome_feedback: outcomeFeedback,
    qlib_benchmark: qlibHealth
      ? {
        passed: qlibHealth.passed,
        data_cutoff: qlibHealth.calendar_end,
        release: qlibSource?.release_tag ?? null,
        promotable: false,
        results: {
          linear: linearBenchmark,
          lightgbm: lightgbmBenchmark,
        },
      }
      : null,
    tushare_production: tushareError
      ? {
        passed: false,
        error: tushareError.error,
        recorded_at: tushareError.recorded_at,
      }
      : tushareCoverage
        ? {
          passed: Boolean(tushareCoverage.passed),
          data_cutoff: tushareCoverage.end_date ?? null,
          trading_days: tushareCoverage.common_required_days ?? 0,
          failures: tushareCoverage.failures ?? [],
        }
        : null,
  };
}

function runResearch(args: string[]) {
  execFileSync("uv", ["run", "ashare-research", "--runtime", researchRuntime, ...args], {
    cwd: researchRoot,
    env: process.env,
    stdio: "inherit",
  });
}

function resolveArtifact(value: string): string {
  if (value.startsWith("models:/")) {
    const version = value.slice("models:/".length);
    return path.join(researchRuntime, "models", version, "model-bundle.pkl");
  }
  return path.isAbsolute(value) ? value : path.resolve(researchRoot, value);
}

function challengerModel(registry: RegistryState) {
  const candidate = registry.candidates?.at(-1);
  if (!candidate) return null;
  const manifestPath = path.join(researchRuntime, "registry", "models", candidate, "manifest.json");
  if (!fs.existsSync(manifestPath)) throw new Error(`candidate manifest missing: ${manifestPath}`);
  const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf-8")) as ModelReference;
  return { ...manifest, stage: "shadow" as const, output: "challenger-predictions.json" };
}

function modelsToRun(registry: RegistryState) {
  const models: Array<ModelReference & {
    stage: "shadow" | "champion";
    output: string;
  }> = [];
  if (registry.active) {
    models.push({
      ...registry.active,
      stage: "champion",
      output: "champion-predictions.json",
    });
  }
  const challenger = challengerModel(registry);
  if (challenger) models.push(challenger);
  return models;
}

function main() {
  if (!fs.existsSync(registryPath)) {
    clearModelSnapshots();
    console.log("[research] no model registry; V1 remains the only active strategy");
    writeRuntimeJson("ml/status.json", {
      generated_at: new Date().toISOString(),
      status: "v1-only",
      ...researchSummary(),
    });
    return;
  }
  const requestedDate = process.env.DASHBOARD_END ?? new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
  const registry = JSON.parse(fs.readFileSync(registryPath, "utf-8")) as RegistryState;
  const activeBefore = registry.active?.model_version ?? null;
  const models = modelsToRun(registry);
  if (models.length === 0) {
    clearModelSnapshots();
    console.log("[research] registry has no candidate or champion; skipping inference");
    writeRuntimeJson("ml/status.json", {
      generated_at: new Date().toISOString(),
      status: "v1-only",
      ...researchSummary(registry),
    });
    return;
  }
  runResearch(["data-sync", "--start", requestedDate, "--end", requestedDate]);
  runResearch(["build-features", "--as-of", requestedDate]);
  runResearch(["backfill-outcomes", "--as-of", requestedDate]);
  const qualityReport = path.join(researchRuntime, "quality", "feature-panel.json");
  const driftReport = path.join(researchRuntime, "drift", "latest.json");
  runResearch(["drift", "--output", driftReport]);
  for (const model of models) {
    runResearch(["verify-model", "--model-version", model.model_version]);
    const bundle = resolveArtifact(model.artifact_uri);
    if (!fs.existsSync(bundle)) throw new Error(`model artifact missing: ${bundle}`);
    const accountState = path.join(researchRuntime, "shadow", `${model.model_version}.json`);
    runResearch([
      "predict",
      "--model-bundle", bundle,
      "--model-version", model.model_version,
      "--stage", model.stage,
      "--universe", path.join(webRoot, "data", "universe.json"),
      "--output", path.join(webRoot, "data", "runtime", "ml", model.output),
      "--account-state", accountState,
      "--quality", qualityReport,
      "--drift", driftReport,
    ]);
    runResearch([
      "evaluate-shadow",
      "--state", accountState,
      "--output", path.join(
        researchRuntime,
        "evaluations",
        model.model_version,
        "shadow.json",
      ),
    ]);
    if (model.stage === "shadow") {
      runResearch([
        "assess-challenger",
        "--model-version", model.model_version,
        "--auto-promote",
      ]);
    }
  }
  const assessedRegistry = JSON.parse(fs.readFileSync(registryPath, "utf-8")) as RegistryState;
  const activeAfterAssessment = assessedRegistry.active?.model_version ?? null;
  const healthPath = path.join(researchRuntime, "monitor", "latest.json");
  if (activeAfterAssessment) {
    runResearch([
      "build-health",
      "--state", path.join(researchRuntime, "shadow", `${activeAfterAssessment}.json`),
      "--baseline-evaluation", path.join(researchRuntime, "baselines", "v1", "evaluation.json"),
      "--quality", qualityReport,
      "--drift", driftReport,
      "--as-of", requestedDate,
      "--output", healthPath,
    ]);
    runResearch([
      "monitor",
      "--health", healthPath,
      "--as-of", requestedDate,
      "--auto-rollback",
    ]);
  }
  const latestRegistry = JSON.parse(fs.readFileSync(registryPath, "utf-8")) as RegistryState;
  const activeAfter = latestRegistry.active?.model_version ?? null;
  if (activeAfter !== activeAfterAssessment) {
    const championSnapshot = modelSnapshotPaths[0];
    if (fs.existsSync(championSnapshot)) fs.unlinkSync(championSnapshot);
  }
  writeRuntimeJson("ml/status.json", {
    generated_at: new Date().toISOString(),
    status: "passed",
    models: models.map(({ model_version, stage }) => ({ model_version, stage })),
    decision_date: requestedDate,
    activation_pending: activeAfter && activeAfter !== activeBefore ? activeAfter : null,
    ...researchSummary(latestRegistry, models.map(({ model_version }) => model_version)),
  });
}

try {
  main();
} catch (error) {
  clearModelSnapshots();
  let registry: RegistryState | undefined;
  if (fs.existsSync(registryPath)) {
    try {
      registry = JSON.parse(fs.readFileSync(registryPath, "utf-8")) as RegistryState;
    } catch {
      registry = undefined;
    }
  }
  writeRuntimeJson("ml/status.json", {
    generated_at: new Date().toISOString(),
    status: "failed",
    error: error instanceof Error ? error.message : String(error),
    ...researchSummary(registry),
  });
  throw error;
}
