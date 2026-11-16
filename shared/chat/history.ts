/**
 * 聊天历史传输 DTO：以真实 GET /api/threads/{thread_id}/messages 返回为准。
 *
 * 平台 UI 模型（Web Message / Android ChatMessage）仍由各自页面映射；
 * 这里只描述服务端契约，字段按真实可用性标注必填/可选。
 */
import type { Attachment } from "./attachments";

/** 运行时操作卡片（后端 ActionCard）。 */
export interface ActionCardDTO {
  card_type: string;
  title: string;
  summary?: string;
  trace_id?: string;
  event_ids?: string[];
  resource_refs?: Record<string, string>;
  status?: string;
  payload_preview?: Record<string, unknown>;
  reminder_id?: string;
  actions?: Array<{ action: string; approval_id?: string; label?: string }>;
}

/** 待处理操作（审批或副作用 operation 的读模型投影）。 */
export interface PendingOperationDTO {
  operation_id: string;
  operation_type: string;
  status: "pending";
  trace_id?: string;
  resource_refs?: Record<string, string>;
  payload_preview?: Record<string, unknown>;
}

/** 历史消息中的工具调用：status 为服务端确认过的真实终态。 */
export interface ToolExecutionDTO {
  tool_call_id: string;
  name: string;
  params?: Record<string, unknown>;
  status?: string;
  result?: unknown;
}

/** 单个 Turn 聚合后的展示消息；旧版事件可能缺少可选字段。 */
export interface ThreadHistoryMessage {
  role: "user" | "assistant";
  content: string;
  event_id?: string;
  trace_id?: string;
  action_cards?: ActionCardDTO[];
  pending_operations?: PendingOperationDTO[];
  tool_calls?: ToolExecutionDTO[];
  attachments?: Attachment[];
}

/** keyset 分页：next_cursor 为上一页首条 Turn 序号，无更多时为 null。 */
export interface ThreadHistoryPage {
  messages: ThreadHistoryMessage[];
  next_cursor: number | null;
  has_more: boolean;
}
