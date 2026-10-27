/**
 * Android 登录 / 首次初始化界面（V49）
 * - 未初始化：创建唯一所有者账号（用户名 + 密码 + 确认密码）
 * - 已初始化：只显示登录表单，不暴露注册入口
 * - 与聊天界面共用同一套 CSS 变量与主题
 */
import { useEffect, useRef, useState, type FormEvent } from "react";
import { authErrorMessage, hasBackendConfig, login, register } from "./api/auth";

type Props = {
  initialized: boolean;
  onAuthenticated: (username: string) => void;
  onInitialized: () => void;
};

export default function LoginScreen({ initialized, onAuthenticated, onInitialized }: Props) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [visible, setVisible] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const usernameRef = useRef<HTMLInputElement>(null);
  const configured = hasBackendConfig();
  const isSetup = !initialized;

  useEffect(() => {
    usernameRef.current?.focus();
  }, [isSetup]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setError("");
    if (!username.trim()) {
      setError("请输入用户名");
      return;
    }
    if (password.length < 8) {
      setError("密码至少 8 位");
      return;
    }
    if (isSetup && password !== confirm) {
      setError("两次输入的密码不一致");
      return;
    }
    setBusy(true);
    try {
      const session = isSetup
        ? await register(username.trim(), password)
        : await login(username.trim(), password);
      onAuthenticated(session.user.username);
    } catch (cause) {
      const message = authErrorMessage(cause);
      if (message.includes("已完成初始化")) onInitialized();
      setError(message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-shell">
      <form className="auth-card" onSubmit={submit} noValidate>
        <div className="auth-brand">
          <img src="./aiive-logo.png" alt="AIive" className="auth-logo" />
          <strong>AIive</strong>
          <span>个人 Agent OS</span>
        </div>

        {!configured && (
          <p className="auth-error" role="alert">
            尚未配置服务器地址，请先填写 apps/android/src/config.ts
          </p>
        )}
        {isSetup && configured && (
          <p className="auth-hint">
            首次使用：创建一个一次性所有者账号。创建后注册入口永久关闭，只能使用该账号登录。
          </p>
        )}

        <label className="auth-field">
          <span>用户名</span>
          <input
            ref={usernameRef}
            type="text"
            autoComplete="username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            placeholder="用户名"
            disabled={busy}
          />
        </label>

        <label className="auth-field">
          <span>密码</span>
          <div className="auth-password">
            <input
              type={visible ? "text" : "password"}
              autoComplete={isSetup ? "new-password" : "current-password"}
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder={isSetup ? "至少 8 位" : "密码"}
              disabled={busy}
            />
            <button
              type="button"
              className="auth-visibility"
              onClick={() => setVisible((value) => !value)}
              aria-label={visible ? "隐藏密码" : "显示密码"}
            >
              {visible ? "隐藏" : "显示"}
            </button>
          </div>
        </label>

        {isSetup && (
          <label className="auth-field">
            <span>确认密码</span>
            <input
              type={visible ? "text" : "password"}
              autoComplete="new-password"
              value={confirm}
              onChange={(event) => setConfirm(event.target.value)}
              placeholder="再次输入密码"
              disabled={busy}
            />
          </label>
        )}

        {error && <p className="auth-error" role="alert">{error}</p>}

        <button type="submit" className="auth-submit" disabled={busy || !configured}>
          {busy ? "请稍候…" : isSetup ? "创建所有者账号" : "登录"}
        </button>
      </form>
    </div>
  );
}
