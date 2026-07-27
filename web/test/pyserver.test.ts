import { test } from "node:test";
import assert from "node:assert/strict";

test("fetchAnalysts sends one deduped batch request", async () => {
  const calls: string[] = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: string | URL | Request) => {
    calls.push(String(input));
    return new Response(JSON.stringify([{ symbol: "300476", current_price: 1 }]), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  }) as typeof fetch;

  try {
    const { fetchAnalysts } = await import("../lib/pyserver");
    const out = await fetchAnalysts(["300476", "300476", "", "601138"]);
    assert.deepEqual(out, [{ symbol: "300476", current_price: 1 }]);
    assert.equal(calls.length, 1);
    const url = new URL(calls[0]);
    assert.equal(url.pathname, "/analysts");
    assert.equal(url.searchParams.get("symbols"), "300476,601138");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("fetchMinuteKlines forwards the bounded historical-minute query", async () => {
  const calls: string[] = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: string | URL | Request) => {
    calls.push(String(input));
    return new Response(JSON.stringify({
      symbol: "000001",
      ts_code: "000001.SZ",
      freq: "5min",
      source: "tushare_stk_mins",
      realtime: false,
      bars: [],
    }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  }) as typeof fetch;

  try {
    const { fetchMinuteKlines } = await import("../lib/pyserver");
    const out = await fetchMinuteKlines(
      "000001",
      "2026-07-01 09:30:00",
      "2026-07-23 15:00:00",
      "5min",
    );
    assert.equal(out.source, "tushare_stk_mins");
    assert.equal(out.realtime, false);
    assert.equal(calls.length, 1);
    const url = new URL(calls[0]);
    assert.equal(url.pathname, "/minute-klines");
    assert.equal(url.searchParams.get("symbol"), "000001");
    assert.equal(url.searchParams.get("start"), "2026-07-01 09:30:00");
    assert.equal(url.searchParams.get("end"), "2026-07-23 15:00:00");
    assert.equal(url.searchParams.get("freq"), "5min");
  } finally {
    globalThis.fetch = originalFetch;
  }
});
