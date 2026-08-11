import { appConfig } from "../config";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
}

export interface StreamCallbacks {
  onStarted: (threadId: string) => void;
  onToken: (text: string) => void;
}

interface StreamResult {
  threadId: string;
  reply: string;
}

function apiUrl(path: string): string {
  if (!appConfig.backendBaseUrl) {
    throw new Error("尚未配置服务器地址，请先填写 apps/android/src/config.ts");
  }
  return `${appConfig.backendBaseUrl}/api/${path.replace(/^\/+/, "")}`;
}

async function responseError(response: Response): Promise<Error> {
  const detail = await response.text();
  return new Error(`服务器请求失败 (${response.status})${detail ? `：${detail}` : ""}`);
}

export function hasBackendConfig(): boolean {
  return Boolean(appConfig.backendBaseUrl);
}

export async function streamMessage(
  message: string,
  threadId: string | undefined,
  callbacks: StreamCallbacks,
  signal: AbortSignal,
): Promise<StreamResult> {
  const response = await fetch(apiUrl("chat/stream"), {
    method: "POST",
    headers: {
      Accept: "text/event-stream",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ message, thread_id: threadId ?? null }),
    signal,
  });
  if (!response.ok) throw await responseError(response);
  if (!response.body) throw new Error("服务器没有返回流式响应");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let activeEvent = "";
  let completed = false;
  let result: StreamResult = { threadId: threadId ?? "", reply: "" };

  const handleLine = (line: string) => {
    if (line.startsWith("event:")) {
      activeEvent = line.slice(6).trim();
      return;
    }
    if (!line.startsWith("data:")) return;

    let data: Record<string, unknown>;
    try {
      data = JSON.parse(line.slice(5).trim()) as Record<string, unknown>;
    } catch {
      return;
    }

    if (activeEvent === "started") {
      const nextThreadId = String(data.thread_id || "");
      if (nextThreadId) {
        result.threadId = nextThreadId;
        callbacks.onStarted(nextThreadId);
      }
    } else if (activeEvent === "token") {
      callbacks.onToken(String(data.text || ""));
    } else if (activeEvent === "done") {
      completed = true;
      result = {
        threadId: String(data.thread_id || result.threadId),
        reply: String(data.reply || ""),
      };
    } else if (activeEvent === "error") {
      throw new Error(String(data.message || "对话请求失败"));
    }
    // tool_call / tool_result / trace_id 有意忽略：Android 版只呈现核心对话。
  };

  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true }).replace(/\r\n/g, "\n");
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      lines.forEach(handleLine);
    }
    if (buffer) handleLine(buffer);
  } finally {
    reader.releaseLock();
  }

  if (!signal.aborted && !completed) {
    throw new Error("连接在回复完成前中断，请重试");
  }
  return result;
}

export async function resetConversation(threadId?: string): Promise<void> {
  if (!threadId || !hasBackendConfig()) return;
  const response = await fetch(apiUrl("thread/reset"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ thread_id: threadId }),
  });
  if (!response.ok) throw await responseError(response);
}
