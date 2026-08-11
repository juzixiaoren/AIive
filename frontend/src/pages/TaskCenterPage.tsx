import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AgentTaskDetail,
  AgentTaskSummary,
  cancelAgentTask,
  createAgentTask,
  decideTaskApproval,
  getAgentTask,
  listAgentTasks,
  sendAgentTaskInput,
} from "../api/agentTasks";
import { useTaskEvents } from "../hooks/useTaskEvents";
import { BASE_PATH } from "../lib/base";


const STATUS_LABELS: Record<string, string> = {
  queued: "排队", dispatching: "调度中", running: "执行中",
  blocked_approval: "等待审批", blocked_user: "等待输入", blocked_node: "节点离线",
  reconciling: "对账中", verifying: "验证中", succeeded: "已完成",
  partial: "部分完成", failed: "失败", cancelled: "已取消",
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
