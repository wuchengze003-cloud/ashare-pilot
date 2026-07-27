// Public chat() entry point with provider selection + aggressive caching.
// Trading signals use the deterministic Dashboard rule scorer; this client is
// reserved for auxiliary AI workflows such as universe maintenance.
import { cached } from "../cache";
import { defaultTimeoutMs, getProvider } from "./providers";
import type { ChatMessage, ChatOptions } from "./types";

export async function chat(
  messages: ChatMessage[],
  opts: ChatOptions = {},
): Promise<string> {
  const provider = getProvider();
  // Configuration check happens BEFORE the cache lookup, matching the
  // pre-refactor behavior: a missing key must fail fast even if a stale
  // cached completion exists from an earlier, configured process.
  provider.assertConfigured();

  const model = opts.model ?? provider.defaultModel;
  const temperature = opts.temperature ?? 0.2;
  const responseFormat = opts.responseFormat ?? "text";
  const ttl = opts.ttlSeconds ?? 12 * 3600;

  // Provider name is part of the cache key: swapping LLM_PROVIDER must never
  // replay another model's cached completions.
  const cacheParts = {
    provider: provider.name,
    model,
    temperature,
    responseFormat,
    messages,
  };
  const doFetch = () =>
    provider.complete({
      model,
      messages,
      temperature,
      responseFormat,
      timeoutMs: defaultTimeoutMs(),
    });

  if (opts.bypassCache) return doFetch();
  return cached(cacheParts, ttl, doFetch);
}
