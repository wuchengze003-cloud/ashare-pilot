// DeepSeek v4 client with aggressive caching. Trading signals use the
// deterministic Dashboard rule scorer; this client is reserved for auxiliary
// AI workflows such as universe maintenance.
import { cached } from "./cache";

const API_KEY = process.env.DEEPSEEK_API_KEY;
const BASE_URL = process.env.DEEPSEEK_BASE_URL ?? "https://api.deepseek.com";
const MODEL = process.env.DEEPSEEK_MODEL ?? "deepseek-v4-pro";
const REQUEST_TIMEOUT_MS = Number(process.env.DEEPSEEK_TIMEOUT_MS ?? 120_000);

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

export async function chat(
  messages: ChatMessage[],
  opts: ChatOptions = {},
): Promise<string> {
  if (!API_KEY) throw new Error("DEEPSEEK_API_KEY is not set");
  const model = opts.model ?? MODEL;
  const temperature = opts.temperature ?? 0.2;
  const responseFormat = opts.responseFormat ?? "text";
  const ttl = opts.ttlSeconds ?? 12 * 3600;

  const cacheParts = { model, temperature, responseFormat, messages };
  const doFetch = async () => {
    const controller = new AbortController();
    let timeout: ReturnType<typeof setTimeout> | undefined;
    const body: Record<string, unknown> = {
      model,
      messages,
      temperature,
      stream: false,
    };
    if (responseFormat === "json_object") {
      body.response_format = { type: "json_object" };
    }
    try {
      const request = fetch(`${BASE_URL}/chat/completions`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${API_KEY}`,
        },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      const r = await Promise.race([
        request,
        new Promise<Response>((_, reject) => {
          timeout = setTimeout(() => {
            controller.abort();
            reject(new Error(`deepseek request timed out after ${REQUEST_TIMEOUT_MS}ms`));
          }, REQUEST_TIMEOUT_MS);
        }),
      ]);
      if (!r.ok) {
        throw new Error(`deepseek ${r.status}: ${await r.text()}`);
      }
      const j = (await r.json()) as {
        choices: { message: { content: string } }[];
      };
      const content = j.choices[0]?.message?.content ?? "";
      // Validate BEFORE returning: cached() persists whatever doFetch resolves to,
      // so an empty or unparseable json_object response must throw here rather than
      // poison the cache for 12h with a string that downstream JSON.parse rejects.
      if (responseFormat === "json_object") {
        if (!content.trim()) throw new Error("deepseek returned empty content");
        try {
          JSON.parse(content);
        } catch {
          throw new Error("deepseek returned unparseable json_object content");
        }
      }
      return content;
    } finally {
      if (timeout) clearTimeout(timeout);
    }
  };

  if (opts.bypassCache) return doFetch();
  return cached(cacheParts, ttl, doFetch);
}
