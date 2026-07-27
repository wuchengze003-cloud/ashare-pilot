// Backwards-compatible facade for the LLM chat client.
//
// Trading signals use the deterministic Dashboard rule scorer; this client is
// reserved for auxiliary AI workflows such as universe maintenance.
//
// The implementation now lives in lib/llm/ behind a provider abstraction.
// Callers keep importing { chat } from "./deepseek" with zero changes; ops
// switches providers with the LLM_PROVIDER env var:
//   - unset / "deepseek" → DeepSeek (historical default, DEEPSEEK_* env vars)
//   - "kimi"            → Kimi via OpenAI-compatible endpoint (KIMI_LLM_* env vars)
// See lib/llm/providers.ts for error semantics when credentials are missing.
export { chat } from "./llm";
export { LlmUnavailableError } from "./llm/types";
export type { ChatMessage, ChatOptions } from "./llm/types";
