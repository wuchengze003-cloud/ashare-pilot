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

test("enhancement clients forward explicit point-in-time history ranges", async () => {
  const calls: string[] = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: string | URL | Request) => {
    calls.push(String(input));
    const url = new URL(String(input));
    const body = url.pathname === "/index-daily"
      ? { index_code: "000300.SH", index_name: "hs300", rows: [] }
      : { symbol: "000001", rows: [] };
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  }) as typeof fetch;

  try {
    const { fetchIndexDaily, fetchMarginDetail, fetchMoneyflow } = await import("../lib/pyserver");
    const range = { startDate: "2026-02-24", endDate: "2026-07-24" };
    await fetchMoneyflow("000001", range);
    await fetchMarginDetail("000001", range);
    await fetchIndexDaily("hs300", range);
    assert.equal(calls.length, 3);
    for (const call of calls) {
      const url = new URL(call);
      assert.equal(url.searchParams.get("start_date"), "20260224");
      assert.equal(url.searchParams.get("end_date"), "20260724");
      assert.equal(url.searchParams.has("days"), false);
    }
  } finally {
    globalThis.fetch = originalFetch;
  }
});
