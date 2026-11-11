import type { ToolCallItem } from "./types";

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
