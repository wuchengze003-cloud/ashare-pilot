import { test } from "node:test";
import assert from "node:assert/strict";
import {
  hasInternalApiAccess,
  isAshareSymbol,
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
