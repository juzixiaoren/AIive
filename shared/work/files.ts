import type { Fetcher, FileMetadata } from "./types";

export const CHUNK_BYTES = 1024 * 1024;
export const PREVIEW_BYTES = 8 * 1024 * 1024;
export const previewTypes = new Set([
  "text/plain",
  "text/markdown",
  "text/csv",
  "application/json",
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
]);
export function mimeType(file: FileMetadata): string {
  return file.mime_type.split(";")[0].trim().toLowerCase();
}
export function canPreview(file: FileMetadata): boolean {
  return file.content_size <= PREVIEW_BYTES && previewTypes.has(mimeType(file));
}
export function safeFilename(name: string): string {
  let result = name
    .normalize("NFKC")
    .replace(/[\\/\u0000-\u001f\u007f<>:"|?*\u202a-\u202e\u2066-\u2069]/g, "_")
    .replace(/^\.+|[. ]+$/g, "")
    .trim();
  if (/^(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\.|$)/i.test(result))
    result = `_${result}`;
  const encoder = new TextEncoder();
  let bounded = "";
  for (const char of result) {
    if (encoder.encode(bounded + char).length > 180) break;
    bounded += char;
  }
  return bounded || "download";
}
export interface Hasher {
  update(chunk: Uint8Array): unknown;
  digest(): Uint8Array;
}
export interface ChunkSink {
  write(chunk: Uint8Array): Promise<void>;
  abort(): Promise<void>;
}

/** Fetch actual bytes, validate every offset/header, and publish nothing until the full hash agrees. */
export async function downloadVerified(
  fetch: Fetcher,
  taskId: string,
  kind: "artifacts" | "evidence",
  file: FileMetadata,
  hash: Hasher,
  sink: ChunkSink,
  signal?: AbortSignal,
  progress?: (bytes: number) => void,
): Promise<void> {
  let offset = 0;
  try {
    if (
      !Number.isSafeInteger(file.content_size) ||
      file.content_size < 0 ||
      !/^[a-f0-9]{64}$/i.test(file.content_hash)
    )
      throw new Error("文件元数据无效，无法安全下载。");
    do {
      signal?.throwIfAborted();
      const response = await fetch(
        `agent-tasks/${encodeURIComponent(taskId)}/${kind}/${encodeURIComponent(file.id)}?offset=${offset}&max_bytes=${CHUNK_BYTES}`,
        { signal },
      );
      if (!response.ok) throw new Error(`文件读取失败（${response.status}）`);
      if (
        response.headers.get("X-AIive-Content-Hash")?.toLowerCase() !==
        file.content_hash.toLowerCase()
      )
        throw new Error("文件已变化，请刷新详情后重新下载。");
      // Enforce the bound while reading as well; never trust a server to honor max_bytes.
      const reader = response.body?.getReader();
      if (!reader) throw new Error("文件响应为空");
      const chunks: Uint8Array[] = [];
      let size = 0;
      try {
        while (true) {
          const part = await reader.read();
          if (part.done) break;
          size += part.value.byteLength;
          if (size > CHUNK_BYTES) {
            await reader.cancel();
            throw new Error("文件分段超过 1 MiB");
          }
          chunks.push(part.value);
        }
      } finally {
        reader.releaseLock();
      }
      const next = offset + size;
      const nextHeader = response.headers.get("X-AIive-Next-Offset");
      const truncated = response.headers.get("X-AIive-Truncated");
      if (
        nextHeader === null ||
        !/^\d+$/.test(nextHeader) ||
        Number(nextHeader) !== next ||
        next > file.content_size ||
        (truncated !== "true" && truncated !== "false")
      )
        throw new Error("文件分段信息不一致");
      if (
        (truncated === "true") !== next < file.content_size ||
        (size === 0 && next < file.content_size)
      )
        throw new Error("文件下载不完整");
      for (const chunk of chunks) {
        signal?.throwIfAborted();
        hash.update(chunk);
        await sink.write(chunk);
      }
      offset = next;
      progress?.(offset);
      if (truncated === "false") break;
    } while (offset < file.content_size);
    signal?.throwIfAborted();
    const digest = Array.from(hash.digest(), (byte) =>
      byte.toString(16).padStart(2, "0"),
    ).join("");
    if (digest !== file.content_hash.toLowerCase())
      throw new Error("SHA256 校验失败，已丢弃下载内容。");
  } catch (error) {
    await sink.abort();
    throw error;
  }
}

/** Browser fallback has an explicit memory ceiling; native Android streams to disk. */
export async function downloadBlob(
  fetch: Fetcher,
  task: string,
  kind: "artifacts" | "evidence",
  file: FileMetadata,
  hash: Hasher,
  signal?: AbortSignal,
): Promise<Blob> {
  if (file.content_size > 128 * 1024 * 1024)
    throw new Error(
      "浏览器最多下载 128 MiB；更大的文件请在 Android 工作中心保存。",
    );
  const chunks: ArrayBuffer[] = [];
  await downloadVerified(
    fetch,
    task,
    kind,
    file,
    hash,
    {
      write: async (chunk) => {
        chunks.push(new Uint8Array(chunk).buffer);
      },
      abort: async () => {
        chunks.length = 0;
      },
    },
    signal,
  );
  return new Blob(chunks, { type: mimeType(file) });
}
