/**
 * 事件时间线页面
 * - 展示系统事件的时间序列，支持按 trace_id 过滤
 * - 事件类型包括：对话开始、用户消息、模型回复、记忆激活/候选、管家信号、上下文截断、删除请求等
 */

import { useEffect, useState } from "react";

/** 事件项数据结构 */
interface EventItem {
  id: string; trace_id: string; event_type: string;
  payload: Record<string, unknown>; created_at: string;
}

/** 事件类型中文标签映射 */
const TYPE_LABELS: Record<string, string> = {
  chat_started: "开始对话", user_message: "用户消息", llm_response: "模型回复",
  chat_ended: "对话结束", system_injection: "系统注入", tool_call: "工具调用",
  tool_result: "工具结果", memory_active: "记忆激活", memory_candidate: "记忆候选",
  steward_signal: "管家信号", context_truncated: "上下文截断", delete_request: "删除请求",
  automatic_recall: "自动召回", memory_extracted: "记忆提取",
};

/** 从事件 payload 生成可读摘要 */
function describe(e: EventItem): string {
  const p = e.payload || {};
  switch (e.event_type) {
    case "user_message":
      return String(p.content ?? "");
    case "llm_response":
      return String(p.content ?? "");
    case "system_injection": {
      const tools = Array.isArray(p.injected_tools) ? p.injected_tools.join(", ") : "";
      const mem = p.memory_count ?? 0;
      const tasks = Number(p.active_task_count ?? 0) + Number(p.due_task_count ?? 0);
      return `注入 ${tools ? tools.split(", ").length : 0} 个工具 · ${mem} 条记忆 · ${tasks} 个任务`;
    }
    case "tool_call": {
      const params = JSON.stringify(p.params ?? {});
      return `${p.name ?? ""}(${params.length > 200 ? params.slice(0, 200) + "…" : params})`;
    }
    case "tool_result": {
      const ok = (p.status ?? "") === "completed";
      return `${p.name ?? ""} → ${ok ? "成功" : "失败"}`;
    }
    case "chat_ended":
      return `工具 ${p.tool_calls ?? 0} 次 · 成功 ${p.tool_succeeded ?? 0} · 失败 ${p.tool_failed ?? 0}`;
    case "automatic_recall": {
      const sel = Number(p.selected_count ?? 0);
      const excl = Number(p.excluded_count ?? 0);
      const total = Number(p.total_candidates ?? 0);
      const items: unknown[] = Array.isArray(p.selected) ? p.selected : [];
      const preview = items.map((it: unknown) => {
        const i = it as Record<string, unknown>;
        return `${i.canonical_key ?? ""}: ${String(i.content_preview ?? "").slice(0, 40)}`;
      }).join(" | ");
      return `查询「${String(p.query ?? "").slice(0, 50)}」→ 命中 ${sel}/${total}（排除 ${excl}）${preview ? " · " + preview : ""}`;
    }
    case "memory_extracted": {
      const total = Number(p.total ?? 0);
      const proposals: unknown[] = Array.isArray(p.proposals) ? p.proposals : [];
      const preview = proposals.map((pp: unknown) => {
        const pr = pp as Record<string, unknown>;
        return `${pr.canonical_key ?? ""}: ${String(pr.content_preview ?? "").slice(0, 30)}`;
      }).join(" | ");
      return `从「${String(p.source_message ?? "").slice(0, 40)}」提取 ${total} 条${preview ? " · " + preview : ""}`;
    }
    default:
      return JSON.stringify(p);
  }
}

/**
 * 事件时间线组件
 * @param traceId - 可选，传入时只显示该 trace 相关的事件
 */
/** 需要可展开查看完整 payload 的事件类型 */
const EXPANDABLE = new Set(["tool_call", "tool_result", "system_injection"]);

export default function EventTimeline({ traceId }: { traceId?: string }) {
  const [events, setEvents] = useState<EventItem[]>([]);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // 根据是否有 traceId 决定请求参数，加载事件列表
  useEffect(() => {
    const controller = new AbortController();
    const url = traceId
      ? `/api/inspector/events?trace_id=${encodeURIComponent(traceId)}&limit=50`
      : `/api/inspector/events?limit=50`;
    const loadEvents = async () => {
      setLoading(true);
      setError(null);
      try {
        const response = await fetch(url, { signal: controller.signal });
        if (!response.ok) throw new Error(`事件加载失败 HTTP ${response.status}`);
        const data: unknown = await response.json();
        if (!Array.isArray(data)) throw new Error("事件响应格式无效");
        setEvents(data as EventItem[]);
      } catch (reason) {
        if (controller.signal.aborted) return;
        setEvents([]);
        setError(reason instanceof Error ? reason.message : "事件加载失败");
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    };
    void loadEvents();
    return () => controller.abort();
  }, [traceId]);

  return (
    <div>
      <h2 className="text-lg font-semibold text-content mb-1">事件时间线</h2>
      {traceId && <p className="text-xs text-faint mb-4 font-mono">trace_id: {traceId}</p>}
      {loading && <div className="text-center text-faint text-sm py-12">加载中…</div>}
      {!loading && error && <div className="text-center text-danger text-sm py-12">{error}</div>}
      {!loading && !error && events.length === 0 && (
        <div className="text-center text-faint text-sm py-12">暂无事件</div>
      )}
      <div className="flex flex-col gap-1">
        {events.map(e => {
          const isOpen = expandedId === e.id;
          const canExpand = EXPANDABLE.has(e.event_type);
          return (
            <div
              key={e.id}
              onClick={() => canExpand ? setExpandedId(isOpen ? null : e.id) : undefined}
              className={`py-2 border-b border-surface-muted text-sm px-2 rounded ${canExpand ? "cursor-pointer hover:bg-background" : ""}`}
            >
              <div className="flex gap-3 items-start">
                {/* 时间戳（仅显示 HH:MM:SS） */}
                <span className="text-faint w-16 shrink-0 font-mono text-xs pt-0.5">{e.created_at?.slice(11, 19)}</span>
                {/* 事件类型标签 */}
                <span className="bg-surface-muted px-2 py-0.5 rounded text-xs font-mono text-code shrink-0">
                  {TYPE_LABELS[e.event_type] || e.event_type}
                </span>
                {/* 事件可读摘要 */}
                <span className="text-muted truncate text-xs pt-0.5 flex-1 min-w-0">{describe(e)}</span>
                {canExpand && (
                  <span className="text-[11px] text-faint shrink-0">{isOpen ? "收起" : "详情"}</span>
                )}
              </div>
              {/* 展开后的完整 payload */}
              {isOpen && canExpand && (
                <pre className="mt-2 ml-20 text-[11px] text-code bg-background rounded-lg px-3 py-2 whitespace-pre-wrap break-all font-mono leading-relaxed max-h-80 overflow-auto">
                  {JSON.stringify(e.payload, null, 2)}
                </pre>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
