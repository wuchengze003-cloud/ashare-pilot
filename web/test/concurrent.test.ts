import { test } from "node:test";
import assert from "node:assert/strict";
import { mapPool } from "../lib/concurrent";

test("mapPool runs in order and preserves indices", async () => {
  const out = await mapPool([10, 20, 30, 40, 50], 2, async (n, i) => {
    await new Promise((r) => setTimeout(r, 5));
    return { n, i };
  });
  assert.deepEqual(out, [
    { n: 10, i: 0 },
    { n: 20, i: 1 },
    { n: 30, i: 2 },
    { n: 40, i: 3 },
    { n: 50, i: 4 },
  ]);
});

test("mapPool respects concurrency limit", async () => {
  let inflight = 0;
  let peak = 0;
  await mapPool(Array.from({ length: 20 }, (_, i) => i), 4, async () => {
    inflight++;
    peak = Math.max(peak, inflight);
    await new Promise((r) => setTimeout(r, 10));
    inflight--;
  });
  assert.ok(peak <= 4, `expected peak<=4, got ${peak}`);
  assert.ok(peak >= 2, `expected real parallelism, got peak=${peak}`);
});

test("mapPool handles empty input", async () => {
  const out = await mapPool([], 4, async () => "x");
  assert.deepEqual(out, []);
});

test("mapPool clamps a zero/negative/NaN limit to one worker", async () => {
  for (const limit of [0, -3, Number.NaN]) {
    const out = await mapPool([1, 2, 3], limit, async (n) => n * 2);
    assert.deepEqual(out, [2, 4, 6], `limit=${limit} should still process every item`);
  }
});

test("mapPool propagates errors", async () => {
  await assert.rejects(
    () => mapPool([1, 2, 3], 2, async (n) => {
      if (n === 2) throw new Error("boom");
      return n;
    }),
    /boom/,
  );
});
