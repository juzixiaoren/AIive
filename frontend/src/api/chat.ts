export interface ActionCard {
  card_type: string;
  title: string;
  summary: string;
  trace_id: string;
  status: string;
  resource_refs: Record<string, string>;
  payload_preview: Record<string, unknown>;
}

export interface ChatResponse {
  reply: string;
  thread_id: string;
  trace_id: string;
  action_cards?: ActionCard[];
  intent_type?: string;
}

export interface StreamEvent {
  event: "token" | "tool_call" | "tool_result" | "action_card" | "done" | "error";
  data: Record<string, unknown>;
}

export async function sendMessage(
  message: string,
  threadId?: string
): Promise<ChatResponse> {
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, thread_id: threadId ?? null }),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Chat API error ${res.status}: ${text}`);
  }
  return res.json();
}

/**
 * Stream a message and receive SSE events via callback.
 * Returns a Promise that resolves when streaming is complete.
 */
export async function sendMessageStream(
  message: string,
  threadId: string | undefined,
  onToken: (text: string) => void,
  onToolCall: (name: string, params: Record<string, unknown>) => void,
  onToolResult: (name: string, result: unknown) => void,
  onActionCard: (card: ActionCard) => void,
  onError: (msg: string) => void,
): Promise<{ thread_id: string; trace_id: string; reply: string; action_cards: ActionCard[] }> {
  const res = await fetch("/api/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, thread_id: threadId ?? null }),
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Stream API error ${res.status}: ${text}`);
  }

  const reader = res.body?.getReader();
  if (!reader) throw new Error("No response body");

  const decoder = new TextDecoder();
  let buffer = "";
  let currentEvent = "";
  let result = { thread_id: "", trace_id: "", reply: "", action_cards: [] as ActionCard[] };

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";

      for (const line of lines) {
        if (line.startsWith("event: ")) {
          currentEvent = line.slice(7).trim();
        } else if (line.startsWith("data: ")) {
          const dataStr = line.slice(6);
          try {
            const data = JSON.parse(dataStr);
            switch (currentEvent) {
              case "token":
                onToken(data.text as string);
                break;
              case "tool_call":
                onToolCall(data.name as string, data.params as Record<string, unknown>);
                break;
              case "tool_result":
                onToolResult(data.name as string, data.result);
                break;
              case "action_card":
                onActionCard(data as unknown as ActionCard);
                break;
              case "done":
                result = {
                  thread_id: data.thread_id as string,
                  trace_id: data.trace_id as string,
                  reply: data.reply as string,
                  action_cards: (data.action_cards || []) as ActionCard[],
                };
                break;
              case "error":
                onError(data.message as string || "Unknown error");
                break;
            }
          } catch {
            // skip malformed JSON
          }
        }
      }
    }
  } finally {
    reader.releaseLock();
  }

  return result;
}

export async function resetThread(threadId?: string): Promise<void> {
  await fetch("/api/thread/reset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ thread_id: threadId ?? null }),
  });
}
