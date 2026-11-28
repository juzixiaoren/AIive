import { useEffect, useRef, useState, type FormEvent } from "react";
import type { WorkClient } from "./client";
import { errorMessage, type WorkView } from "./types";
import "./continuation.css";

const MAX_FILES = 5;

export function ContinuationForm({ view, api, onOpen }: {
  view: WorkView;
  api: WorkClient;
  onOpen: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [instruction, setInstruction] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const pending = useRef(false);
  const mounted = useRef(true);
  const request = useRef<{ draft: string; id: string } | null>(null);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  const metadata = view.continuation;
  const sourceId = metadata?.source_task_id;
  const files = [
    ...view.artifacts.map(file => ({ ...file, kind: "artifact" as const })),
    ...view.evidence.map(file => ({ ...file, kind: "evidence" as const })),
  ].filter(file => /^(text\/|application\/json)/.test(file.mime_type));
  const keyFor = (file: (typeof files)[number]) => `${file.kind}:${file.id}`;
  const missingSelection = selected.some(key => !files.some(file => keyFor(file) === key));
  const canStart = metadata?.can_start && (metadata.report_available || files.length > 0);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const text = instruction.trim();
    if (pending.current || !canStart || !text || missingSelection || (!metadata?.report_available && !selected.length)) return;
    const chosen = files.filter(file => selected.includes(keyFor(file)));
    const artifactIds = chosen.filter(file => file.kind === "artifact").map(file => file.id).sort();
    const evidenceIds = chosen.filter(file => file.kind === "evidence").map(file => file.id).sort();
    const draft = JSON.stringify([text, artifactIds, evidenceIds]);
    if (request.current?.draft !== draft) request.current = { draft, id: crypto.randomUUID() };
    pending.current = true;
    setBusy(true);
    setError("");
    try {
      const result = await api.continueWork(view.task.task_id, request.current.id, text, artifactIds, evidenceIds);
      if (mounted.current) onOpen(result.task_id);
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      pending.current = false;
      setBusy(false);
    }
  };

  return (
    <div className="work-continuation">
      {sourceId && (
        <button type="button" disabled={busy} onClick={() => onOpen(sourceId)}>查看原工作</button>
      )}
      {canStart && !open && (
        <button type="button" onClick={() => setOpen(true)}>基于此成果继续</button>
      )}
      {open && (
        <form onSubmit={submit}>
          <label>
            本次修改要求
            <textarea value={instruction} onChange={event => setInstruction(event.target.value)}
              maxLength={2000} rows={3} disabled={busy} required placeholder="例如：只更新风险部分" />
          </label>
          {files.length > 0 && (
            <fieldset>
              <legend>引用文本成果或证据（最多五份）</legend>
              {files.map(file => {
                const key = keyFor(file);
                return (
                  <label key={key}>
                    <input type="checkbox" checked={selected.includes(key)}
                      disabled={busy || (!selected.includes(key) && selected.length >= MAX_FILES)}
                      onChange={event => setSelected(current => event.target.checked
                        ? [...current, key] : current.filter(value => value !== key))} />
                    {file.name || file.summary.slice(0, 100) || (file.kind === "artifact" ? "文本产物" : "文本证据")}
                  </label>
                );
              })}
            </fieldset>
          )}
          {metadata?.report_available && <p>Agent 会参考原工作报告。</p>}
          {missingSelection && <p role="alert">有已选文件不可用。清除选择后可重试。</p>}
          {error && <p role="alert">{error}</p>}
          <div>
            <button type="submit" disabled={busy || !canStart || !instruction.trim() || missingSelection || (!metadata?.report_available && !selected.length)}>
              {busy ? "正在创建…" : "创建续作"}
            </button>
            {missingSelection && <button type="button" disabled={busy} onClick={() => setSelected([])}>清除选择</button>}
            <button type="button" disabled={busy} onClick={() => setOpen(false)}>取消</button>
          </div>
        </form>
      )}
    </div>
  );
}
