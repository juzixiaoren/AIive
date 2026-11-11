import { useState } from "react";
import type { ToolCallItem } from "./types";

/**
 * 单条工具调用的展示卡片
 * - pending：旋转 spinner，表示正在执行（同时作为占位，避免结果到达前抖动）
 * - completed / failed / cancelled：状态 chip + 可折叠的参数与结果
 */
export default function ToolCallCard({ call }: { call: ToolCallItem }) {
  const [open, setOpen] = useState(false);

  const statusMeta = {
    pending: {
      label: "执行中",
      chip: "bg-primary-soft text-primary-border border-primary-border",
      icon: <span className="w-3.5 h-3.5 rounded-full border-2 border-primary-ring border-t-primary animate-spin" />,
    },
    completed: {
      label: "完成",
      chip: "bg-success-soft text-success-text border-success-border",
      icon: (
        <svg className="w-3.5 h-3.5 text-success-text" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7" />
        </svg>
      ),
    },
    failed: {
      label: "失败",
      chip: "bg-danger-soft text-danger-hover border-danger-border",
      icon: (
        <svg className="w-3.5 h-3.5 text-danger" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M6 18L18 6M6 6l12 12" />
        </svg>
      ),
    },
    execution_unknown: {
      label: "待确认",
      chip: "bg-warning-soft text-warning-text border-warning-border",
      icon: <span className="w-3.5 h-3.5 rounded-full border-2 border-warning-border border-t-transparent animate-spin" />,
    },
    cancelled: {
      label: "已停止",
      chip: "bg-surface-muted text-muted border-divider",
      icon: (
        <svg className="w-3.5 h-3.5 text-faint" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M6 18L18 6M6 6l12 12" />
        </svg>
      ),
    },
  }[call.status];

  const paramStr = call.params ? JSON.stringify(call.params) : "";
  const resultStr = call.result ?? "";

  return (
    <div className="animate-tool-in rounded-xl border border-divider bg-surface px-3 py-2 shadow-sm">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center gap-2 text-left focus:outline-none"
      >
        <span className="shrink-0 flex items-center justify-center w-4 h-4">{statusMeta.icon}</span>
        <span className="text-sm font-medium text-title truncate">{call.name}</span>
        <span className={`ml-auto text-[11px] px-2 py-0.5 rounded-full border whitespace-nowrap ${statusMeta.chip}`}>
          {statusMeta.label}
        </span>
        <svg
          className={`w-3.5 h-3.5 text-faint transition-transform duration-200 ${open ? "rotate-180" : ""}`}
          fill="none" stroke="currentColor" viewBox="0 0 24 24"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>
      {open && (
        <div className="mt-2 flex flex-col gap-1.5 animate-fade-in">
          <div>
            <div className="text-[11px] font-medium text-faint mb-0.5">参数</div>
            <pre className="text-[11px] text-code bg-background rounded-lg px-2.5 py-1.5 whitespace-pre-wrap break-all font-mono max-h-40 overflow-auto">
              {paramStr ? (paramStr.length > 400 ? paramStr.slice(0, 400) + "…" : paramStr) : "（无）"}
            </pre>
          </div>
          {call.result !== undefined && (
            <div>
              <div className="text-[11px] font-medium text-faint mb-0.5">结果</div>
              <pre className="text-[11px] text-code bg-background rounded-lg px-2.5 py-1.5 whitespace-pre-wrap break-all font-mono max-h-48 overflow-auto">
                {resultStr.length > 600 ? resultStr.slice(0, 600) + "…" : resultStr}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * 聊天页面组件
 * @param onInspectTrace - 点击 trace_id 时回调，用于跳转到上下文检查器
 */
