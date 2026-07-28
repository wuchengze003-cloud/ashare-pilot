import { test } from "node:test";
import assert from "node:assert/strict";
import { tideScorer } from "../lib/strategies/tide";
import { prismScorer } from "../lib/strategies/prism";
import { STRATEGIES, getStrategy, getDefaultStrategy } from "../lib/strategyRegistry";
import type { SymbolSnapshot } from "../lib/strategyTypes";

function makeSnapshot(symbol: string, closes: number[], volumes?: number[]): SymbolSnapshot {
  return {
    symbol,
    name: `Stock ${symbol}`,
    theme: "光模块",
    closes,
    volumes: volumes ?? closes.map(() => 1_000_000),
  };
}

// Generate a trending price series with volume
function trendingSeries(n: number, startPrice = 50, drift = 0.005): { closes: number[]; volumes: number[] } {
  const closes: number[] = [startPrice];
  const volumes: number[] = [1_000_000];
  for (let i = 1; i < n; i++) {
    closes.push(closes[i - 1] * (1 + drift + (Math.sin(i * 0.3) * 0.002)));
    volumes.push(1_000_000 * (1 + Math.sin(i * 0.5) * 0.3));
  }
  return { closes, volumes };
}

test("strategy registry has 3 strategies", () => {
  assert.equal(STRATEGIES.length, 3);
  assert.equal(STRATEGIES[0].id, "momentum-v1");
  assert.equal(STRATEGIES[1].id, "tide");
  assert.equal(STRATEGIES[2].id, "prism");
});

test("getStrategy returns correct strategy", () => {
  const tide = getStrategy("tide");
  assert.ok(tide);
  assert.equal(tide.name, "潮汐");
  assert.equal(tide.codename, "Tide");
  assert.equal(getStrategy("nonexistent"), undefined);
});

test("getDefaultStrategy returns momentum-v1", () => {
  assert.equal(getDefaultStrategy().id, "momentum-v1");
});

test("tide scorer produces valid signals", async () => {
  const scorer = tideScorer({ minScoreToBuy: 0.3 });
  const snapshots: SymbolSnapshot[] = [];
  for (let i = 0; i < 8; i++) {
    const { closes, volumes } = trendingSeries(40, 50 + i * 5, 0.003 + i * 0.001);
    snapshots.push(makeSnapshot(`sz00000${i}`, closes, volumes));
  }
  const signals = await scorer(snapshots, { asOf: "2026-06-01", mode: "backtest" });
  assert.ok(signals.length > 0);
  for (const s of signals) {
    assert.ok(["buy", "hold", "sell"].includes(s.action));
    assert.ok(s.confidence >= 0 && s.confidence <= 1);
    assert.ok(s.rationale.startsWith("潮汐:"));
  }
});

test("prism scorer produces valid signals", async () => {
  const scorer = prismScorer({ minScoreToBuy: 0.3 });
  const snapshots: SymbolSnapshot[] = [];
  for (let i = 0; i < 8; i++) {
    const { closes, volumes } = trendingSeries(40, 50 + i * 5, 0.003 + i * 0.001);
    snapshots.push(makeSnapshot(`sz00000${i}`, closes, volumes));
  }
  const signals = await scorer(snapshots, { asOf: "2026-06-01", mode: "backtest" });
  assert.ok(signals.length > 0);
  for (const s of signals) {
    assert.ok(["buy", "hold", "sell"].includes(s.action));
    assert.ok(s.confidence >= 0 && s.confidence <= 1);
    assert.ok(s.rationale.startsWith("棱镜:"));
  }
});

test("tide scorer returns empty for insufficient data", async () => {
  const scorer = tideScorer();
  const snapshots = [makeSnapshot("sz000001", [50, 51, 52])]; // too few closes
  const signals = await scorer(snapshots, { asOf: "2026-06-01", mode: "backtest" });
  assert.equal(signals.length, 0);
});

test("prism scorer returns empty for insufficient data", async () => {
  const scorer = prismScorer();
  const snapshots = [makeSnapshot("sz000001", [50, 51, 52])];
  const signals = await scorer(snapshots, { asOf: "2026-06-01", mode: "backtest" });
  assert.equal(signals.length, 0);
});

test("all strategy createScorer factories work", () => {
  for (const strategy of STRATEGIES) {
    const scorer = strategy.createScorer({ minScoreToBuy: 0.5 });
    assert.equal(typeof scorer, "function");
  }
});
