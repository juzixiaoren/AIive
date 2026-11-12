import { Filesystem, Directory } from "@capacitor/filesystem";
import { Share } from "@capacitor/share";
import { Capacitor } from "@capacitor/core";
import type { Attachment } from "../../../../shared/chat/attachments";
export async function saveChatAttachment(file: Attachment, blob: Blob, share: boolean): Promise<void> {
  if (!Capacitor.isNativePlatform()) {
    const url = URL.createObjectURL(blob); const a = document.createElement("a"); a.href = url; a.download = file.filename; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); return;
  }
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let text = ""; for (let i = 0; i < bytes.length; i += 8192) text += String.fromCharCode(...bytes.subarray(i, i + 8192));
  const path = `AIive/${crypto.randomUUID()}/${file.filename.replace(/[^a-zA-Z0-9.\u4e00-\u9fff_-]/g, "_")}`;
  const directory = share ? Directory.Cache : Directory.Documents;
  try {
    if (!share) {
      const permission = await Filesystem.checkPermissions();
      if (permission.publicStorage !== "granted" && (await Filesystem.requestPermissions()).publicStorage !== "granted") throw new Error("需要存储权限才能保存文档");
    }
    await Filesystem.writeFile({ path, directory, data: btoa(text), recursive: true });
    if (share) { const { uri } = await Filesystem.getUri({ path, directory }); await Share.share({ title: file.filename, files: [uri] }); }
  } catch (error) { await Filesystem.deleteFile({ path, directory }).catch(() => undefined); throw error; }
}
