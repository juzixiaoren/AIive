/**
 * 工具与能力页面
 * - 展示后端注册的所有工具能力列表
 * - 每个工具卡片包含：能力 ID、风险等级、是否需要确认、是否写外部世界、来源和信任级别
 */

import { useEffect, useState } from "react";

/** 风险等级中文标签映射 */
const RISK_LABELS: Record<string, string> = { low: "低", medium: "中", high: "高", critical: "严重" };
/** 风险等级对应颜色样式 */
const RISK_STYLES: Record<string, string> = {
  low: "bg-success-soft text-success-text border-success-border",
  medium: "bg-warning-soft text-warning-text border-warning-border",
  high: "bg-warning-soft text-warning-text border-warning-border",
  critical: "bg-danger-soft text-danger-text border-danger-border",
};

/**
 * 工具与能力页面组件
 */
export default function ToolsPage() {
  const [tools, setTools] = useState<Array<Record<string, unknown>>>([]);

  // 组件挂载时从后端加载工具列表
  useEffect(() => {
    fetch("/api/tools").then(r => r.json()).then(setTools);
  }, []);

  return (
    <div>
      <h2 className="text-lg font-semibold text-content mb-4">工具与能力</h2>
      {tools.length === 0 && <div className="text-center text-faint text-sm py-12">暂无注册工具</div>}
      <div className="flex flex-col gap-3">
        {tools.map(t => (
          <div key={String(t.capability_id)} className="bg-surface border border-divider rounded-xl p-4 shadow-sm">
            {/* 工具头部：能力 ID + 风险等级 + 特殊标签 */}
            <div className="flex gap-2 items-center mb-2 flex-wrap">
              <span className="font-mono text-sm font-semibold text-content">{String(t.capability_id)}</span>
              <span className={`text-[11px] px-2 py-0.5 rounded-full border font-medium ${RISK_STYLES[String(t.risk_level)] || ""}`}>
                {RISK_LABELS[String(t.risk_level)] || String(t.risk_level)} 风险
              </span>
              {Boolean(t.requires_confirmation) && (
                <span className="text-[11px] bg-accent-soft text-accent-text border border-accent-border px-2 py-0.5 rounded-full">需确认</span>
              )}
              {Boolean(t.writes_external_world) && (
                <span className="text-[11px] bg-warning-soft text-warning border border-warning-border px-2 py-0.5 rounded-full">写外部</span>
              )}
            </div>
            {/* 工具来源和信任级别 */}
            <div className="flex gap-4 text-xs text-muted mb-1">
              <span>来源: {String(t.definition_source)}</span>
              <span>信任: {String(t.definition_trust_level)}</span>
            </div>
            {/* 描述符哈希值 */}
            <p className="text-[11px] text-faint font-mono">哈希: {String(t.descriptor_hash)}</p>
          </div>
        ))}
      </div>
    </div>
  );
}
