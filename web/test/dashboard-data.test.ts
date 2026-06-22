import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import Database from "better-sqlite3";
import { buildSymbolSeriesFromPyserverCache } from "../lib/dashboardData";

function makeRows(start: string, count: number, firstClose: number) {
  const d = new Date(`${start}T00:00:00Z`);
  return Array.from({ length: count }, (_, i) => {
    const date = d.toISOString().slice(0, 10);
    d.setUTCDate(d.getUTCDate() + 1);
    const close = firstClose + i;
    return {
      date,
      open: close,
      high: close,
      low: close,
      close,
      volume: 1_000_000,
    };
  });
}

test("pyserver cache loader merges short fresh caches with longer history", () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "scc-dashboard-data-"));
  const dbPath = path.join(tmp, "cache.db");
  const db = new Database(dbPath);
  db.exec(`
    CREATE TABLE cache (
      key TEXT PRIMARY KEY,
      payload TEXT NOT NULL,
      fetched_at INTEGER NOT NULL,
      ttl_seconds INTEGER NOT NULL
    )
  `);

  const longRows = makeRows("2026-01-01", 35, 100);
  const shortRows = [
    { ...longRows.at(-1)!, close: 500, open: 500, high: 500, low: 500 },
    ...makeRows("2026-02-05", 1, 501),
  ];
  const insert = db.prepare(
    "INSERT INTO cache (key, payload, fetched_at, ttl_seconds) VALUES (?, ?, ?, ?)",
  );
  insert.run("kline:300502:20260101:20260204:qfq", JSON.stringify(longRows), 1_000, 3600);
  insert.run("kline:300502:20260204:20260205:qfq", JSON.stringify(shortRows), 2_000, 3600);
  db.close();

  const { series } = buildSymbolSeriesFromPyserverCache([
    { symbol: "300502", name: "新易盛", theme: "光模块" },
  ], dbPath);

  assert.equal(series.length, 1);
  assert.equal(series[0].klines.length, 36);
  assert.equal(series[0].klines.at(-2)?.close, 500);
  assert.equal(series[0].klines.at(-1)?.date, "2026-02-05");
  assert.equal(series[0].klines.at(-1)?.close, 501);
});
