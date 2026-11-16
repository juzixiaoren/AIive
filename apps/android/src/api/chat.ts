import type { ExecutionStatus } from "../../../../shared/chat/session";
import type { ApprovalResponse } from "../../../../shared/chat/approval";
export type { ExecutionStatus, ApprovalResponse };
import type { Attachment } from "../../../../shared/chat/attachments";
import { workCardsFrom, type WorkCard } from "../../../../shared/work/types";
import { subscribeThread, type Envelope } from "../../../../shared/work/connection";
import { appConfig } from "../config";
import { apiFetch, getToken } from "./auth";
import { readChatEventStream } from "../../../../shared/chat/stream";
import {
  ChatApiError,
  chatApiErrorFromPayload,
  chatApiErrorFromResponse,
} from "../../../../shared/chat/errors";
import type {
  ThreadHistoryMessage,
  ThreadHistoryPage,
} from "../../../../shared/chat/history";

export { ChatApiError };
export type { ThreadHistoryMessage, ThreadHistoryPage };

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  attachments?: Attachment[];
  approvals?: ApprovalItem[];
  workCards?: WorkCard[];
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
  workCards: WorkCard[];
  cancelled: boolean;
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
  if (!response.ok) throw await chatApiErrorFromResponse(response);
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
  if (!response.ok) throw await chatApiErrorFromResponse(response);
  return response.json() as Promise<ThreadHistoryPage>;
}

export async function getExecutionStatus(threadId?: string): Promise<ExecutionStatus> {
  const query = threadId ? `?thread_id=${encodeURIComponent(threadId)}` : "";
  const response = await apiFetch(`conversation/status${query}`);
  if (!response.ok) throw await chatApiErrorFromResponse(response);
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
  if (!response.ok) throw await chatApiErrorFromResponse(response);
  return response.json() as Promise<{ ok: boolean; status: string; turn_record_id: string | null }>;
}

/**
 * 订阅线程 WebSocket：首帧认证后接收服务端广播的执行状态。
 * 返回取消订阅函数；断线每 5 秒重连一次（重连后服务端会补发当前状态）。
 */
export function subscribeThreadEvents(
  threadId: string,
  onStatus: (status: ExecutionStatus, metadata: { initialSnapshot: boolean }) => void,
  onMessage?: (event: Envelope) => void,
): () => void {
  let initial = true;
  return subscribeThread(`${appConfig.backendBaseUrl.replace(/^http/i, "ws")}/ws/${encodeURIComponent(threadId)}`, getToken, event => {
    if (event.type === "execution_status" && event.data) {
      onStatus(event.data as unknown as ExecutionStatus, { initialSnapshot: initial });
      initial = false;
    } else onMessage?.(event);
  }, reason => { if (reason !== "execution_idle") initial = true; });
}

export async function streamMessage(
  message: string,
  threadId: string | undefined,
  callbacks: StreamCallbacks,
  signal: AbortSignal,
  attachmentIds: string[] = [],
  turnId?: string,
): Promise<StreamResult> {
  const response = await apiFetch("chat/stream", {
    method: "POST",
    headers: {
      Accept: "text/event-stream",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ message, thread_id: threadId ?? null, attachment_ids: attachmentIds, turn_id: turnId }),
    signal,
  });
  if (!response.ok) throw await chatApiErrorFromResponse(response);
  if (!response.body) throw new Error("服务器没有返回流式响应");

  let completed = false;
  let cancelled = false;
  let result: StreamResult = { threadId: threadId ?? "", reply: "", approvals: [], workCards: [], cancelled: false };

  await readChatEventStream(response, ({ event, data }) => {
    if (event === "started") {
      const nextThreadId = String(data.thread_id || "");
      const turnRecordId = String(data.turn_record_id || "");
      if (nextThreadId) {
        result.threadId = nextThreadId;
        callbacks.onStarted(nextThreadId, turnRecordId);
      }
    } else if (event === "token") {
      callbacks.onToken(String(data.text || ""));
    } else if (event === "cancelled") {
      // 服务端已完成取消收口：本轮用户消息不再进入历史。
      cancelled = true;
    } else if (event === "done") {
      completed = true;
      const cards = Array.isArray(data.action_cards) ? data.action_cards : [];
      result = {
        threadId: String(data.thread_id || result.threadId),
        reply: String(data.reply || ""),
        approvals: approvalsFromCards(cards),
        workCards: workCardsFrom(cards),
        cancelled: false,
      };
    } else if (event === "error") {
      throw chatApiErrorFromPayload(data);
    }
    // 普通 tool_call / tool_result 暂不展示；审批卡片必须呈现，才能远程放行桌面高危操作。
  });

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
  if (!response.ok) throw await chatApiErrorFromResponse(response);
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
  if (!response.ok) throw await chatApiErrorFromResponse(response);
  return response.json() as Promise<ApprovalResponse>;
}
