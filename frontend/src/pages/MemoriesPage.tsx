/**
 * 记忆管理页面：展示数据库中的可见记忆，并调用真实生命周期 API。
 */
import { FormEvent, useCallback, useEffect, useState } from "react";

type MemoryItem = {
  id: string;
  memory_type: string;
  lifecycle_state: string;
  content: string;
  confidence: number;
  pinned: boolean;
  created_at: string;
  updated_at: string;
};

type MemoryAction = "sleep" | "archive" | "forget";

const STATE_LABELS: Record<string, string> = {
  active: "活跃",
  sleeping: "休眠",
  archived: "归档",
};

export default function MemoriesPage() {
  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [query, setQuery] = useState("");
  const [state, setState] = useState("all");
  const [content, setContent] = useState("");
  const [memoryType, setMemoryType] = useState("fact");
  const [pinned, setPinned] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [actingId, setActingId] = useState("");
  const [error, setError] = useState("");

  const loadMemories = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const response = await fetch("api/memories");
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setMemories(await response.json() as MemoryItem[]);
    } catch (err) {
      setError(`加载记忆失败: ${err instanceof Error ? err.message : "未知错误"}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void loadMemories(); }, [loadMemories]);

  const createMemory = async (event: FormEvent) => {
    event.preventDefault();
    if (!content.trim()) return;
    setSaving(true);
    setError("");
    try {
      const response = await fetch("api/memories", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": crypto.randomUUID(),
        },
        body: JSON.stringify({ content: content.trim(), memory_type: memoryType, pinned }),
      });
      const payload = await response.json().catch(() => ({})) as {
        ok?: boolean;
        outcome?: string;
        reason?: string;
        detail?: string;
      };
      if (!response.ok || !payload.ok) {
        throw new Error(payload.detail || payload.reason || `记忆未写入: ${payload.outcome || `HTTP ${response.status}`}`);
      }
      setContent("");
      setPinned(false);
      await loadMemories();
    } catch (err) {
      setError(`创建记忆失败: ${err instanceof Error ? err.message : "未知错误"}`);
    } finally {
      setSaving(false);
    }
  };

  const runAction = async (memory: MemoryItem, action: MemoryAction) => {
    const actionLabel = action === "sleep" ? "休眠" : action === "archive" ? "归档" : "遗忘";
    if (!window.confirm(`确认${actionLabel}这条记忆？`)) return;
    setActingId(memory.id);
    setError("");
    try {
      const response = await fetch(`api/memories/${memory.id}/${action}`, {
        method: "POST",
        headers: action === "forget" ? { "Content-Type": "application/json" } : undefined,
        body: action === "forget" ? JSON.stringify({ reason: "用户从记忆管理页面请求遗忘" }) : undefined,
      });
      const result = await response.json() as { ok?: boolean; reason?: string; detail?: string };
      if (!response.ok || !result.ok) throw new Error(result.detail || result.reason || `HTTP ${response.status}`);
      await loadMemories();
    } catch (err) {
      setError(`${actionLabel}记忆失败: ${err instanceof Error ? err.message : "未知错误"}`);
    } finally {
      setActingId("");
    }
  };

  const normalizedQuery = query.trim().toLowerCase();
  const visible = memories.filter(memory => {
    if (state !== "all" && memory.lifecycle_state !== state) return false;
    return !normalizedQuery
      || memory.content.toLowerCase().includes(normalizedQuery)
      || memory.memory_type.toLowerCase().includes(normalizedQuery);
  });

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-lg font-semibold text-content">记忆</h2>
        <p className="text-xs text-faint mt-1">展示后端返回的最近 100 条可见记忆；搜索仅作用于当前列表。</p>
      </div>

      <form onSubmit={createMemory} className="bg-surface border border-divider rounded-xl p-4 shadow-sm space-y-3">
        <textarea
          value={content}
          onChange={event => setContent(event.target.value)}
          placeholder="写入一条明确、可长期使用的记忆"
          rows={3}
          className="w-full rounded-lg border border-divider bg-background px-3 py-2 text-sm text-content focus:outline-none focus:ring-2 focus:ring-primary-ring"
        />
        <div className="flex flex-wrap items-center gap-3">
          <select value={memoryType} onChange={event => setMemoryType(event.target.value)} className="rounded-lg border border-divider bg-surface px-3 py-2 text-sm text-content">
            <option value="fact">事实</option>
            <option value="preference">偏好</option>
            <option value="instruction">指令</option>
            <option value="episodic">经历</option>
          </select>
          <label className="inline-flex items-center gap-2 text-sm text-muted">
            <input type="checkbox" checked={pinned} onChange={event => setPinned(event.target.checked)} />
            固定保留
          </label>
          <button type="submit" disabled={saving || !content.trim()} className="ml-auto px-4 py-2 rounded-lg bg-primary text-on-primary text-sm font-medium disabled:opacity-40">
            {saving ? "写入中…" : "写入记忆"}
          </button>
        </div>
      </form>

      <div className="flex gap-3">
        <input value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索当前列表" className="flex-1 rounded-lg border border-divider bg-surface px-3 py-2 text-sm text-content" />
        <select value={state} onChange={event => setState(event.target.value)} className="rounded-lg border border-divider bg-surface px-3 py-2 text-sm text-content">
          <option value="all">全部状态</option>
          <option value="active">活跃</option>
          <option value="sleeping">休眠</option>
          <option value="archived">归档</option>
        </select>
      </div>

      {error && <div className="bg-danger-soft border border-danger-border text-danger-text rounded-lg px-3 py-2 text-sm">{error}</div>}
      {loading && <div className="text-center text-faint py-10">正在加载记忆…</div>}
      {!loading && visible.length === 0 && <div className="text-center text-faint py-10">没有匹配的可见记忆</div>}

      <div className="space-y-3">
        {visible.map(memory => (
          <article key={memory.id} className="bg-surface border border-divider rounded-xl p-4 shadow-sm">
            <div className="flex flex-wrap items-center gap-2 mb-2">
              <span className="text-[11px] px-2 py-0.5 rounded-full bg-primary-soft text-primary-hover border border-primary-border">{memory.memory_type}</span>
              <span className="text-[11px] px-2 py-0.5 rounded-full bg-surface-muted text-code border border-divider">{STATE_LABELS[memory.lifecycle_state] || memory.lifecycle_state}</span>
              {memory.pinned && <span className="text-[11px] px-2 py-0.5 rounded-full bg-warning-soft text-warning-text border border-warning-border">固定</span>}
              <span className="ml-auto text-[11px] text-faint font-mono">{memory.id.slice(0, 8)}</span>
            </div>
            <p className="text-sm text-content whitespace-pre-wrap break-words">{memory.content}</p>
            <div className="flex flex-wrap items-center gap-2 mt-3">
              {memory.lifecycle_state === "active" && <button disabled={actingId === memory.id} onClick={() => runAction(memory, "sleep")} className="text-xs px-3 py-1.5 rounded-lg border border-divider text-muted hover:text-title disabled:opacity-40">休眠</button>}
              {memory.lifecycle_state !== "archived" && <button disabled={actingId === memory.id} onClick={() => runAction(memory, "archive")} className="text-xs px-3 py-1.5 rounded-lg border border-warning-border text-warning-text hover:bg-warning-soft disabled:opacity-40">归档</button>}
              <button disabled={actingId === memory.id} onClick={() => runAction(memory, "forget")} className="text-xs px-3 py-1.5 rounded-lg border border-danger-border text-danger-text hover:bg-danger-soft disabled:opacity-40">遗忘</button>
              <span className="ml-auto text-[11px] text-faint">置信度 {(memory.confidence * 100).toFixed(0)}%</span>
            </div>
          </article>
        ))}
      </div>
    </div>
  );
}
