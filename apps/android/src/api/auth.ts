/**
 * Android 认证客户端（V49）
 * - 与 Web 复用同一套后端契约：/api/auth/status|register|login|me
 * - 登录成功后把同一个 access_token 持久化到 localStorage，APK 重启后仍保持登录
 * - 所有受保护请求统一经 apiFetch 注入 Authorization: Bearer <token>
 * - 401 清除本地凭证并广播登录失效事件，不无限重试
 */
import { appConfig } from "../config";

const TOKEN_KEY = "aiive.android.auth.v1";
export const AUTH_EXPIRED_EVENT = "aiive:auth-expired";

export interface AuthStatus {
  initialized: boolean;
}

export interface AuthSession {
  access_token: string;
  token_type: string;
  expires_at: string;
  user: { username: string };
}

export class AuthError extends Error {
  constructor(message: string, public readonly code: string, public readonly status: number) {
    super(message);
    this.name = "AuthError";
  }
}

export function hasBackendConfig(): boolean {
  return Boolean(appConfig.backendBaseUrl);
}

export function getToken(): string | null {
  try {
    const value = localStorage.getItem(TOKEN_KEY);
    return value && value.trim() ? value : null;
  } catch {
    return null;
  }
}

export function setToken(token: string): void {
  try {
    localStorage.setItem(TOKEN_KEY, token);
  } catch {
    // 存储不可用时退化为仅本次会话有效
  }
}

export function clearToken(): void {
  try {
    localStorage.removeItem(TOKEN_KEY);
  } catch {
    // 忽略存储异常
  }
}

export function logout(): void {
  clearToken();
}

function notifyExpired(): void {
  clearToken();
  try {
    window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
  } catch {
    // 非浏览器环境忽略
  }
}

function url(path: string): string {
  if (!appConfig.backendBaseUrl) {
    throw new Error("尚未配置服务器地址，请先填写 apps/android/src/config.ts");
  }
  return `${appConfig.backendBaseUrl}/api/${path.replace(/^\/+/, "")}`;
}

async function toError(response: Response, fallback: string): Promise<AuthError> {
  let code = fallback;
  try {
    const body = await response.json();
    if (typeof body?.detail === "string") code = body.detail;
  } catch {
    // 非 JSON 响应沿用兜底码
  }
  return new AuthError(code, code, response.status);
}

export function authErrorMessage(error: unknown): string {
  if (error instanceof AuthError) {
    switch (error.code) {
      case "invalid_credentials":
        return "用户名或密码不正确";
      case "account_already_initialized":
        return "系统已完成初始化，请直接登录";
      case "too_many_attempts":
        return "尝试次数过多，请稍后再试";
      case "invalid_username":
        return "用户名不能为空，且不超过 64 个字符";
      case "invalid_password":
        return "密码长度需在 8 到 256 个字符之间";
      default:
        return `请求失败（${error.status}）`;
    }
  }
  return error instanceof Error ? error.message : "请求失败，请稍后再试";
}

/** 受保护请求的统一入口：注入 Bearer Token，401 时清除凭证。 */
export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(url(path), { ...init, headers });
  if (response.status === 401) notifyExpired();
  return response;
}

export async function getAuthStatus(): Promise<AuthStatus> {
  const response = await fetch(url("auth/status"));
  if (!response.ok) throw await toError(response, "auth_status_failed");
  return response.json() as Promise<AuthStatus>;
}

async function acceptSession(response: Response): Promise<AuthSession> {
  if (!response.ok) throw await toError(response, "auth_failed");
  const session = (await response.json()) as AuthSession;
  setToken(session.access_token);
  return session;
}

export async function register(username: string, password: string): Promise<AuthSession> {
  const response = await fetch(url("auth/register"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  return acceptSession(response);
}

export async function login(username: string, password: string): Promise<AuthSession> {
  const response = await fetch(url("auth/login"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  return acceptSession(response);
}

export async function getMe(): Promise<{ username: string }> {
  const response = await apiFetch("auth/me");
  if (!response.ok) throw await toError(response, "auth_me_failed");
  return response.json() as Promise<{ username: string }>;
}
