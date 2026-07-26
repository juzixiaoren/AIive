/**
 * 开发者只读诊断页面：查看 Thread、Trace、LLM 调用、Outbox 和 Epoch 状态。
 */
import { FormEvent, useEffect, useMemo, useState } from "react";

type DebugEvent = {
  id: string;
  trace_id: string;
  thread_id: string;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string;
};

type LLMCall = {
  id: string;
  model: string;
  latency_ms: number;
  input_preview?: string;
  output_preview?: string;
  created_at: string;
};

type OutboxJob = {
  id: string;
  operation_id: string;
  job_type: string;
  status: string;
  retry_count: number;
  error_message?: string;
  created_at: string;
};

type EpochStatus = {
  thread_id: string;
  active_epoch_id?: string;
  active_epoch_no?: number;
  open_segment_id?: string;
  open_segment_status?: string;
  sealed_segment_count: number;
};

function loadActiveThread(): { threadId: string; traceIds: string[] } {
  try {
    const raw = localStorage.getItem("aiive_active_thread");
    if (!raw) return { threadId: "", traceIds: [] };
    const data = JSON.parse(raw) as { threadId?: string; messages?: Array<{ traceId?: string }> };
    return {
      threadId: data.threadId || "",
      traceIds: Array.from(new Set((data.messages || []).map(message => message.traceId || "").filter(Boolean))).reverse(),
    };
  } catch {
    return { threadId: "", traceIds: [] };
  }
}

export default function DeveloperPage({ selectedTraceId }: { selectedTraceId?: string }) {
  const stored = loadActiveThread();
  const [threadId, setThreadId] = useState(stored.threadId);
  const [traceId, setTraceId] = useState(selectedTraceId || stored.traceIds[0] || "");
  const [events, setEvents] = useState<DebugEvent[]>([]);
  const [calls, setCalls] = useState<LLMCall[]>([]);
  const [jobs, setJobs] = useState<OutboxJob[]>([]);
  const [epoch, setEpoch] = useState<EpochStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (selectedTraceId) setTraceId(selectedTraceId);
  }, [selectedTraceId]);

  const eventTraceIds = useMemo(
    () => Array.from(new Set([...stored.traceIds, ...events.map(event => event.trace_id).filter(Boolean)])),
    [events, stored.traceIds],
  );

  const loadThread = async (event?: FormEvent) => {
    event?.preventDefault();
    if (!threadId.trim()) return;
    setLoading(true);
    setError("");
    try {
      const [eventResponse, epochResponse] = await Promise.all([
        fetch(`api/debug/events?thread_id=${encodeURIComponent(threadId.trim())}`),
        fetch(`api/epochs/${encodeURIComponent(threadId.trim())}`),
      ]);
      if (!eventResponse.ok || !epochResponse.ok) throw new Error(`HTTP ${eventResponse.status}/${epochResponse.status}`);
      const threadEvents = await eventResponse.json() as DebugEvent[];
      setEvents(threadEvents);
      setEpoch(await epochResponse.json() as EpochStatus);
      if (!traceId && threadEvents.length > 0) setTraceId(threadEvents[threadEvents.length - 1].trace_id);
    } catch (err) {
      setError(`加载 Thread 诊断失败: ${err instanceof Error ? err.message : "未知错误"}`);
    } finally {
      setLoading(false);
    }
  };

  const loadTrace = async () => {
    if (!traceId.trim()) return;
    setLoading(true);
    setError("");
    try {
      const [eventResponse, callResponse, jobResponse] = await Promise.all([
        fetch(`api/debug/events?trace_id=${encodeURIComponent(traceId.trim())}`),
        fetch(`api/debug/llm_calls?trace_id=${encodeURIComponent(traceId.trim())}`),
        fetch(`api/outbox/jobs?trace_id=${encodeURIComponent(traceId.trim())}`),
      ]);
      if (!eventResponse.ok || !callResponse.ok || !jobResponse.ok) throw new Error("诊断接口返回错误");
      setEvents(await eventResponse.json() as DebugEvent[]);
      setCalls(await callResponse.json() as LLMCall[]);
      setJobs(await jobResponse.json() as OutboxJob[]);
    } catch (err) {
      setError(`加载 Trace 诊断失败: ${err instanceof Error ? err.message : "未知错误"}`);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-lg font-semibold text-content">开发者诊断</h2>
        <p className="text-xs text-faint mt-1">只读页面。敏感 payload、输入和输出默认折叠，不提供 seal、rollover、retry 或 requeue 操作。</p>
      </div>

      <form onSubmit={loadThread} className="bg-surface border border-divider rounded-xl p-4 shadow-sm space-y-3">
        <label className="block text-xs text-muted">Thread ID</label>
        <div className="flex gap-2">
          <input value={threadId} onChange={event => setThreadId(event.target.value)} className="flex-1 rounded-lg border border-divider px-3 py-2 text-sm font-mono text-content" placeholder="输入 thread_id" />
          <button disabled={loading || !threadId.trim()} className="px-4 py-2 rounded-lg bg-primary text-on-primary text-sm disabled:opacity-40">加载 Thread</button>
        </div>
        {epoch && (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
            <div className="bg-surface-muted rounded-lg p-2" title={epoch.active_epoch_id || undefined}>
              <p className="text-faint">Epoch</p>
              <p className="font-mono text-title break-all">{epoch.active_epoch_no == null ? "-" : `#${epoch.active_epoch_no}`}</p>
              <p className="font-mono text-[10px] text-faint break-all">{epoch.active_epoch_id?.slice(0, 8) || "-"}</p>
            </div>
            <div className="bg-surface-muted rounded-lg p-2"><p className="text-faint">Open Segment</p><p className="font-mono text-title break-all">{epoch.open_segment_id?.slice(0, 8) || "-"}</p></div>
            <div className="bg-surface-muted rounded-lg p-2"><p className="text-faint">Segment 状态</p><p className="font-mono text-title">{epoch.open_segment_status || "-"}</p></div>
            <div className="bg-surface-muted rounded-lg p-2"><p className="text-faint">已密封</p><p className="font-mono text-title">{epoch.sealed_segment_count}</p></div>
          </div>
        )}
      </form>

      <section className="bg-surface border border-divider rounded-xl p-4 shadow-sm space-y-3">
        <label className="block text-xs text-muted">Trace ID</label>
        <div className="flex gap-2">
          <input list="developer-traces" value={traceId} onChange={event => setTraceId(event.target.value)} className="flex-1 rounded-lg border border-divider px-3 py-2 text-sm font-mono text-content" placeholder="选择或粘贴 trace_id" />
          <datalist id="developer-traces">{eventTraceIds.map(id => <option key={id} value={id} />)}</datalist>
          <button type="button" onClick={loadTrace} disabled={loading || !traceId.trim()} className="px-4 py-2 rounded-lg bg-primary text-on-primary text-sm disabled:opacity-40">加载 Trace</button>
        </div>
        <p className="text-[11px] text-faint">候选来自当前浏览器消息及该 Thread 最多 100 条事件，不代表完整历史。</p>
      </section>

      {error && <div className="bg-danger-soft border border-danger-border text-danger-text rounded-lg px-3 py-2 text-sm">{error}</div>}
      {loading && <div className="text-center text-faint py-6">正在加载诊断数据…</div>}

      <section>
        <h3 className="text-sm font-medium text-title mb-2">事件 ({events.length})</h3>
        <div className="space-y-2">
          {events.map(item => (
            <details key={item.id} className="bg-surface border border-divider rounded-lg p-3">
              <summary className="cursor-pointer text-xs text-content"><span className="font-mono">{item.event_type}</span><span className="ml-2 text-faint">{item.created_at}</span></summary>
              <pre className="mt-3 max-h-72 overflow-auto text-[11px] text-code bg-background rounded-lg p-3 whitespace-pre-wrap break-all">{JSON.stringify(item.payload, null, 2)}</pre>
            </details>
          ))}
        </div>
      </section>

      <section>
        <h3 className="text-sm font-medium text-title mb-2">LLM 调用 ({calls.length})</h3>
        <div className="space-y-2">
          {calls.map(call => (
            <details key={call.id} className="bg-surface border border-divider rounded-lg p-3">
              <summary className="cursor-pointer text-xs text-content">{call.model} · {call.latency_ms}ms</summary>
              <div className="mt-3 space-y-2">
                <pre className="max-h-56 overflow-auto text-[11px] text-code bg-background rounded-lg p-3 whitespace-pre-wrap">输入预览{"\n"}{call.input_preview || "-"}</pre>
                <pre className="max-h-56 overflow-auto text-[11px] text-code bg-background rounded-lg p-3 whitespace-pre-wrap">输出预览{"\n"}{call.output_preview || "-"}</pre>
              </div>
            </details>
          ))}
        </div>
      </section>

      <section>
        <h3 className="text-sm font-medium text-title mb-2">Outbox ({jobs.length})</h3>
        <div className="space-y-2">
          {jobs.map(job => (
            <div key={job.id} className="bg-surface border border-divider rounded-lg p-3 text-xs">
              <div className="flex justify-between gap-3"><span className="font-mono text-content">{job.job_type}</span><span className="text-muted">{job.status}</span></div>
              <p className="text-faint mt-1">重试 {job.retry_count} 次 · {job.created_at}</p>
              {job.error_message && <details className="mt-2"><summary className="cursor-pointer text-danger-text">错误详情</summary><pre className="mt-2 whitespace-pre-wrap text-[11px]">{job.error_message}</pre></details>}
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
