/**
 * 全局认证状态（V49）
 * - 应用内唯一的登录态来源：页面不得各自维护 Token
 * - 启动时先查 /api/auth/status，再按本地 Token 调 /api/auth/me 恢复登录
 * - 任何受保护接口返回 401 都会清 Token 并回到未登录态
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  authErrorMessage,
  getAuthStatus,
  getMe,
  login as loginRequest,
  logout as logoutRequest,
  register as registerRequest,
} from "../api/auth";
import { AUTH_EXPIRED_EVENT, getToken } from "../api/client";
import { syncDesktopToken } from "../lib/desktop";
import { disconnectNotificationSocket } from "./useNotificationSocket";

export type AuthPhase = "loading" | "anonymous" | "authenticated";

interface AuthContextValue {
  phase: AuthPhase;
  initialized: boolean;
  username: string;
  error: string;
  busy: boolean;
  login: (username: string, password: string) => Promise<void>;
  register: (username: string, password: string) => Promise<void>;
  logout: () => void;
  clearError: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [phase, setPhase] = useState<AuthPhase>("loading");
  const [initialized, setInitialized] = useState(true);
  const [username, setUsername] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const bootstrap = async () => {
      try {
        const status = await getAuthStatus();
        if (cancelled) return;
        setInitialized(status.initialized);
        if (!status.initialized || !getToken()) {
          setPhase("anonymous");
          return;
        }
        const me = await getMe();
        if (cancelled) return;
        // 桌面版：恢复登录态后同步 Token，Node Host 才能完成 WebSocket 认证。
        syncDesktopToken(getToken());
        setUsername(me.username);
        setPhase("authenticated");
      } catch {
        if (!cancelled) setPhase("anonymous");
      }
    };
    void bootstrap();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const handler = () => {
      syncDesktopToken(null);
      setUsername("");
      setPhase("anonymous");
    };
    window.addEventListener(AUTH_EXPIRED_EVENT, handler);
    return () => window.removeEventListener(AUTH_EXPIRED_EVENT, handler);
  }, []);

  const login = useCallback(async (name: string, password: string) => {
    setBusy(true);
    setError("");
    try {
      const session = await loginRequest(name, password);
      syncDesktopToken(session.access_token);
      setUsername(session.user.username);
      setInitialized(true);
      setPhase("authenticated");
    } catch (reason) {
      setError(authErrorMessage(reason));
    } finally {
      setBusy(false);
    }
  }, []);

  const register = useCallback(async (name: string, password: string) => {
    setBusy(true);
    setError("");
    try {
      const session = await registerRequest(name, password);
      syncDesktopToken(session.access_token);
      setUsername(session.user.username);
      setInitialized(true);
      setPhase("authenticated");
    } catch (reason) {
      const message = authErrorMessage(reason);
      // 并发注册：后端返回 409 说明系统已被初始化，直接切到登录表单。
      if (message.includes("已完成初始化")) setInitialized(true);
      setError(message);
    } finally {
      setBusy(false);
    }
  }, []);

  const logout = useCallback(() => {
    logoutRequest();
    syncDesktopToken(null);
    disconnectNotificationSocket();
    setUsername("");
    setPhase("anonymous");
  }, []);

  const clearError = useCallback(() => setError(""), []);

  const value = useMemo<AuthContextValue>(
    () => ({ phase, initialized, username, error, busy, login, register, logout, clearError }),
    [phase, initialized, username, error, busy, login, register, logout, clearError],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth 必须在 AuthProvider 内使用");
  return value;
}
