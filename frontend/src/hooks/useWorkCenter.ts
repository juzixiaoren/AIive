import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { WorkClient } from "../../../shared/work/client";
import { WorkController } from "../../../shared/work/controller";
import { subscribeThread } from "../../../shared/work/connection";
import { apiFetch, getToken } from "../api/client";
import { BASE_PATH } from "../lib/base";

export function useWorkCenter(selected: string) {
  const controller = useMemo(
    () =>
      new WorkController(
        new WorkClient((path, init) =>
          apiFetch(`${BASE_PATH}api/${path}`, init),
        ),
      ),
    [],
  );
  const state = useSyncExternalStore(
    controller.subscribe,
    controller.getSnapshot,
  );
  const [connectionError, setConnectionError] = useState("");
  useEffect(() => {
    controller.start();
    let disposed = false,
      unsubscribe: (() => void) | undefined;
    void controller.refresh();
    const connect = async () => {
      try {
        const { thread_id } = await controller.api.currentThread();
        if (disposed) return;
        unsubscribe?.();
        const protocol = location.protocol === "https:" ? "wss:" : "ws:";
        unsubscribe = subscribeThread(
          `${protocol}//${location.host}${BASE_PATH}ws/${encodeURIComponent(thread_id)}`,
          getToken,
          (event) => {
            if (event.type === "work_changed" && event.data)
              controller.invalidate(event.data);
          },
          () => {
            setConnectionError("");
            void controller.refresh();
          },
        );
      } catch {
        if (!disposed)
          setConnectionError("实时连接暂不可用，请检查网络或点击刷新。");
      }
    };
    void connect();
    window.addEventListener("online", connect);
    return () => {
      disposed = true;
      unsubscribe?.();
      window.removeEventListener("online", connect);
      controller.dispose();
    };
  }, [controller]);
  useEffect(() => {
    controller.select(selected);
  }, [controller, selected]);
  return { controller, state, connectionError };
}
