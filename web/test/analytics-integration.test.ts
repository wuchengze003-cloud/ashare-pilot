import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";

test("the replay recorder receives the same website id as the tracker", () => {
  const source = readFileSync(path.resolve(process.cwd(), "app/Analytics.tsx"), "utf8");
  const recorderBranch = source.match(/\{trackerReady && replayEnabled[\s\S]+?\) : null\}/)?.[0];

  assert.ok(recorderBranch, "recorder branch is missing");
  assert.match(recorderBranch, /data-website-id=\{websiteId\}/);
});
