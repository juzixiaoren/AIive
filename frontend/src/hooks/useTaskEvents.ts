import { useEffect, useRef } from "react";
import { BASE_PATH } from "../lib/base";
import type { AgentTaskEvent } from "../api/agentTasks";


/** 订阅选中任务的增量事件；断线补齐仍由详情 REST/轮询负责。 */
export function useTaskEvents(taskId: string, callback: (event: AgentTaskEvent) => void) {
  const callbackRef = useRef(callback);
  const cursorRef = useRef(0);
  callbackRef.current = callback;

  useEffect(() => {
    if (!taskId) return;
    cursorRef.current = 0;
    let socket: WebSocket | null = null;
    let reconnect: ReturnType<typeof setTimeout> | null = null;
    let disposed = false;

    const connect = () => {
      if (disposed) return;
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      socket = new WebSocket(
        `${protocol}//${window.location.host}${BASE_PATH}api/agent-tasks/ws/${encodeURIComponent(taskId)}`
        + `?after_sequence=${cursorRef.current > 0 ? cursorRef.current : "latest"}`,
      );
      socket.onmessage = (message) => {
        try {
          const envelope = JSON.parse(String(message.data));
          if (envelope?.type === "task_event" && envelope.data) {
            cursorRef.current = Math.max(cursorRef.current, Number(envelope.data.sequence || 0));
            callbackRef.current(envelope.data);
          }
        } catch { /* REST 刷新会补齐非法或丢失帧。 */ }
      };
      socket.onclose = () => {
        socket = null;
        if (!disposed) reconnect = setTimeout(connect, 2000);
      };
      socket.onerror = () => socket?.close();
    };
    connect();
    return () => {
      disposed = true;
      if (reconnect) clearTimeout(reconnect);
      socket?.close();
    };
  }, [taskId]);
}
