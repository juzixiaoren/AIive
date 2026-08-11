import { appConfig } from "../config";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  approvals?: ApprovalItem[];
}

export interface ApprovalItem {
  approvalId: string;
  toolCallId: string;
  toolName: string;
  params: Record<string, unknown>;
  status: "pending" | "executing" | "succeeded" | "denied" | "failed" | "interrupted_unknown";
  result?: unknown;
  error?: string;
}

export interface StreamCallbacks {
  onStarted: (threadId: string) => void;
  onToken: (text: string) => void;
}

interface StreamResult {
  threadId: string;
  reply: string;
  approvals: ApprovalItem[];
}

export interface ApprovalResponse {
  ok: boolean;
  action: "succeeded" | "denied" | "failed" | "interrupted_unknown";
  approval_id: string;
  tool_name?: string;
  tool_result?: unknown;
  error?: string;
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
  let result: StreamResult = { threadId: threadId ?? "", reply: "", approvals: [] };

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
      const cards = Array.isArray(data.action_cards) ? data.action_cards : [];
      const approvals: ApprovalItem[] = cards.flatMap((candidate) => {
        if (!candidate || typeof candidate !== "object") return [];
        const card = candidate as Record<string, unknown>;
        if (card.card_type !== "approval_required") return [];
        const refs = card.resource_refs && typeof card.resource_refs === "object"
          ? card.resource_refs as Record<string, unknown>
          : {};
        const preview = card.payload_preview && typeof card.payload_preview === "object"
          ? card.payload_preview as Record<string, unknown>
          : {};
        const approvalId = String(refs.approval_id || preview.approval_id || "");
        if (!approvalId) return [];
        return [{
          approvalId,
          toolCallId: String(refs.tool_call_id || preview.tool_call_id || ""),
          toolName: String(preview.tool_name || "未知工具"),
          params: preview.tool_params && typeof preview.tool_params === "object"
            ? preview.tool_params as Record<string, unknown>
            : {},
          status: "pending" as const,
        }];
      });
      result = {
        threadId: String(data.thread_id || result.threadId),
        reply: String(data.reply || ""),
        approvals,
      };
    } else if (activeEvent === "error") {
      throw new Error(String(data.message || "对话请求失败"));
    }
    // 普通 tool_call / tool_result 暂不展示；审批卡片必须呈现，才能远程放行桌面高危操作。
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

export async function respondApproval(
  approvalId: string,
  action: "approve" | "deny",
): Promise<ApprovalResponse> {
  const response = await fetch(apiUrl("approval/respond"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approval_id: approvalId, action }),
  });
  if (!response.ok) throw await responseError(response);
  return response.json() as Promise<ApprovalResponse>;
}
