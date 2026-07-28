import { NextResponse } from "next/server";
import { STRATEGIES } from "@/lib/strategyRegistry";

export const dynamic = "force-dynamic";

export async function GET() {
  return NextResponse.json({
    strategies: STRATEGIES.map((s) => ({
      id: s.id,
      name: s.name,
      codename: s.codename,
      description: s.description,
      factors: s.factors,
      defaultMinScore: s.defaultMinScore,
    })),
  });
}
