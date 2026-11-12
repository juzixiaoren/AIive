/** A known pre-execution rejection invalidates server IDs, never the local File. */
export function recoverAttachmentDraft<T extends { attachment?: unknown; progress: number; error?: string }>(items: T[]): T[] {
  return items.map(item => ({ ...item, attachment: undefined, progress: 0,
    error: "本轮未执行。可拆分或移除文件，然后点击重试重新上传。" }));
}
