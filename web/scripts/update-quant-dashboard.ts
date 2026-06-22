// Refresh the strategy Dashboard's simulated portfolio.
//
// This script updates pyserver's kline cache for the current universe and then
// rebuilds ignored runtime snapshots under web/data/runtime/.
//
// Usage:
//   cd web && npm run dashboard:update
//
// Env overrides:
//   PYSERVER_URL=http://localhost:8001
//   DASHBOARD_START=2026-02-24
//   DASHBOARD_END=2026-06-22
//   UPDATE_QUANT_CONCURRENCY=3
import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { loadEntries } from "../lib/universe";
import { mapPool } from "../lib/concurrent";
import { writeRuntimeJson } from "../lib/runtimeData";

function loadDotEnvLocal() {
  const envPath = path.resolve(__dirname, "..", ".env.local");
  if (!fs.existsSync(envPath)) return;
  for (const line of fs.readFileSync(envPath, "utf-8").split("\n")) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    if (!m) continue;
    const [, key, raw] = m;
    if (process.env[key]) continue;
    process.env[key] = raw.replace(/\s+#.*$/, "").replace(/^["']|["']$/g, "").trim();
  }
}

function yyyymmdd(date: Date) {
  return date.toISOString().slice(0, 10).replaceAll("-", "");
}

async function main() {
  loadDotEnvLocal();
  const { fetchAnalysts, fetchKlines } = await import("../lib/pyserver");

  const startDate = process.env.DASHBOARD_START ?? "2026-02-24";
  const requestedEnd = process.env.DASHBOARD_END ?? new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
  const padStart = new Date(`${startDate}T00:00:00Z`);
  padStart.setUTCDate(padStart.getUTCDate() - 120);

  const start = yyyymmdd(padStart);
  const end = requestedEnd.replaceAll("-", "");
  const entries = loadEntries();
  const symbols = [...new Set(["sh000300", ...entries.map((entry) => entry.symbol)])];
  const concurrency = Number(process.env.UPDATE_QUANT_CONCURRENCY ?? 3);

  console.log(`Refreshing ${symbols.length} kline series from ${start} to ${end}`);
  let ok = 0;
  let failed = 0;
  await mapPool(symbols, concurrency, async (symbol, index) => {
    try {
      const rows = await fetchKlines(symbol, start, end);
      if (rows.length < 20) throw new Error(`only ${rows.length} bars`);
      ok++;
      process.stdout.write(`  ${index + 1}/${symbols.length} ${symbol} ok ${rows.at(-1)?.date ?? ""}\n`);
    } catch (error) {
      failed++;
      process.stdout.write(
        `  ${index + 1}/${symbols.length} ${symbol} FAIL ${error instanceof Error ? error.message : String(error)}\n`,
      );
    }
  });

  if (ok === 0) {
    throw new Error("No kline series refreshed. Check pyserver and PYSERVER_URL.");
  }
  if (failed > Math.max(3, Math.floor(symbols.length * 0.2))) {
    throw new Error(`Too many kline refresh failures: ${failed}/${symbols.length}`);
  }

  try {
    const analyst = await fetchAnalysts(entries.map((entry) => entry.symbol));
    writeRuntimeJson("analyst.json", {
      generated_at: new Date().toISOString(),
      items: analyst,
    });
    console.log(`Wrote runtime analyst snapshot for ${analyst.length} symbols`);
  } catch (error) {
    writeRuntimeJson("analyst.json", {
      generated_at: new Date().toISOString(),
      items: entries.map((entry) => ({ symbol: entry.symbol })),
      error: error instanceof Error ? error.message : String(error),
    });
    console.warn("Analyst snapshot unavailable; wrote symbol-only runtime fallback");
  }

  execFileSync("npx", ["tsx", "scripts/build-dashboard.ts"], {
    cwd: path.resolve(__dirname, ".."),
    env: process.env,
    stdio: "inherit",
  });
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
