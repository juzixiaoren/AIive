import { WorkClient } from "../../../shared/work/client";
import { useWorkCenter as useSharedWorkCenter } from "../../../shared/work/useWorkCenter";
import { apiFetch, getToken } from "../api/client";
import { BASE_PATH } from "../lib/base";

// 模块级稳定引用：Hook 内以它们作为依赖，避免每次渲染重建 Controller。
const createClient = () =>
  new WorkClient((path, init) => apiFetch(BASE_PATH + "api/" + path, init));

const socketUrl = (threadId: string) => {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  return protocol + "//" + location.host + BASE_PATH + "ws/" + encodeURIComponent(threadId);
};

export function useWorkCenter(selected: string) {
  return useSharedWorkCenter(
    selected,
    createClient,
    socketUrl,
    getToken,
    "实时连接暂不可用，请检查网络或点击刷新。",
  );
}
