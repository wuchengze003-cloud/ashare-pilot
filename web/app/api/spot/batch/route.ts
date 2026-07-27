import { NextRequest, NextResponse } from "next/server";
import { fetchSpots } from "@/lib/pyserver";
import { parseAshareSymbols } from "@/lib/apiSecurity";

export const runtime = "nodejs";

export async function POST(req: NextRequest) {
  const body = (await req.json().catch(() => ({}))) as { symbols?: unknown };
  const parsed = parseAshareSymbols(body.symbols);
  if (!parsed.ok) return NextResponse.json({ error: parsed.error }, { status: parsed.status });
  const { symbols } = parsed;

  try {
    return NextResponse.json(await fetchSpots(symbols));
  } catch (e) {
    console.error("[api/spot/batch] fetchSpots failed", { symbols, error: e instanceof Error ? e.message : String(e) });
    return NextResponse.json([]);
  }
}
