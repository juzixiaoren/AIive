import { useCallback, useEffect, useId, useRef, useState } from "react";
import "./tool-trust.css";

const modes = {
  standard: "标准模式",
  full_trust: "完全信任",
  partial_trust: "部分信任",
} as const;

const riskLabels: Record<string, string> = { low: "低", medium: "中", high: "高", critical: "严重" };
const endpoint = "settings/tool-trust";

type Mode = keyof typeof modes;

interface ToolIdentity {
  capability_id: string;
  definition_source: string;
  descriptor_hash: string;
  executor_node_id: string;
  mcp_executor_identity: string;
}

interface ToolEntry {
  identity: ToolIdentity;
  name: string;
  description: string;
  risk_level: string;
  namespace_id: string;
  namespace_name: string;
  standard_requires_approval: boolean;
  standard_approval_condition: string | null;
}

interface Settings {
  mode: Mode;
  approval_exempt_capabilities: ToolIdentity[];
  tools: ToolEntry[];
}

interface Draft {
  mode: Mode;
  selected: Map<string, ToolIdentity>;
}

const identityKey = (identity: ToolIdentity) => JSON.stringify([
  identity.capability_id, identity.definition_source, identity.descriptor_hash,
  identity.executor_node_id, identity.mcp_executor_identity,
]);

export function ToolTrustControl({ apiFetch }: { apiFetch: (path: string, init?: RequestInit) => Promise<Response> }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const requestId = useRef(0);
  const titleId = useId();
  const [settings, setSettings] = useState<Settings>();
  const [draft, setDraft] = useState<Draft>();
  const [query, setQuery] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [unavailable, setUnavailable] = useState(false);

  const load = useCallback(async (editing = false) => {
    const id = ++requestId.current;
    try {
      const response = await apiFetch(endpoint, { cache: "no-store" });
      if (!response.ok) throw new Error();
      const next: Settings = await response.json();
      if (id !== requestId.current) return;
      setUnavailable(false);
      setSettings(next);
      if (editing) {
        setDraft({
          mode: next.mode,
          selected: new Map(next.approval_exempt_capabilities.map(item => [identityKey(item), item])),
        });
      }
    } catch {
      if (id !== requestId.current) return;
      setUnavailable(true);
      if (editing) setError("读取工具信任设置失败，请重试。");
    }
  }, [apiFetch]);

  useEffect(() => {
    const refresh = () => {
      if (!dialog.current?.open && document.visibilityState !== "hidden") void load();
    };
    void load();
    const timer = window.setInterval(refresh, 30_000);
    window.addEventListener("focus", refresh);
    window.addEventListener("aiive:resume", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      requestId.current += 1;
      window.clearInterval(timer);
      window.removeEventListener("focus", refresh);
      window.removeEventListener("aiive:resume", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [load]);

  function reloadDraft() {
    setError("");
    setDraft(undefined);
    void load(true);
  }

  async function save() {
    if (!draft || saving) return;
    setSaving(true);
    setError("");
    try {
      const response = await apiFetch(endpoint, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          mode: draft.mode,
          ...(draft.mode === "partial_trust" && { approval_exempt_capabilities: [...draft.selected.values()] }),
        }),
      });
      if (response.status === 409) {
        setDraft(undefined);
        setError("工具目录已更新，请重新加载后核对勾选项。");
        return;
      }
      if (!response.ok) throw new Error();
      setSettings(await response.json());
      setUnavailable(false);
      dialog.current?.close();
    } catch {
      setError("保存工具信任设置失败，请重试。");
    } finally {
      setSaving(false);
    }
  }

  function toggle(identity: ToolIdentity, checked: boolean) {
    setDraft(current => {
      if (!current) return current;
      const selected = new Map(current.selected);
      if (checked) selected.set(identityKey(identity), identity);
      else selected.delete(identityKey(identity));
      return { ...current, selected };
    });
  }

  const groups = new Map<string, ToolEntry[]>();
  const term = query.trim().toLocaleLowerCase();
  for (const tool of settings?.tools ?? []) {
    const label = `${tool.name} ${tool.identity.capability_id} ${tool.namespace_name} ${tool.description}`;
    if (!tool.standard_requires_approval || !label.toLocaleLowerCase().includes(term)) continue;
    const group = groups.get(tool.namespace_id) ?? [];
    group.push(tool);
    groups.set(tool.namespace_id, group);
  }
  const visibleKeys = new Set(settings?.tools.filter(tool => tool.standard_requires_approval).map(tool => identityKey(tool.identity)));
  const otherGrants = [...(draft?.selected.values() ?? [])].filter(identity => !visibleKeys.has(identityKey(identity)));

  return <>
    <button type="button" className="tool-trust-trigger" aria-haspopup="dialog"
      data-danger={settings?.mode === "full_trust"} title={unavailable ? "无法同步工具信任模式" : "工具信任设置"}
      onClick={() => { setQuery(""); dialog.current?.showModal(); reloadDraft(); }}>
      {settings ? modes[settings.mode] : unavailable ? "工具信任" : "信任模式…"}
      <span aria-hidden="true">⌄</span>
    </button>
    <dialog ref={dialog} className="tool-trust-dialog" aria-labelledby={titleId}
      onCancel={event => { if (saving) event.preventDefault(); }}>
      <div className="tool-trust-heading">
        <h2 id={titleId}>工具信任模式</h2>
        <button type="button" aria-label="关闭" disabled={saving} onClick={() => dialog.current?.close()}>×</button>
      </div>
      {!draft && !error && <p role="status">正在加载工具目录…</p>}
      {draft && <form onSubmit={event => { event.preventDefault(); void save(); }}>
        <fieldset className="tool-trust-modes" disabled={saving} aria-label="选择工具信任模式">
          {(Object.keys(modes) as Mode[]).map(mode => <label key={mode}>
            <input type="radio" name={titleId} value={mode} checked={draft.mode === mode}
              onChange={() => setDraft({ ...draft, mode })} />
            {modes[mode]}
          </label>)}
        </fieldset>
        {draft.mode === "full_trust" && <p className="tool-trust-warning">
          <strong>所有工具都将跳过人工审批。</strong> 这包括 Shell、删除、版本发布和回滚。任务授权与执行范围仍然有效。
        </p>}
        {draft.mode === "partial_trust" && <fieldset className="tool-trust-options" disabled={saving}>
          <legend>勾选需要免审批的工具</legend>
          <input type="search" aria-label="搜索工具" placeholder="搜索名称、ID 或用途" value={query}
            onChange={event => setQuery(event.target.value)} />
          <div className="tool-trust-list">
            {!groups.size && <p>没有匹配的工具。</p>}
            {[...groups].map(([namespace, tools]) => <section key={namespace}>
              <h3>{tools[0].namespace_name}</h3>
              {tools.map(tool => <label className="tool-trust-tool" key={identityKey(tool.identity)}>
                <input type="checkbox" checked={draft.selected.has(identityKey(tool.identity))}
                  onChange={event => toggle(tool.identity, event.target.checked)} />
                <span>
                  <strong>{tool.name}</strong> <span>· {riskLabels[tool.risk_level] ?? tool.risk_level}风险</span>
                  <code>{tool.identity.capability_id}</code>
                  <span>{tool.description}</span>
                  {tool.standard_approval_condition === "non_read_only_command" && <strong>
                    勾选后，所有 Shell 命令均免审批。工作区和任务范围限制仍然有效。
                  </strong>}
                </span>
              </label>)}
            </section>)}
            {otherGrants.length > 0 && <section>
              <h3>工具目录以外的已保存授权</h3>
              {otherGrants.map(identity => <label className="tool-trust-tool" key={identityKey(identity)}>
                <input type="checkbox" checked onChange={event => toggle(identity, event.target.checked)} />
                <span><code>{identity.capability_id}</code>{identity.executor_node_id || identity.definition_source}</span>
              </label>)}
            </section>}
          </div>
        </fieldset>}
        <p className="tool-trust-note">已有待审批事项仍需单独批准或拒绝。</p>
        <div className="tool-trust-actions">
          <button type="button" disabled={saving} onClick={() => dialog.current?.close()}>取消</button>
          <button type="submit" disabled={saving}>{saving ? "保存中…" : "保存"}</button>
        </div>
      </form>}
      {error && <p role="alert" className="tool-trust-warning">{error}</p>}
      {error && !draft && <button type="button" onClick={reloadDraft}>重新加载</button>}
    </dialog>
  </>;
}
