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

/** 上下文类型的中文标签和样式映射 */
const KIND_META: Record<string, { label: string; bg: string; text: string; border: string }> = {
  stable_prefix:   { label: "系统前缀",  bg: "bg-purple-50", text: "text-purple-700", border: "border-purple-200" },
  tool_schemas:    { label: "工具列表",  bg: "bg-teal-50",    text: "text-teal-700",    border: "border-teal-200" },
  evidence_memory: { label: "注入记忆",  bg: "bg-amber-50",   text: "text-amber-700",   border: "border-amber-200" },
  history_message: { label: "历史消息",  bg: "bg-blue-50",    text: "text-blue-700",    border: "border-blue-200" },
  user_message:    { label: "当前消息",  bg: "bg-rose-50",    text: "text-rose-700",    border: "border-rose-200" },
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
};

/**
 * 上下文检查器组件
 * @param traceId - 要检查的 trace ID，由外部传入
 */
export default function ContextInspector({ traceId }: { traceId?: string }) {
  const [data, setData] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState<number | null>(null);
  const [modalItem, setModalItem] = useState<ContextItem | null>(null);

  // 当 traceId 变化时，从后端加载上下文快照
  useEffect(() => {
    if (traceId) {
      setError("");
      setExpanded(null);
      setModalItem(null);
      fetch(`/api/context-runs/${traceId}`)
        .then(r => { if (!r.ok) throw new Error(`${r.status}`); return r.json(); })
        .then(setData)
        .catch(e => setError(`加载失败: ${e.message}`));
    }
  }, [traceId]);

  // 无 traceId 时显示引导提示
  if (!traceId) return (
    <div className="text-center text-slate-400 py-16">
      <p className="text-lg mb-2">🔍 上下文检查器</p>
      <p className="text-sm">在对话页面点击 trace_id 即可查看该次对话的完整上下文</p>
    </div>
  );

  if (error) return <div className="text-center text-red-500 py-16">{error}</div>;
  if (!data || (data as Record<string, unknown>).error) return <div className="text-center text-slate-400 py-16">
    <p className="text-sm">未找到 trace 记录: {traceId.slice(0, 8)}…</p>
    <p className="text-xs mt-2 text-slate-300">请确保已经产生过对话</p>
  </div>;

  const snapshots = (data.snapshots || []) as Array<Record<string, unknown>>;
  const items = (snapshots[0]?.context_items as ContextItem[]) || [];
  const meta = (snapshots[0]?.meta || {}) as Record<string, unknown>;
  const injected = (meta.injected_memory_ids as string[]) || [];
  const llm_calls = (data.llm_calls || []) as Array<Record<string, unknown>>;

  return (
    <div>
      <h2 className="text-lg font-semibold text-slate-800 mb-1">上下文快照</h2>
      <p className="text-xs text-slate-400 mb-4 font-mono break-all">trace_id: {traceId}</p>

      {/* 统计卡片区域 */}
      <div className="grid grid-cols-4 gap-3 mb-5">
        {[
          { label: "前缀哈希", value: String(snapshots[0]?.stable_prefix_hash || "-") },
          { label: "上下文项", value: String(items.length) },
          { label: "预估 Token", value: `~${meta.total_tokens || "-"}` },
          { label: "注入记忆", value: `${injected.length} 条` },
        ].map(c => (
          <div key={c.label} className="bg-white border border-slate-200 rounded-lg p-3 text-center shadow-sm">
            <div className="text-xs text-slate-400 mb-0.5">{c.label}</div>
            <div className="text-sm font-mono text-slate-700 truncate">{c.value}</div>
          </div>
        ))}
      </div>

      {/* LLM 调用（完整输入/输出） */}
      {llm_calls.length > 0 && (
        <div className="mb-5">
          <h3 className="text-sm font-medium text-slate-600 mb-3">
            LLM 调用 ({llm_calls.length})
            <span className="text-xs text-slate-400 ml-2 font-normal">如实记录每次模型输入与输出</span>
          </h3>
          <div className="flex flex-col gap-3">
            {llm_calls.map((c, i) => (
              <div key={i} className="border border-slate-200 rounded-xl shadow-sm overflow-hidden">
                <div className="px-4 py-2 bg-slate-50 flex items-center gap-3">
                  <span className="text-xs font-medium px-2 py-0.5 rounded-full bg-slate-200 text-slate-700">#{i + 1}</span>
                  <code className="text-xs text-slate-600 font-mono">{String(c.model || "-")}</code>
                  <span className="text-[11px] text-slate-400">{c.latency_ms} ms</span>
                </div>
                <div className="px-4 py-3 grid grid-cols-1 gap-3">
                  <div>
                    <div className="text-[10px] text-slate-400 uppercase mb-1">输入（完整消息）</div>
                    <pre className="text-[11px] text-slate-600 bg-slate-50 rounded-lg px-3 py-2 whitespace-pre-wrap break-all font-mono leading-relaxed max-h-60 overflow-auto">
                      {String(c.input_preview || "(空)")}
                    </pre>
                  </div>
                  <div>
                    <div className="text-[10px] text-slate-400 uppercase mb-1">输出（内容 + 工具调用）</div>
                    <pre className="text-[11px] text-slate-600 bg-slate-50 rounded-lg px-3 py-2 whitespace-pre-wrap break-all font-mono leading-relaxed max-h-60 overflow-auto">
                      {String(c.output_preview || "(空)")}
                    </pre>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 注入的记忆 ID 列表 */}
      {injected.length > 0 && (
        <div className="mb-4">
          <h3 className="text-sm font-medium text-slate-600 mb-2">注入的记忆</h3>
          <div className="flex flex-wrap gap-1">
            {injected.map((id: string) => (
              <code key={id} className="text-[11px] bg-blue-50 text-blue-600 px-2 py-0.5 rounded font-mono">{id.slice(0, 8)}</code>
            ))}
          </div>
        </div>
      )}

      {/* 上下文项列表 */}
      <h3 className="text-sm font-medium text-slate-600 mb-3">
        上下文项 ({items.length})
        <span className="text-xs text-slate-400 ml-2 font-normal">点击卡片展开详情</span>
      </h3>
      {items.length === 0 ? (
        <p className="text-sm text-slate-400">无上下文项</p>
      ) : (
        <div className="flex flex-col gap-2">
          {items.map((item, i) => {
            const kindMeta = KIND_META[item.kind] || { label: item.kind, bg: "bg-slate-50", text: "text-slate-600", border: "border-slate-200" };
            const isOpen = expanded === i;

            return (
              <div
                key={i}
                onClick={() => setExpanded(isOpen ? null : i)}
                className={`
                  border rounded-xl cursor-pointer transition-all duration-200 shadow-sm hover:shadow-md
                  ${kindMeta.border} ${kindMeta.bg}
                  ${isOpen ? "ring-2 ring-slate-300" : ""}
                `}
              >
                {/* 折叠状态：摘要头部 */}
                <div className="px-4 py-3 flex items-center gap-3 select-none">
                  <span className={`text-xs font-medium px-2.5 py-0.5 rounded-full ${kindMeta.text} ${kindMeta.bg} border ${kindMeta.border} shrink-0`}>
                    {kindMeta.label}
                  </span>
                  <span className="text-xs text-slate-600 truncate flex-1 min-w-0">
                    {(item.content_preview || "").slice(0, 120)}
                  </span>
                  <span className="text-[11px] text-slate-400 font-mono shrink-0">~{item.token_estimate}t</span>
                  <svg
                    className={`w-4 h-4 text-slate-400 shrink-0 transition-transform duration-200 ${isOpen ? "rotate-180" : ""}`}
                    fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}
                  >
                    <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
                  </svg>
                </div>

                {/* 展开状态：详细信息 */}
                {isOpen && (
                  <div className="px-4 pb-4 border-t border-slate-200/60">
                    {/* 元数据网格 */}
                    <div className="grid grid-cols-4 gap-2 mt-3 mb-3">
                      {[
                        { label: "类型", value: kindMeta.label },
                        { label: "来源", value: SOURCE_LABEL[item.source] || item.source },
                        { label: "信任级别", value: TRUST_LABEL[item.trust_level] || item.trust_level },
                        { label: "Token", value: `~${item.token_estimate}` },
                      ].map(f => (
                        <div key={f.label} className="bg-white/60 rounded-lg px-2.5 py-1.5">
                          <div className="text-[10px] text-slate-400 uppercase">{f.label}</div>
                          <div className="text-xs text-slate-700 font-medium truncate">{f.value}</div>
                        </div>
                      ))}
                    </div>

                    {/* Item ID */}
                    <div className="mb-2">
                      <div className="text-[10px] text-slate-400 uppercase mb-0.5">Item ID</div>
                      <code className="text-[11px] bg-white/60 text-slate-500 px-2 py-0.5 rounded font-mono break-all block">
                        {item.item_id}
                      </code>
                    </div>

                    {/* 内容预览（限制 2 行） */}
                    <div className="mb-0.5">
                      <div className="text-[10px] text-slate-400 uppercase mb-1">内容预览</div>
                      <pre className="text-xs text-slate-600 bg-white/70 rounded-lg px-3 py-2 whitespace-pre-wrap break-all font-mono leading-relaxed line-clamp-2">
                        {item.content_preview || "(空)"}
                      </pre>
                    </div>

                    {/* "查看更多" 链接 */}
                    <div
                      className="text-xs text-blue-500 hover:text-blue-600 cursor-pointer mt-1 select-none"
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
            className="absolute inset-0 bg-black/40 backdrop-blur-sm"
            onClick={() => setModalItem(null)}
          />

          {/* 弹窗主体 */}
          <div className="relative w-full max-w-3xl max-h-[70vh] bg-white rounded-2xl shadow-2xl flex flex-col animate-fade-in">
            {/* 弹窗头部 */}
            <div className="shrink-0 px-5 py-3 border-b border-slate-100 flex items-center justify-between">
              <div className="flex items-center gap-2 min-w-0">
                {(() => {
                  const m = KIND_META[modalItem.kind] || { label: modalItem.kind, bg: "bg-slate-50", text: "text-slate-600", border: "border-slate-200" };
                  return (
                    <span className={`text-xs font-medium px-2.5 py-0.5 rounded-full ${m.text} ${m.bg} border ${m.border} shrink-0`}>
                      {m.label}
                    </span>
                  );
                })()}
                <span className="text-sm font-semibold text-slate-800 truncate">{modalItem.item_id}</span>
              </div>
              <button
                onClick={() => setModalItem(null)}
                className="p-1.5 rounded-lg hover:bg-slate-100 text-slate-400 hover:text-slate-600 transition-colors shrink-0"
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
                  <div key={f.label} className="bg-slate-50 rounded-lg px-2.5 py-1.5">
                    <div className="text-[10px] text-slate-400 uppercase">{f.label}</div>
                    <div className="text-xs text-slate-700 font-medium truncate">{f.value}</div>
                  </div>
                ))}
              </div>
            </div>

            {/* 可滚动的完整内容区域 */}
            <div className="flex-1 overflow-y-auto px-5 pb-6">
              <pre className="text-xs text-slate-700 whitespace-pre-wrap break-all font-mono leading-relaxed">
                {modalItem.content_preview || "(空)"}
              </pre>
            </div>
          </div>
        </div>
      )}

      {/* 弹窗淡入动画样式 */}
      <style>{`
        @keyframes fade-in {
          from { opacity: 0; transform: scale(0.96); }
          to   { opacity: 1; transform: scale(1); }
        }
        .animate-fade-in {
          animation: fade-in 0.2s ease-out;
        }
      `}</style>
    </div>
  );
}
