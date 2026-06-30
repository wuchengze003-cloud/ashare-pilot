import { NextRequest, NextResponse } from "next/server";
import { mapPool } from "@/lib/concurrent";
import { ruleBasedScorer } from "@/lib/dashboardBacktest";
import { mergeSpotIntoKlines } from "@/lib/liveKlines";
import { fetchFundamental, fetchKlines, fetchSpot } from "@/lib/pyserver";
import { readRuntimeJson } from "@/lib/runtimeData";
import { toExecutableSignals } from "@/lib/signalPolicy";
import type { SymbolSnapshot } from "@/lib/strategyTypes";
import { loadEntries } from "@/lib/universe";
import { hasInternalApiAccess, internalApiDeniedResponse } from "@/lib/apiSecurity";

export const runtime = "nodejs";
export const maxDuration = 180;

const LOAD_CONCURRENCY = Number(process.env.SIGNALS_LOAD_CONCURRENCY ?? 6);
const DEFAULT_LOOKBACK_DAYS = 140;
const DEFAULT_MAX_POSITIONS = 5;

type DatedSymbolSnapshot = SymbolSnapshot & {
  latestDate?: string;
};

interface OptimizedParams {
  maxPositions?: number;
  minScoreToBuy?: number;
}

interface StaticSignalsSnapshot {
  generated_at: string;
  source?: string;
  score_model?: string;
  signal_date?: string;
  latest_complete_date?: string;
  signal_basis?: string;
  snapshot_label?: string;
  max_positions?: number;
  universe_count?: number;
  scored_count?: number;
  skipped_count?: number;
  optimized_params?: OptimizedParams;
  signals?: Array<{
    symbol: string;
    action: "buy" | "hold" | "sell";
    confidence: number;
    size: number;
    rationale: string;
  }>;
}

function boundedInt(value: string | null, fallback: number, min: number, max: number): number {
  if (value == null || value.trim() === "") return fallback;
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.max(min, Math.min(max, Math.floor(n)));
}

function yyyymmdd(d: Date): string {
  return d.toISOString().slice(0, 10).replaceAll("-", "");
}

function boundedFloat(value: string | null, fallback: number, min: number, max: number): number {
  if (value == null || value.trim() === "") return fallback;
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.max(min, Math.min(max, n));
}

function loadOptimizedParams(): OptimizedParams {
  try {
    const parsed = readRuntimeJson<{
      optimizedParams?: OptimizedParams;
      config?: { maxPositions?: number };
      latestPlan?: { maxPositions?: number; minScoreToBuy?: number };
    }>("backtest.json");
    if (!parsed) return {};
    return {
      maxPositions:
        parsed.optimizedParams?.maxPositions ??
        parsed.latestPlan?.maxPositions ??
        parsed.config?.maxPositions,
      minScoreToBuy: parsed.optimizedParams?.minScoreToBuy ?? parsed.latestPlan?.minScoreToBuy,
    };
  } catch {
    return {};
  }
}

function loadStaticSignalsSnapshot(): StaticSignalsSnapshot | null {
  try {
    return readRuntimeJson<StaticSignalsSnapshot>("signals.json");
  } catch {
    return null;
  }
}

function signalCounts(signals: Array<{ action: "buy" | "hold" | "sell" }>) {
  return signals.reduce<Record<string, number>>((acc, s) => {
    acc[s.action] = (acc[s.action] ?? 0) + 1;
    return acc;
  }, {});
}

function snapshotSignals(snapshot: StaticSignalsSnapshot, maxPositions: number) {
  const raw = snapshot.signals ?? [];
  if (snapshot.max_positions === maxPositions) return raw;
  return toExecutableSignals(raw, { maxPositions });
}

function fallbackSignalsResponse(reason: string, maxPositions: number) {
  const snapshot = loadStaticSignalsSnapshot();
  if (!snapshot?.signals?.length) {
    return NextResponse.json(
      { error: reason, generated_at: new Date().toISOString() },
      { status: 502 },
    );
  }
  const effectiveMaxPositions = snapshot.max_positions ?? maxPositions;
  const signals = snapshotSignals(snapshot, effectiveMaxPositions);
  return NextResponse.json({
    generated_at: new Date().toISOString(),
    source: "static-snapshot-fallback",
    snapshot_source: snapshot.source ?? null,
    score_model: snapshot.score_model ?? "dashboard-rule",
    stale: true,
    fallback_reason: reason,
    signal_date: snapshot.signal_date ?? null,
    latest_complete_date: snapshot.latest_complete_date ?? snapshot.signal_date ?? null,
    signal_basis: snapshot.signal_basis ?? "cached-snapshot",
    snapshot_label: snapshot.snapshot_label ?? null,
    max_positions: effectiveMaxPositions,
    optimized_params: snapshot.optimized_params ?? null,
    universe_count: snapshot.universe_count ?? null,
    scored_count: snapshot.scored_count ?? null,
    skipped_count: snapshot.skipped_count ?? null,
    counts: signalCounts(signals),
    gross_buy_weight: Number(
      signals
        .filter((s) => s.action === "buy")
        .reduce((sum, s) => sum + s.size, 0)
        .toFixed(4),
    ),
    signals,
  });
}

function runtimeSignalsResponse(snapshot: StaticSignalsSnapshot, maxPositions: number) {
  const effectiveMaxPositions = snapshot.max_positions ?? maxPositions;
  const signals = snapshotSignals(snapshot, effectiveMaxPositions);
  return NextResponse.json({
    generated_at: new Date().toISOString(),
    source: "runtime-snapshot",
    snapshot_source: snapshot.source ?? null,
    score_model: snapshot.score_model ?? "dashboard-rule",
    stale: false,
    signal_date: snapshot.signal_date ?? null,
    latest_complete_date: snapshot.latest_complete_date ?? snapshot.signal_date ?? null,
    signal_basis: snapshot.signal_basis ?? "cached-snapshot",
    snapshot_label: snapshot.snapshot_label ?? null,
    max_positions: effectiveMaxPositions,
    optimized_params: snapshot.optimized_params ?? null,
    universe_count: snapshot.universe_count ?? null,
    scored_count: snapshot.scored_count ?? null,
    skipped_count: snapshot.skipped_count ?? null,
    counts: signalCounts(signals),
    gross_buy_weight: Number(
      signals
        .filter((s) => s.action === "buy")
        .reduce((sum, s) => sum + s.size, 0)
        .toFixed(4),
    ),
    signals,
  });
}

export async function GET(req: NextRequest) {
  const optimized = loadOptimizedParams();
  const lookbackDays = boundedInt(
    req.nextUrl.searchParams.get("lookbackDays"),
    DEFAULT_LOOKBACK_DAYS,
    40,
    400,
  );
  const maxPositions = boundedInt(
    req.nextUrl.searchParams.get("maxPositions"),
    optimized.maxPositions ?? DEFAULT_MAX_POSITIONS,
    1,
    20,
  );
  const minScoreToBuy = boundedFloat(
    req.nextUrl.searchParams.get("minScoreToBuy"),
    optimized.minScoreToBuy ?? 0.58,
    0,
    1,
  );
  const requestedAsOf = req.nextUrl.searchParams.get("asOf");
  const asOf = requestedAsOf ?? new Date().toISOString().slice(0, 10);
  const runtimeSnapshot = loadStaticSignalsSnapshot();
  const forceLive = req.nextUrl.searchParams.get("forceLive") === "1";
  const hasCustomLiveParams =
    req.nextUrl.searchParams.has("lookbackDays") ||
    req.nextUrl.searchParams.has("minScoreToBuy");
  const requiresLiveData =
    forceLive ||
    hasCustomLiveParams ||
    !runtimeSnapshot?.signals?.length ||
    Boolean(requestedAsOf && runtimeSnapshot.signal_date !== requestedAsOf);
  if (requiresLiveData && !hasInternalApiAccess(req.headers)) {
    return internalApiDeniedResponse();
  }
  if (
    !forceLive &&
    !hasCustomLiveParams &&
    runtimeSnapshot?.signals?.length &&
    (!requestedAsOf || runtimeSnapshot.signal_date === requestedAsOf)
  ) {
    return runtimeSignalsResponse(runtimeSnapshot, maxPositions);
  }

  const start = new Date(asOf);
  start.setDate(start.getDate() - lookbackDays);
  const startDate = yyyymmdd(start);
  const endDate = asOf.replaceAll("-", "");

  const universe = loadEntries();
  let skipped = 0;
  const spotSources: Record<string, number> = {};
  const spotTimes: string[] = [];
  const snapshots = await mapPool(universe, LOAD_CONCURRENCY, async (entry): Promise<DatedSymbolSnapshot | null> => {
    const [klines, fund, spot] = await Promise.all([
      fetchKlines(entry.symbol, startDate, endDate).catch(() => []),
      fetchFundamental(entry.symbol).catch(() => undefined),
      fetchSpot(entry.symbol).catch(() => null),
    ]);
    if (spot?.source) spotSources[spot.source] = (spotSources[spot.source] ?? 0) + 1;
    if (spot?.as_of) spotTimes.push(spot.as_of);
    const liveKlines = mergeSpotIntoKlines(klines, spot, asOf);
    if (liveKlines.length < 25) {
      skipped++;
      return null;
    }
    return {
      symbol: entry.symbol,
      name: entry.name,
      theme: entry.theme,
      note: entry.note,
      latestDate: liveKlines.at(-1)?.date,
      closes: liveKlines.map((k) => k.close),
      volumes: liveKlines.map((k) => k.volume),
      global_supply: entry.global_supply ?? null,
      fundamental: fund
        ? {
            pe_ttm: fund.pe_ttm,
            pb: fund.pb,
            market_cap: fund.market_cap,
            profit_yoy: fund.profit_yoy,
          }
        : undefined,
    };
  });

  const usable = snapshots.filter((s): s is DatedSymbolSnapshot => s !== null);
  if (usable.length === 0) {
    return fallbackSignalsResponse("no usable kline data from pyserver", maxPositions);
  }

  const signalDate = usable
    .map((s) => s.latestDate)
    .filter((d): d is string => Boolean(d))
    .sort()
    .at(-1) ?? asOf;
  const rawSignals = await ruleBasedScorer({ minScoreToBuy })(usable, { asOf: signalDate, mode: "backtest" });
  const signals = toExecutableSignals(rawSignals, { maxPositions });
  const counts = signalCounts(signals);

  return NextResponse.json({
    generated_at: new Date().toISOString(),
    source: "pyserver",
    score_model: "dashboard-rule",
    signal_date: signalDate,
    latest_complete_date: signalDate,
    signal_basis: signalDate === asOf ? "realtime-spot-merged" : "latest-complete-bar",
    lookback_days: lookbackDays,
    max_positions: maxPositions,
    optimized_params: optimized,
    min_score_to_buy: minScoreToBuy,
    universe_count: universe.length,
    scored_count: usable.length,
    skipped_count: skipped,
    spot_sources: spotSources,
    spot_as_of_min: spotTimes.sort()[0] ?? null,
    spot_as_of_max: spotTimes.sort().at(-1) ?? null,
    counts,
    gross_buy_weight: Number(
      signals
        .filter((s) => s.action === "buy")
        .reduce((sum, s) => sum + s.size, 0)
        .toFixed(4),
    ),
    signals,
  });
}
