// LLM provider contract.
//
// Architecture intent: the trading pipeline is deterministic (rule scorer in
// lib/dashboardBacktest.ts); LLMs are only used for auxiliary workflows such as
// universe maintenance (lib/universe-refresh.ts). Keeping the LLM behind a
// provider interface lets ops swap DeepSeek for Kimi (or a local bridge) via
// the LLM_PROVIDER env var without touching call sites, and gives a single
// place for graceful-degradation error semantics when a provider lacks
// credentials in the current environment.

export interface ChatMessage {
  role: "system" | "user" | "assistant";
  content: string;
}

export interface ChatOptions {
  model?: string;
  temperature?: number;
  responseFormat?: "json_object" | "text";
  ttlSeconds?: number;
  bypassCache?: boolean;
}

/** Thrown when the selected provider cannot run in this environment
 *  (missing API key, unknown provider name). Distinct from a request-level
 *  failure: this is a configuration problem, not a transient API error, so
 *  callers should treat it as non-retryable. */
export class LlmUnavailableError extends Error {
  readonly provider: string;
  constructor(provider: string, message: string) {
    super(message);
    this.name = "LlmUnavailableError";
    this.provider = provider;
  }
}

export interface CompleteArgs {
  model: string;
  messages: ChatMessage[];
  temperature: number;
  responseFormat: "json_object" | "text";
  timeoutMs: number;
}

export interface LlmProvider {
  /** Stable provider id, included in the SQLite cache key so a model swap
   *  never replays another provider's cached completions. */
  readonly name: string;
  readonly defaultModel: string;
  /** @throws LlmUnavailableError when credentials/config are absent. */
  assertConfigured(): void;
  /** Raw chat completion; returns the assistant message content. */
  complete(args: CompleteArgs): Promise<string>;
}
