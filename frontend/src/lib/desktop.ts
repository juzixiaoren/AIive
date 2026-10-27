/**
 * Electron Desktop 桥接（V49）
 * - 桌面版复用同一套 Web 登录界面；登录态变化时把 Token 交给主进程内存，
 *   再由主进程经 IPC 下发给 Node Host 完成 /ws/desktop/{node_id} 认证。
 * - Token 不写入 localStorage 之外的持久层、不打印日志、不作为命令行参数传递。
 */

interface AiiveDesktopBridge {
  getRuntimeInfo?: () => Promise<Record<string, unknown>>;
  setAuthToken?: (token: string | null) => Promise<unknown>;
}

declare global {
  interface Window {
    aiiveDesktop?: AiiveDesktopBridge;
  }
}

export function isDesktopShell(): boolean {
  return typeof window !== "undefined" && Boolean(window.aiiveDesktop);
}

/** 同步登录态给桌面主进程；浏览器环境下是空操作。 */
export function syncDesktopToken(token: string | null): void {
  if (typeof window === "undefined") return;
  const bridge = window.aiiveDesktop;
  if (!bridge?.setAuthToken) return;
  void bridge.setAuthToken(token).catch(() => {
    // 桌面桥不可用时忽略：Web 登录本身不受影响。
  });
}
