import { NextRequest, NextResponse } from "next/server";
import {
  buildProductionSignalsApiPayload,
  readProductionGate,
  readProductionSignals,
} from "@/lib/productionGate";
import { isIsoDateString } from "@/lib/apiSecurity";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const requestedAsOf = req.nextUrl.searchParams.get("asOf");
  if (requestedAsOf !== null && !isIsoDateString(requestedAsOf)) {
    return NextResponse.json(
      { error: "asOf must be a valid YYYY-MM-DD date" },
      { status: 400 },
    );
  }

  const unsupportedParams = [
    "forceLive",
    "lookbackDays",
    "minScoreToBuy",
    "maxPositions",
  ].filter((name) => req.nextUrl.searchParams.has(name));
  if (unsupportedParams.length > 0) {
    return NextResponse.json(
      {
        error: "production signals are immutable; runtime scoring parameters are not supported",
        unsupported_params: unsupportedParams,
      },
      { status: 400 },
    );
  }

  const gate = readProductionGate();
  const signals = readProductionSignals();
  return NextResponse.json(
    buildProductionSignalsApiPayload(gate, signals, requestedAsOf),
    {
      status: 200,
      headers: {
        "Cache-Control": "no-store",
      },
    },
  );
}
