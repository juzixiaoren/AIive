import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { WorkClient } from "./client";
import { WorkController } from "./controller";
import { subscribeThread } from "./connection";

/**
 * 工作中心 Hook 的唯一实现：Controller 生命周期、状态订阅、Thread 通道重连与
 * 在线恢复都由这里维护；Web 与 Android 只提供传输层差异（见各自包装文件）。
 *
 * 传入的 createClient / socketUrl / token 必须是模块级稳定引用，否则每次渲染
 * 都会重建 Controller。
 */
export function useWorkCenter(
  selected: string,
  createClient: () => WorkClient,
  socketUrl: (threadId: string) => string,
  token: () => string | null,
  offlineMessage: string,
) {
  const controller = useMemo(() => new WorkController(createClient()), [createClient]);
  const state = useSyncExternalStore(
    controller.subscribe,
    controller.getSnapshot,
  );
  const [connectionError, setConnectionError] = useState("");

  useEffect(() => {
    controller.start();
    let disposed = false;
    let unsubscribe: (() => void) | undefined;
    void controller.refresh();
    const connect = async () => {
      try {
        const { thread_id } = await controller.api.currentThread();
        if (disposed) return;
        unsubscribe?.();
        unsubscribe = subscribeThread(
          socketUrl(thread_id),
          token,
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
        if (!disposed) setConnectionError(offlineMessage);
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
  }, [controller, socketUrl, token, offlineMessage]);

  useEffect(() => {
    controller.select(selected);
  }, [controller, selected]);

  return { controller, state, connectionError };
}
