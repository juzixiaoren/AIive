import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AgentTaskDetail,
  AgentTaskGoal,
  AgentTaskSummary,
  approveAgentTaskGoalStep,
  cancelAgentTask,
  controlAgentTaskGoal,
  createAgentTask,
  decideTaskApproval,
  getAgentTask,
  getAgentTaskGoal,
  listAgentTasks,
  sendAgentTaskInput,
  startAgentTaskGoalStep,
} from "../api/agentTasks";
import { useTaskEvents } from "../hooks/useTaskEvents";
import { BASE_PATH } from "../lib/base";


const STATUS_LABELS: Record<string, string> = {
  queued: "排队", dispatching: "调度中", running: "执行中",
  blocked_approval: "等待审批", blocked_user: "等待输入", blocked_node: "节点离线",
  reconciling: "对账中", verifying: "验证中", succeeded: "已完成",
  partial: "部分完成", failed: "失败", cancelled: "已取消",
};


const GOAL_STATUS_LABELS: Record<string, string> = {
  proposed: "待确认", active: "进行中", paused: "已暂停", blocked: "受阻",
  completed: "已完成", rejected: "已拒绝", abandoned: "已结束",
};

const GOAL_STEP_STATUS_LABELS: Record<string, string> = {
  pending: "待开始", ready: "待执行", running: "执行中", waiting_user: "等待你",
  waiting_time: "等待触发", done: "已完成", failed: "失败", skipped: "已跳过", missed: "已错过",
};

const STEP_KIND_LABELS: Record<string, string> = {
  autonomous: "自动执行", interactive: "需要你参与", approval_required: "需要审批",
};

function activeThreadId(): string {
  try { return JSON.parse(localStorage.getItem("aiive_active_thread") || "{}").threadId || ""; }
  catch { return ""; }
}

function badge(status: string): string {
  if (status === "succeeded") return "bg-success-soft text-success-text border-success-border";
  if (["failed", "cancelled"].includes(status)) return "bg-danger-soft text-danger-text border-danger-border";
  if (status.startsWith("blocked") || status === "reconciling") return "bg-warning-soft text-warning-text border-warning-border";
  return "bg-primary-soft text-primary border-primary-border";
}

export default function TaskCenterPage() {
  const [tasks, setTasks] = useState<AgentTaskSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string>("");
  const [detail, setDetail] = useState<AgentTaskDetail | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [goal, setGoal] = useState("");
  const [taskInput, setTaskInput] = useState("");
  const [goalPlan, setGoalPlan] = useState<AgentTaskGoal | null>(null);
  const eventRefreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const threadId = useMemo(activeThreadId, []);

  const refresh = useCallback(async (preferredId?: string) => {
    try {
      const next = await listAgentTasks();
      setTasks(next);
      const target = preferredId || selectedId || next[0]?.id;
      if (target) {
        setSelectedId(target);
        setDetail(await getAgentTask(target));
      } else setDetail(null);
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "任务加载失败");
    }
  }, [selectedId]);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), 4000);
    return () => {
      clearInterval(timer);
      if (eventRefreshTimer.current) clearTimeout(eventRefreshTimer.current);
    };
  }, [refresh]);

  useTaskEvents(selectedId, () => {
    if (eventRefreshTimer.current) clearTimeout(eventRefreshTimer.current);
    eventRefreshTimer.current = setTimeout(() => void refresh(), 100);
  });

  const loadGoal = useCallback(async (taskId: string) => {
    try { setGoalPlan(await getAgentTaskGoal(taskId)); }
    catch { setGoalPlan(null); }
  }, []);

  const goalTaskId = detail?.task_type === "long_term_goal" ? detail.id : "";

  useEffect(() => {
    if (!goalTaskId) { setGoalPlan(null); return; }
    void loadGoal(goalTaskId);
  }, [goalTaskId, detail?.updated_at, loadGoal]);

  const selectTask = async (taskId: string) => {
    setSelectedId(taskId);
    try { setDetail(await getAgentTask(taskId)); } catch (reason) {
      setError(reason instanceof Error ? reason.message : "任务详情加载失败");
    }
  };

  const create = async (event: FormEvent) => {
    event.preventDefault();
    if (!threadId || !goal.trim()) return;
    setBusy(true);
    try {
      const created = await createAgentTask({ thread_id: threadId, goal: goal.trim(), task_type: "general" });
      setGoal("");
      setSelectedId(created.id);
      await refresh(created.id);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "创建失败"); }
    finally { setBusy(false); }
  };

  const mutate = async (fn: () => Promise<void>) => {
    setBusy(true);
    try { await fn(); await refresh(); setError(""); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "操作失败"); }
    finally { setBusy(false); }
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold text-content">任务中心</h2>
          <p className="text-xs text-muted mt-1">持久任务、AgentRun、Action、审批、证据和产物的完整事实视图</p>
        </div>
        <button onClick={() => void refresh()} className="px-3 py-1.5 text-xs rounded border border-divider text-content hover:bg-surface-muted">刷新</button>
      </div>

      <form onSubmit={create} className="rounded-xl border border-divider bg-surface p-3 flex gap-2">
        <input
          value={goal} onChange={(event) => setGoal(event.target.value)}
          placeholder={threadId ? "为当前对话创建持久任务" : "先在对话页建立一个会话"}
          disabled={!threadId || busy}
          className="flex-1 rounded-lg border border-divider bg-surface-muted px-3 py-2 text-sm text-content outline-none focus:border-primary"
        />
        <button disabled={!threadId || !goal.trim() || busy} className="rounded-lg bg-primary px-4 py-2 text-sm text-on-primary disabled:opacity-40">创建</button>
      </form>
      {error && <div className="rounded-lg border border-danger-border bg-danger-soft px-3 py-2 text-sm text-danger-text">{error}</div>}

      <div className="grid grid-cols-[minmax(220px,0.8fr)_minmax(0,1.7fr)] gap-4 min-h-[520px]">
        <div className="space-y-2 overflow-y-auto max-h-[70vh] pr-1">
          {tasks.map((task) => (
            <button key={task.id} onClick={() => void selectTask(task.id)} className={`w-full text-left rounded-xl border p-3 transition ${selectedId === task.id ? "border-primary bg-primary-soft" : "border-divider bg-surface hover:bg-surface-muted"}`}>
              <div className="flex items-start justify-between gap-2">
                <span className="text-sm font-medium text-content line-clamp-2">{task.title}</span>
                <span className={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] ${badge(task.status)}`}>{STATUS_LABELS[task.status] || task.status}</span>
              </div>
              <div className="mt-2 text-[11px] text-muted">{task.task_type} · {task.action_count} actions · {task.id.slice(0, 8)}</div>
            </button>
          ))}
          {!tasks.length && <div className="rounded-xl border border-dashed border-divider p-6 text-center text-sm text-muted">暂无持久任务</div>}
        </div>

        <div className="rounded-xl border border-divider bg-surface p-4 overflow-y-auto max-h-[70vh]">
          {!detail ? <div className="text-sm text-muted">选择一个任务查看详情</div> : (
            <div className="space-y-5">
              <div>
                <div className="flex items-center justify-between gap-3">
                  <h3 className="font-semibold text-content">{detail.title}</h3>
                  <span className={`rounded-full border px-2 py-1 text-xs ${badge(detail.status)}`}>{STATUS_LABELS[detail.status] || detail.status}</span>
                </div>
                <p className="mt-2 text-sm text-muted whitespace-pre-wrap">{detail.goal}</p>
                <div className="mt-2 text-[11px] font-mono text-faint">task:{detail.id} · thread:{detail.thread_id.slice(0, 8)}</div>
              </div>

              {detail.report && <section className="rounded-lg border border-success-border bg-success-soft p-3">
                <div className="text-xs font-semibold text-success-text">TaskReport · {detail.report.status}</div>
                <p className="mt-1 text-sm text-content">{detail.report.summary}</p>
              </section>}

              {detail.approvals.filter((item) => item.status === "pending").map((approval) => (
                <section key={approval.id} className="rounded-lg border border-warning-border bg-warning-soft p-3">
                  <div className="text-sm font-semibold text-warning-text">需要审批：{approval.tool_name}</div>
                  <pre className="mt-2 max-h-40 overflow-auto text-xs text-content whitespace-pre-wrap">{JSON.stringify({ risk: approval.risk_snapshot, preconditions: approval.preconditions, effects: approval.effects }, null, 2)}</pre>
                  <div className="mt-3 flex gap-2">
                    <button disabled={busy} onClick={() => void mutate(() => decideTaskApproval(approval.id, "approve"))} className="rounded bg-primary px-3 py-1.5 text-xs text-on-primary">批准</button>
                    <button disabled={busy} onClick={() => void mutate(() => decideTaskApproval(approval.id, "deny"))} className="rounded border border-danger-border px-3 py-1.5 text-xs text-danger-text">拒绝</button>
                  </div>
                </section>
              ))}

              {detail.status === "blocked_user" && <form onSubmit={(event) => { event.preventDefault(); if (taskInput.trim()) void mutate(async () => { await sendAgentTaskInput(detail.id, taskInput.trim()); setTaskInput(""); }); }} className="flex gap-2">
                <input value={taskInput} onChange={(event) => setTaskInput(event.target.value)} className="flex-1 rounded border border-divider bg-surface-muted px-3 py-2 text-sm text-content" placeholder="补充任务所需信息" />
                <button className="rounded bg-primary px-3 py-2 text-xs text-on-primary">发送</button>
              </form>}


              {goalPlan && (
                <section className="rounded-lg border border-divider bg-surface-muted p-3 space-y-3">
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <h4 className="text-sm font-semibold text-content">长期目标</h4>
                      <p className="mt-1 text-xs text-muted">{goalPlan.plan.summary || "（无摘要）"}</p>
                    </div>
                    <span className="shrink-0 rounded-full border border-divider px-2 py-0.5 text-[10px] text-muted">
                      {GOAL_STATUS_LABELS[goalPlan.plan.status] || goalPlan.plan.status}
                    </span>
                  </div>
                  <div className="text-[11px] text-muted">
                    进度 {goalPlan.summary.progress.done}/{goalPlan.summary.progress.total}
                    {goalPlan.plan.deadline ? ` · 截止 ${goalPlan.plan.deadline}` : ""}
                    {goalPlan.summary.next_trigger_at ? ` · 下次 ${new Date(goalPlan.summary.next_trigger_at).toLocaleString()}` : ""}
                  </div>
                  {goalPlan.plan.blocked_reason && (
                    <div className="text-[11px] text-warning-text">受阻：{goalPlan.plan.blocked_reason}</div>
                  )}
                  <div className="space-y-1">
                    {goalPlan.plan.steps.map((step) => (
                      <div key={step.id} className="rounded border border-divider bg-surface px-2 py-1.5">
                        <div className="flex items-center justify-between gap-2 text-xs">
                          <span className={step.id === goalPlan.plan.current_step_id ? "font-semibold text-content" : "text-content"}>{step.title}</span>
                          <span className="text-[10px] text-muted">{GOAL_STEP_STATUS_LABELS[step.status] || step.status}</span>
                        </div>
                        <div className="mt-1 flex flex-wrap items-center gap-2 text-[10px] text-faint">
                          <span>{STEP_KIND_LABELS[step.kind] || step.kind}</span>
                          {step.next_trigger_at && <span>下次 {new Date(step.next_trigger_at).toLocaleString()}</span>}
                          {step.missed_count > 0 && <span className="text-warning-text">错过 {step.missed_count} 次</span>}
                          {step.evidence_refs.length > 0 && <span>证据 {step.evidence_refs.length}</span>}
                          {step.kind === "approval_required" && step.status === "waiting_user" && !step.approved_at && (
                            <button disabled={busy} onClick={() => void mutate(async () => { await approveAgentTaskGoalStep(goalTaskId, step.id); await loadGoal(goalTaskId); })} className="rounded bg-primary px-2 py-0.5 text-[10px] text-on-primary">审批通过</button>
                          )}
                          {step.kind === "interactive" && step.status === "waiting_user" && (
                            <button disabled={busy} onClick={() => void mutate(async () => { await startAgentTaskGoalStep(goalTaskId, step.id); await loadGoal(goalTaskId); })} className="rounded border border-primary-border px-2 py-0.5 text-[10px] text-primary">开始</button>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {goalPlan.plan.status === "proposed" && (
                      <>
                        <button disabled={busy} onClick={() => void mutate(async () => { await controlAgentTaskGoal(goalTaskId, "confirm"); await loadGoal(goalTaskId); })} className="rounded bg-primary px-3 py-1.5 text-xs text-on-primary">确认计划</button>
                        <button disabled={busy} onClick={() => void mutate(async () => { await controlAgentTaskGoal(goalTaskId, "reject", { reason: "用户拒绝" }); await loadGoal(goalTaskId); })} className="rounded border border-danger-border px-3 py-1.5 text-xs text-danger-text">拒绝</button>
                      </>
                    )}
                    {["active", "blocked"].includes(goalPlan.plan.status) && (
                      <button disabled={busy} onClick={() => void mutate(async () => { await controlAgentTaskGoal(goalTaskId, "pause"); await loadGoal(goalTaskId); })} className="rounded border border-divider px-3 py-1.5 text-xs text-content">暂停</button>
                    )}
                    {["paused", "blocked"].includes(goalPlan.plan.status) && (
                      <button disabled={busy} onClick={() => void mutate(async () => { await controlAgentTaskGoal(goalTaskId, "resume"); await loadGoal(goalTaskId); })} className="rounded border border-divider px-3 py-1.5 text-xs text-content">恢复</button>
                    )}
                    {!["completed", "rejected", "abandoned"].includes(goalPlan.plan.status) && (
                      <button disabled={busy} onClick={() => void mutate(async () => { await controlAgentTaskGoal(goalTaskId, "end", { reason: "用户结束目标" }); await loadGoal(goalTaskId); })} className="rounded border border-danger-border px-3 py-1.5 text-xs text-danger-text">结束目标</button>
                    )}
                  </div>
                </section>
              )}

              <section>
                <h4 className="text-sm font-semibold text-content mb-2">Action 时间线</h4>
                <div className="space-y-2">
                  {detail.actions.map((action) => <div key={action.id} className="rounded-lg border border-divider bg-surface-muted p-3">
                    <div className="flex justify-between gap-2 text-xs"><span className="font-mono text-content">#{action.sequence} {action.capability_id}</span><span className="text-muted">{action.status}</span></div>
                    {action.error && <div className="mt-1 text-xs text-danger-text">{action.error}</div>}
                    {action.result_summary?.summary !== undefined && <div className="mt-1 text-xs text-muted line-clamp-3">{String(action.result_summary.summary)}</div>}
                  </div>)}
                  {!detail.actions.length && <div className="text-xs text-muted">尚无 Action</div>}
                </div>
              </section>

              <section className="grid grid-cols-2 gap-3">
                <div><h4 className="text-sm font-semibold text-content mb-2">Evidence</h4>{detail.evidence.map((item) => <a key={String(item.id)} href={`${BASE_PATH}api/agent-tasks/${detail.id}/evidence/${item.id}`} target="_blank" rel="noreferrer" className="block mb-1 text-xs text-primary hover:underline">{String(item.kind)} · {String(item.content_size)} bytes</a>)}</div>
                <div><h4 className="text-sm font-semibold text-content mb-2">Artifacts</h4>{detail.artifacts.map((item) => <a key={String(item.id)} href={`${BASE_PATH}api/agent-tasks/${detail.id}/artifacts/${item.id}`} className="block mb-1 text-xs text-primary hover:underline">{String(item.name)}</a>)}</div>
              </section>

              {!['succeeded', 'partial', 'failed', 'cancelled'].includes(detail.status) && <button disabled={busy} onClick={() => void mutate(() => cancelAgentTask(detail.id))} className="rounded border border-danger-border px-3 py-1.5 text-xs text-danger-text">取消任务</button>}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
