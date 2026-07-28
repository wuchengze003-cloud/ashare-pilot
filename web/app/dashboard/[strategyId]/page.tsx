import { notFound } from "next/navigation";
import { readStrategyJson } from "@/lib/runtimeData";
import { getStrategy, STRATEGIES } from "@/lib/strategyRegistry";
import { StrategyDetailView } from "../StrategyDetailView";
import type { DashboardData } from "../types";

export const dynamic = "force-dynamic";

export function generateStaticParams() {
  return STRATEGIES.map((s) => ({ strategyId: s.id }));
}

interface PageProps {
  params: Promise<{ strategyId: string }>;
}

export default async function StrategyDetailPage({ params }: PageProps) {
  const { strategyId } = await params;
  const meta = getStrategy(strategyId);
  if (!meta) notFound();

  const data = readStrategyJson<DashboardData>(strategyId, "backtest.json");
  return <StrategyDetailView data={data} strategyId={strategyId} strategyMeta={meta} />;
}
