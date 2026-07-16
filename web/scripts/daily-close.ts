// End-of-day control plane for the local and deployed strategy application.
//
// This deliberately keeps market/news interpretation outside deterministic
// code. A Codex automation runs this command, then produces the sourced recap.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execFileSync, spawn } from "node:child_process";
import { loadActiveEntries } from "../lib/universe";
import { buildSymbolSeriesFromPyserverCache } from "../lib/dashboardData";
import {
  parseSymbolList,
  shanghaiDateTimeParts,
  validateDailyCloseData,
  type AnalystCoverageItem,
} from "../lib/dailyClose";
import { readRuntimeJson, writeRuntimeJson } from "../lib/runtimeData";
import {
  assertRuntimeArtifacts,
  readRuntimeValidationInput,
  type RuntimeBacktestSnapshot,
  type RuntimeMetaSnapshot,
  type RuntimeSignalsSnapshot,
} from "../lib/runtimeValidation";

const webRoot = path.resolve(__dirname, "..");
const repoRoot = path.resolve(webRoot, "..");
const pyserverRoot = path.join(repoRoot, "pyserver");
const runLogDir = path.join(webRoot, ".cache", "daily-close");

interface StepResult {
  name: string;
  status: "passed" | "skipped" | "failed";
  durationMs: number;
  detail?: string;
}

interface DailyCloseHealth {
  generated_at: string;
  expected_market_date: string;
  latest_market_date?: string;
  status: "passed" | "failed" | "stale-or-no-session";
  steps: StepResult[];
  local: {
    pyserver_url: string;
    web_url: string;
  };
  remote?: {
    base_url: string;
  };
  error?: string;
}

function loadDotEnv(file: string) {
  if (!fs.existsSync(file)) return;
  for (const line of fs.readFileSync(file, "utf-8").split("\n")) {
    const match = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    if (!match || process.env[match[1]]) continue;
    process.env[match[1]] = match[2]
      .replace(/\s+#.*$/, "")
      .replace(/^["']|["']$/g, "")
      .trim();
  }
}

function runCommand(
  steps: StepResult[],
  name: string,
  command: string,
  args: string[],
  options: { cwd?: string; env?: NodeJS.ProcessEnv } = {},
) {
  const started = Date.now();
  console.log(`\n[daily-close] ${name}`);
  try {
    execFileSync(command, args, {
      cwd: options.cwd ?? repoRoot,
      env: options.env ?? process.env,
      stdio: "inherit",
    });
    steps.push({ name, status: "passed", durationMs: Date.now() - started });
  } catch (error) {
    steps.push({
      name,
      status: "failed",
      durationMs: Date.now() - started,
      detail: error instanceof Error ? error.message : String(error),
    });
    throw error;
  }
}

function runIsolatedBuild(steps: StepResult[], nextBasePath: string) {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "a-share-health-build-"));
  try {
    fs.cpSync(webRoot, tempRoot, {
      recursive: true,
      filter: (source) => {
        const relative = path.relative(webRoot, source);
        if (!relative) return true;
        const topLevel = relative.split(path.sep)[0];
        return ![
          "node_modules",
          ".next",
          ".next-health",
          ".cache",
          ".env",
          ".env.local",
        ].includes(topLevel);
      },
    });
    fs.symlinkSync(path.join(webRoot, "node_modules"), path.join(tempRoot, "node_modules"), "dir");
    runCommand(steps, "isolated production build", "npm", ["run", "build"], {
      cwd: tempRoot,
      env: { ...process.env, NEXT_BASE_PATH: nextBasePath },
    });
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true });
  }
}

async function httpOk(url: string, timeoutMs = 10_000): Promise<boolean> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { cache: "no-store", signal: controller.signal });
    return response.ok;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

async function waitForHttp(url: string, timeoutMs = 60_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await httpOk(url, 5_000)) return;
    await new Promise((resolve) => setTimeout(resolve, 1_000));
  }
  throw new Error(`service did not become ready: ${url}`);
}

function startDetached(
  name: string,
  command: string,
  args: string[],
  cwd: string,
  env: NodeJS.ProcessEnv,
) {
  fs.mkdirSync(runLogDir, { recursive: true });
  const logPath = path.join(runLogDir, `${name}.log`);
  const logFd = fs.openSync(logPath, "a");
  const child = spawn(command, args, {
    cwd,
    env,
    detached: true,
    stdio: ["ignore", logFd, logFd],
  });
  child.unref();
  fs.closeSync(logFd);
  console.log(`[daily-close] started ${name} pid=${child.pid}, log=${logPath}`);
}

async function ensurePyserver(steps: StepResult[], pyserverUrl: string) {
  const started = Date.now();
  const healthUrl = new URL("health", pyserverUrl.endsWith("/") ? pyserverUrl : `${pyserverUrl}/`).toString();
  if (await httpOk(healthUrl)) {
    steps.push({ name: "ensure pyserver", status: "passed", durationMs: Date.now() - started, detail: "reused" });
    return;
  }
  const port = new URL(pyserverUrl).port || "8001";
  startDetached(
    "pyserver",
    "uv",
    ["run", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", port],
    pyserverRoot,
    process.env,
  );
  try {
    await waitForHttp(healthUrl);
    steps.push({ name: "ensure pyserver", status: "passed", durationMs: Date.now() - started, detail: "started" });
  } catch (error) {
    steps.push({ name: "ensure pyserver", status: "failed", durationMs: Date.now() - started });
    throw error;
  }
}

async function ensureWeb(steps: StepResult[], webUrl: string) {
  const started = Date.now();
  if (await httpOk(webUrl)) {
    steps.push({ name: "ensure local web", status: "passed", durationMs: Date.now() - started, detail: "reused" });
    return;
  }
  const parsed = new URL(webUrl);
  const port = parsed.port || "3100";
  startDetached(
    "web",
    "npm",
    ["run", "dev", "--", "--hostname", "127.0.0.1", "--port", port],
    webRoot,
    { ...process.env, NEXT_PUBLIC_SITE_URL: webUrl },
  );
  try {
    await waitForHttp(webUrl, 90_000);
    steps.push({ name: "ensure local web", status: "passed", durationMs: Date.now() - started, detail: "started" });
  } catch (error) {
    steps.push({ name: "ensure local web", status: "failed", durationMs: Date.now() - started });
    throw error;
  }
}

async function validateUrl(steps: StepResult[], name: string, url: string) {
  const started = Date.now();
  if (!(await httpOk(url, 30_000))) {
    steps.push({ name, status: "failed", durationMs: Date.now() - started, detail: url });
    throw new Error(`${name} failed: ${url}`);
  }
  steps.push({ name, status: "passed", durationMs: Date.now() - started, detail: url });
}

async function validateSignalsEndpoint(
  steps: StepResult[],
  name: string,
  url: string,
  expectedDate: string,
) {
  const started = Date.now();
  try {
    const response = await fetch(url, { cache: "no-store", signal: AbortSignal.timeout(60_000) });
    if (!response.ok) throw new Error(`${name} returned HTTP ${response.status}`);
    const body = await response.json() as { signal_date?: string; latest_complete_date?: string; signals?: unknown[] };
    if (body.signal_date !== expectedDate || body.latest_complete_date !== expectedDate) {
      throw new Error(
        `${name} is stale: signal=${body.signal_date}, complete=${body.latest_complete_date}, expected=${expectedDate}`,
      );
    }
    steps.push({
      name,
      status: "passed",
      durationMs: Date.now() - started,
      detail: `${body.signals?.length ?? 0} signals`,
    });
  } catch (error) {
    steps.push({
      name,
      status: "failed",
      durationMs: Date.now() - started,
      detail: error instanceof Error ? error.message : String(error),
    });
    throw error;
  }
}

async function main() {
  loadDotEnv(path.join(webRoot, ".env.local"));
  loadDotEnv(path.join(pyserverRoot, ".env"));

  const now = shanghaiDateTimeParts();
  const expectedDate = process.env.DAILY_CLOSE_EXPECT_DATE ?? now.date;
  if (process.env.DAILY_CLOSE_ALLOW_INTRADAY !== "1" && now.hour < 15) {
    throw new Error("daily close pipeline cannot run before 15:00 Asia/Shanghai");
  }

  const pyserverUrl = process.env.PYSERVER_URL ?? "http://127.0.0.1:8001";
  const localWebUrl = process.env.DAILY_CLOSE_LOCAL_WEB_URL ?? "http://127.0.0.1:3100";
  const deployHost = process.env.DEPLOY_HOST ?? "root@47.77.231.22";
  const nextBasePath = process.env.NEXT_BASE_PATH ?? "/a-share";
  const remoteBaseUrl = process.env.DAILY_CLOSE_REMOTE_BASE_URL ?? "http://47.77.231.22/a-share";
  const steps: StepResult[] = [];
  const health: DailyCloseHealth = {
    generated_at: new Date().toISOString(),
    expected_market_date: expectedDate,
    status: "failed",
    steps,
    local: { pyserver_url: pyserverUrl, web_url: localWebUrl },
    remote: { base_url: remoteBaseUrl },
  };

  try {
    await ensurePyserver(steps, pyserverUrl);
    runCommand(steps, "refresh market data and dashboard", "npm", ["run", "dashboard:update"], {
      cwd: webRoot,
      env: { ...process.env, PYSERVER_URL: pyserverUrl },
    });
    runCommand(steps, "validate runtime consistency", "npm", ["run", "dashboard:validate"], { cwd: webRoot });

    const universe = loadActiveEntries(expectedDate);
    const pyserverCacheDb = path.resolve(webRoot, process.env.PYSERVER_CACHE_DB ?? "../pyserver/cache.db");
    const marketData = buildSymbolSeriesFromPyserverCache(universe, pyserverCacheDb);
    const benchmarkLatestDate = marketData.benchmark.at(-1)?.date;
    health.latest_market_date = benchmarkLatestDate;
    if (benchmarkLatestDate !== expectedDate) {
      health.status = "stale-or-no-session";
      health.error = `benchmark latest=${benchmarkLatestDate ?? "missing"}, expected=${expectedDate}`;
      writeRuntimeJson("daily-close-health.json", health);
      throw new Error(`${health.error}; refusing to deploy stale data`);
    }

    const validationInput = readRuntimeValidationInput();
    assertRuntimeArtifacts(validationInput);
    const analyst = readRuntimeJson<{ items?: AnalystCoverageItem[] }>("analyst.json");
    const coverageIssues = validateDailyCloseData({
      expectedDate,
      expectedUniverseCount: universe.length,
      benchmarkLatestDate,
      series: marketData.series.map((item) => ({
        symbol: item.entry.symbol,
        latestDate: item.klines.at(-1)?.date,
      })),
      allowedStaleSymbols: parseSymbolList(process.env.DAILY_CLOSE_ALLOWED_STALE_SYMBOLS),
      backtest: validationInput.backtest as RuntimeBacktestSnapshot | null,
      signals: validationInput.signals as RuntimeSignalsSnapshot | null,
      meta: validationInput.meta as RuntimeMetaSnapshot | null,
      analystItems: analyst?.items ?? [],
    });
    if (coverageIssues.length > 0) {
      throw new Error(
        `daily close data validation failed:\n${coverageIssues.map((issue) => `- ${issue.code}: ${issue.message}`).join("\n")}`,
      );
    }
    steps.push({ name: "validate complete close coverage", status: "passed", durationMs: 0, detail: `${universe.length}/${universe.length}` });

    runCommand(steps, "python syntax check", "uv", ["run", "python", "-m", "py_compile", "main.py"], { cwd: pyserverRoot });
    runCommand(steps, "typescript check", path.join(webRoot, "node_modules", ".bin", "tsc"), ["--noEmit"], { cwd: webRoot });
    runCommand(steps, "unit tests", "npm", ["test"], { cwd: webRoot });
    runCommand(steps, "dependency audit (high/critical gate)", "npm", ["audit", "--audit-level=high"], { cwd: webRoot });
    runIsolatedBuild(steps, nextBasePath);

    if (process.env.DAILY_CLOSE_SKIP_DEPLOY === "1") {
      steps.push({ name: "deploy server", status: "skipped", durationMs: 0 });
    } else {
      runCommand(steps, "deploy server", "npm", ["run", "deploy:server"], {
        cwd: webRoot,
        env: { ...process.env, DEPLOY_HOST: deployHost, NEXT_BASE_PATH: nextBasePath },
      });
    }

    await ensureWeb(steps, localWebUrl);
    await validateUrl(steps, "validate local home", localWebUrl);
    await validateUrl(steps, "validate local dashboard", new URL("dashboard", `${localWebUrl}/`).toString());
    await validateSignalsEndpoint(
      steps,
      "validate local signals",
      new URL("api/signals", `${localWebUrl}/`).toString(),
      expectedDate,
    );

    if (process.env.DAILY_CLOSE_SKIP_DEPLOY !== "1") {
      await validateUrl(steps, "validate remote home", remoteBaseUrl);
      await validateUrl(steps, "validate remote dashboard", `${remoteBaseUrl}/dashboard`);
      await validateSignalsEndpoint(
        steps,
        "validate remote signals",
        `${remoteBaseUrl}/api/signals`,
        expectedDate,
      );
    }

    health.status = "passed";
    health.generated_at = new Date().toISOString();
    writeRuntimeJson("daily-close-health.json", health);
    console.log(`\n[daily-close] PASS ${expectedDate}`);
  } catch (error) {
    health.generated_at = new Date().toISOString();
    health.error = error instanceof Error ? error.message : String(error);
    if (health.status !== "stale-or-no-session") health.status = "failed";
    writeRuntimeJson("daily-close-health.json", health);
    throw error;
  }
}

main().catch((error) => {
  console.error(`\n[daily-close] FAILED: ${error instanceof Error ? error.message : String(error)}`);
  process.exit(1);
});
