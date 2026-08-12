/**
 * 能力页面：展示已安装能力和 ToolRegistry 的真实安全元数据。
 */
import { useEffect, useState } from "react";
import { BASE_PATH } from "../lib/base";

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
  uses_network: boolean;
};

type Skill = {
  skill_id: string;
  name: string;
  description: string;
  version: string;
  status: string;
  capabilities: string[];
  requires_network: boolean;
  supported_formats: string[];
  missing_capabilities: string[];
};

type MCPCandidate = {
  capability_id: string;
  name: string;
  state: string;
  description: string;
  declared_tools: string[];
  homepage: string;
  installable: boolean;
};

type KnowledgeCatalog = {
  documents: { document_id: string; title: string; doc_type: string; status: string; chunks: number }[];
  allowed_roots: string[];
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
  const [skills, setSkills] = useState<Skill[]>([]);
  const [candidates, setCandidates] = useState<MCPCandidate[]>([]);
  const [knowledge, setKnowledge] = useState<KnowledgeCatalog>({ documents: [], allowed_roots: [] });
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      fetch(`${BASE_PATH}api/mcp/capabilities`).then(response => {
        if (!response.ok) throw new Error(`能力 HTTP ${response.status}`);
        return response.json() as Promise<InstalledCapability[]>;
      }),
      fetch(`${BASE_PATH}api/tools`).then(response => {
        if (!response.ok) throw new Error(`工具 HTTP ${response.status}`);
        return response.json() as Promise<RegisteredTool[]>;
      }),
      fetch(`${BASE_PATH}api/skills`).then(response => {
        if (!response.ok) throw new Error(`Skill HTTP ${response.status}`);
        return response.json() as Promise<Skill[]>;
      }),
      fetch(`${BASE_PATH}api/capabilities?state=candidate`).then(response => {
        if (!response.ok) throw new Error(`MCP catalog HTTP ${response.status}`);
        return response.json() as Promise<MCPCandidate[]>;
      }),
      fetch(`${BASE_PATH}api/knowledge`).then(response => {
        if (!response.ok) throw new Error(`知识库 HTTP ${response.status}`);
        return response.json() as Promise<KnowledgeCatalog>;
      }),
    ]).then(([capabilities, registeredTools, builtinSkills, mcpCandidates, knowledgeCatalog]) => {
      setInstalled(capabilities);
      setTools(registeredTools);
      setSkills(builtinSkills);
      setCandidates(mcpCandidates);
      setKnowledge(knowledgeCatalog);
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
              <h3 className="text-sm font-medium text-title">内置 Skills</h3>
              <span className="text-xs text-faint">{skills.length} 项</span>
            </div>
            <div className="grid gap-3 md:grid-cols-2">
              {skills.map(skill => (
                <article key={skill.skill_id} className="bg-surface border border-divider rounded-xl p-4 shadow-sm">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-content">{skill.name}</span>
                    <span className={`text-[11px] px-2 py-0.5 rounded-full border ${skill.status === "ready" ? RISK_STYLES.low : RISK_STYLES.medium}`}>{skill.status}</span>
                    {skill.requires_network && <span className="text-[11px] text-muted">需联网</span>}
                  </div>
                  <p className="text-xs text-muted mt-2">{skill.description}</p>
                  <p className="text-[11px] font-mono text-faint mt-2">{skill.skill_id} · v{skill.version}</p>
                  <p className="text-[11px] text-faint mt-2 break-words">工具: {skill.capabilities.join(", ")}</p>
                  {skill.supported_formats.length > 0 && <p className="text-[11px] text-faint mt-1">格式: {skill.supported_formats.join(", ")}</p>}
                </article>
              ))}
            </div>
          </section>

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
              <h3 className="text-sm font-medium text-title">可安装 MCP 预设</h3>
              <span className="text-xs text-faint">{candidates.length} 项</span>
            </div>
            <div className="space-y-3">
              {candidates.map(candidate => (
                <article key={candidate.capability_id} className="bg-surface border border-divider rounded-xl p-4 shadow-sm">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium text-content">{candidate.name}</span>
                    <span className="text-[11px] px-2 py-0.5 rounded-full bg-surface-muted text-muted border border-divider">candidate</span>
                    {!candidate.installable && <span className="text-[11px] text-warning-text">当前安装器不支持</span>}
                  </div>
                  <p className="text-xs text-muted mt-2">{candidate.description}</p>
                  <p className="text-[11px] text-faint mt-2 break-words">声明工具: {candidate.declared_tools.join(", ")}</p>
                  {candidate.homepage && <a className="text-xs text-accent hover:underline mt-2 inline-block" href={candidate.homepage} target="_blank" rel="noreferrer">上游主页</a>}
                </article>
              ))}
            </div>
          </section>

          <section>
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-sm font-medium text-title">外部知识库</h3>
              <span className="text-xs text-faint">{knowledge.documents.length} 篇文档</span>
            </div>
            <div className="bg-surface border border-divider rounded-xl p-4 shadow-sm">
              {knowledge.documents.length === 0 ? (
                <p className="text-sm text-faint">尚未导入文档；可在对话中使用“文档处理”或“本地知识库” Skill。</p>
              ) : knowledge.documents.slice(0, 10).map(document => (
                <div key={document.document_id} className="py-2 border-b border-divider last:border-b-0 flex items-center justify-between gap-3">
                  <span className="text-sm text-content truncate">{document.title}</span>
                  <span className="text-[11px] text-faint whitespace-nowrap">{document.doc_type} · {document.chunks} chunks · {document.status}</span>
                </div>
              ))}
              <p className="text-[11px] text-faint mt-3 break-all">允许根目录: {knowledge.allowed_roots.join(" · ") || "未配置"}</p>
            </div>
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
                    {tool.uses_network && <span className="text-[11px] px-2 py-0.5 rounded-full bg-surface-muted text-muted border border-divider">联网</span>}
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
