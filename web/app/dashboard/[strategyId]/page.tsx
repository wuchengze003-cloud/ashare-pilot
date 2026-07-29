import { redirect } from "next/navigation";

export const dynamic = "force-dynamic";

interface PageProps {
  params: Promise<{ strategyId: string }>;
}

export default async function StrategyDetailPage({ params }: PageProps) {
  await params;
  redirect("/dashboard");
}
