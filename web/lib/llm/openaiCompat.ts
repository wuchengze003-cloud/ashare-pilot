// Shared OpenAI-compatible /chat/completions client.
// DeepSeek, Kimi (api.moonshot.cn) and any local bridge that speaks the
// OpenAI wire format can all reuse this request path.
import type { ChatMessage } from "./types";

export interface OpenAiCompatConfig {
  provider: string;
  apiKey: string;
  baseUrl: string;
}

export async function openAiCompatComplete(
  cfg: OpenAiCompatConfig,
  args: {
    model: string;
    messages: ChatMessage[];
    temperature: number;
    responseFormat: "json_object" | "text";
    timeoutMs: number;
  },
): Promise<string> {
  const controller = new AbortController();
  let timeout: ReturnType<typeof setTimeout> | undefined;
  const body: Record<string, unknown> = {
    model: args.model,
    messages: args.messages,
    temperature: args.temperature,
    stream: false,
  };
  if (args.responseFormat === "json_object") {
    body.response_format = { type: "json_object" };
  }
  try {
    const request = fetch(`${cfg.baseUrl}/chat/completions`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${cfg.apiKey}`,
      },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    const r = await Promise.race([
      request,
      new Promise<Response>((_, reject) => {
        timeout = setTimeout(() => {
          controller.abort();
          reject(
            new Error(
              `${cfg.provider} request timed out after ${args.timeoutMs}ms`,
            ),
          );
        }, args.timeoutMs);
      }),
    ]);
    if (!r.ok) {
      throw new Error(`${cfg.provider} ${r.status}: ${await r.text()}`);
    }
    const j = (await r.json()) as {
      choices: { message: { content: string } }[];
    };
    const content = j.choices[0]?.message?.content ?? "";
    // Validate BEFORE returning: cached() persists whatever this resolves to,
    // so an empty or unparseable json_object response must throw here rather
    // than poison the cache for 12h with a string downstream JSON.parse rejects.
    if (args.responseFormat === "json_object") {
      if (!content.trim()) throw new Error(`${cfg.provider} returned empty content`);
      try {
        JSON.parse(content);
      } catch {
        throw new Error(`${cfg.provider} returned unparseable json_object content`);
      }
    }
    return content;
  } finally {
    if (timeout) clearTimeout(timeout);
  }
}
