// Kimi data bridge — file-based, zero-credential integration with Kimi Work.
//
// Architecture intent (see reports/2026-07-23/kimi-datasource-benchmark.md §3.3):
// Kimi's built-in finance plugins (iFinD / Wind / Gildata) are only reachable
// from inside a Kimi Work session with its injected credentials, and exporting
// those credentials into this project's runtime would spread a Kimi account
// key into CI/servers with an unpublished quota model. Instead, a Kimi Work
// Automation runs after the A-share close, calls the plugins there, and drops
// CSV/JSON files into a convention directory (data/kimi-bridge/ by default).
// This module is the ONLY project-side piece of that contract: it reads the
// dropped files read-only, validates their schema, refuses stale data, and
// degrades to an empty result when nothing has been dropped.
//
// Environments without Kimi (plain Node, CI, other agents) need no
// credentials at all — they simply see status "missing" and fall back to
// their normal data path (easy-tdx / Tushare / SQLite cache).
//
// Drop-file contract v1:
//   screen-results.(csv|json)     Gildata/iFinD 智能选股结果
//   consensus.(csv|json)          一致预期 / 盈利预测增量
//   Any feed may instead be written as <name>-YYYYMMDD.<ext>; when several
//   dated files exist the newest date wins. Freshness is judged by file
//   mtime against KIMI_BRIDGE_MAX_AGE_HOURS (default 48h, 0 disables).
import fs from "node:fs";
import path from "node:path";

export type BridgeStatus = "ok" | "missing" | "stale" | "invalid";

export interface BridgeResult<T> {
  status: BridgeStatus;
  rows: T[];
  /** Rows that failed per-row validation and were dropped. */
  droppedRows: number;
  filePath: string | null;
  /** ISO mtime of the file that was read, when one exists. */
  mtime: string | null;
  ageHours: number | null;
  /** Human-readable reason when status !== "ok"; undefined on success. */
  message?: string;
}

export interface BridgeReadOptions {
  /** Max acceptable file age in hours. Defaults to
   *  KIMI_BRIDGE_MAX_AGE_HOURS ?? 48. Pass 0 to disable the freshness check. */
  maxAgeHours?: number;
  /** Defaults to new Date(); injectable for tests. */
  now?: Date;
}

const DATE_SUFFIX_RE = /-(\d{8})\.(csv|json)$/;

export function bridgeDir(): string {
  return (
    process.env.KIMI_BRIDGE_DIR ?? path.join(process.cwd(), "data", "kimi-bridge")
  );
}

/** Minimal RFC-4180-ish CSV parser: handles quoted fields, escaped quotes
 *  (""), and embedded newlines inside quotes (Gildata drops embed multi-line
 *  Markdown tables in a single cell). Returns rows of raw string fields. */
export function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let inQuotes = false;
  let i = 0;
  while (i < text.length) {
    const c = text[i];
    if (inQuotes) {
      if (c === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i += 2;
          continue;
        }
        inQuotes = false;
        i++;
        continue;
      }
      field += c;
      i++;
      continue;
    }
    if (c === '"') {
      inQuotes = true;
      i++;
      continue;
    }
    if (c === ",") {
      row.push(field);
      field = "";
      i++;
      continue;
    }
    if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(field);
      field = "";
      rows.push(row);
      row = [];
      i++;
      continue;
    }
    field += c;
    i++;
  }
  if (field.length > 0 || row.length > 0) {
    row.push(field);
    rows.push(row);
  }
  return rows;
}

interface ResolvedFile {
  filePath: string;
  ext: "csv" | "json";
}

/** Resolve a feed name ("screen-results") to the file to read: an exact
 *  <name>.csv / <name>.json match wins; otherwise the newest dated sibling
 *  <name>-YYYYMMDD.<ext>. Returns null when nothing matches (also when the
 *  bridge directory itself does not exist). */
function resolveFeedFile(name: string): ResolvedFile | null {
  const dir = bridgeDir();
  let entries: string[];
  try {
    entries = fs.readdirSync(dir);
  } catch {
    return null;
  }
  for (const ext of ["csv", "json"] as const) {
    if (entries.includes(`${name}.${ext}`)) {
      return { filePath: path.join(dir, `${name}.${ext}`), ext };
    }
  }
  const dated = entries
    .filter((f) => f.startsWith(`${name}-`) && DATE_SUFFIX_RE.test(f))
    .sort();
  if (dated.length === 0) return null;
  const pick = dated[dated.length - 1];
  const m = DATE_SUFFIX_RE.exec(pick);
  return { filePath: path.join(dir, pick), ext: m?.[2] as "csv" | "json" };
}

function empty<T>(status: BridgeStatus, filePath: string | null, message?: string): BridgeResult<T> {
  return {
    status,
    rows: [],
    droppedRows: 0,
    filePath,
    mtime: null,
    ageHours: null,
    message,
  };
}

function checkFreshness(
  filePath: string,
  opts: BridgeReadOptions,
): { ok: true; mtime: Date; ageHours: number } | { ok: false; result: BridgeResult<never> } {
  const maxAgeHours =
    opts.maxAgeHours ?? Number(process.env.KIMI_BRIDGE_MAX_AGE_HOURS ?? 48);
  const stat = fs.statSync(filePath);
  const now = opts.now ?? new Date();
  const ageHours = (now.getTime() - stat.mtimeMs) / 3_600_000;
  if (maxAgeHours > 0 && ageHours > maxAgeHours) {
    return {
      ok: false,
      result: empty(
        "stale",
        filePath,
        `bridge file is ${ageHours.toFixed(1)}h old, older than the ${maxAgeHours}h freshness limit; ` +
          "treating it as absent until the Kimi drop task lands a newer file",
      ),
    };
  }
  return { ok: true, mtime: stat.mtime, ageHours };
}

/** Generic feed reader: locate → freshness-check → parse → per-row validate.
 *  Never throws for data problems; status communicates the failure mode and
 *  rows is [] so callers can fall back to their normal data path. */
export function readBridgeFeed<T>(
  name: string,
  opts: BridgeReadOptions & {
    /** Columns that must exist in the CSV header (for csv feeds). */
    requiredColumns?: string[];
    /** Map + validate one raw record. Return null to drop the row. */
    mapRow: (raw: Record<string, string>) => T | null;
  },
): BridgeResult<T> {
  const resolved = resolveFeedFile(name);
  if (!resolved) {
    return empty(
      "missing",
      null,
      `no ${name} drop found in ${bridgeDir()}; ` +
        "this environment has no Kimi bridge data — use the normal data path",
    );
  }

  const fresh = checkFreshness(resolved.filePath, opts);
  if (!fresh.ok) {
    return fresh.result as BridgeResult<T>;
  }

  let records: Record<string, string>[];
  try {
    const text = fs.readFileSync(resolved.filePath, "utf8");
    if (resolved.ext === "json") {
      const parsed = JSON.parse(text) as unknown;
      if (!Array.isArray(parsed)) {
        return empty("invalid", resolved.filePath, "JSON feed must be an array of objects");
      }
      records = parsed.map((r) =>
        r && typeof r === "object"
          ? Object.fromEntries(
              Object.entries(r as Record<string, unknown>).map(([k, v]) => [k, String(v)]),
            )
          : {},
      );
    } else {
      const table = parseCsv(text);
      if (table.length === 0) {
        return empty("invalid", resolved.filePath, "CSV feed is empty");
      }
      const header = table[0].map((h) => h.trim());
      const missingCols = (opts.requiredColumns ?? []).filter((c) => !header.includes(c));
      if (missingCols.length > 0) {
        return empty(
          "invalid",
          resolved.filePath,
          `CSV header is missing required column(s): ${missingCols.join(", ")}`,
        );
      }
      records = table.slice(1).map((cells) =>
        Object.fromEntries(header.map((h, idx) => [h, cells[idx] ?? ""])),
      );
    }
  } catch (e) {
    return empty(
      "invalid",
      resolved.filePath,
      `failed to parse ${resolved.ext.toUpperCase()} feed: ${e instanceof Error ? e.message : String(e)}`,
    );
  }

  const rows: T[] = [];
  let droppedRows = 0;
  for (const rec of records) {
    const mapped = opts.mapRow(rec);
    if (mapped === null) droppedRows++;
    else rows.push(mapped);
  }

  return {
    status: "ok",
    rows,
    droppedRows,
    filePath: resolved.filePath,
    mtime: fresh.mtime.toISOString(),
    ageHours: Math.round(fresh.ageHours * 10) / 10,
  };
}

// ---------------------------------------------------------------------------
// Feed v1: 智能选股结果 (screen-results)
// Columns: symbol,name,theme?,market_cap_yi?,pct_chg_20d?,concepts?(用 ; 分隔)
// ---------------------------------------------------------------------------

export interface ScreenResultRow {
  symbol: string; // 6 位 A 股代码
  name: string;
  theme?: string;
  marketCapYi?: number;
  pctChange20d?: number;
  concepts?: string[];
}

const A_SHARE_SYMBOL_RE = /^\d{6}(\.(SH|SZ|BJ))?$/i;

function num(v: string | undefined): number | undefined {
  if (v === undefined || v.trim() === "") return undefined;
  const n = Number(v);
  return Number.isFinite(n) ? n : undefined;
}

export function readScreenResults(opts: BridgeReadOptions = {}): BridgeResult<ScreenResultRow> {
  return readBridgeFeed<ScreenResultRow>("screen-results", {
    ...opts,
    requiredColumns: ["symbol", "name"],
    mapRow: (r) => {
      const symbol = (r.symbol ?? "").trim();
      const name = (r.name ?? "").trim();
      if (!A_SHARE_SYMBOL_RE.test(symbol) || !name) return null;
      const concepts = (r.concepts ?? "")
        .split(/[;；]/)
        .map((s) => s.trim())
        .filter(Boolean);
      return {
        symbol,
        name,
        theme: r.theme?.trim() || undefined,
        marketCapYi: num(r.market_cap_yi),
        pctChange20d: num(r.pct_chg_20d),
        concepts: concepts.length > 0 ? concepts : undefined,
      };
    },
  });
}

// ---------------------------------------------------------------------------
// Feed v1: 一致预期 / 盈利预测 (consensus)
// Columns: symbol,metric,period,value,unit?,source?
// ---------------------------------------------------------------------------

export interface ConsensusRow {
  symbol: string;
  metric: string; // e.g. net_profit / revenue / pe_forecast
  period: string; // e.g. FY1 / FY2 / 2026Q3
  value: number;
  unit?: string;
  source?: string; // ifind / wind / gildata
}

export function readConsensus(opts: BridgeReadOptions = {}): BridgeResult<ConsensusRow> {
  return readBridgeFeed<ConsensusRow>("consensus", {
    ...opts,
    requiredColumns: ["symbol", "metric", "period", "value"],
    mapRow: (r) => {
      const symbol = (r.symbol ?? "").trim();
      const metric = (r.metric ?? "").trim();
      const period = (r.period ?? "").trim();
      const value = num(r.value);
      if (!A_SHARE_SYMBOL_RE.test(symbol) || !metric || !period || value === undefined) {
        return null;
      }
      return {
        symbol,
        metric,
        period,
        value,
        unit: r.unit?.trim() || undefined,
        source: r.source?.trim() || undefined,
      };
    },
  });
}

/** Diagnostic snapshot of every known feed — handy for health endpoints and
 *  the daily-close pipeline to log bridge state without reading files twice. */
export function bridgeStatus(opts: BridgeReadOptions = {}): {
  dir: string;
  feeds: Record<string, BridgeStatus>;
} {
  return {
    dir: bridgeDir(),
    feeds: {
      "screen-results": readScreenResults(opts).status,
      consensus: readConsensus(opts).status,
    },
  };
}
