import type { ThreadHistoryMessage, ThreadHistoryPage } from "../../../../shared/chat/history";
import type { Message, ToolCallItem } from "./types";

export function historyMessage(message: ThreadHistoryMessage, threadId: string): Message {
  return {
    id: message.event_id || genMessageId(),
    role: message.role === "user" ? "user" : "agent",
    content: message.content,
    attachments: message.attachments,
    traceId: message.trace_id || "",
    threadId,
    actionCards: message.action_cards || [],
    toolCalls: (message.tool_calls ?? []).map(call => ({
      id: call.tool_call_id,
      name: call.name,
      params: call.params || {},
      status: normalizeToolCallStatus(call.status),
      result: call.result == null ? undefined
        : typeof call.result === "string" ? call.result : JSON.stringify(call.result),
    })),
  };
}

/** undefined refreshes the latest page. null means the oldest page was already expanded. */
export async function readHistoryWindow(
  fetchPage: (before?: number, from?: number) => Promise<ThreadHistoryPage>,
  expandedCursor?: number | null,
): Promise<{ messages: ThreadHistoryMessage[]; cursor: number | null; revision?: number }> {
  const from = expandedCursor ?? undefined;
  for (let attempt = 0; attempt < 2; attempt++) {
    const first = await fetchPage(undefined, from);
    const revision = first.visibility_revision;
    if (expandedCursor === undefined) return { messages: first.messages, cursor: first.next_cursor, revision };

    const pages = [first.messages];
    let cursor = first.next_cursor;
    let sameRevision = true;
    while (cursor !== null) {
      const page = await fetchPage(cursor, from);
      if (page.visibility_revision !== revision) {
        sameRevision = false;
        break;
      }
      if (page.next_cursor !== null && page.next_cursor >= cursor) {
        throw new Error("历史消息分页无法继续");
      }
      pages.push(page.messages);
      cursor = page.next_cursor;
    }
    if (sameRevision) return { messages: pages.reverse().flat(), cursor: expandedCursor, revision };
  }
  throw new Error("历史消息正在更新，请重试");
}

/** 将后端及历史兼容状态收敛为 UI 状态；未识别状态不得声明完成。 */
export function normalizeToolCallStatus(status?: string): ToolCallItem["status"] {
  switch (status) {
    case "completed":
    case "committed":
    case "succeeded":
      return "completed";
    case "failed":
    case "error":
    case "execution_failed":
      return "failed";
    case "pending":
    case "pending_approval":
      return "pending";
    case "cancelled":
    case "preempted":
      return "cancelled";
    case "queued":
    case "running":
    case "execution_unknown":
    case "interrupted_unknown":
    case "unknown":
    default:
      return "execution_unknown";
  }
}

/** 生成消息唯一标识，作为稳定的 React 列表 key，避免头部插入更早消息时整体重挂载 */
export function genMessageId(): string {
  try {
    return crypto.randomUUID();
  } catch {
    return `m-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  }
}
