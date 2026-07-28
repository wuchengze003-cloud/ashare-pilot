import { NextResponse } from "next/server";
import { readRuntimeJson } from "@/lib/runtimeData";

export const dynamic = "force-dynamic";

interface RuntimeManifest {
  generated_at?: string;
  git_sha?: string;
  universe_sha?: string;
  data_date?: string;
  latest_complete_date?: string;
  snapshot_basis?: string;
  strategies?: Array<{
    id: string;
    name: string;
    params?: Record<string, unknown>;
    file_sha256?: Record<string, string>;
  }>;
  cost_model?: string;
}

export async function GET() {
  const manifest = readRuntimeJson<RuntimeManifest>("manifest.json");

  if (!manifest) {
    return NextResponse.json(
      {
        commitSha: null,
        dataDate: null,
        universeSha: null,
        runtimeManifest: null,
        error: "manifest.json not found — dashboard has not been generated yet",
      },
      { status: 200 },
    );
  }

  return NextResponse.json({
    commitSha: manifest.git_sha ?? null,
    dataDate: manifest.data_date ?? manifest.latest_complete_date ?? null,
    universeSha: manifest.universe_sha ?? null,
    runtimeManifest: {
      generatedAt: manifest.generated_at ?? null,
      snapshotBasis: manifest.snapshot_basis ?? null,
      strategies: manifest.strategies ?? [],
      costModel: manifest.cost_model ?? null,
    },
  });
}
