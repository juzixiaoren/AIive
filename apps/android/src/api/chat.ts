import { appConfig } from "../config";
import { apiFetch, getToken } from "./auth";

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
  onStarted: (threadId: string, turnRecordId: string) => void;
  onToken: (text: string) => void;
}

interface StreamResult {
  threadId: string;
  reply: string;
  approvals: ApprovalItem[];
  cancelled: boolean;
}

/** 结构化聊天错误：保留服务端 code / status / retryable，供 UI 精确回滚。 */
export class ChatApiError extends Error {
  constructor(
    message: string,
    public readonly code: string,
    public readonly status: number,
    public readonly retryable: boolean,
    public readonly traceId?: string,
  ) {
    super(message);
    this.name = "ChatApiError";
  }
}

export interface ApprovalResponse {
  ok: boolean;
  action: "succeeded" | "denied" | "failed" | "interrupted_unknown";
  approval_id: string;
  tool_name?: string;
  tool_result?: unknown;
  error?: string;
}

/** 服务端唯一长期对话的执行状态（由线程级租约派生）。 */
export interface ExecutionStatus {
  thread_id: string;
  state: "idle" | "running" | "busy";
  cancellable: boolean;
  interrupting: boolean;
  owner_kind: string | null;
  active_turn: {
    turn_record_id: string | null;
    turn_id: string | null;
    started_at: string | null;
    cancel_requested_at: string | null;
  } | null;
}

export interface ThreadHistoryMessage {
  role: "user" | "assistant";
  content: string;
  event_id: string;
  trace_id?: string;
  action_cards?: unknown[];
}

export interface ThreadHistoryPage {
  messages: ThreadHistoryMessage[];
  next_cursor: number | null;
  has_more: boolean;
}

async function responseError(response: Response): Promise<Error> {
  const detail = await response.text();
  return new Error(`服务器请求失败 (${response.status})${detail ? `：${detail}` : ""}`);
}

/** 把非 2xx 响应解析为结构化错误（detail 为 FastAPI 包装层）。 */
async function chatResponseError(response: Response): Promise<ChatApiError> {
  const text = await response.text();
  try {
    const body = JSON.parse(text) as { detail?: Record<string, unknown> };
    const detail = (body.detail ?? body) as Record<string, unknown>;
    return new ChatApiError(
      String(detail.message || detail.code || "对话请求失败"),
      String(detail.code || "chat_stream_failed"),
      Number(detail.status || response.status),
      Boolean(detail.retryable),
      typeof detail.trace_id === "string" ? detail.trace_id : undefined,
    );
  } catch {
    return new ChatApiError(
      `服务器请求失败 (${response.status})${text ? `：${text}` : ""}`,
      "chat_stream_failed",
      response.status,
      false,
    );
  }
}

export function hasBackendConfig(): boolean {
  return Boolean(appConfig.backendBaseUrl);
}

/** 从操作卡片中提取待审批项（历史恢复与流式完成共用同一映射）。 */
export function approvalsFromCards(cards: unknown[]): ApprovalItem[] {
  return cards.flatMap((candidate) => {
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
}

/** 后端是 Thread ID 唯一事实来源：启动时获取（服务端负责初始化）。 */
export async function getCurrentConversation(): Promise<string> {
  const response = await apiFetch("conversation/current");
  if (!response.ok) throw await responseError(response);
  const payload = (await response.json()) as { thread_id?: string };
  return String(payload.thread_id || "");
}

export async function getThreadMessages(
  threadId: string,
  beforeSequence?: number,
): Promise<ThreadHistoryPage> {
  const query = new URLSearchParams({ page_size: "50" });
  if (beforeSequence !== undefined) query.set("before_sequence", String(beforeSequence));
  const response = await apiFetch(
    `threads/${encodeURIComponent(threadId)}/messages?${query.toString()}`,
  );
  if (!response.ok) throw await responseError(response);
  return response.json() as Promise<ThreadHistoryPage>;
}

export async function getExecutionStatus(threadId?: string): Promise<ExecutionStatus> {
  const query = threadId ? `?thread_id=${encodeURIComponent(threadId)}` : "";
  const response = await apiFetch(`conversation/status${query}`);
  if (!response.ok) throw await responseError(response);
  return response.json() as Promise<ExecutionStatus>;
}

/** 请求服务端中断前台 Turn（只登记取消信号，收口由执行者完成）。 */
export async function interruptTurn(
  threadId: string,
  turnRecordId?: string,
): Promise<{ ok: boolean; status: string; turn_record_id: string | null }> {
  const response = await apiFetch("conversation/interrupt", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ thread_id: threadId, turn_record_id: turnRecordId ?? null }),
  });
  if (!response.ok) throw await responseError(response);
  return response.json() as Promise<{ ok: boolean; status: string; turn_record_id: string | null }>;
}

/**
 * 订阅线程 WebSocket：首帧认证后接收服务端广播的执行状态。
 * 返回取消订阅函数；断线每 5 秒重连一次（重连后服务端会补发当前状态）。
 */
export function subscribeThreadEvents(
  threadId: string,
  onStatus: (status: ExecutionStatus) => void,
): () => void {
  let socket: WebSocket | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let disposed = false;

  const connect = () => {
    if (disposed) return;
    const base = appConfig.backendBaseUrl.replace(/^http/i, "ws");
    socket = new WebSocket(`${base}/ws/${encodeURIComponent(threadId)}`);
    socket.onopen = () => {
      socket?.send(JSON.stringify({ type: "auth", access_token: getToken() ?? "" }));
    };
    socket.onmessage = (event) => {
      try {
        const payload = JSON.parse(String(event.data)) as {
          type?: string;
          data?: Record<string, unknown>;
        };
        if (payload.type === "execution_status" && payload.data) {
          onStatus(payload.data as unknown as ExecutionStatus);
        }
      } catch {
        // 非 JSON 帧忽略，不打断连接。
      }
    };
    socket.onclose = () => {
      if (disposed) return;
      reconnectTimer = setTimeout(connect, 5000);
    };
    socket.onerror = () => socket?.close();
  };

  connect();
  return () => {
    disposed = true;
    if (reconnectTimer) clearTimeout(reconnectTimer);
    socket?.close();
  };
}

export async function streamMessage(
  message: string,
  threadId: string | undefined,
  callbacks: StreamCallbacks,
  signal: AbortSignal,
): Promise<StreamResult> {
  const response = await apiFetch("chat/stream", {
    method: "POST",
    headers: {
      Accept: "text/event-stream",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ message, thread_id: threadId ?? null }),
    signal,
  });
  if (!response.ok) throw await chatResponseError(response);
  if (!response.body) throw new Error("服务器没有返回流式响应");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let activeEvent = "";
  let completed = false;
  let cancelled = false;
  let result: StreamResult = { threadId: threadId ?? "", reply: "", approvals: [], cancelled: false };

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
      const turnRecordId = String(data.turn_record_id || "");
      if (nextThreadId) {
        result.threadId = nextThreadId;
        callbacks.onStarted(nextThreadId, turnRecordId);
      }
    } else if (activeEvent === "token") {
      callbacks.onToken(String(data.text || ""));
    } else if (activeEvent === "cancelled") {
      // 服务端已完成取消收口：本轮用户消息不再进入历史。
      cancelled = true;
    } else if (activeEvent === "done") {
      completed = true;
      const cards = Array.isArray(data.action_cards) ? data.action_cards : [];
      result = {
        threadId: String(data.thread_id || result.threadId),
        reply: String(data.reply || ""),
        approvals: approvalsFromCards(cards),
        cancelled: false,
      };
    } else if (activeEvent === "error") {
      throw new ChatApiError(
        String(data.message || data.code || "对话请求失败"),
        String(data.code || "chat_stream_failed"),
        Number(data.status || 500),
        Boolean(data.retryable),
        typeof data.trace_id === "string" ? data.trace_id : undefined,
      );
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

  if (cancelled) {
    return { ...result, cancelled: true };
  }
  if (!signal.aborted && !completed) {
    throw new Error("连接在回复完成前中断，请重试");
  }
  return result;
}

export async function resetConversation(threadId?: string): Promise<void> {
  if (!threadId || !hasBackendConfig()) return;
  const response = await apiFetch("thread/reset", {
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
  const response = await apiFetch("approval/respond", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approval_id: approvalId, action }),
  });
  if (!response.ok) throw await responseError(response);
  return response.json() as Promise<ApprovalResponse>;
}
