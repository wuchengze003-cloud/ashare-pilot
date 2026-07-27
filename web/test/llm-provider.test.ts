import { test, before, after, beforeEach } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";

// Redirect the SQLite cache to a temp dir BEFORE importing.
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "scc-llm-"));
const origCwd = process.cwd();
process.chdir(tmp);

const PROVIDER_ENV_KEYS = [
  "LLM_PROVIDER",
  "LLM_TIMEOUT_MS",
  "DEEPSEEK_API_KEY",
  "DEEPSEEK_BASE_URL",
  "DEEPSEEK_MODEL",
  "KIMI_LLM_API_KEY",
  "KIMI_LLM_BASE_URL",
  "KIMI_LLM_MODEL",
] as const;

const savedEnv = new Map<string, string | undefined>(
  PROVIDER_ENV_KEYS.map((k) => [k, process.env[k]]),
);
for (const k of PROVIDER_ENV_KEYS) delete process.env[k];

// Import the chat() facade through the legacy deepseek.ts shim to prove the
// refactor keeps the old import path working.
let chat: typeof import("../lib/deepseek").chat;
const origFetch = globalThis.fetch;

interface FetchCapture {
  calls: number;
  urls: string[];
  auths: (string | null)[];
}

function stubContent(content: string): FetchCapture {
  const cap: FetchCapture = { calls: 0, urls: [], auths: [] };
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    cap.calls++;
    cap.urls.push(String(input));
    const headers = new Headers(init?.headers);
    cap.auths.push(headers.get("authorization"));
    return new Response(JSON.stringify({ choices: [{ message: { content } }] }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  }) as typeof fetch;
  return cap;
}

before(async () => {
  chat = (await import("../lib/deepseek")).chat;
});

beforeEach(() => {
  globalThis.fetch = origFetch;
  for (const k of PROVIDER_ENV_KEYS) delete process.env[k];
});

after(() => {
  globalThis.fetch = origFetch;
  for (const [k, v] of savedEnv) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
  process.chdir(origCwd);
});

const msgs = (tag: string) => [{ role: "user" as const, content: `llm-provider-${tag}` }];

test("defaults to deepseek and fails fast when DEEPSEEK_API_KEY is missing", async () => {
  await assert.rejects(() => chat(msgs("nokey")), /DEEPSEEK_API_KEY is not set/);
});

test("deepseek happy path hits the DeepSeek endpoint with a bearer key", async () => {
  process.env.DEEPSEEK_API_KEY = "ds-test-key";
  const cap = stubContent("hello from deepseek");
  const out = await chat(msgs("deepseek-ok"));
  assert.equal(out, "hello from deepseek");
  assert.equal(cap.calls, 1);
  assert.ok(cap.urls[0].startsWith("https://api.deepseek.com/"));
  assert.equal(cap.auths[0], "Bearer ds-test-key");
});

test("LLM_PROVIDER=kimi without a key reports an actionable error (graceful degradation)", async () => {
  process.env.LLM_PROVIDER = "kimi";
  process.env.DEEPSEEK_API_KEY = "ds-test-key"; // must NOT be used as a fallback
  await assert.rejects(
    () => chat(msgs("kimi-nokey")),
    (e: unknown) => {
      assert.ok(e instanceof Error);
      assert.equal((e as { name?: string }).name, "LlmUnavailableError");
      assert.match(e.message, /KIMI_LLM_API_KEY/);
      assert.match(e.message, /LLM_PROVIDER=kimi/);
      return true;
    },
  );
});

test("kimi provider calls its OpenAI-compatible endpoint, not DeepSeek", async () => {
  process.env.LLM_PROVIDER = "kimi";
  process.env.KIMI_LLM_API_KEY = "kimi-test-key";
  process.env.KIMI_LLM_BASE_URL = "https://kimi-bridge.local/v1";
  process.env.KIMI_LLM_MODEL = "kimi-k2-test";
  const cap = stubContent("hello from kimi");
  const out = await chat(msgs("kimi-ok"), { bypassCache: true });
  assert.equal(out, "hello from kimi");
  assert.equal(cap.calls, 1);
  assert.ok(cap.urls[0].startsWith("https://kimi-bridge.local/v1/chat/completions"));
  assert.equal(cap.auths[0], "Bearer kimi-test-key");
});

test("unknown LLM_PROVIDER value fails fast instead of silently falling back", async () => {
  process.env.LLM_PROVIDER = "openai";
  process.env.DEEPSEEK_API_KEY = "ds-test-key";
  await assert.rejects(
    () => chat(msgs("unknown")),
    (e: unknown) => {
      assert.ok(e instanceof Error);
      assert.equal((e as { name?: string }).name, "LlmUnavailableError");
      assert.match(e.message, /Unknown LLM_PROVIDER/);
      return true;
    },
  );
});

test("kimi json_object responses are validated before caching", async () => {
  process.env.LLM_PROVIDER = "kimi";
  process.env.KIMI_LLM_API_KEY = "kimi-test-key";
  const cap = stubContent("not json {");
  const m = msgs("kimi-badjson");
  await assert.rejects(
    () => chat(m, { responseFormat: "json_object" }),
    /unparseable/,
  );
  await assert.rejects(
    () => chat(m, { responseFormat: "json_object" }),
    /unparseable/,
  );
  // Bad content must never be cached: both calls re-hit the API.
  assert.equal(cap.calls, 2);
});
