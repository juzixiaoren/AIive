/**
 * 登录 / 首次初始化页（V49）
 * - 未初始化：显示一次性创建所有者账号的表单（用户名 + 密码 + 确认密码）
 * - 已初始化：只显示登录表单，不暴露任何注册入口
 * - 视觉沿用 AIive 现有语义色令牌，自动适配深色/浅色主题
 */
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { useAuth } from "../hooks/useAuth";
import { BASE_PATH } from "../lib/base";

function EyeIcon({ open }: { open: boolean }) {
  return open ? (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7Z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  ) : (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3 3l18 18" />
      <path d="M10.6 5.2A10.9 10.9 0 0 1 12 5c6.4 0 10 7 10 7a17.6 17.6 0 0 1-3.2 4.2" />
      <path d="M6.3 6.5A17.4 17.4 0 0 0 2 12s3.6 7 10 7a10.7 10.7 0 0 0 4.2-.8" />
      <path d="M9.9 9.9a3 3 0 0 0 4.2 4.2" />
    </svg>
  );
}

export default function LoginPage() {
  const { initialized, busy, error, login, register, clearError } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [localError, setLocalError] = useState("");
  const usernameRef = useRef<HTMLInputElement>(null);

  const isSetup = !initialized;

  useEffect(() => {
    usernameRef.current?.focus();
  }, [isSetup]);

  const onSubmit = useCallback(
    async (event: FormEvent) => {
      event.preventDefault();
      setLocalError("");
      clearError();
      if (!username.trim()) {
        setLocalError("请输入用户名");
        return;
      }
      if (password.length < 8) {
        setLocalError("密码至少 8 位");
        return;
      }
      if (isSetup && password !== confirm) {
        setLocalError("两次输入的密码不一致");
        return;
      }
      if (isSetup) await register(username.trim(), password);
      else await login(username.trim(), password);
    },
    [clearError, confirm, initialized, isSetup, login, password, register, username],
  );

  const message = localError || error;

  return (
    <div className="min-h-screen flex items-center justify-center bg-background px-4 py-10">
      <div className="w-full max-w-sm rounded-2xl border border-divider bg-surface p-6 sm:p-8 shadow-sm">
        <div className="flex flex-col items-center text-center">
          <img
            src={`${BASE_PATH}aiive-logo.png`}
            alt="AIive Logo"
            className="h-14 w-14 object-contain drop-shadow-sm"
          />
          <h1 className="mt-3 text-2xl font-bold tracking-tight text-title">AIive</h1>
          <p className="mt-1 text-sm text-muted">个人 Agent OS</p>
        </div>

        <form className="mt-6 space-y-4" onSubmit={onSubmit} noValidate>
          {isSetup && (
            <p className="rounded-lg border border-primary-border bg-primary-soft px-3 py-2 text-xs leading-relaxed text-content">
              首次使用：创建一个一次性所有者账号。创建后注册入口永久关闭，只能使用该账号登录。
            </p>
          )}

          <div className="space-y-1.5">
            <label htmlFor="aiive-username" className="block text-xs font-medium text-muted">
              用户名
            </label>
            <input
              id="aiive-username"
              ref={usernameRef}
              type="text"
              autoComplete="username"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              className="w-full rounded-lg border border-divider bg-surface px-3 py-2 text-sm text-content outline-none transition-colors focus:border-primary focus:ring-2 focus:ring-primary-ring"
              placeholder="用户名"
              disabled={busy}
            />
          </div>

          <div className="space-y-1.5">
            <label htmlFor="aiive-password" className="block text-xs font-medium text-muted">
              密码
            </label>
            <div className="relative">
              <input
                id="aiive-password"
                type={showPassword ? "text" : "password"}
                autoComplete={isSetup ? "new-password" : "current-password"}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                className="w-full rounded-lg border border-divider bg-surface px-3 py-2 pr-11 text-sm text-content outline-none transition-colors focus:border-primary focus:ring-2 focus:ring-primary-ring"
                placeholder={isSetup ? "至少 8 位" : "密码"}
                disabled={busy}
              />
              <button
                type="button"
                onClick={() => setShowPassword((value) => !value)}
                className="absolute inset-y-0 right-0 flex w-11 items-center justify-center text-faint transition-colors hover:text-content"
                aria-label={showPassword ? "隐藏密码" : "显示密码"}
                title={showPassword ? "隐藏密码" : "显示密码"}
                tabIndex={-1}
              >
                <EyeIcon open={showPassword} />
              </button>
            </div>
          </div>

          {isSetup && (
            <div className="space-y-1.5">
              <label htmlFor="aiive-confirm" className="block text-xs font-medium text-muted">
                确认密码
              </label>
              <input
                id="aiive-confirm"
                type={showPassword ? "text" : "password"}
                autoComplete="new-password"
                value={confirm}
                onChange={(event) => setConfirm(event.target.value)}
                className="w-full rounded-lg border border-divider bg-surface px-3 py-2 text-sm text-content outline-none transition-colors focus:border-primary focus:ring-2 focus:ring-primary-ring"
                placeholder="再次输入密码"
                disabled={busy}
              />
            </div>
          )}

          {message && (
            <p role="alert" className="rounded-lg border border-danger-border bg-danger-soft px-3 py-2 text-xs text-danger-text">
              {message}
            </p>
          )}

          <button
            type="submit"
            disabled={busy}
            className="flex w-full items-center justify-center gap-2 rounded-lg bg-primary px-4 py-2.5 text-sm font-medium text-on-primary transition-colors hover:bg-primary-hover disabled:cursor-not-allowed disabled:opacity-60"
          >
            {busy && (
              <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-on-primary border-t-transparent" aria-hidden="true" />
            )}
            {isSetup ? "创建所有者账号" : "登录"}
          </button>
        </form>

        <p className="mt-5 text-center text-[11px] leading-relaxed text-faint">
          {isSetup ? "仅在首次部署时出现，注册后不再显示" : "仅限本机所有者账号登录"}
        </p>
      </div>
    </div>
  );
}
