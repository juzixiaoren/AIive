import { WorkClient } from "../../../../shared/work/client";
import { useWorkCenter as useSharedWorkCenter } from "../../../../shared/work/useWorkCenter";
import { apiFetch, getToken } from "../api/auth";
import { appConfig } from "../config";

// 模块级稳定引用：Hook 内以它们作为依赖，避免每次渲染重建 Controller。
const createClient = () => new WorkClient(apiFetch);

const socketUrl = (threadId: string) =>
  appConfig.backendBaseUrl.replace(/^http/i, "ws") + "/ws/" + encodeURIComponent(threadId);

export function useWorkCenter(selected: string) {
  return useSharedWorkCenter(
    selected,
    createClient,
    socketUrl,
    getToken,
    "暂时无法同步，请检查网络后刷新。",
  );
}
