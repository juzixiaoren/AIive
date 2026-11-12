import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { WorkClient } from "../../../../shared/work/client";
import { WorkController } from "../../../../shared/work/controller";
import { subscribeThread } from "../../../../shared/work/connection";
import { apiFetch, getToken } from "../api/auth";
import { appConfig } from "../config";

export function useWorkCenter(selected: string) {
  const controller = useMemo(
    () => new WorkController(new WorkClient(apiFetch)),
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
        unsubscribe = subscribeThread(
          `${appConfig.backendBaseUrl.replace(/^http/i, "ws")}/ws/${encodeURIComponent(thread_id)}`,
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
        if (!disposed) setConnectionError("暂时无法同步，请检查网络后刷新。");
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
