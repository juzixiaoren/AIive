/**
 * 能力页面：展示已安装能力和 ToolRegistry 的真实安全元数据。
 */
import { useEffect, useState } from "react";

type InstalledCapability = {
  capability_id: string;
  name: string;
  state: string;
  descriptor_hash: string;
  created_at: string;
};

type RegisteredTool = {
  capability_id: string;
  risk_level: string;
  requires_confirmation: boolean;
  writes_external_world: boolean;
  definition_source: string;
  definition_trust_level: string;
  descriptor_hash: string;
};

const RISK_STYLES: Record<string, string> = {
  low: "bg-success-soft text-success-text border-success-border",
  medium: "bg-warning-soft text-warning-text border-warning-border",
  high: "bg-warning-soft text-warning-text border-warning-border",
  critical: "bg-danger-soft text-danger-text border-danger-border",
};

function formatInstalledAt(value: string): string {
  if (!value) return "-";
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.getTime()) ? "-" : timestamp.toLocaleString();
}

export default function CapabilitiesPage() {
  const [installed, setInstalled] = useState<InstalledCapability[]>([]);
  const [tools, setTools] = useState<RegisteredTool[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      fetch("api/mcp/capabilities").then(response => {
        if (!response.ok) throw new Error(`能力 HTTP ${response.status}`);
        return response.json() as Promise<InstalledCapability[]>;
      }),
      fetch("api/tools").then(response => {
        if (!response.ok) throw new Error(`工具 HTTP ${response.status}`);
        return response.json() as Promise<RegisteredTool[]>;
      }),
    ]).then(([capabilities, registeredTools]) => {
      setInstalled(capabilities);
      setTools(registeredTools);
    }).catch(err => {
      setError(`加载能力失败: ${err instanceof Error ? err.message : "未知错误"}`);
    }).finally(() => setLoading(false));
  }, []);

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-lg font-semibold text-content">能力</h2>
        <p className="text-xs text-faint mt-1">只读展示已安装能力和 ToolRegistry 中的安全声明；安装与激活通过 Chat 和真实冒烟流程完成。</p>
      </div>
      {error && <div className="bg-danger-soft border border-danger-border text-danger-text rounded-lg px-3 py-2 text-sm">{error}</div>}
      {loading && <div className="text-center text-faint py-10">正在加载能力…</div>}

      {!loading && (
        <>
          <section>
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-sm font-medium text-title">已安装能力</h3>
              <span className="text-xs text-faint">{installed.length} 项</span>
            </div>
            {installed.length === 0 ? (
              <div className="bg-surface border border-divider rounded-xl py-8 text-center text-faint text-sm">暂无已安装 MCP 能力</div>
            ) : (
              <div className="space-y-3">
                {installed.map(capability => (
                  <article key={capability.capability_id} className="bg-surface border border-divider rounded-xl p-4 shadow-sm">
                    <div className="flex items-center gap-2">
                      <span className="font-medium text-content">{capability.name}</span>
                      <span className="text-[11px] px-2 py-0.5 rounded-full bg-success-soft text-success-text border border-success-border">{capability.state}</span>
                    </div>
                    <p className="text-xs font-mono text-muted mt-1">{capability.capability_id}</p>
                    <p className="text-[11px] text-faint mt-2" title={capability.created_at || undefined}>安装于: {formatInstalledAt(capability.created_at)}</p>
                    <p className="text-[11px] font-mono text-faint mt-1 break-all">哈希: {capability.descriptor_hash}</p>
                  </article>
                ))}
              </div>
            )}
          </section>

          <section>
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-sm font-medium text-title">已注册工具</h3>
              <span className="text-xs text-faint">{tools.length} 项</span>
            </div>
            <div className="space-y-3">
              {tools.map(tool => (
                <article key={tool.capability_id} className="bg-surface border border-divider rounded-xl p-4 shadow-sm">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-sm font-semibold text-content">{tool.capability_id}</span>
                    <span className={`text-[11px] px-2 py-0.5 rounded-full border ${RISK_STYLES[tool.risk_level] || "bg-surface-muted text-code border-divider"}`}>{tool.risk_level} 风险</span>
                    {tool.requires_confirmation && <span className="text-[11px] px-2 py-0.5 rounded-full bg-danger-soft text-danger-hover border border-danger-border">需确认</span>}
                    {tool.writes_external_world && <span className="text-[11px] px-2 py-0.5 rounded-full bg-warning-soft text-warning-text border border-warning-border">写外部</span>}
                  </div>
                  <div className="flex flex-wrap gap-4 text-xs text-muted mt-2">
                    <span>来源: {tool.definition_source}</span>
                    <span>信任: {tool.definition_trust_level}</span>
                  </div>
                  <p className="text-[11px] font-mono text-faint mt-2 break-all">哈希: {tool.descriptor_hash}</p>
                </article>
              ))}
            </div>
          </section>
        </>
      )}
    </div>
  );
}
