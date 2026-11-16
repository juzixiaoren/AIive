import { apiFetch } from "./client";
import { readChatEventStream } from "../../../shared/chat/stream";
import {
  ChatApiError,
  chatApiErrorFromPayload,
  chatApiErrorFromResponse,
} from "../../../shared/chat/errors";
import type {
  ActionCardDTO,
  PendingOperationDTO,
  ThreadHistoryMessage,
  ThreadHistoryPage,
  ToolExecutionDTO,
} from "../../../shared/chat/history";

/**
 * 聊天 API 接口层
 * - 服务端数据结构统一来自 shared/chat/history（Web 与 Android 同一份传输契约）
 * - 提供 sendMessageStream：SSE 流式发送消息，支持逐 token 回调
 * - 提供 resetThread：重置对话线程
 */
export { ChatApiError as ApiError };
export type {
  ThreadHistoryMessage,
  ThreadHistoryPage,
  ToolExecutionDTO,
};
export type { ActionCardDTO as ActionCard, PendingOperationDTO as PendingOperation };

export async function getThreadMessages(threadId: string, beforeSequence?: number): Promise<ThreadHistoryPage> {
  const query = new URLSearchParams({ page_size: "50" });
  if (beforeSequence !== undefined) query.set("before_sequence", String(beforeSequence));
  const res = await apiFetch(`api/threads/${encodeURIComponent(threadId)}/messages?${query.toString()}`);
  if (!res.ok) throw await chatApiErrorFromResponse(res);
  return res.json();
}

/** SSE 流式事件类型定义 */
export interface StreamEvent {
  event: "started" | "token" | "tool_call" | "tool_result" | "done" | "cancelled" | "error";
  data: Record<string, unknown>;
}

/** 服务端已提交 Turn 后立即发送的流式启动信息。 */
export interface StreamStartedEvent {
  thread_id: string;
  turn_id: string;
  /** 服务端 TurnRecord 主键：中断接口必须携带，防止旧停止请求误伤新 Turn。 */
  turn_record_id: string;
  trace_id: string;
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
  attachmentIds: string[] = [],
  turnId?: string,
): Promise<{
  thread_id: string;
  trace_id: string;
  reply: string;
  action_cards: ActionCardDTO[];
  pending_operations: PendingOperationDTO[];
  tool_calls?: ToolExecutionDTO[];
  tool_results?: ToolExecutionDTO[];
  cancelled?: boolean;
}> {
  const res = await apiFetch("api/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, thread_id: threadId ?? null, attachment_ids: attachmentIds, turn_id: turnId }),
    signal,
  });

  if (!res.ok) throw await chatApiErrorFromResponse(res);

  let seenDone = false;
  let seenCancelled = false;
  let result = {
    thread_id: "",
    trace_id: "",
    reply: "",
    action_cards: [] as ActionCardDTO[],
    pending_operations: [] as PendingOperationDTO[],
    tool_calls: [] as ToolExecutionDTO[],
    tool_results: [] as ToolExecutionDTO[],
  };

  try {
    await readChatEventStream(res, ({ event, data }) => {
      // JSON 解析失败已在共享解析器中静默跳过；回调内异常必须记录，
      // 否则事件（如 tool_result）会无声丢失。
      try {
        switch (event) {
          case "started":
            onStarted({
              thread_id: data.thread_id as string,
              turn_id: data.turn_id as string,
              turn_record_id: (data.turn_record_id as string) || "",
              trace_id: (data.trace_id as string) || "",
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
          case "cancelled":
            // 服务端已完成取消收口：本轮用户消息不再进入历史。
            seenCancelled = true;
            break;
          case "done":
            // 流式传输完成，收集最终结果
            seenDone = true;
            result = {
              thread_id: data.thread_id as string,
              trace_id: data.trace_id as string,
              reply: data.reply as string,
              action_cards: (data.action_cards || []) as ActionCardDTO[],
              pending_operations: (data.pending_operations || []) as PendingOperationDTO[],
              tool_calls: (data.tool_calls || []) as ToolExecutionDTO[],
              tool_results: (data.tool_results || []) as ToolExecutionDTO[],
            };
            break;
          case "error": {
            const error = chatApiErrorFromPayload(data);
            onError(error.message);
            throw error;
          }
        }
      } catch (error) {
        if (error instanceof ChatApiError) throw error;
        console.error("SSE 事件回调处理失败:", event, error);
      }
    });
  } catch (err) {
    // 用户主动中断（AbortSignal）时正常结束，不抛出错误
    if (signal?.aborted || (err instanceof Error && err.name === "AbortError")) {
      // 主动中断，忽略
    } else {
      throw err;
    }
  }

  if (seenCancelled) {
    return { ...result, cancelled: true };
  }
  if (!signal?.aborted && !seenDone) {
    throw new StreamIncompleteError();
  }

  return result;
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

/** 获取或初始化唯一长期对话的 Thread ID（后端是唯一事实来源）。 */
export async function getCurrentConversation(): Promise<{ thread_id: string }> {
  const res = await apiFetch("api/conversation/current");
  if (!res.ok) throw await chatApiErrorFromResponse(res);
  return res.json();
}

/** 查询当前执行状态：idle / running（可中断）/ busy（后台占用）。 */
export async function getExecutionStatus(threadId?: string): Promise<ExecutionStatus> {
  const query = threadId ? `?thread_id=${encodeURIComponent(threadId)}` : "";
  const res = await apiFetch(`api/conversation/status${query}`);
  if (!res.ok) throw await chatApiErrorFromResponse(res);
  return res.json();
}

/** 请求服务端中断前台 Turn；只登记取消信号，锁在执行者收口后释放。 */
export async function interruptTurn(
  threadId: string,
  turnRecordId?: string,
): Promise<{ ok: boolean; status: string; turn_record_id: string | null }> {
  const res = await apiFetch("api/conversation/interrupt", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      thread_id: threadId,
      turn_record_id: turnRecordId ?? null,
    }),
  });
  if (!res.ok) throw await chatApiErrorFromResponse(res);
  return res.json();
}

/**
 * 重置对话线程上下文
 * @param threadId - 可选的对话线程 ID
 */
export async function resetThread(threadId?: string): Promise<void> {
  const res = await apiFetch("api/thread/reset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ thread_id: threadId ?? null }),
  });
  if (!res.ok) throw await chatApiErrorFromResponse(res);
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
  const res = await apiFetch("api/approval/respond", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approval_id: approvalId, action }),
  });
  if (!res.ok) throw await chatApiErrorFromResponse(res);
  return res.json();
}
