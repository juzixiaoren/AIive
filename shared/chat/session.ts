/** 服务端线程执行状态；Web 与 Android 共用。 */
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
