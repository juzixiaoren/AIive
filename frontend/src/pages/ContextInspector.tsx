/**
 * 上下文检查器页面
 * - 根据 trace_id 查询并展示单次对话的完整上下文快照
 * - 展示上下文项列表（系统前缀、工具列表、注入记忆、历史消息、当前消息等）
 * - 支持展开/折叠每个上下文项查看详情
 * - 支持弹窗查看完整内容
 */

import { useEffect, useState } from "react";

/** 上下文项数据结构 */
type ContextItem = {
  item_id: string;
  kind: string;
  source: string;
  trust_level: string;
  content_preview: string;
  token_estimate: number;
};

/** 注入记忆条目数据结构 */
type InjectedMemory = {
  memory_id: string;
  canonical_key: string;
  memory_type: string;
};

type ContextSnapshot = {
  stable_prefix_hash: string;
  context_items: ContextItem[];
  meta: Record<string, unknown>;
  token_total?: number;
};

type ContextRunResponse = {
  trace_id: string;
  snapshots: ContextSnapshot[];
};

type ContextItemDetailResponse = {
  item_id: string;
  full_content: string;
};

type LoadState = "idle" | "loading" | "ready" | "not_found" | "error";

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
  memory_store: "记忆库",
  user: "用户",
  runtime_event: "运行时事件",
  system_command: "系统指令",
};

/**
 * 上下文检查器组件
 * @param traceId - 要检查的 trace ID，由外部传入
 */
export default function ContextInspector({ traceId }: { traceId?: string }) {
  const [data, setData] = useState<ContextRunResponse | null>(null);
  const [loadState, setLoadState] = useState<LoadState>("idle");
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState<number | null>(null);
  const [modalItem, setModalItem] = useState<ContextItem | null>(null);
  /** 懒加载的完整内容缓存: item_id → full_content */
  const [fullContents, setFullContents] = useState<Record<string, string>>({});
  const [loadingItem, setLoadingItem] = useState<string | null>(null);

  // "查看更多" modal 打开时触发懒加载完整内容
  useEffect(() => {
    if (modalItem && !fullContents[modalItem.item_id]) {
      fetchDetail(modalItem.item_id);
    }
  }, [modalItem?.item_id]); // eslint-disable-line react-hooks/exhaustive-deps

  // 当 traceId 变化时，从后端加载上下文快照
  useEffect(() => {
    const controller = new AbortController();
    setData(null);
    setError("");
    setExpanded(null);
    setModalItem(null);
    setFullContents({});
    setLoadingItem(null);
    if (!traceId) {
      setLoadState("idle");
      return () => controller.abort();
    }

    setLoadState("loading");
    fetch(`/api/context-runs/${encodeURIComponent(traceId)}`, { signal: controller.signal })
      .then(async response => {
        if (response.status === 404) {
          setLoadState("not_found");
          return null;
        }
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json() as Promise<ContextRunResponse>;
      })
      .then(result => {
        if (result) {
          setData(result);
          setLoadState("ready");
        }
      })
      .catch(reason => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setError(`加载失败: ${reason instanceof Error ? reason.message : "未知错误"}`);
        setLoadState("error");
      });
    return () => controller.abort();
  }, [traceId]);

  /** 懒加载某条上下文项的完整内容 */
  const fetchDetail = (itemId: string) => {
    if (fullContents[itemId] !== undefined || loadingItem) return;
    setLoadingItem(itemId);
    fetch(`/api/context-runs/${encodeURIComponent(traceId || "")}/items/${encodeURIComponent(itemId)}`)
      .then(async response => {
        if (response.status === 404) throw new Error("上下文项不存在");
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json() as Promise<ContextItemDetailResponse>;
      })
      .then(response => {
        setFullContents(prev => ({ ...prev, [itemId]: response.full_content || "(空)" }));
      })
      .catch(reason => {
        setFullContents(prev => ({
          ...prev,
          [itemId]: `加载失败: ${reason instanceof Error ? reason.message : "未知错误"}`,
        }));
      })
      .finally(() => setLoadingItem(null));
  };

  /** 展开项的处理：展开不自动加载，点击"查看更多"才触发懒加载 */
  const handleExpand = (i: number, _item: ContextItem) => {
    if (expanded === i) {
      setExpanded(null);
      return;
    }
    setExpanded(i);
  };

  // 无 traceId 时显示引导提示
  if (!traceId) return (
    <div className="text-center text-faint py-16">
      <p className="text-lg mb-2">🔍 上下文检查器</p>
      <p className="text-sm">在对话页面点击 trace_id 即可查看该次对话的完整上下文</p>
    </div>
  );

  if (loadState === "loading") return <div className="text-center text-faint py-16">正在加载上下文快照…</div>;
  if (loadState === "error") return <div className="text-center text-danger py-16">{error}</div>;
  if (loadState === "not_found") return <div className="text-center text-faint py-16">
    <p className="text-sm">未找到上下文快照: {traceId.slice(0, 8)}…</p>
    <p className="text-xs mt-2 text-subtle">该 trace 可能不存在，或尚未生成上下文快照</p>
  </div>;
  if (!data || data.snapshots.length === 0) return null;

  const snapshots = data.snapshots;
  const items = snapshots[0].context_items || [];
  const meta = snapshots[0].meta || {};
  const injected = (meta.injected_memory_ids as InjectedMemory[]) || [];
  const tokenTotal = snapshots[0]?.token_total as number | undefined;

  return (
    <div>
      <h2 className="text-lg font-semibold text-content mb-1">上下文快照</h2>
      <p className="text-xs text-faint mb-4 font-mono break-all">trace_id: {traceId}</p>

      {/* 统计卡片区域 */}
      <div className="grid grid-cols-4 gap-3 mb-5">
        {[
          { label: "前缀哈希", value: String(snapshots[0]?.stable_prefix_hash || "-") },
          { label: "上下文项", value: String(items.length) },
          { label: "预估 Token", value: tokenTotal ? `~${tokenTotal}` : "-" },
          { label: "注入记忆", value: `${injected.length} 条` },
        ].map(c => (
          <div key={c.label} className="bg-surface border border-divider rounded-lg p-3 text-center shadow-sm">
            <div className="text-xs text-faint mb-0.5">{c.label}</div>
            <div className="text-sm font-mono text-title truncate">{c.value}</div>
          </div>
        ))}
      </div>

      {/* 注入的记忆 ID 列表 */}
      {injected.length > 0 && (
        <div className="mb-4">
          <h3 className="text-sm font-medium text-code mb-2">注入的记忆</h3>
          <div className="flex flex-wrap gap-1">
            {injected.map((m: InjectedMemory) => (
              <span
                key={m.memory_id}
                title={m.memory_id}
                className="text-[11px] bg-primary-soft text-primary-hover px-2 py-0.5 rounded font-mono inline-flex items-center gap-1"
              >
                {m.memory_type && <span className="opacity-70">[{m.memory_type}]</span>}
                <span>{m.canonical_key || m.memory_id.slice(0, 8)}</span>
              </span>
            ))}
          </div>
        </div>
      )}

      {/* 上下文项列表 */}
      <h3 className="text-sm font-medium text-code mb-3">
        上下文项 ({items.length})
        <span className="text-xs text-faint ml-2 font-normal">点击卡片展开详情</span>
      </h3>
      {items.length === 0 ? (
        <p className="text-sm text-faint">无上下文项</p>
      ) : (
        <div className="flex flex-col gap-2">
          {items.map((item, i) => {
            const kindMeta = KIND_META[item.kind] || { label: item.kind, bg: "bg-background", text: "text-code", border: "border-divider" };
            const isOpen = expanded === i;

            return (
              <div
                key={i}
                onClick={() => handleExpand(i, item)}
                className={`
                  border rounded-xl cursor-pointer transition-all duration-200 shadow-sm hover:shadow-md
                  ${kindMeta.border} ${kindMeta.bg}
                  ${isOpen ? "ring-2 ring-subtle" : ""}
                `}
              >
                {/* 折叠状态：摘要头部 */}
                <div className="px-4 py-3 flex items-center gap-3 select-none">
                  <span className={`text-xs font-medium px-2.5 py-0.5 rounded-full ${kindMeta.text} ${kindMeta.bg} border ${kindMeta.border} shrink-0`}>
                    {kindMeta.label}
                  </span>
                  <span className="text-xs text-code truncate flex-1 min-w-0">
                    {(item.content_preview || "").slice(0, 120)}
                  </span>
                  <span className="text-[11px] text-faint font-mono shrink-0">~{item.token_estimate}t</span>
                  <svg
                    className={`w-4 h-4 text-faint shrink-0 transition-transform duration-200 ${isOpen ? "rotate-180" : ""}`}
                    fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}
                  >
                    <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
                  </svg>
                </div>

                {/* 展开状态：详细信息 */}
                {isOpen && (
                  <div className="px-4 pb-4 border-t border-divider/60">
                    {/* 元数据网格 */}
                    <div className="grid grid-cols-4 gap-2 mt-3 mb-3">
                      {[
                        { label: "类型", value: kindMeta.label },
                        { label: "来源", value: SOURCE_LABEL[item.source] || item.source },
                        { label: "信任级别", value: TRUST_LABEL[item.trust_level] || item.trust_level },
                        { label: "Token", value: `~${item.token_estimate}` },
                      ].map(f => (
                        <div key={f.label} className="bg-surface/60 rounded-lg px-2.5 py-1.5">
                          <div className="text-[10px] text-faint uppercase">{f.label}</div>
                          <div className="text-xs text-title font-medium truncate">{f.value}</div>
                        </div>
                      ))}
                    </div>

                    {/* Item ID */}
                    <div className="mb-2">
                      <div className="text-[10px] text-faint uppercase mb-0.5">Item ID</div>
                      <code className="text-[11px] bg-surface/60 text-muted px-2 py-0.5 rounded font-mono break-all block">
                        {item.item_id}
                      </code>
                    </div>

                    {/* 内容预览 / 完整内容 */}
                    <div className="mb-0.5">
                      <div className="text-[10px] text-faint uppercase mb-1">
                        {fullContents[item.item_id] !== undefined ? "完整内容" : "内容预览"}
                        {loadingItem === item.item_id && <span className="ml-1 animate-pulse">加载中...</span>}
                      </div>
                      <pre className="text-xs text-code bg-surface/70 rounded-lg px-3 py-2 whitespace-pre-wrap break-all font-mono leading-relaxed max-h-60 overflow-auto">
                        {fullContents[item.item_id] !== undefined
                          ? fullContents[item.item_id]
                          : item.content_preview || "(空)"}
                      </pre>
                    </div>

                    {/* "查看更多" 链接 */}
                    <div
                      className="text-xs text-primary hover:text-primary-hover cursor-pointer mt-1 select-none"
                      onClick={(e) => { e.stopPropagation(); setModalItem(item); }}
                    >
                      点击查看更多 →
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* 居中全量内容弹窗 */}
      {modalItem && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-6">
          {/* 背景遮罩 */}
          <div
            className="absolute inset-0 bg-content/40 backdrop-blur-sm"
            onClick={() => setModalItem(null)}
          />

          {/* 弹窗主体 */}
          <div className="relative w-full max-w-3xl max-h-[70vh] bg-surface rounded-2xl shadow-2xl flex flex-col animate-fade-in">
            {/* 弹窗头部 */}
            <div className="shrink-0 px-5 py-3 border-b border-surface-muted flex items-center justify-between">
              <div className="flex items-center gap-2 min-w-0">
                {(() => {
                  const m = KIND_META[modalItem.kind] || { label: modalItem.kind, bg: "bg-background", text: "text-code", border: "border-divider" };
                  return (
                    <span className={`text-xs font-medium px-2.5 py-0.5 rounded-full ${m.text} ${m.bg} border ${m.border} shrink-0`}>
                      {m.label}
                    </span>
                  );
                })()}
                <span className="text-sm font-semibold text-content truncate">{modalItem.item_id}</span>
              </div>
              <button
                onClick={() => setModalItem(null)}
                className="p-1.5 rounded-lg hover:bg-surface-muted text-faint hover:text-code transition-colors shrink-0"
              >
                <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>

            {/* 弹窗元数据 */}
            <div className="shrink-0 px-5 py-3">
              <div className="grid grid-cols-4 gap-2">
                {[
                  { label: "来源", value: SOURCE_LABEL[modalItem.source] || modalItem.source },
                  { label: "信任级别", value: TRUST_LABEL[modalItem.trust_level] || modalItem.trust_level },
                  { label: "Token", value: `~${modalItem.token_estimate}` },
                  { label: "长度", value: `${(modalItem.content_preview || "").length} 字符` },
                ].map(f => (
                  <div key={f.label} className="bg-background rounded-lg px-2.5 py-1.5">
                    <div className="text-[10px] text-faint uppercase">{f.label}</div>
                    <div className="text-xs text-title font-medium truncate">{f.value}</div>
                  </div>
                ))}
              </div>
            </div>

            {/* 可滚动的完整内容区域 */}
            <div className="flex-1 overflow-y-auto px-5 pb-6">
              {loadingItem === modalItem.item_id ? (
                <div className="text-sm text-faint py-8 text-center animate-pulse">加载中...</div>
              ) : (
                <pre className={`text-xs whitespace-pre-wrap break-all font-mono leading-relaxed ${
                  (fullContents[modalItem.item_id] || "").startsWith("错误") || (fullContents[modalItem.item_id] || "").startsWith("加载失败")
                    ? "text-danger"
                    : "text-title"
                }`}>
                  {fullContents[modalItem.item_id] !== undefined
                    ? fullContents[modalItem.item_id]
                    : modalItem.content_preview || "(空)"}
                </pre>
              )}
            </div>
          </div>
        </div>
      )}

    </div>
  );
}
