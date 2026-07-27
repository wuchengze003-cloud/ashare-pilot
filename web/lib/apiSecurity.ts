import { timingSafeEqual } from "node:crypto";

export const MAX_PUBLIC_SYMBOLS = 100;

const A_SHARE_SYMBOL = /^\d{6}$/;

export type SymbolListResult =
  | { ok: true; symbols: string[] }
  | { ok: false; error: string; status: number };

export function isAshareSymbol(value: string): boolean {
  return A_SHARE_SYMBOL.test(value);
}

export function parseAshareSymbols(
  value: unknown,
  maxSymbols = MAX_PUBLIC_SYMBOLS,
): SymbolListResult {
  if (!Array.isArray(value)) {
    return { ok: false, error: "symbols must be an array", status: 400 };
  }

  const symbols: string[] = [];
  const seen = new Set<string>();
  for (const item of value) {
    if (typeof item !== "string") {
      return { ok: false, error: "symbols must contain strings only", status: 400 };
    }
    const symbol = item.trim();
    if (!isAshareSymbol(symbol)) {
      return { ok: false, error: `invalid A-share symbol: ${symbol.slice(0, 16)}`, status: 400 };
    }
    if (!seen.has(symbol)) {
      seen.add(symbol);
      symbols.push(symbol);
      if (symbols.length > maxSymbols) {
        return { ok: false, error: `at most ${maxSymbols} symbols are allowed`, status: 413 };
      }
    }
  }

  if (symbols.length === 0) {
    return { ok: false, error: "symbols required", status: 400 };
  }
  return { ok: true, symbols };
}

function sameSecret(left: string, right: string): boolean {
  const leftBuffer = Buffer.from(left);
  const rightBuffer = Buffer.from(right);
  return leftBuffer.length === rightBuffer.length && timingSafeEqual(leftBuffer, rightBuffer);
}

/** Timing-safe check of a caller-provided token against a configured secret.
 *  Fails closed when the secret is not configured. */
export function hasConfiguredTokenAccess(
  provided: string | null,
  configuredToken: string | undefined,
): boolean {
  if (!configuredToken) return false;
  return Boolean(provided && sameSecret(provided, configuredToken));
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

/** Strict YYYY-MM-DD check that also rejects impossible dates like 2026-02-30. */
export function isIsoDateString(value: string): boolean {
  if (!ISO_DATE.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

export function hasInternalApiAccess(
  headers: Headers,
  configuredToken = process.env.INTERNAL_API_TOKEN,
  nodeEnv = process.env.NODE_ENV,
): boolean {
  if (!configuredToken) return nodeEnv !== "production";
  const provided = headers.get("x-internal-api-token");
  return Boolean(provided && sameSecret(provided, configuredToken));
}

export function internalApiDeniedResponse(): Response {
  return Response.json(
    { error: "This operation is available only to the internal update workflow." },
    { status: 403 },
  );
}
