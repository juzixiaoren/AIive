import {
  Filesystem,
  Directory,
  type FilesystemPlugin,
} from "@capacitor/filesystem";
import { Share, type SharePlugin } from "@capacitor/share";
import { sha256 } from "@noble/hashes/sha2.js";
import {
  canPreview,
  downloadVerified,
  mimeType,
  safeFilename,
} from "../../../../shared/work/files";
import type { Fetcher, FileMetadata } from "../../../../shared/work/types";

type Storage = Pick<
  FilesystemPlugin,
  | "writeFile"
  | "appendFile"
  | "deleteFile"
  | "mkdir"
  | "rmdir"
  | "copy"
  | "getUri"
  | "readFile"
  | "checkPermissions"
  | "requestPermissions"
>;
export interface NativeFile {
  save(): Promise<string>;
  share(): Promise<void>;
  preview(): Promise<Blob>;
  dispose(): Promise<void>;
}
function base64(bytes: Uint8Array): string {
  let text = "";
  for (let i = 0; i < bytes.length; i += 8192)
    text += String.fromCharCode(...bytes.subarray(i, i + 8192));
  return btoa(text);
}
/** Dependencies are injectable to test actual filesystem/share calls without a device. */
export async function prepareNativeFile(
  fetch: Fetcher,
  task: string,
  kind: "artifacts" | "evidence",
  file: FileMetadata,
  signal?: AbortSignal,
  progress?: (bytes: number) => void,
  fs: Storage = Filesystem,
  share: Pick<SharePlugin, "share"> = Share,
): Promise<NativeFile> {
  const name = safeFilename(
    file.name || `${file.kind || "evidence"}-${file.id}`,
  );
  const folder = `aiive-work/${crypto.randomUUID()}`;
  const path = `${folder}/${name}`;
  let shared = false,
    disposed = false;
  const cleanup = async () => {
    try {
      await fs.deleteFile({ path, directory: Directory.Cache });
    } finally {
      await fs
        .rmdir({ path: folder, directory: Directory.Cache })
        .catch(() => undefined);
    }
  };
  try {
    await fs.writeFile({
      path,
      directory: Directory.Cache,
      data: "",
      recursive: true,
    });
    await downloadVerified(
      fetch,
      task,
      kind,
      file,
      sha256.create(),
      {
        write: async (chunk) => {
          await fs.appendFile({
            path,
            directory: Directory.Cache,
            data: base64(chunk),
          });
        },
        abort: async () => {
          await cleanup();
          disposed = true;
        },
      },
      signal,
      progress,
    );
  } catch (error) {
    if (!disposed) await cleanup().catch(() => undefined);
    throw error;
  }
  const assertReady = () => {
    if (disposed) throw new Error("文件已释放，请重新下载。");
  };
  return {
    async save() {
      assertReady();
      // The plugin handles OS-specific public storage permissions (Android 10 and earlier).
      const permission = await fs.checkPermissions();
      if (
        permission.publicStorage !== "granted" &&
        (await fs.requestPermissions()).publicStorage !== "granted"
      )
        throw new Error("需要存储权限才能保存到 Documents。");
      const destination = `AIive/${crypto.randomUUID()}`;
      const target = `${destination}/${name}`;
      try {
        await fs.mkdir({
          path: destination,
          directory: Directory.Documents,
          recursive: true,
        });
        await fs.copy({
          from: path,
          directory: Directory.Cache,
          to: target,
          toDirectory: Directory.Documents,
        });
        return `Documents/${target}`;
      } catch (error) {
        await fs
          .deleteFile({ path: target, directory: Directory.Documents })
          .catch(() => undefined);
        await fs
          .rmdir({ path: destination, directory: Directory.Documents })
          .catch(() => undefined);
        throw error;
      }
    },
    async share() {
      assertReady();
      const { uri } = await fs.getUri({ path, directory: Directory.Cache });
      await share.share({
        title: name,
        files: [uri],
        dialogTitle: "分享工作成果",
      });
      // Recipients may read the URI after the chooser returns. Leave successful shares in OS cache.
      shared = true;
    },
    async preview() {
      assertReady();
      if (!canPreview(file))
        throw new Error("此文件不支持预览，请保存或分享。");
      const { data } = await fs.readFile({ path, directory: Directory.Cache });
      if (data instanceof Blob) return data;
      return new Blob([Uint8Array.from(atob(data), (c) => c.charCodeAt(0))], {
        type: mimeType(file),
      });
    },
    async dispose() {
      if (disposed) return;
      disposed = true;
      if (!shared) await cleanup();
    },
  };
}
