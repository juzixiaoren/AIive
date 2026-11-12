import { useEffect, useRef, useState } from "react";
import { Capacitor } from "@capacitor/core";
import { sha256 } from "@noble/hashes/sha2.js";
import {
  canPreview,
  downloadBlob,
  mimeType,
  safeFilename,
} from "../../../../shared/work/files";
import { errorMessage, type FileMetadata } from "../../../../shared/work/types";
import { prepareNativeFile, type NativeFile } from "./nativeFiles";
import { apiFetch } from "../api/auth";

export default function WorkFile({
  task,
  file,
  kind,
}: {
  task: string;
  file: FileMetadata;
  kind: "artifacts" | "evidence";
}) {
  const [busy, setBusy] = useState(false),
    [message, setMessage] = useState(""),
    [error, setError] = useState("");
  const [preview, setPreview] = useState<{
    url?: string;
    text?: string;
  } | null>(null);
  const abort = useRef<AbortController | null>(null),
    native = useRef<NativeFile | null>(null);
  useEffect(
    () => () => {
      abort.current?.abort();
      void native.current?.dispose().catch(() => undefined);
    },
    [],
  );
  useEffect(
    () => () => {
      if (preview?.url) URL.revokeObjectURL(preview.url);
    },
    [preview],
  );
  const run = async (action: "save" | "share" | "preview") => {
    if (busy) return;
    setBusy(true);
    setError("");
    setPreview(null);
    setMessage("下载并校验中…");
    const controller = new AbortController();
    abort.current = controller;
    try {
      let blob: Blob | undefined;
      if (Capacitor.isNativePlatform()) {
        await native.current?.dispose();
        native.current = null;
        const prepared = await prepareNativeFile(
          apiFetch,
          task,
          kind,
          file,
          controller.signal,
          (bytes) =>
            setMessage(
              `已下载 ${bytes.toLocaleString()} / ${file.content_size.toLocaleString()} 字节`,
            ),
        );
        if (controller.signal.aborted) {
          await prepared.dispose();
          return;
        }
        native.current = prepared;
        if (action === "save") setMessage(`已保存到 ${await prepared.save()}`);
        else if (action === "share") {
          await prepared.share();
          setMessage("已打开系统分享面板");
        } else blob = await prepared.preview();
      } else {
        blob = await downloadBlob(
          apiFetch,
          task,
          kind,
          file,
          sha256.create(),
          controller.signal,
        );
        if (action === "share") {
          const shareFile = new File(
            [blob],
            safeFilename(file.name || file.id),
            { type: mimeType(file) },
          );
          if (!navigator.canShare?.({ files: [shareFile] }))
            throw new Error(
              "当前浏览器不支持文件分享，请使用 Android 应用或保存文件。",
            );
          await navigator.share({ files: [shareFile] });
          setMessage("已打开分享面板");
        } else if (action === "save") {
          const url = URL.createObjectURL(blob),
            link = document.createElement("a");
          link.href = url;
          link.download = safeFilename(file.name || file.id);
          link.click();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
          setMessage("已交给浏览器保存");
        }
      }
      if (action === "preview" && blob && canPreview(file)) {
        setPreview(
          mimeType(file).startsWith("image/")
            ? { url: URL.createObjectURL(blob) }
            : { text: await blob.text() },
        );
        setMessage("SHA256 校验通过");
      }
    } catch (cause) {
      setMessage("");
      if (!controller.signal.aborted) setError(errorMessage(cause));
      await native.current?.dispose().catch(() => undefined);
      native.current = null;
    } finally {
      setBusy(false);
    }
  };
  return (
    <article className="mobile-file">
      <h3>{file.name || file.summary || file.kind}</h3>
      <p>{file.summary}</p>
      <small>
        {file.mime_type} · {file.content_size.toLocaleString()} 字节
      </small>
      <div className="mobile-actions">
        <button disabled={busy} onClick={() => void run("save")}>
          保存
        </button>
        <button disabled={busy} onClick={() => void run("share")}>
          分享
        </button>
        {canPreview(file) && (
          <button disabled={busy} onClick={() => void run("preview")}>
            预览
          </button>
        )}
        {busy && (
          <button onClick={() => abort.current?.abort()}>取消下载</button>
        )}
      </div>
      {message && <p role="status">{message}</p>}
      {error && <p role="alert">{error}</p>}
      {preview && (
        <div>
          <button onClick={() => setPreview(null)}>关闭预览</button>
          {preview.url ? (
            <img src={preview.url} alt="文件预览" />
          ) : (
            <pre>{preview.text}</pre>
          )}
        </div>
      )}
    </article>
  );
}
