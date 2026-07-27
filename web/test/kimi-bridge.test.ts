import { test, before, after, beforeEach } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";

// Bridge dir must be set before any read call; kimiBridge resolves it lazily
// per call, so setting it at module scope is enough.
const bridgeDirTmp = fs.mkdtempSync(path.join(os.tmpdir(), "scc-bridge-"));
const savedDir = process.env.KIMI_BRIDGE_DIR;
const savedMaxAge = process.env.KIMI_BRIDGE_MAX_AGE_HOURS;
process.env.KIMI_BRIDGE_DIR = bridgeDirTmp;
delete process.env.KIMI_BRIDGE_MAX_AGE_HOURS;

let bridge: typeof import("../lib/kimiBridge");

function writeFeed(name: string, content: string): string {
  const p = path.join(bridgeDirTmp, name);
  fs.writeFileSync(p, content);
  return p;
}

function cleanDir(): void {
  for (const f of fs.readdirSync(bridgeDirTmp)) {
    fs.rmSync(path.join(bridgeDirTmp, f), { force: true });
  }
}

before(async () => {
  bridge = await import("../lib/kimiBridge");
});

beforeEach(() => {
  cleanDir();
});

after(() => {
  if (savedDir === undefined) delete process.env.KIMI_BRIDGE_DIR;
  else process.env.KIMI_BRIDGE_DIR = savedDir;
  if (savedMaxAge === undefined) delete process.env.KIMI_BRIDGE_MAX_AGE_HOURS;
  else process.env.KIMI_BRIDGE_MAX_AGE_HOURS = savedMaxAge;
  fs.rmSync(bridgeDirTmp, { recursive: true, force: true });
});

test("missing bridge directory degrades gracefully to an empty result", () => {
  const p = process.env.KIMI_BRIDGE_DIR;
  process.env.KIMI_BRIDGE_DIR = path.join(bridgeDirTmp, "does-not-exist");
  try {
    const r = bridge.readScreenResults();
    assert.equal(r.status, "missing");
    assert.deepEqual(r.rows, []);
    assert.match(r.message ?? "", /no screen-results drop/);
  } finally {
    process.env.KIMI_BRIDGE_DIR = p;
  }
});

test("valid screen-results.csv parses into validated rows", () => {
  writeFeed(
    "screen-results.csv",
    [
      "symbol,name,market_cap_yi,pct_chg_20d,concepts",
      "300308,中际旭创,1450.5,12.3,光模块;CPO",
      "688256,寒武纪,3200.1,8.7,AI芯片",
    ].join("\n"),
  );
  const r = bridge.readScreenResults();
  assert.equal(r.status, "ok");
  assert.equal(r.rows.length, 2);
  assert.equal(r.rows[0].symbol, "300308");
  assert.equal(r.rows[0].name, "中际旭创");
  assert.equal(r.rows[0].marketCapYi, 1450.5);
  assert.deepEqual(r.rows[0].concepts, ["光模块", "CPO"]);
  assert.equal(r.droppedRows, 0);
});

test("rows with malformed symbols or empty names are dropped, not fatal", () => {
  writeFeed(
    "screen-results.csv",
    [
      "symbol,name",
      "300308,中际旭创",
      "ABC123,坏代码",
      "999999,",
      "hk00700,腾讯控股",
    ].join("\n"),
  );
  const r = bridge.readScreenResults();
  assert.equal(r.status, "ok");
  assert.equal(r.rows.length, 1);
  assert.equal(r.rows[0].symbol, "300308");
  assert.equal(r.droppedRows, 3);
});

test("stale files are refused with a clear reason", () => {
  const p = writeFeed("screen-results.csv", "symbol,name\n300308,中际旭创\n");
  // Age the file to 3 days ago, beyond the default 48h freshness limit.
  const old = new Date(Date.now() - 72 * 3600 * 1000);
  fs.utimesSync(p, old, old);
  const r = bridge.readScreenResults();
  assert.equal(r.status, "stale");
  assert.deepEqual(r.rows, []);
  assert.match(r.message ?? "", /freshness limit/);
  assert.equal(r.filePath, p);
});

test("maxAgeHours=0 disables the freshness check", () => {
  const p = writeFeed("screen-results.csv", "symbol,name\n300308,中际旭创\n");
  const old = new Date(Date.now() - 720 * 3600 * 1000);
  fs.utimesSync(p, old, old);
  const r = bridge.readScreenResults({ maxAgeHours: 0 });
  assert.equal(r.status, "ok");
  assert.equal(r.rows.length, 1);
});

test("CSV missing a required column is invalid, not silently misparsed", () => {
  writeFeed("screen-results.csv", "ticker,label\n300308,中际旭创\n");
  const r = bridge.readScreenResults();
  assert.equal(r.status, "invalid");
  assert.match(r.message ?? "", /symbol/);
});

test("newest dated file wins over older dated siblings", () => {
  writeFeed("screen-results-20260722.csv", "symbol,name\n300308,旧数据\n");
  writeFeed("screen-results-20260723.csv", "symbol,name\n688256,新数据\n");
  const r = bridge.readScreenResults();
  assert.equal(r.status, "ok");
  assert.equal(r.rows.length, 1);
  assert.equal(r.rows[0].symbol, "688256");
});

test("exact name.csv beats dated siblings", () => {
  writeFeed("screen-results-20260723.csv", "symbol,name\n688256,带日期\n");
  writeFeed("screen-results.csv", "symbol,name\n300308,无日期\n");
  const r = bridge.readScreenResults();
  assert.equal(r.status, "ok");
  assert.equal(r.rows[0].symbol, "300308");
});

test("JSON consensus feed parses and validates numeric values", () => {
  writeFeed(
    "consensus.json",
    JSON.stringify([
      { symbol: "300308", metric: "net_profit", period: "FY1", value: 527.2, unit: "亿元", source: "ifind" },
      { symbol: "688256", metric: "net_profit", period: "FY1", value: "not-a-number" },
    ]),
  );
  const r = bridge.readConsensus();
  assert.equal(r.status, "ok");
  assert.equal(r.rows.length, 1);
  assert.equal(r.rows[0].value, 527.2);
  assert.equal(r.rows[0].source, "ifind");
  assert.equal(r.droppedRows, 1);
});

test("CSV parser handles quoted fields with embedded commas and newlines", () => {
  const rows = bridge.parseCsv(
    'a,b,c\n1,"two, with comma","line1\nline2"\n3,plain,"quote ""inside"""',
  );
  assert.deepEqual(rows, [
    ["a", "b", "c"],
    ["1", "two, with comma", "line1\nline2"],
    ["3", "plain", 'quote "inside"'],
  ]);
});

test("bridgeStatus reports per-feed state without throwing", () => {
  writeFeed("screen-results.csv", "symbol,name\n300308,中际旭创\n");
  const s = bridge.bridgeStatus();
  assert.equal(s.feeds["screen-results"], "ok");
  assert.equal(s.feeds.consensus, "missing");
  assert.equal(s.dir, bridgeDirTmp);
});
