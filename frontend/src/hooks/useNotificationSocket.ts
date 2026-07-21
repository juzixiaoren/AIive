/**
 * 全局通知 WebSocket Hook
 * - 与后端全局通知通道（/ws/__global__）建立单条长连接，全应用共用
 * - 后端在提醒触发 / 确认 / 延时 / 删除时主动推送最新 pending 数量
 * - 用于替代前端定时轮询，实现角标与收件箱的即时更新
 */

import { useEffect, useRef, useState } from "react";

type NotificationData = { pending_count: number };

// 全局通知通道：与后端 ws_manager.GLOBAL_THREAD_ID 保持一致
const WS_PATH = "__global__";

let socket: WebSocket | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
const listeners = new Set<(data: NotificationData) => void>();

function ensureSocket() {
  if (
    socket &&
    (socket.readyState === WebSocket.OPEN ||
      socket.readyState === WebSocket.CONNECTING)
  ) {
    return;
  }
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${protocol}//${window.location.host}/ws/${WS_PATH}`;
  socket = new WebSocket(url);

  socket.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      if (msg.type === "notification" && msg.data) {
        const data: NotificationData = { pending_count: msg.data.pending_count ?? 0 };
        listeners.forEach((l) => l(data));
      }
    } catch {
      // 忽略非法消息
    }
  };

  socket.onclose = () => {
    socket = null;
    if (reconnectTimer === null) {
      reconnectTimer = setTimeout(ensureSocket, 3000);
    }
  };

  socket.onerror = () => {
    socket?.close();
  };
}

function subscribe(listener: (data: NotificationData) => void) {
  listeners.add(listener);
  ensureSocket();
  return () => {
    listeners.delete(listener);
  };
}

/** 返回实时同步的 pending 通知数量（替代轮询）。 */
export function useNotificationCount(): number {
  const [count, setCount] = useState(0);
  useEffect(() => subscribe((d) => setCount(d.pending_count)), []);
  return count;
}

/** 订阅通知事件，回调中可执行刷新等操作（如收件箱重新拉取）。 */
export function useNotificationListener(cb: (data: NotificationData) => void) {
  const ref = useRef(cb);
  ref.current = cb;
  useEffect(() => subscribe((d) => ref.current(d)), []);
}
