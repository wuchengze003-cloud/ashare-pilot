import { NextRequest, NextResponse } from "next/server";
import { fetchAnalysts, fetchAnalyst } from "@/lib/pyserver";
import { fallback, timeout } from "@/lib/analyst-fallback";
import { parseAshareSymbols } from "@/lib/apiSecurity";

export const runtime = "nodejs";

const ANALYST_TIMEOUT_MS = 35_000;

export async function POST(req: NextRequest) {
  const body = (await req.json().catch(() => ({}))) as { symbols?: unknown };
  const parsed = parseAshareSymbols(body.symbols);
  if (!parsed.ok) return NextResponse.json({ error: parsed.error }, { status: parsed.status });
  const { symbols } = parsed;

  try {
    if (symbols.length === 1) {
      const data = await Promise.race([fetchAnalyst(symbols[0]), timeout(ANALYST_TIMEOUT_MS)]);
      return NextResponse.json([data]);
    }
    const data = await Promise.race([fetchAnalysts(symbols), timeout(ANALYST_TIMEOUT_MS)]);
    return NextResponse.json(data);
  } catch {
    const { data, anySucceeded } = await fallback(symbols);
    if (!anySucceeded) {
      return NextResponse.json({ error: "analyst service unavailable" }, { status: 504 });
    }
    return NextResponse.json(data);
  }
}
