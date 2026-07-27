// Provider registry. Config is resolved lazily inside getProvider() on every
// call (never at module import) so tests and ops tooling can switch providers
// by mutating process.env without re-importing modules.
import { openAiCompatComplete } from "./openaiCompat";
import { LlmUnavailableError } from "./types";
import type { ChatMessage, CompleteArgs, LlmProvider } from "./types";

export const SUPPORTED_PROVIDERS = ["deepseek", "kimi"] as const;
export type ProviderName = (typeof SUPPORTED_PROVIDERS)[number];

function sharedTimeoutMs(): number {
  return Number(
    process.env.LLM_TIMEOUT_MS ?? process.env.DEEPSEEK_TIMEOUT_MS ?? 120_000,
  );
}

class OpenAiCompatProvider implements LlmProvider {
  constructor(
    readonly name: string,
    private readonly apiKey: string | undefined,
    private readonly baseUrl: string,
    readonly defaultModel: string,
    private readonly missingKeyMessage: string,
  ) {}

  assertConfigured(): void {
    if (!this.apiKey) {
      throw new LlmUnavailableError(this.name, this.missingKeyMessage);
    }
  }

  complete(args: CompleteArgs): Promise<string> {
    // assertConfigured() runs before complete() in chat(); the non-null
    // assertion is safe by construction.
    return openAiCompatComplete(
      { provider: this.name, apiKey: this.apiKey as string, baseUrl: this.baseUrl },
      args,
    );
  }
}

/** DeepSeek — the historical default. Error message kept byte-identical to
 *  the pre-refactor lib/deepseek.ts ("DEEPSEEK_API_KEY is not set") so log
 *  greps and existing failure handling keep working. */
function deepseekProvider(): LlmProvider {
  return new OpenAiCompatProvider(
    "deepseek",
    process.env.DEEPSEEK_API_KEY,
    process.env.DEEPSEEK_BASE_URL ?? "https://api.deepseek.com",
    process.env.DEEPSEEK_MODEL ?? "deepseek-v4-pro",
    "DEEPSEEK_API_KEY is not set",
  );
}

/** Kimi — reserved provider for environments where DeepSeek is unavailable.
 *  Talks to the Moonshot OpenAI-compatible endpoint (api.moonshot.cn/v1 by
 *  default). A local bridge (e.g. a Kimi Work sidecar relaying to the
 *  desktop session) can be used instead by pointing KIMI_LLM_BASE_URL at the
 *  bridge's /v1 root; the wire format is identical.
 *
 *  Deliberately does NOT embed the agent-gw datasource SDK: the market-data
 *  plugins (iFinD/Wind/Gildata) are a data concern handled by the file-based
 *  bridge in lib/kimiBridge.ts, not by this LLM layer. */
function kimiProvider(): LlmProvider {
  return new OpenAiCompatProvider(
    "kimi",
    process.env.KIMI_LLM_API_KEY,
    process.env.KIMI_LLM_BASE_URL ?? "https://api.moonshot.cn/v1",
    process.env.KIMI_LLM_MODEL ?? "kimi-k2-0905-preview",
    "LLM_PROVIDER=kimi but KIMI_LLM_API_KEY is not set. " +
      "Export a Moonshot API key, or point KIMI_LLM_BASE_URL at a local Kimi bridge, " +
      "or unset LLM_PROVIDER to fall back to DeepSeek.",
  );
}

/** Resolve the active provider from LLM_PROVIDER (default: deepseek).
 *  Unknown names fail fast with an actionable message — a typo'd provider
 *  must never silently fall back, because that would hide a config error
 *  behind a different model's behavior. */
export function getProvider(name?: string): LlmProvider {
  const selected = (name ?? process.env.LLM_PROVIDER ?? "deepseek").trim().toLowerCase();
  switch (selected) {
    case "deepseek":
      return deepseekProvider();
    case "kimi":
      return kimiProvider();
    default:
      throw new LlmUnavailableError(
        selected,
        `Unknown LLM_PROVIDER "${selected}" (supported: ${SUPPORTED_PROVIDERS.join(", ")})`,
      );
  }
}

export function defaultTimeoutMs(): number {
  return sharedTimeoutMs();
}

export type { ChatMessage };
