// Watchlist loader/writer. The list lives in `web/data/universe.json` so
// it can be edited by hand or refreshed by DeepSeek via the API route.
// Server-only — uses Node fs.
import fs from "node:fs";
import path from "node:path";

export interface UniverseEntry {
  symbol: string;
  name: string;
  theme: string;
  note?: string;
  /** Does the company sell into the global AI supply chain (NVIDIA, AMD,
   *  Apple, Google, hyperscalers) — vs. domestic-only revenue. */
  global_supply?: boolean;
  /** Current curation tier. Watch entries never enter live scoring unless they
   *  also carry a historical strategy_until boundary. */
  pool_tier?: "core" | "watch";
  /** Inclusive point-in-time strategy membership boundaries. */
  strategy_from?: string;
  strategy_until?: string;
  /** Preserve the historical theme when a classification changes. */
  previous_theme?: string;
  theme_effective_from?: string;
  review_reason?: string;
}

export interface UniverseFile {
  $schema_note?: string;
  updated_at: string;
  updated_by: string;
  entries: UniverseEntry[];
}

const FILE = path.join(process.cwd(), "data", "universe.json");

export function readUniverse(): UniverseFile {
  const raw = fs.readFileSync(FILE, "utf-8");
  return JSON.parse(raw) as UniverseFile;
}

export function writeUniverse(file: UniverseFile): void {
  // Write-then-rename so a crash mid-write can't truncate the live file: rename
  // is atomic within a directory, so readers see either the old or new file whole.
  const tmp = path.join(path.dirname(FILE), `.${path.basename(FILE)}.${process.pid}.tmp`);
  fs.writeFileSync(tmp, JSON.stringify(file, null, 2) + "\n", "utf-8");
  fs.renameSync(tmp, FILE);
}

function shanghaiToday(): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}

export function isStrategyEntryAsOf(entry: UniverseEntry, asOf: string): boolean {
  const hasHistoricalRange = Boolean(entry.strategy_from || entry.strategy_until);
  if (entry.pool_tier === "watch" && !hasHistoricalRange) return false;
  if (entry.strategy_from && asOf < entry.strategy_from) return false;
  if (entry.strategy_until && asOf > entry.strategy_until) return false;
  return true;
}

export function resolveEntryAsOf(entry: UniverseEntry, asOf: string): UniverseEntry {
  if (
    entry.previous_theme &&
    entry.theme_effective_from &&
    asOf < entry.theme_effective_from
  ) {
    return { ...entry, theme: entry.previous_theme };
  }
  return entry;
}

export function activeEntriesAsOf(entries: UniverseEntry[], asOf: string): UniverseEntry[] {
  return entries
    .filter((entry) => isStrategyEntryAsOf(entry, asOf))
    .map((entry) => resolveEntryAsOf(entry, asOf));
}

/** All curated records, including current watch candidates. */
export function loadEntries(): UniverseEntry[] {
  return readUniverse().entries;
}

/** Entries that participate in at least one historical or current strategy period. */
export function loadStrategyEntries(): UniverseEntry[] {
  return loadEntries().filter(
    (entry) => entry.pool_tier !== "watch" || Boolean(entry.strategy_from || entry.strategy_until),
  );
}

export function loadActiveEntries(asOf = shanghaiToday()): UniverseEntry[] {
  return activeEntriesAsOf(loadEntries(), asOf);
}

export function loadWatchEntries(): UniverseEntry[] {
  return loadEntries().filter((entry) => entry.pool_tier === "watch");
}
