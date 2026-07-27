import { NextRequest } from "next/server";
import { activeEntriesAsOf, readUniverse } from "@/lib/universe";
import { proposeRefresh } from "@/lib/universe-refresh";
import { hasConfiguredTokenAccess } from "@/lib/apiSecurity";

export const runtime = "nodejs";
export const maxDuration = 180;

// NDJSON: progress / log / result / error
export async function POST(req: NextRequest) {
  const provided = req.headers.get("x-universe-refresh-token");
  if (!hasConfiguredTokenAccess(provided, process.env.UNIVERSE_REFRESH_TOKEN)) {
    return Response.json(
      { error: "Universe refresh is backend-only. Run the server-side research refresh workflow with an internal token." },
      { status: 403 },
    );
  }

  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    async start(controller) {
      const send = (obj: unknown) => {
        controller.enqueue(encoder.encode(JSON.stringify(obj) + "\n"));
      };
      try {
        const current = readUniverse();
        const today = new Date().toISOString().slice(0, 10);
        const activeCount = activeEntriesAsOf(current.entries, today).length;
        send({ type: "log", message: `当前正式池 ${activeCount} 只，请求 DeepSeek 提议变更…` });

        const proposal = await proposeRefresh(current);
        send({
          type: "log",
          message: `提议: +${proposal.adds.length} / -${proposal.removes.length} / 改类 ${proposal.reclassifies.length}`,
        });
        send({ type: "log", message: proposal.rationale });

        send({
          type: "result",
          result: {
            proposal,
            applied: false,
            requires_manual_review: true,
            message: "候选变更已生成，未写入正式股票池。",
          },
        });
        controller.close();
      } catch (e) {
        console.error("[api/universe/refresh] unhandled error during refresh stream", { error: e instanceof Error ? e.message : String(e), stack: e instanceof Error ? e.stack : undefined });
        send({ type: "error", message: e instanceof Error ? e.message : String(e) });
        controller.close();
      }
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "application/x-ndjson; charset=utf-8",
      "Cache-Control": "no-store",
      "X-Accel-Buffering": "no",
    },
  });
}
