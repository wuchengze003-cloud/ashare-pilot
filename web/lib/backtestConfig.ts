// Request-body validation for POST /api/backtest. The route streams NDJSON, so
// any config error must be rejected BEFORE the stream starts — otherwise a
// malformed body (missing endDate, bad dates, non-numeric knobs) throws outside
// the stream's try/catch and surfaces as an opaque 500.
import type { BacktestConfig } from "./backtest";
import { isIsoDateString } from "./apiSecurity";

export type BacktestConfigParseResult =
  | { ok: true; cfg: BacktestConfig }
  | { ok: false; error: string };

function boundedNumber(
  value: unknown,
  fallback: number,
  min: number,
  max: number,
  name: string,
): { ok: true; value: number } | { ok: false; error: string } {
  if (value === undefined || value === null) return { ok: true, value: fallback };
  const n = Number(value);
  if (!Number.isFinite(n) || n < min || n > max) {
    return { ok: false, error: `${name} must be a number in [${min}, ${max}]` };
  }
  return { ok: true, value: n };
}

function boundedInt(
  value: unknown,
  fallback: number,
  min: number,
  max: number,
  name: string,
): { ok: true; value: number } | { ok: false; error: string } {
  const r = boundedNumber(value, fallback, min, max, name);
  if (!r.ok) return r;
  if (!Number.isInteger(r.value)) {
    return { ok: false, error: `${name} must be an integer in [${min}, ${max}]` };
  }
  return r;
}

export function parseBacktestConfigBody(body: unknown): BacktestConfigParseResult {
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    return { ok: false, error: "request body must be a JSON object" };
  }
  const b = body as Record<string, unknown>;

  if (typeof b.startDate !== "string" || !isIsoDateString(b.startDate)) {
    return { ok: false, error: "startDate must be a valid YYYY-MM-DD date" };
  }
  if (typeof b.endDate !== "string" || !isIsoDateString(b.endDate)) {
    return { ok: false, error: "endDate must be a valid YYYY-MM-DD date" };
  }
  if (b.startDate > b.endDate) {
    return { ok: false, error: "startDate must be on or before endDate" };
  }

  const startCash = boundedNumber(b.startCash, 1_000_000, 1, 1e12, "startCash");
  if (!startCash.ok) return startCash;
  const feeBps = boundedNumber(b.feeBps, 10, 0, 1000, "feeBps");
  if (!feeBps.ok) return feeBps;
  const maxPositions = boundedInt(b.maxPositions, 5, 1, 50, "maxPositions");
  if (!maxPositions.ok) return maxPositions;
  const rebalanceEveryNDays = boundedInt(
    b.decisionEveryNDays ?? b.rebalanceEveryNDays,
    1,
    1,
    250,
    "decisionEveryNDays",
  );
  if (!rebalanceEveryNDays.ok) return rebalanceEveryNDays;
  const minHoldBars = boundedInt(b.minHoldBars, 5, 0, 250, "minHoldBars");
  if (!minHoldBars.ok) return minHoldBars;
  const rebalanceThresholdPct = boundedNumber(
    b.rebalanceThresholdPct,
    5,
    0,
    100,
    "rebalanceThresholdPct",
  );
  if (!rebalanceThresholdPct.ok) return rebalanceThresholdPct;
  const sharpeTarget = boundedNumber(b.sharpeTarget, 3, 0, 100, "sharpeTarget");
  if (!sharpeTarget.ok) return sharpeTarget;

  const autoSellUnselected =
    b.autoSellUnselected === undefined || b.autoSellUnselected === null
      ? true
      : b.autoSellUnselected === true || b.autoSellUnselected === false
        ? b.autoSellUnselected
        : null;
  if (autoSellUnselected === null) {
    return { ok: false, error: "autoSellUnselected must be a boolean" };
  }
  if (
    b.optimizationWindow !== undefined &&
    b.optimizationWindow !== null &&
    typeof b.optimizationWindow !== "string"
  ) {
    return { ok: false, error: "optimizationWindow must be a string" };
  }

  return {
    ok: true,
    cfg: {
      startCash: startCash.value,
      rebalanceEveryNDays: rebalanceEveryNDays.value,
      decisionEveryNDays: rebalanceEveryNDays.value,
      executionPrice: "next_open",
      startDate: b.startDate,
      endDate: b.endDate,
      feeBps: feeBps.value,
      maxPositions: maxPositions.value,
      autoSellUnselected,
      minHoldBars: minHoldBars.value,
      rebalanceThresholdPct: rebalanceThresholdPct.value,
      sharpeTarget: sharpeTarget.value,
      optimizationWindow:
        (b.optimizationWindow as string | undefined) ?? "post_cny_2026",
    },
  };
}
