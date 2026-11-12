import { useEffect, useRef, useState } from "react";
export interface Attachment { id: string; filename: string; mime: string; size: number; sha256: string }
export type AttachmentFetch = (path: string, init?: RequestInit) => Promise<Response>;
interface Pending { key: string; file: File; progress: number; attachment?: Attachment; error?: string; preview?: string }
const accept = ".pdf,.docx,.txt,.md,.markdown,.html,.htm,.json,.csv,.tsv,.png,.jpg,.jpeg,.webp";
export function AttachmentComposer({ fetcher, uploadUrl, readCredential, disabled, onChange }: {
  fetcher: AttachmentFetch; uploadUrl: string; readCredential: () => string | null; disabled: boolean;
  onChange: (attachments: Attachment[], blocked: boolean) => void;
}) {
  const [items, setItems] = useState<Pending[]>([]);
  const latest = useRef(items); latest.current = items;
  const requests = useRef(new Set<XMLHttpRequest>());
  const [error, setError] = useState("");
  useEffect(() => { onChange(items.flatMap(i => i.attachment ? [i.attachment] : []), items.some(i => !i.attachment)); }, [items]);
  useEffect(() => () => { for (const r of requests.current) r.abort(); for (const i of latest.current) if (i.preview) URL.revokeObjectURL(i.preview); }, []);
  function upload(item: Pending) {
    setItems(old => old.map(i => i.key === item.key ? { ...i, error: undefined, progress: 0 } : i));
    const xhr = new XMLHttpRequest(); requests.current.add(xhr);
    xhr.open("POST", uploadUrl); xhr.timeout = 120000;
    const auth = readCredential(); if (auth) xhr.setRequestHeader("Authorization", `Bearer ${auth}`);
    xhr.setRequestHeader("X-Filename", encodeURIComponent(item.file.name));
    xhr.setRequestHeader("Content-Type", item.file.type || "application/octet-stream");
    xhr.upload.onprogress = e => { if (e.lengthComputable) setItems(old => old.map(i => i.key === item.key ? { ...i, progress: Math.round(e.loaded / e.total * 100) } : i)); };
    const fail = (message: string) => { requests.current.delete(xhr); setItems(old => old.map(i => i.key === item.key ? { ...i, error: message } : i)); };
    xhr.onerror = xhr.ontimeout = () => fail("上传失败，请重试");
    xhr.onload = () => {
      requests.current.delete(xhr);
      try {
        const body = JSON.parse(xhr.responseText);
        if (xhr.status < 200 || xhr.status >= 300) { fail(String(body.detail || "上传失败")); return; }
        if (!latest.current.some(i => i.key === item.key)) { void fetcher(`attachments/${body.id}`, { method: "DELETE" }); return; }
        setItems(old => old.map(i => i.key === item.key ? { ...i, attachment: body, progress: 100 } : i));
      } catch { fail("上传响应无效，请重试"); }
    };
    xhr.send(item.file);
  }
  function select(files: FileList | null) {
    if (!files) return;
    if (items.length + files.length > 5) { setError("每条消息最多 5 个附件"); return; }
    setError("");
    const selected = Array.from(files).map(file => ({ key: crypto.randomUUID(), file, progress: 0, preview: file.type.startsWith("image/") ? URL.createObjectURL(file) : undefined }));
    setItems(old => [...old, ...selected]);
    for (const item of selected) upload(item);
  }
  async function remove(item: Pending) {
    if (item.attachment) {
      try { const res = await fetcher(`attachments/${item.attachment.id}`, { method: "DELETE" }); if (!res.ok) throw new Error("删除附件失败"); }
      catch { setError("删除附件失败，请重试"); return; }
    }
    if (item.preview) URL.revokeObjectURL(item.preview);
    setItems(old => old.filter(i => i.key !== item.key));
  }
  return <div style={{ padding: 8, width: "100%", boxSizing: "border-box", overflowWrap: "anywhere" }}>
    <label style={{ cursor: "pointer", display: "inline-block", padding: 8, border: "1px solid #8886", borderRadius: 8 }}>📎 文件<input style={{ display: "none" }} aria-label="选择附件" type="file" multiple accept={accept} disabled={disabled} onChange={e => { select(e.target.files); e.target.value = ""; }} /></label>
    <label style={{ cursor: "pointer", display: "inline-block", padding: 8, border: "1px solid #8886", borderRadius: 8 }}> 🖼 图片<input style={{ display: "none" }} aria-label="选择图片" type="file" multiple accept="image/png,image/jpeg,image/webp" disabled={disabled} onChange={e => { select(e.target.files); e.target.value = ""; }} /></label>
    {error && <p role="alert">{error}</p>}
    {items.map(item => <div key={item.key} style={{ border: "1px solid #8886", padding: 6 }}>
      {item.preview && <img src={item.preview} alt={item.file.name} style={{ maxHeight: 100 }} />}
      <span>{item.file.name} · {(item.file.size / 1024).toFixed(1)} KiB · {item.file.type || "文件"}</span>
      {!item.attachment && !item.error && <progress max={100} value={item.progress} aria-label="上传进度" />}
      {item.error && <span role="alert">{item.error}<button disabled={disabled} onClick={() => upload(item)}>重试</button></span>}
      <button disabled={disabled} onClick={() => void remove(item)}>移除</button>
    </div>)}
  </div>;
}
export function AttachmentCards({ attachments = [], fetcher, nativeSave }: { attachments?: Attachment[]; fetcher: AttachmentFetch; nativeSave?: (file: Attachment, blob: Blob, share: boolean) => Promise<void> }) {
  const [preview, setPreview] = useState("");
  const [error, setError] = useState("");
  useEffect(() => () => { if (preview) URL.revokeObjectURL(preview); }, [preview]);
  async function open(file: Attachment, view: boolean, share = false) {
    try {
      const response = await fetcher(`attachments/${encodeURIComponent(file.id)}/content`);
      if (!response.ok) throw new Error("附件不可用或已被遗忘");
      const blob = await response.blob();
      const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", await blob.arrayBuffer()));
      if (blob.size !== file.size || Array.from(digest).map(b => b.toString(16).padStart(2, "0")).join("") !== file.sha256) throw new Error("附件完整性校验失败");
      if (!view && nativeSave) { await nativeSave(file, blob, share); return; }
      const url = URL.createObjectURL(blob);
      if (view && file.mime.startsWith("image/")) { setPreview(url); return; }
      const a = document.createElement("a"); a.href = url; a.download = file.filename; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) { setError(e instanceof Error ? e.message : "读取失败"); }
  }
  return <div>{attachments.map(file => <div key={file.id} style={{ border: "1px solid #8886", padding: 6 }}>
    <span>{file.filename} · {(file.size / 1024).toFixed(1)} KiB · {file.mime}</span>
    {file.mime.startsWith("image/") && <button onClick={() => void open(file, true)}>查看图片</button>}
    <button onClick={() => void open(file, false)}>保存附件</button>
    {nativeSave && <button onClick={() => void open(file, false, true)}>分享</button>}
  </div>)}{error && <p role="alert">{error}</p>}
  {preview && <div role="dialog" aria-label="图片预览" style={{ position: "fixed", inset: 0, background: "#000d", zIndex: 100, display: "grid", placeItems: "center" }} onClick={() => setPreview("")}><button onClick={() => setPreview("")}>关闭</button><img src={preview} alt="附件图片" style={{ maxWidth: "95vw", maxHeight: "85vh" }} /></div>}
  </div>;
}
