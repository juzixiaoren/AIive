/** 普通 Turn 与 Persistent Task 审批响应。 */
export interface ApprovalResponse {
  ok: boolean;
  action: "succeeded" | "denied" | "failed" | "interrupted_unknown";
  approval_id: string;
  task_id?: string | null;
  action_id?: string | null;
  tool_name?: string;
  tool_result?: unknown;
  trace_id?: string | null;
  error?: string | null;
  idempotent_replay: boolean;
}
