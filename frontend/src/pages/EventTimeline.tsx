import { useEffect, useState } from "react";

interface EventItem {
  id: string; trace_id: string; event_type: string;
  payload: Record<string, unknown>; created_at: string;
}

const TYPE_LABELS: Record<string, string> = {
  chat_started: "开始对话", user_message: "用户消息", llm_response: "模型回复",
  memory_active: "记忆激活", memory_candidate: "记忆候选", steward_signal: "管家信号",
  context_truncated: "上下文截断", delete_request: "删除请求",
};

export default function EventTimeline({ traceId }: { traceId?: string }) {
  const [events, setEvents] = useState<EventItem[]>([]);

  useEffect(() => {
    const url = traceId
      ? `/api/inspector/events?trace_id=${traceId}&limit=50`
      : `/api/inspector/events?limit=50`;
    fetch(url).then(r => r.json()).then(setEvents);
  }, [traceId]);

  return (
    <div>
      <h2 className="text-lg font-semibold text-slate-800 mb-1">事件时间线</h2>
      {traceId && <p className="text-xs text-slate-400 mb-4 font-mono">trace_id: {traceId}</p>}
      {events.length === 0 && (
        <div className="text-center text-slate-400 text-sm py-12">暂无事件</div>
      )}
      <div className="flex flex-col gap-1">
        {events.map(e => (
          <div key={e.id} className="flex gap-3 items-start py-2 border-b border-slate-100 text-sm hover:bg-slate-50 px-2 rounded">
            <span className="text-slate-400 w-16 shrink-0 font-mono text-xs pt-0.5">{e.created_at?.slice(11, 19)}</span>
            <span className="bg-slate-100 px-2 py-0.5 rounded text-xs font-mono text-slate-600 shrink-0">
              {TYPE_LABELS[e.event_type] || e.event_type}
            </span>
            <span className="text-slate-500 truncate text-xs pt-0.5">{JSON.stringify(e.payload).slice(0, 100)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
