import { useEffect, useState } from "react";

const RISK_LABELS: Record<string, string> = { low: "低", medium: "中", high: "高", critical: "严重" };
const RISK_STYLES: Record<string, string> = {
  low: "bg-emerald-50 text-emerald-700 border-emerald-200",
  medium: "bg-amber-50 text-amber-700 border-amber-200",
  high: "bg-orange-50 text-orange-700 border-orange-200",
  critical: "bg-red-50 text-red-700 border-red-200",
};

export default function ToolsPage() {
  const [tools, setTools] = useState<Array<Record<string, unknown>>>([]);

  useEffect(() => {
    fetch("/api/tools").then(r => r.json()).then(setTools);
  }, []);

  return (
    <div>
      <h2 className="text-lg font-semibold text-slate-800 mb-4">工具与能力</h2>
      {tools.length === 0 && <div className="text-center text-slate-400 text-sm py-12">暂无注册工具</div>}
      <div className="flex flex-col gap-3">
        {tools.map(t => (
          <div key={String(t.capability_id)} className="bg-white border border-slate-200 rounded-xl p-4 shadow-sm">
            <div className="flex gap-2 items-center mb-2 flex-wrap">
              <span className="font-mono text-sm font-semibold text-slate-800">{String(t.capability_id)}</span>
              <span className={`text-[11px] px-2 py-0.5 rounded-full border font-medium ${RISK_STYLES[String(t.risk_level)] || ""}`}>
                {RISK_LABELS[String(t.risk_level)] || String(t.risk_level)} 风险
              </span>
              {Boolean(t.requires_confirmation) && (
                <span className="text-[11px] bg-purple-50 text-purple-600 border border-purple-200 px-2 py-0.5 rounded-full">需确认</span>
              )}
              {Boolean(t.writes_external_world) && (
                <span className="text-[11px] bg-orange-50 text-orange-600 border border-orange-200 px-2 py-0.5 rounded-full">写外部</span>
              )}
            </div>
            <div className="flex gap-4 text-xs text-slate-500 mb-1">
              <span>来源: {String(t.definition_source)}</span>
              <span>信任: {String(t.definition_trust_level)}</span>
            </div>
            <p className="text-[11px] text-slate-400 font-mono">哈希: {String(t.descriptor_hash)}</p>
          </div>
        ))}
      </div>
    </div>
  );
}
