import type { Attachment } from "../../../../shared/chat/attachments";
import type { ActionCard } from "../../api/chat";

/** 单条工具调用的展示模型，状态随流推进并保留无法确认的真实终态 */
export interface ToolCallItem {
  id: string;
  name: string;
  params: Record<string, unknown>;
  status: "pending" | "completed" | "failed" | "execution_unknown" | "cancelled";
  result?: string;
}

/** 消息数据结构 */
export interface Message {
  attachments?: Attachment[];
  /** 稳定唯一标识：后端历史取 event_id，本地消息取 crypto.randomUUID */
  id: string;
  role: "user" | "agent" | "action";
  content: string;
  traceId: string;
  threadId: string;
  actionCards?: ActionCard[];
  /** 结构化工具调用列表，替代原先扁平的 action 文本气泡 */
  toolCalls?: ToolCallItem[];
}
