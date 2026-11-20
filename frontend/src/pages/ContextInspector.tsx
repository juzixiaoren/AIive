import { useEffect, useRef, useState } from "react";
import { apiFetch } from "../api/client";

type ContextItem = {
  item_id: string;
  kind: string;
  source: string;
  trust_level: string;
  content_preview: string;
  preview_length: number;
  preview_truncated: boolean;
  has_detail: boolean;
  token_estimate: number;
};

type ContextSelectionTrace = {
  policy_version: string | null;
  candidates: {
    candidate_id: string;
    source_type: string;
    source_id: string;
    source_version: string | null;
    retention_tier: number;
    native_score: number | null;
    source_rank: number;
    rank_percentile: number;
    estimated_tokens: number | null;
    selected: boolean;
    exclusion_reason: string | null;
  }[];
  eviction_order: string[];
};

type ContextSnapshot = {
  snapshot_id: string;
  trace_id: string;
  stable_prefix_hash: string;
  context_items: ContextItem[];
  token_total: number;
  total_tool_calls: number;
  created_at: string;
  selection_trace: ContextSelectionTrace;
  injected_memory_ids: {
    memory_id: string;
    canonical_key: string | null;
    memory_type: string | null;
  }[];
};

type ContextRunResponse = {
  trace_id: string;
  snapshots: ContextSnapshot[];
};

type ContextItemDetailResponse = {
  snapshot_id: string;
  item_id: string;
  full_content: string;
  content_length: number;
};

type RunState =
  | { status: "loading" | "not_found" }
  | { status: "success"; data: ContextRunResponse }
  | { status: "error"; message: string };

type DetailState =
  | { status: "idle" | "loading" }
  | { status: "success"; data: ContextItemDetailResponse }
  | { status: "error"; message: string };

/** 上下文类型的中文标签和样式映射 */
const KIND_META: Record<string, { label: string; bg: string; text: string; border: string }> = {
  stable_prefix:      { label: "系统前缀",  bg: "bg-accent-soft", text: "text-accent-text", border: "border-accent-border" },
  core_memory:        { label: "核心记忆",  bg: "bg-accent-soft/70", text: "text-accent-text", border: "border-accent-border/60" },
  working_state:      { label: "工作状态",  bg: "bg-accent-soft/50", text: "text-accent-text", border: "border-accent-border/50" },
  epoch_checkpoint:   { label: "纪元检查点", bg: "bg-surface-muted", text: "text-code", border: "border-divider" },
  segment_summary:    { label: "分段摘要",  bg: "bg-surface-muted", text: "text-code", border: "border-divider" },
  sealing_bridge:     { label: "密封桥接",  bg: "bg-surface-muted", text: "text-code", border: "border-divider" },
  history_summary:    { label: "历史摘要",  bg: "bg-surface-muted", text: "text-code", border: "border-divider" },
  tool_schemas:       { label: "工具列表",  bg: "bg-success-soft",    text: "text-success-text",    border: "border-success-border" },
  evidence_memory:    { label: "注入记忆",  bg: "bg-warning-soft",   text: "text-warning-text",   border: "border-warning-border" },
  history_user:       { label: "用户消息",  bg: "bg-primary-soft",    text: "text-primary",    border: "border-primary-border" },
  history_assistant:  { label: "模型回复",  bg: "bg-primary-soft/60", text: "text-primary/70", border: "border-primary-border/50" },
  user_message:       { label: "当前消息",  bg: "bg-danger-soft",    text: "text-danger-text",    border: "border-danger-border" },
  agent_output:       { label: "Agent 输出", bg: "bg-accent-soft",  text: "text-accent-text", border: "border-accent-border" },
  recall_memory:      { label: "召回记忆",  bg: "bg-success-soft",   text: "text-success-text",   border: "border-success-border" },
  tool_call:          { label: "工具调用",  bg: "bg-success-soft",   text: "text-success-text",   border: "border-success-border" },
  tool_result:        { label: "工具结果",  bg: "bg-warning-soft",  text: "text-warning-text",  border: "border-warning-border" },
};

/** 信任级别中文标签 */
const TRUST_LABEL: Record<string, string> = {
  trusted: "可信",
  untrusted: "不可信",
};

/** 来源渠道中文标签 */
const SOURCE_LABEL: Record<string, string> = {
  system: "系统",
  thread: "对话",
  history: "对话历史",
  memory: "记忆库",
  tools: "工具",
  agent: "Agent",
  attention: "注意力",
  memory_store: "记忆库",
  user: "用户",
  runtime_event: "运行时事件",
  system_command: "系统指令",
};

function kindMeta(kind: string) {
  return KIND_META[kind] || { label: kind, bg: "bg-background", text: "text-code", border: "border-divider" };
}

function itemMetadata(item: ContextItem) {
  return [
    { label: "来源", value: SOURCE_LABEL[item.source] || item.source },
    { label: "信任级别", value: TRUST_LABEL[item.trust_level] || item.trust_level },
    { label: "Token", value: `~${item.token_estimate}` },
  ];
}

function KindBadge({ kind }: { kind: string }) {
  const meta = kindMeta(kind);
  return (
    <span className={`text-xs font-medium px-2.5 py-0.5 rounded-full ${meta.text} ${meta.bg} border ${meta.border}`}>
      {meta.label}
    </span>
  );
}

async function copyContent(content: string, dialog: HTMLDialogElement) {
  if (navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(content);
      return;
    } catch {
      if (!dialog.isConnected) return;
    }
  }
  const field = document.createElement("textarea");
  field.value = content;
  field.setAttribute("readonly", "");
  field.style.cssText = "position:fixed;left:-9999px";
  const previousFocus = document.activeElement;
  dialog.append(field);
  field.select();
  try {
    if (!document.execCommand("copy")) throw new Error("Copy failed");
  } finally {
    field.remove();
    if (previousFocus instanceof HTMLElement) previousFocus.focus();
  }
}

function DetailDialog({ item, detail, onClose, onRetry }: {
  item: ContextItem;
  detail: DetailState;
  onClose: () => void;
  onRetry: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [copyStatus, setCopyStatus] = useState("");
  const content = detail.status === "success" ? detail.data : null;

  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => element?.close();
  }, []);

  async function handleCopy() {
    if (!content || !dialog.current) return;
    try {
      await copyContent(content.full_content, dialog.current);
      setCopyStatus("已复制完整内容");
    } catch {
      setCopyStatus("复制失败，请选中正文手动复制");
    }
  }

  return (
    <dialog
      ref={dialog}
      aria-labelledby="context-detail-title"
      onCancel={event => { event.preventDefault(); onClose(); }}
      onClick={event => { if (event.target === event.currentTarget) onClose(); }}
      className="m-auto w-[calc(100%-2rem)] max-w-3xl max-h-[85dvh] overflow-hidden rounded-2xl bg-surface p-0 text-content shadow-2xl backdrop:bg-content/40"
    >
      <div className="flex max-h-[85dvh] min-w-0 flex-col">
        <header className="flex shrink-0 items-center justify-between gap-2 border-b border-divider px-4 py-3">
          <h3 id="context-detail-title" className="font-semibold">完整上下文</h3>
          <button type="button" onClick={onClose} className="rounded-lg px-3 py-1 text-sm hover:bg-surface-muted">
            关闭
          </button>
        </header>
        <div className="min-h-0 overflow-y-auto px-4 py-3">
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <KindBadge kind={item.kind} />
            <span className="text-xs text-faint">Item ID</span>
            <code className="break-all text-xs">{item.item_id}</code>
          </div>
          <dl className="mb-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
            {[...itemMetadata(item), { label: "字符数", value: String(content?.content_length ?? item.preview_length) }].map(field => (
              <div key={field.label} className="flex gap-1">
                <dt>{field.label}</dt><dd>{field.value}</dd>
              </div>
            ))}
          </dl>
          {detail.status === "error" ? (
            <div className="py-4">
              <p role="alert" className="mb-2 text-sm text-danger">{detail.message}</p>
              <button type="button" onClick={onRetry} className="rounded bg-primary-soft px-3 py-1 text-sm text-primary">
                重试
              </button>
            </div>
          ) : content ? (
            content.full_content === "" ? (
              <p className="text-sm text-faint">正文为空</p>
            ) : (
              <pre aria-label="完整正文" className="min-w-0 whitespace-pre-wrap [overflow-wrap:anywhere] text-xs leading-relaxed">
                {content.full_content}
              </pre>
            )
          ) : (
            <p role="status" className="py-4 text-sm text-faint">正在加载完整内容…</p>
          )}
        </div>
        {content && (
          <footer className="flex shrink-0 flex-wrap items-center gap-3 border-t border-divider px-4 py-3">
            <button type="button" onClick={handleCopy} className="rounded bg-primary-soft px-3 py-1 text-sm text-primary">
              复制完整内容
            </button>
            <span role="status" className="text-xs text-muted">{copyStatus}</span>
          </footer>
        )}
      </div>
    </dialog>
  );
}

function ContextRun({ traceId }: { traceId: string }) {
  const [run, setRun] = useState<RunState>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);
  const [snapshotId, setSnapshotId] = useState("");
  const [modalItem, setModalItem] = useState<ContextItem | null>(null);
  const [details, setDetails] = useState(new Map<string, DetailState>());
  const requests = useRef(new Map<string, AbortController>());
  const runUrl = `api/context-runs/${encodeURIComponent(traceId)}`;

  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      try {
        const response = await apiFetch(runUrl, { signal: controller.signal, cache: "no-store" });
        if (controller.signal.aborted) return;
        if (response.status === 404) {
          setRun({ status: "not_found" });
          return;
        }
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data: ContextRunResponse = await response.json();
        if (!controller.signal.aborted) {
          setRun(data.snapshots.length ? { status: "success", data } : { status: "not_found" });
        }
      } catch (error) {
        if (!controller.signal.aborted) {
          setRun({ status: "error", message: `快照加载失败：${error instanceof Error ? error.message : "未知错误"}` });
        }
      }
    }
    void load();
    return () => controller.abort();
  }, [runUrl, attempt]);

  useEffect(() => {
    const pending = requests.current;
    return () => {
      for (const controller of pending.values()) controller.abort();
      pending.clear();
    };
  }, []);

  function selectSnapshot(id: string) {
    setModalItem(null);
    const pending = [...requests.current.keys()];
    for (const controller of requests.current.values()) controller.abort();
    requests.current.clear();
    setDetails(previous => {
      const next = new Map(previous);
      for (const key of pending) next.delete(key);
      return next;
    });
    setSnapshotId(id);
  }

  async function loadDetail(activeSnapshotId: string, itemId: string) {
    const key = JSON.stringify([activeSnapshotId, itemId]);
    if (details.get(key)?.status === "success" || requests.current.has(key)) return;
    const controller = new AbortController();
    requests.current.set(key, controller);
    setDetails(previous => new Map(previous).set(key, { status: "loading" }));

    let detail: DetailState;
    try {
      const query = new URLSearchParams({ snapshot_id: activeSnapshotId });
      const response = await apiFetch(`${runUrl}/items/${encodeURIComponent(itemId)}?${query}`, {
        signal: controller.signal,
        cache: "no-store",
      });
      if (response.status === 404) throw new Error("快照或上下文项已不可用");
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data: ContextItemDetailResponse = await response.json();
      if (data.snapshot_id !== activeSnapshotId || data.item_id !== itemId) {
        throw new Error("服务器返回了其他上下文项");
      }
      detail = { status: "success", data };
    } catch (error) {
      detail = { status: "error", message: `内容加载失败：${error instanceof Error ? error.message : "未知错误"}` };
    }
    if (requests.current.get(key) !== controller) return;
    requests.current.delete(key);
    setDetails(previous => new Map(previous).set(key, detail));
  }

  if (run.status === "loading") return <p role="status" className="py-16 text-center text-faint">正在加载上下文快照…</p>;
  if (run.status === "error") return (
    <div className="py-16 text-center">
      <p role="alert" className="mb-2 text-danger">{run.message}</p>
      <button type="button" className="text-primary hover:underline" onClick={() => {
        setRun({ status: "loading" });
        setAttempt(previous => previous + 1);
      }}>重试</button>
    </div>
  );
  if (run.status !== "success") return (
    <p className="py-16 text-center text-faint">未找到上下文快照（{traceId.slice(0, 8)}）</p>
  );

  const { snapshots } = run.data;
  const snapshot = snapshots.find(entry => entry.snapshot_id === snapshotId) ?? snapshots[0];
  const { context_items: items, injected_memory_ids: injected } = snapshot;
  const modalKey = modalItem ? JSON.stringify([snapshot.snapshot_id, modalItem.item_id]) : "";

  return (
    <div>
      <h2 className="mb-1 text-lg font-semibold text-content">上下文快照</h2>
      <p className="mb-4 break-all font-mono text-xs text-faint">Trace ID {traceId}</p>
      {snapshots.length > 1 && (
        <label className="mb-4 flex flex-wrap items-center gap-2 text-sm text-code">
          快照
          <select value={snapshot.snapshot_id} onChange={event => selectSnapshot(event.target.value)}
            className="min-w-0 rounded-lg border border-divider bg-surface p-2">
            {snapshots.map(entry => (
              <option key={entry.snapshot_id} value={entry.snapshot_id}>
                {new Date(entry.created_at).toLocaleString()} · {entry.snapshot_id.slice(0, 8)}
              </option>
            ))}
          </select>
        </label>
      )}
      <dl className="mb-5 grid grid-cols-2 gap-3 sm:grid-cols-4">
        {[
          { label: "前缀哈希", value: snapshot.stable_prefix_hash || "—" },
          { label: "上下文项", value: items.length },
          { label: "预估 Token", value: `~${snapshot.token_total}` },
          { label: "注入记忆", value: `${injected.length} 条` },
        ].map(field => (
          <div key={field.label} className="min-w-0 rounded-lg border border-divider bg-surface p-3 text-center">
            <dt className="mb-0.5 text-xs text-faint">{field.label}</dt>
            <dd className="truncate font-mono text-sm text-title" title={String(field.value)}>{field.value}</dd>
          </div>
        ))}
      </dl>
      {injected.length > 0 && (
        <div className="mb-4">
          <h3 className="mb-2 text-sm font-medium text-code">注入的记忆</h3>
          <ul className="flex flex-wrap gap-1">
            {injected.map(memory => (
              <li key={memory.memory_id} title={memory.memory_id}
                className="rounded bg-primary-soft px-2 py-0.5 font-mono text-xs text-primary-hover">
                {memory.memory_type && `[${memory.memory_type}] `}
                {memory.canonical_key || memory.memory_id.slice(0, 8)}
              </li>
            ))}
          </ul>
        </div>
      )}
      <h3 className="mb-3 text-sm font-medium text-code">上下文项</h3>
      {items.length === 0 && <p className="text-sm text-faint">无上下文项</p>}
      <ul className="flex flex-col gap-2">
        {items.map(item => {
          const meta = kindMeta(item.kind);
          return (
            <li key={item.item_id} className={`min-w-0 rounded-xl border px-4 py-3 ${meta.border} ${meta.bg}`}>
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <KindBadge kind={item.kind} />
                <dl className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted">
                  {itemMetadata(item).map(field => (
                    <div key={field.label} className="flex gap-1">
                      <dt>{field.label}</dt><dd>{field.value}</dd>
                    </div>
                  ))}
                </dl>
              </div>
              <pre className="mt-2 overflow-x-auto whitespace-pre rounded-lg bg-surface/70 px-3 py-2 font-mono text-xs leading-relaxed text-code">
                {item.content_preview || "（空）"}
              </pre>
              {item.preview_truncated && (item.has_detail ? (
                <button type="button" onClick={() => {
                  setModalItem(item);
                  void loadDetail(snapshot.snapshot_id, item.item_id);
                }} className="mt-2 text-xs text-primary hover:underline">
                  查看更多
                </button>
              ) : (
                <p className="mt-2 text-xs text-faint">此快照仅提供预览</p>
              ))}
            </li>
          );
        })}
      </ul>
      {modalItem && (
        <DetailDialog
          key={modalKey}
          item={modalItem}
          detail={details.get(modalKey) ?? { status: "idle" }}
          onClose={() => setModalItem(null)}
          onRetry={() => void loadDetail(snapshot.snapshot_id, modalItem.item_id)}
        />
      )}
    </div>
  );
}

export default function ContextInspector({ traceId }: { traceId?: string }) {
  if (traceId) return <ContextRun key={traceId} traceId={traceId} />;
  return <p className="py-16 text-center text-sm text-faint">在对话页面点击 Trace ID 查看上下文快照</p>;
}
