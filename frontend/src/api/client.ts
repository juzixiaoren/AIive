/**
 * 统一请求客户端（V49）
 * - 唯一 Token 读写入口：localStorage 持久化，刷新/重启后仍保持登录
 * - 自动为同源 AIive API 注入 Authorization: Bearer <token>
 * - 401 统一清除 Token 并广播登录失效事件；403 保留登录态（权限/审批不满足）
 * - 第三方地址绝不附带 Token
 */

export const TOKEN_STORAGE_KEY = "aiive.auth.token";
export const AUTH_EXPIRED_EVENT = "aiive:auth-expired";

/** 认证接口本身不参与 401 自动登出（登录失败也返回 401）。 */
const AUTH_ENDPOINTS = ["api/auth/status", "api/auth/register", "api/auth/login"];

export function getToken(): string | null {
  try {
    const value = localStorage.getItem(TOKEN_STORAGE_KEY);
    return value && value.trim() ? value : null;
  } catch {
    return null;
  }
}

export function setToken(token: string): void {
  try {
    localStorage.setItem(TOKEN_STORAGE_KEY, token);
  } catch {
    // 存储不可用（隐私模式/配额）时退化为仅内存态：本次会话仍可用，刷新后需重新登录。
  }
}

export function clearToken(): void {
  try {
    localStorage.removeItem(TOKEN_STORAGE_KEY);
  } catch {
    // 忽略存储异常
  }
}

function emitAuthExpired(): void {
  try {
    window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
  } catch {
    // 非浏览器环境（测试）忽略
  }
}

/** 判断目标是否属于 AIive 自身 API：相对路径或同源绝对地址。 */
export function isSameSiteRequest(target: string): boolean {
  if (!/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(target)) return true;
  try {
    return new URL(target).origin === window.location.origin;
  } catch {
    return false;
  }
}

function isAuthEndpoint(target: string): boolean {
  return AUTH_ENDPOINTS.some((path) => target.includes(path));
}

function targetOf(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  return input.url;
}

/**
 * 带认证的统一 fetch。
 * 业务页面一律使用它，不要直接调用 fetch，避免遗漏 Token 或重复实现 401 处理。
 */
export async function apiFetch(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
  const target = targetOf(input);
  const sameSite = isSameSiteRequest(target);
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token && sameSite && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  const response = await fetch(input, { ...init, headers });
  if (response.status === 401 && sameSite && !isAuthEndpoint(target)) {
    clearToken();
    emitAuthExpired();
  }
  return response;
}

/**
 * WebSocket 首帧认证：浏览器无法给 WS 设置 Authorization 头，
 * 因此连接建立后立即发送认证帧，服务端验证通过前不会推送任何业务数据。
 */
export function sendWsAuth(socket: WebSocket): void {
  const token = getToken();
  if (!token) {
    socket.close();
    return;
  }
  socket.send(JSON.stringify({ type: "auth", access_token: token }));
}
