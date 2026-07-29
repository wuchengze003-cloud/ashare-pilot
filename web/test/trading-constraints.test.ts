import { test } from "node:test";
import assert from "node:assert/strict";

import {
  configuredPriceLimitFraction,
  loadTradingConstraints,
} from "../lib/tradingConstraints";

test("production trading constraints encode the agreed capital and risk ceiling", () => {
  const constraints = loadTradingConstraints();

  assert.equal(constraints.initialCapitalYuan, 1_000_000);
  assert.equal(constraints.maxDrawdownPct, 15);
  assert.equal(constraints.tPlusOne, true);
  assert.equal(constraints.lotSize, 100);
  assert.ok(constraints.supportedSignalFrequencies.includes("5min"));
  assert.equal(constraints.intradayExecutionPrice, "next_bar_open");
});

test("configured board limits include ST, STAR, ChiNext, and BSE tiers", () => {
  assert.equal(configuredPriceLimitFraction("600000", "ST浦发"), 0.05);
  assert.equal(configuredPriceLimitFraction("688256", "寒武纪"), 0.2);
  assert.equal(configuredPriceLimitFraction("300308", "中际旭创"), 0.2);
  assert.equal(configuredPriceLimitFraction("920002", "万达轴承"), 0.3);
});
