/** Public WorkReadModel only. Execution arguments and TaskState never belong here. */
export type Bucket = "attention" | "active" | "waiting" | "ended";
export const buckets: Record<Bucket, string> = {
  attention: "待你处理",
  active: "进行中",
  waiting: "等待中",
  ended: "已结束",
};
export interface Progress {
  total: number;
  done: number;
  failed: number;
  pending: number;
  waiting: number;
}
export interface Device {
  node_id: string | null;
  name: string | null;
  platform: string | null;
  online: boolean;
  relationship: "current" | "last_used" | "configured" | "unknown";
}
export interface Attention {
  id: string;
  kind: string;
  title: string;
  description: string;
  resource_id: string;
  created_at: string;
  expires_at: string | null;
  allowed_actions: string[];
  question_id?: string | null;
  step_id?: string | null;
  approval_id?: string | null;
  preview?: Record<string, unknown> | null;
}
export interface WorkSummary {
  task_id: string;
  title: string;
  task_type: string;
  task_status: string;
  bucket: Bucket;
  bucket_reason: string;
  attention_count: number;
  primary_attention: Attention | null;
  progress: Progress | null;
  target_device: Device;
  updated_at: string;
  created_at: string;
}
export interface TimelineEvent {
  id: string;
  sequence: number;
  title: string;
  summary: string;
  event_type: string;
  created_at: string;
}
export interface FileMetadata {
  id: string;
  name?: string;
  artifact_type?: string;
  kind?: string;
  summary: string;
  mime_type: string;
  content_size: number;
  content_hash: string;
  created_at: string;
}
export interface GoalStep {
  id: string;
  title: string;
  kind: string;
  status: string;
  depends_on: string[];
  acceptance: string[];
  requires_evidence: boolean;
  note: string;
  schedule?: Record<string, unknown> | null;
  trigger?: Record<string, unknown> | null;
  next_trigger_at?: string | null;
}
export interface Goal {
  summary: string;
  status: string;
  revision: number;
  deadline: string | null;
  timezone: string;
  acceptance: string[];
  steps: GoalStep[];
  progress: Progress;
  blocked_reason: string;
}
export interface WorkView {
  task: Pick<
    WorkSummary,
    | "task_id"
    | "title"
    | "task_type"
    | "task_status"
    | "created_at"
    | "updated_at"
  > & {
    goal: string;
    report: { summary: string } | null;
    last_error: string | null;
  };
  bucket: Bucket;
  data_warning: string | null;
  goal: Goal | null;
  attention_items: Attention[];
  target_device: Device;
  current_activity: {
    action_id: string;
    capability_id: string;
    status: string;
  } | null;
  actions: Array<{
    id: string;
    sequence: number;
    capability_id: string;
    status: string;
    risk_level: string;
    requires_approval: boolean;
    created_at: string;
    started_at: string | null;
    completed_at: string | null;
  }>;
  timeline: TimelineEvent[];
  last_sequence: number;
  artifacts: FileMetadata[];
  evidence: FileMetadata[];
  truncated: Record<string, boolean>;
}
export interface Inbox {
  items: WorkSummary[];
  next_cursor: string | null;
  has_more: boolean;
}
export interface Timeline {
  items: TimelineEvent[];
  next_sequence: number;
  has_more: boolean;
}
export type GoalControl =
  | "confirm"
  | "modify"
  | "reject"
  | "pause"
  | "resume"
  | "end";
export type QuestionIntent = "answer" | "modify_plan" | "reject";
export type Fetcher = (path: string, init?: RequestInit) => Promise<Response>;
export interface WorkCard {
  taskId: string;
  title: string;
  summary: string;
  status: string;
}

/** Same mapping for stream completion and persisted history; one card per task. */
export function workCardsFrom(cards: unknown[]): WorkCard[] {
  const result = new Map<string, WorkCard>();
  for (const candidate of cards) {
    if (!candidate || typeof candidate !== "object") continue;
    const c = candidate as Record<string, unknown>;
    if (c.card_type !== "agent_task_created") continue;
    const refs = c.resource_refs as Record<string, unknown> | undefined;
    if (typeof refs?.task_id !== "string" || !refs.task_id) continue;
    result.set(refs.task_id, {
      taskId: refs.task_id,
      title: String(c.title || "工作已创建"),
      summary: String(c.summary || ""),
      status: String(
        (c.payload_preview as Record<string, unknown>)?.status || "",
      ),
    });
  }
  return [...result.values()];
}

const statuses: Record<string, string> = {
  queued: "排队中",
  dispatching: "派发中",
  running: "执行中",
  verifying: "核验中",
  blocked_node: "等待设备",
  blocked_user: "等待输入",
  blocked_approval: "等待审批",
  reconciling: "等待 Journal 对账",
  succeeded: "已完成",
  partial: "部分完成",
  failed: "失败",
  cancelled: "已取消",
  unknown: "结果未知，等待 Journal 对账",
  proposed: "待确认",
  active: "进行中",
  paused: "已暂停",
  blocked: "受阻",
  completed: "已完成",
  rejected: "已拒绝",
  abandoned: "已结束",
  pending: "待执行",
  ready: "已就绪",
  waiting_user: "等待你处理",
  waiting_trigger: "等待触发",
  done: "已完成",
  skipped: "已跳过",
  planned: "已规划",
  validated: "已校验",
  awaiting_approval: "待审批",
  dispatched: "已派发",
  invalidated: "已失效",
};
export function statusLabel(value: string): string {
  return statuses[value] || value;
}
export function deviceLabel(device: Device): string {
  if (!device.node_id) return "未指定桌面设备";
  const relation = {
    current: "当前执行设备",
    last_used: "上次使用设备",
    configured: "指定设备",
    unknown: "设备",
  }[device.relationship];
  return `${relation}：${device.name || device.node_id} · ${device.platform || "未知平台"} · ${device.online ? "在线" : "离线"}`;
}
export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "请求失败，请重试";
}
