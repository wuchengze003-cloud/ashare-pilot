import { test } from "node:test";
import assert from "node:assert/strict";

import {
  buildBuySignalHistoryRows,
  paginateBuySignalHistory,
  summarizeBuySignalHistory,
} from "../lib/buySignalHistory";

test("buy signal history keeps every archived buy and calculates current performance", () => {
  const rows = buildBuySignalHistoryRows(
    [
      {
        signal_date: "2026-06-25",
        signals: [
          { symbol: "A", action: "buy", size: 0.25, rationale: "buy A", signalPrice: 10 },
          { symbol: "B", action: "hold", size: 0.25, rationale: "hold B", signalPrice: 20 },
        ],
      },
      {
        signal_date: "2026-06-24",
        signals: [
          { symbol: "C", action: "buy", size: 0.25, rationale: "buy C", signalPrice: 25 },
        ],
      },
    ],
    new Map([["A", 11], ["C", 20]]),
    new Map([["A", "2026-06-29"], ["C", "2026-06-29"]]),
  );

  assert.equal(rows.length, 2);
  assert.equal(rows[0].symbol, "A");
  assert.ok(Math.abs((rows[0].changePct ?? 0) - 10) < 1e-9);
  assert.ok(Math.abs((rows[1].changePct ?? 0) + 20) < 1e-9);
});

test("buy signal history summary and pagination use the complete row set", () => {
  const rows = Array.from({ length: 23 }, (_, index) => ({
    signalDate: `2026-06-${String(29 - Math.floor(index / 4)).padStart(2, "0")}`,
    symbol: String(index),
    name: null,
    theme: null,
    signalPrice: 10,
    signalPriceDate: null,
    currentPrice: index === 22 ? null : 10,
    currentAsOf: null,
    changePct: index === 22 ? null : index % 2 === 0 ? 2 : -1,
    rationale: "test",
  }));

  const summary = summarizeBuySignalHistory(rows);
  assert.equal(summary.totalSignals, 23);
  assert.equal(summary.validSignals, 22);
  assert.equal(summary.positiveSignals, 11);
  assert.equal(summary.winRatePct, 50);
  assert.equal(summary.averageChangePct, 0.5);

  const secondPage = paginateBuySignalHistory(rows, 2, 10);
  assert.equal(secondPage.totalPages, 3);
  assert.equal(secondPage.startIndex, 10);
  assert.equal(secondPage.endIndex, 20);
  assert.deepEqual(secondPage.rows.map((row) => row.symbol), rows.slice(10, 20).map((row) => row.symbol));

  const clamped = paginateBuySignalHistory(rows, 99, 10);
  assert.equal(clamped.page, 3);
  assert.equal(clamped.rows.length, 3);
});

test("buy signal history treats sub-display precision changes as flat", () => {
  const rows = buildBuySignalHistoryRows(
    [{
      signal_date: "2026-06-29",
      signals: [{
        symbol: "A",
        action: "buy",
        size: 0.25,
        rationale: "buy A",
        signalPrice: 19.42,
      }],
    }],
    new Map([["A", 19.419]]),
    new Map([["A", "2026-06-29"]]),
  );

  assert.equal(rows[0].changePct, 0);
  assert.equal(summarizeBuySignalHistory(rows).positiveSignals, 0);
});
