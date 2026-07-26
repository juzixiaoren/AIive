/**
 * 聊天 API 接口层
 * - 定义前端与后端聊天接口的数据结构（ActionCard、ChatResponse、StreamEvent）
 * - 提供 sendMessageStream：SSE 流式发送消息，支持逐 token 回调
 * - 提供 resetThread：重置对话线程
 */

/** 操作卡片，用于在聊天界面中展示工具执行结果 */
export interface ActionCard {
  card_type: string;
  title: string;
  summary: string;
  trace_id: string;
  event_ids: string[];
  status: string;
  resource_refs: Record<string, string>;
  payload_preview: Record<string, unknown>;
  reminder_id: string;
  actions: Array<{ action: string; approval_id: string; label: string }>;
}

export interface PendingOperation {
  operation_id: string;
  operation_type: string;
  status: "pending";
  trace_id: string;
  resource_refs: Record<string, string>;
  payload_preview: Record<string, unknown>;
}

/** 聊天响应体，包含模型回复、线程 ID、追踪 ID及结构化运行结果 */
export interface ChatResponse {
  reply: string;
  thread_id: string;
  trace_id: string;
  action_cards: ActionCard[];
  pending_operations: PendingOperation[];
}

export interface ToolExecution {
  tool_call_id: string;
  name: string;
  params: Record<string, unknown>;
  status: string;
  result?: unknown;
}

export interface ThreadHistoryMessage {
  role: "user" | "assistant";
  content: string;
  event_id: string;
  trace_id: string;
  action_cards: ActionCard[];
  pending_operations: PendingOperation[];
  tool_calls: ToolExecution[];
}

export interface ThreadHistoryPage {
  messages: ThreadHistoryMessage[];
  next_cursor: number | null;
  has_more: boolean;
}

export async function getThreadMessages(threadId: string, beforeSequence?: number): Promise<ThreadHistoryPage> {
  const query = new URLSearchParams({ page_size: "50" });
  if (beforeSequence !== undefined) query.set("before_sequence", String(beforeSequence));
  const res = await fetch(`api/threads/${encodeURIComponent(threadId)}/messages?${query.toString()}`);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`历史消息加载失败 ${res.status}: ${text}`);
  }
  return res.json();
}

/** SSE 流式事件类型定义 */
export interface StreamEvent {
  event: "started" | "token" | "tool_call" | "tool_result" | "done" | "error";
  data: Record<string, unknown>;
}

/** 服务端已提交 Turn 后立即发送的流式启动信息。 */
export interface StreamStartedEvent {
  thread_id: string;
  turn_id: string;
  trace_id: string;
}

/** 后端返回的结构化 API 错误 */
export class ApiError extends Error {
  constructor(
    message: string,
    public readonly code: string,
    public readonly status: number,
    public readonly retryable: boolean,
    public readonly traceId?: string,
    public readonly retryAfterSeconds?: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** 流式连接未收到服务端完成事件时抛出，用于触发历史恢复。 */
export class StreamIncompleteError extends Error {
  constructor() {
    super("流式连接在完成前中断，正在从服务端恢复对话状态");
    this.name = "StreamIncompleteError";
  }
}

/**
 * 流式发送消息并接收 SSE 事件回调
 * 返回一个 Promise，在流式传输完成时 resolve
 * @param message - 用户输入的消息文本
 * @param threadId - 可选的对话线程 ID
 * @param onStarted - Turn 持久化后收到真实线程标识时的回调
 * @param onToken - 收到 token 时的回调
 * @param onToolCall - 检测到工具调用时的回调（工具名 + 参数）
 * @param onToolResult - 工具执行完成时的回调（工具名 + 结果 + 状态）
 * @param onError - 发生错误时的回调
 * @param signal - 可选的 AbortSignal，用于中断流式请求
 * @returns 包含 thread_id、trace_id、reply 和 action_cards 的结果对象
 */
export async function sendMessageStream(
  message: string,
  threadId: string | undefined,
  onStarted: (event: StreamStartedEvent) => void,
  onToken: (text: string) => void,
  onToolCall: (toolCallId: string, name: string, params: Record<string, unknown>) => void,
  onToolResult: (toolCallId: string, name: string, result: unknown, status?: string) => void,
  onError: (msg: string) => void,
  signal?: AbortSignal,
): Promise<{
  thread_id: string;
  trace_id: string;
  reply: string;
  action_cards: ActionCard[];
  pending_operations: PendingOperation[];
  tool_calls?: Array<{ tool_call_id: string; name: string; params?: Record<string, unknown>; status?: string }>;
  tool_results?: Array<{ tool_call_id: string; name: string; params?: Record<string, unknown>; result?: unknown; status?: string }>;
}> {
  const res = await fetch("api/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, thread_id: threadId ?? null }),
    signal,
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
  let seenDone = false;
  let result = {
    thread_id: "",
    trace_id: "",
    reply: "",
    action_cards: [] as ActionCard[],
    pending_operations: [] as PendingOperation[],
    tool_calls: [] as Array<{ tool_call_id: string; name: string; params?: Record<string, unknown>; status?: string }>,
    tool_results: [] as Array<{ tool_call_id: string; name: string; params?: Record<string, unknown>; result?: unknown; status?: string }>,
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      // 保留未完成的行到缓冲区，供下次拼接
      buffer = lines.pop() || "";

      for (const line of lines) {
        // 解析 SSE 事件类型行
        if (line.startsWith("event: ")) {
          currentEvent = line.slice(7).trim();
        } else if (line.startsWith("data: ")) {
          // 解析 SSE 数据行
          const dataStr = line.slice(6);
          try {
            const data = JSON.parse(dataStr);
            switch (currentEvent) {
              case "started":
                onStarted({
                  thread_id: data.thread_id as string,
                  turn_id: data.turn_id as string,
                  trace_id: data.trace_id as string,
                });
                break;
              case "token":
                // 逐 token 回调，用于打字机效果
                onToken(data.text as string);
                break;
              case "tool_call":
                // 工具调用开始
                onToolCall(data.tool_call_id as string || "", data.name as string, data.params as Record<string, unknown>);
                break;
              case "tool_result":
                // 工具执行结果（含状态：completed / failed）
                onToolResult(data.tool_call_id as string || "", data.name as string, data.result, data.status as string);
                break;
              case "done":
                // 流式传输完成，收集最终结果
                seenDone = true;
                result = {
                  thread_id: data.thread_id as string,
                  trace_id: data.trace_id as string,
                  reply: data.reply as string,
                  action_cards: (data.action_cards || []) as ActionCard[],
                  pending_operations: (data.pending_operations || []) as PendingOperation[],
                  tool_calls: (data.tool_calls || []) as Array<{ tool_call_id: string; name: string; params?: Record<string, unknown>; status?: string }>,
                  tool_results: (data.tool_results || []) as Array<{ tool_call_id: string; name: string; params?: Record<string, unknown>; result?: unknown; status?: string }>,
                };
                break;
              case "error": {
                const error = new ApiError(
                  (data.message as string) || "聊天请求失败",
                  (data.code as string) || "chat_stream_failed",
                  (data.status as number) || 500,
                  Boolean(data.retryable),
                  data.trace_id as string | undefined,
                  data.retry_after_seconds as number | undefined,
                );
                onError(error.message);
                throw error;
              }
            }
          } catch (error) {
            if (error instanceof ApiError) throw error;
            // 跳过格式错误的 JSON 数据
          }
        }
      }
    }
  } catch (err) {
    // 用户主动中断（AbortSignal）时正常结束，不抛出错误
    if (signal?.aborted || (err instanceof Error && err.name === "AbortError")) {
      // 主动中断，忽略
    } else {
      throw err;
    }
  } finally {
    reader.releaseLock();
  }

  if (!signal?.aborted && !seenDone) {
    throw new StreamIncompleteError();
  }

  return result;
}

/**
 * 重置对话线程上下文
 * @param threadId - 可选的对话线程 ID
 */
export async function resetThread(threadId?: string): Promise<void> {
  await fetch("api/thread/reset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ thread_id: threadId ?? null }),
  });
}

/**
 * 审批响应接口，用于确认或拒绝需要审批的工具调用
 */
export interface ApprovalResponse {
  ok: boolean;
  action: "succeeded" | "denied" | "failed" | "interrupted_unknown";
  approval_id: string;
  tool_name?: string;
  tool_result?: unknown;
  trace_id?: string;
  error?: string;
  idempotent_replay: boolean;
}

/**
 * 响应工具审批：只提交审批 ID 和用户决策，工具调用内容由服务端读取
 * @param approvalId - 审批 ID
 * @param action - "approve" 或 "deny"
 */
export async function respondApproval(
  approvalId: string,
  action: "approve" | "deny",
): Promise<ApprovalResponse> {
  const res = await fetch("api/approval/respond", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approval_id: approvalId, action }),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Approval API error ${res.status}: ${text}`);
  }
  return res.json();
}
