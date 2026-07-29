import { test } from "node:test";
import assert from "node:assert/strict";
import {
  hasConfiguredTokenAccess,
  hasInternalApiAccess,
  hasOpsPageAccess,
  isAshareSymbol,
  isIsoDateString,
  parseAshareSymbols,
} from "../lib/apiSecurity";

test("A-share symbols accept six digits only", () => {
  assert.equal(isAshareSymbol("688256"), true);
  assert.equal(isAshareSymbol("sh688256"), false);
  assert.equal(isAshareSymbol("688256?x=1"), false);
});

test("symbol arrays are deduplicated and bounded", () => {
  assert.deepEqual(parseAshareSymbols([" 688256 ", "688256", "300476"]), {
    ok: true,
    symbols: ["688256", "300476"],
  });
  assert.equal(parseAshareSymbols(["688256", "300476"], 1).ok, false);
  assert.equal(parseAshareSymbols(["not-a-code"]).ok, false);
});

test("internal API token is required in production and compared exactly", () => {
  assert.equal(hasInternalApiAccess(new Headers(), undefined, "development"), true);
  assert.equal(hasInternalApiAccess(new Headers(), undefined, "production"), false);
  assert.equal(
    hasInternalApiAccess(new Headers({ "x-internal-api-token": "correct" }), "correct", "production"),
    true,
  );
  assert.equal(
    hasInternalApiAccess(new Headers({ "x-internal-api-token": "wrong" }), "correct", "production"),
    false,
  );
});

test("hasConfiguredTokenAccess fails closed and matches exactly", () => {
  assert.equal(hasConfiguredTokenAccess(null, "secret"), false);
  assert.equal(hasConfiguredTokenAccess("secret", undefined), false);
  assert.equal(hasConfiguredTokenAccess("secret", "secret"), true);
  assert.equal(hasConfiguredTokenAccess("Secret", "secret"), false);
  assert.equal(hasConfiguredTokenAccess("secret2", "secret"), false);
  assert.equal(hasConfiguredTokenAccess("sec", "secret"), false);
});

test("ops page is local-only in development and fail-closed in production", () => {
  assert.equal(
    hasOpsPageAccess(new Headers({ host: "127.0.0.1:3100" }), null, undefined, "development"),
    true,
  );
  assert.equal(
    hasOpsPageAccess(new Headers({ host: "example.com" }), null, undefined, "development"),
    false,
  );
  assert.equal(
    hasOpsPageAccess(new Headers({ host: "127.0.0.1:3100" }), null, undefined, "production"),
    false,
  );
  assert.equal(
    hasOpsPageAccess(
      new Headers({ host: "example.com", authorization: "Bearer correct" }),
      null,
      "correct",
      "production",
    ),
    true,
  );
  assert.equal(
    hasOpsPageAccess(new Headers({ host: "example.com" }), "correct", "correct", "production"),
    true,
  );
});

test("isIsoDateString rejects malformed and impossible dates", () => {
  assert.equal(isIsoDateString("2026-07-23"), true);
  assert.equal(isIsoDateString("2026-02-30"), false);
  assert.equal(isIsoDateString("2026-13-01"), false);
  assert.equal(isIsoDateString("20260723"), false);
  assert.equal(isIsoDateString("2026-7-23"), false);
  assert.equal(isIsoDateString("../../etc/passwd"), false);
  assert.equal(isIsoDateString(""), false);
});
