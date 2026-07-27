import { NextRequest } from "next/server";
import { loadStrategyEntries } from "@/lib/universe";
import { fetchKlines, type Kline } from "@/lib/pyserver";
import { runBacktest, type SymbolSeries } from "@/lib/backtest";
import { parseBacktestConfigBody } from "@/lib/backtestConfig";
import { mapPool } from "@/lib/concurrent";
import { saveBacktestResult } from "@/lib/cache";
import { hasInternalApiAccess, internalApiDeniedResponse } from "@/lib/apiSecurity";

const LOAD_CONCURRENCY = Number(process.env.BACKTEST_LOAD_CONCURRENCY ?? 6);

export const runtime = "nodejs";
export const maxDuration = 300;

// NDJSON streaming protocol. Each line is one JSON object, one of:
//   { type: "progress", phase, done, total }
//   { type: "log", message }
//   { type: "result", result, stored }    // terminal — full BacktestResult
//   { type: "error", message }            // terminal
export async function POST(req: NextRequest) {
  if (!hasInternalApiAccess(req.headers)) return internalApiDeniedResponse();

  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return Response.json({ error: "request body must be valid JSON" }, { status: 400 });
  }
  const parsed = parseBacktestConfigBody(body);
  if (!parsed.ok) {
    return Response.json({ error: parsed.error }, { status: 400 });
  }
  const cfg = parsed.cfg;

  const padStart = new Date(cfg.startDate);
  padStart.setDate(padStart.getDate() - 120);
  const aksStart = padStart.toISOString().slice(0, 10).replaceAll("-", "");
  const aksEnd = cfg.endDate.replaceAll("-", "");

  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    async start(controller) {
      const send = (obj: unknown) => {
        controller.enqueue(encoder.encode(JSON.stringify(obj) + "\n"));
      };
      try {
        const universe = loadStrategyEntries();
        send({ type: "progress", phase: "loading", done: 0, total: universe.length });
        let loaded = 0;
        let failed = 0;
        // No fundamentals here: only today's snapshot exists, and feeding it
        // to historical rebalances is look-ahead (see lib/backtest.ts header).
        const loadedSeries = await mapPool(universe, LOAD_CONCURRENCY, async (entry): Promise<SymbolSeries | null> => {
          let klines: Kline[] | null = null;
          let why = "";
          try {
            klines = await fetchKlines(entry.symbol, aksStart, aksEnd);
            if (klines.length < 20) {
              why = `only ${klines.length} bars`;
              klines = null;
            }
          } catch (e) {
            why = e instanceof Error ? e.message : String(e);
          }
          loaded++;
          send({ type: "progress", phase: "loading", done: loaded, total: universe.length });
          if (!klines) {
            failed++;
            send({ type: "log", message: `skip ${entry.symbol} ${entry.name}: ${why.slice(0, 120)}` });
            return null;
          }
          return { entry, klines };
        });
        const series: SymbolSeries[] = loadedSeries.filter((x): x is SymbolSeries => x !== null);

        send({ type: "log", message: `${series.length} symbols loaded (${failed} failed/skipped)` });

        if (series.length === 0) {
          send({ type: "error", message: "no data loaded from pyserver" });
          controller.close();
          return;
        }

        const result = await runBacktest(series, cfg, (p) => {
          send({ type: "progress", ...p });
        });
        const stored = saveBacktestResult(result);
        send({ type: "log", message: `stored backtest ${stored.id}` });
        send({ type: "result", result, stored });
        controller.close();
      } catch (e) {
        console.error("[api/backtest] unhandled error during backtest stream", { error: e instanceof Error ? e.message : String(e), stack: e instanceof Error ? e.stack : undefined });
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
