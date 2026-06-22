import { NextRequest } from "next/server";
import { readUniverse } from "@/lib/universe";
import { proposeRefresh, applyRefresh } from "@/lib/universe-refresh";

export const runtime = "nodejs";
export const maxDuration = 180;

// NDJSON: progress / log / result / error
export async function POST(req: NextRequest) {
  const token = process.env.UNIVERSE_REFRESH_TOKEN;
  const provided = req.headers.get("x-universe-refresh-token");
  if (!token || provided !== token) {
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
        send({ type: "log", message: `当前股票池 ${current.entries.length} 只，请求 DeepSeek 提议变更…` });

        const proposal = await proposeRefresh(current);
        send({
          type: "log",
          message: `提议: +${proposal.adds.length} / -${proposal.removes.length} / 改类 ${proposal.reclassifies.length}`,
        });
        send({ type: "log", message: proposal.rationale });

        let validated = 0;
        const total = proposal.adds.filter(
          (a) => a.symbol && !current.entries.some((e) => e.symbol === a.symbol),
        ).length;
        send({ type: "progress", done: 0, total });

        const result = await applyRefresh(current, proposal, {
          onValidate: (symbol, ok) => {
            validated++;
            send({
              type: "log",
              message: `${ok ? "✓" : "✗"} 验证 ${symbol}`,
            });
            send({ type: "progress", done: validated, total });
          },
        });

        send({ type: "result", result });
        controller.close();
      } catch (e) {
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
