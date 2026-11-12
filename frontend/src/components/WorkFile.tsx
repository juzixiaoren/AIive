import { useEffect, useRef, useState } from "react";
import { sha256 } from "@noble/hashes/sha2.js";
import {
  canPreview,
  downloadBlob,
  mimeType,
  safeFilename,
} from "../../../shared/work/files";
import { errorMessage, type FileMetadata } from "../../../shared/work/types";
import type { WorkClient } from "../../../shared/work/client";

export default function WorkFile({
  api,
  task,
  file,
  kind,
}: {
  api: WorkClient;
  task: string;
  file: FileMetadata;
  kind: "artifacts" | "evidence";
}) {
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const [preview, setPreview] = useState<{
    url?: string;
    text?: string;
  } | null>(null);
  const abort = useRef<AbortController | null>(null);
  useEffect(() => () => abort.current?.abort(), []);
  useEffect(
    () => () => {
      if (preview?.url) URL.revokeObjectURL(preview.url);
    },
    [preview],
  );
  const read = async (show: boolean) => {
    if (busy) return;
    setBusy(true);
    setError("");
    setPreview(null);
    const controller = new AbortController();
    abort.current = controller;
    try {
      const blob = await downloadBlob(
        api.fetch,
        task,
        kind,
        file,
        sha256.create(),
        controller.signal,
      );
      if (show && canPreview(file)) {
        setPreview(
          mimeType(file).startsWith("image/")
            ? { url: URL.createObjectURL(blob) }
            : { text: await blob.text() },
        );
      } else {
        const url = URL.createObjectURL(blob),
          link = document.createElement("a");
        link.href = url;
        link.download = safeFilename(
          file.name || `${file.kind || "evidence"}-${file.id}`,
        );
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
    } catch (cause) {
      if (!controller.signal.aborted) setError(errorMessage(cause));
    } finally {
      setBusy(false);
    }
  };
  return (
    <article className="work-file">
      <strong>{file.name || file.summary || file.kind}</strong>
      <p>{file.summary}</p>
      <small>
        {file.mime_type} · {file.content_size.toLocaleString()} 字节 ·{" "}
        {new Date(file.created_at).toLocaleString()}
      </small>
      <div className="work-buttons">
        <button disabled={busy} onClick={() => void read(false)}>
          {busy ? "下载并校验…" : "保存文件"}
        </button>
        {canPreview(file) && (
          <button disabled={busy} onClick={() => void read(true)}>
            预览
          </button>
        )}
        {busy && (
          <button onClick={() => abort.current?.abort()}>取消下载</button>
        )}
      </div>
      {error && <p role="alert">{error}</p>}
      {preview && (
        <div>
          <button onClick={() => setPreview(null)}>关闭预览</button>
          {preview.url ? (
            <img src={preview.url} alt={file.name || "证据预览"} />
          ) : (
            <pre>{preview.text}</pre>
          )}
        </div>
      )}
    </article>
  );
}
