import { test } from "node:test";
import assert from "node:assert/strict";
import { mergeSpotIntoKlines } from "../lib/liveKlines";
import type { Kline, Spot } from "../lib/pyserver";

const fridayBars: Kline[] = [
  { date: "2025-06-19", open: 10, high: 10.2, low: 9.9, close: 10.1, volume: 1000 },
  { date: "2025-06-20", open: 10.1, high: 10.4, low: 10, close: 10.3, volume: 1200 },
];

function spot(asOf: string | undefined, price = 10.5): Spot {
  return {
    symbol: "600000",
    name: "X",
    price,
    change_pct: 0,
    volume: 2000,
    source: "easy_tdx",
    as_of: asOf,
  };
}

test("does not append a synthetic weekend bar from realtime quotes", () => {
  const merged = mergeSpotIntoKlines(fridayBars, spot("2025-06-22T10:00:00"), "2025-06-22");
  assert.deepEqual(merged, fridayBars);
});

test("does not append a fallback-date bar when spot timestamp is missing", () => {
  const merged = mergeSpotIntoKlines(fridayBars, spot(undefined), "2025-06-22");
  assert.deepEqual(merged, fridayBars);
});

test("updates the existing bar when the spot date equals the latest kline date", () => {
  const merged = mergeSpotIntoKlines(fridayBars, spot("2025-06-20T14:30:00", 10.6), "2025-06-20");
  assert.equal(merged.length, fridayBars.length);
  assert.equal(merged.at(-1)?.date, "2025-06-20");
  assert.equal(merged.at(-1)?.close, 10.6);
  assert.equal(merged.at(-1)?.high, 10.6);
});

test("can append a later weekday bar when the quote has a real trading date", () => {
  const merged = mergeSpotIntoKlines(fridayBars, spot("2025-06-23T14:30:00", 10.7), "2025-06-23");
  assert.equal(merged.length, fridayBars.length + 1);
  assert.equal(merged.at(-1)?.date, "2025-06-23");
  assert.equal(merged.at(-1)?.close, 10.7);
});
