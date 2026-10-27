/**
 * 认证 API（V49）：初始化状态、首次注册、登录、当前账号。
 * 只有这三个接口允许匿名，其余接口都会在服务端要求 Bearer Token。
 */
import { apiFetch, clearToken, setToken } from "./client";

export interface AuthStatus {
  initialized: boolean;
}

export interface AuthUser {
  username: string;
}

export interface AuthSession {
  access_token: string;
  token_type: string;
  expires_at: string;
  user: AuthUser;
}

export class AuthApiError extends Error {
  constructor(message: string, public readonly code: string, public readonly status: number) {
    super(message);
    this.name = "AuthApiError";
  }
}

async function parseError(response: Response, fallback: string): Promise<AuthApiError> {
  let code = fallback;
  try {
    const body = await response.json();
    if (typeof body?.detail === "string") code = body.detail;
  } catch {
    // 非 JSON 响应沿用兜底文案
  }
  return new AuthApiError(code, code, response.status);
}

export function authErrorMessage(error: unknown): string {
  if (error instanceof AuthApiError) {
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

export async function getAuthStatus(): Promise<AuthStatus> {
  const response = await apiFetch("api/auth/status");
  if (!response.ok) throw await parseError(response, "auth_status_failed");
  return response.json() as Promise<AuthStatus>;
}

async function acceptSession(response: Response): Promise<AuthSession> {
  if (!response.ok) throw await parseError(response, "auth_failed");
  const session = (await response.json()) as AuthSession;
  setToken(session.access_token);
  return session;
}

export async function register(username: string, password: string): Promise<AuthSession> {
  const response = await apiFetch("api/auth/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  return acceptSession(response);
}

export async function login(username: string, password: string): Promise<AuthSession> {
  const response = await apiFetch("api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  return acceptSession(response);
}

export async function getMe(): Promise<AuthUser> {
  const response = await apiFetch("api/auth/me");
  if (!response.ok) throw await parseError(response, "auth_me_failed");
  return response.json() as Promise<AuthUser>;
}

/** 登出只清除本地凭证；服务端没有会话表，也不做 Token 撤销。 */
export function logout(): void {
  clearToken();
}
