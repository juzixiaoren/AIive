import { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  type ChatMessage,
  type ExecutionStatus,
  type ThreadHistoryMessage,
  approvalsFromCards,
  getCurrentConversation,
  getExecutionStatus,
  getThreadMessages,
  hasBackendConfig,
  interruptTurn,
  respondApproval,
  streamMessage,
  subscribeThreadEvents,
} from "./api/chat";
import { appConfig } from "./config";
import LoginScreen from "./LoginScreen";
import { AUTH_EXPIRED_EVENT, getAuthStatus, getMe, getToken, logout } from "./api/auth";

type Theme = "light" | "dark";

interface StoredChat {
  threadId?: string;
  messages?: ChatMessage[];
}

function messageId(): string {
  try {
    return crypto.randomUUID();
  } catch {
    return `message-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  }
}

function loadChat(): Required<StoredChat> {
  try {
    const value = JSON.parse(localStorage.getItem(appConfig.storageKey) || "{}") as StoredChat;
    return {
      threadId: value.threadId || "",
      messages: (value.messages || []).filter(
        (message) => message.content && (message.role === "user" || message.role === "assistant"),
      ).slice(-200),
    };
  } catch {
    return { threadId: "", messages: [] };
  }
}

function initialTheme(): Theme {
  const stored = localStorage.getItem(appConfig.themeKey);
  if (stored === "light" || stored === "dark") return stored;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function SunIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2m0 16v2M4.93 4.93l1.42 1.42m11.3 11.3 1.42 1.42M2 12h2m16 0h2M4.93 19.07l1.42-1.42m11.3-11.3 1.42-1.42" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M20.7 15.1A8.5 8.5 0 0 1 8.9 3.3 8.5 8.5 0 1 0 20.7 15.1Z" />
    </svg>
  );
}

function SendIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="m4 12 16-8-6.2 16-2.2-6.2L4 12Z" />
      <path d="m11.6 13.8 4-4" />
    </svg>
  );
}

function StopIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <rect x="7" y="7" width="10" height="10" rx="2" />
    </svg>
  );
}

/** 服务器历史是事实来源：本地缓存只用于展示加速，启动时一律以服务端覆盖。 */
function historyToMessages(messages: ThreadHistoryMessage[]): ChatMessage[] {
  return messages
    .map((message) => ({
      id: message.event_id || messageId(),
      role: message.role === "user" ? "user" as const : "assistant" as const,
      content: message.content,
      approvals: approvalsFromCards(Array.isArray(message.action_cards) ? message.action_cards : []),
    }))
    .filter((message) => message.content || (message.approvals?.length ?? 0) > 0);
}

function ChatApp({ username, onLogout }: { username: string; onLogout: () => void }) {
  const [initialChat] = useState(loadChat);
  const [messages, setMessages] = useState<ChatMessage[]>(initialChat.messages);
  const [threadId, setThreadId] = useState(initialChat.threadId);
  const [input, setInput] = useState("");
  const [theme, setTheme] = useState<Theme>(initialTheme);
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState("");
  const [approvalBusyId, setApprovalBusyId] = useState("");
  /** 服务端线程级执行状态：idle / running（可中断）/ busy（后台占用）。 */
  const [execState, setExecState] = useState<"idle" | "running" | "busy">("idle");
  const [remoteRunning, setRemoteRunning] = useState(false);
  const remoteRunningRef = useRef(false);
  const [stopping, setStopping] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const streamingRef = useRef(false);
  const activeTurnRecordIdRef = useRef("");
  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const configured = hasBackendConfig();

  /** 用服务端状态收敛发送/停止按钮（初始化、WS 广播、重连共用）。 */
  const applyStatus = useCallback((status: ExecutionStatus) => {
    setExecState(status.state);
    if (status.state === "running" && status.active_turn?.turn_record_id) {
      activeTurnRecordIdRef.current = status.active_turn.turn_record_id;
      const remote = !streamingRef.current && !status.interrupting;
      remoteRunningRef.current = remote;
      setRemoteRunning(remote);
    } else {
      activeTurnRecordIdRef.current = "";
      remoteRunningRef.current = false;
      setRemoteRunning(false);
    }
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    localStorage.setItem(appConfig.themeKey, theme);
    const themeColor = theme === "dark" ? "#111214" : "#f5f5f3";
    document.querySelector<HTMLMetaElement>('meta[name="theme-color"]')?.setAttribute("content", themeColor);
  }, [theme]);

  useEffect(() => {
    try {
      localStorage.setItem(
        appConfig.storageKey,
        JSON.stringify({ threadId, messages: messages.filter((message) => message.content).slice(-200) }),
      );
    } catch {
      // Storage exhaustion must never interrupt a conversation.
    }
  }, [messages, threadId]);

  useEffect(() => {
    const element = scrollRef.current;
    if (!element) return;
    element.scrollTo({ top: element.scrollHeight, behavior: streaming ? "auto" : "smooth" });
  }, [messages, streaming]);

  useEffect(() => () => abortRef.current?.abort(), []);

  // 启动即从服务器获取唯一长期 Thread 与历史（三端共用同一 Thread ID）。
  useEffect(() => {
    if (!configured) return;
    let disposed = false;
    const bootstrap = async () => {
      try {
        const tid = await getCurrentConversation();
        if (disposed || !tid) return;
        setThreadId(tid);
        setMessages(historyToMessages((await getThreadMessages(tid)).messages));
        if (disposed) return;
        applyStatus(await getExecutionStatus(tid));
      } catch (cause) {
        if (!disposed) setError(cause instanceof Error ? cause.message : "对话恢复失败");
      }
    };
    void bootstrap();
    return () => { disposed = true; };
  }, [applyStatus, configured]);

  /** 以服务器历史为准刷新消息（其他设备结束/取消后调用）。 */
  const refreshHistory = useCallback(async () => {
    if (!threadId) return;
    try {
      setMessages(historyToMessages((await getThreadMessages(threadId)).messages));
    } catch {
      // 历史刷新失败不打断当前会话，下一次操作会再次对齐。
    }
  }, [threadId]);

  // 订阅线程 WebSocket：其他设备开始执行/结束/取消时同步按钮状态。
  useEffect(() => {
    if (!configured || !threadId) return;
    return subscribeThreadEvents(threadId, (status) => {
      const wasRemote = remoteRunningRef.current;
      applyStatus(status);
      if (status.state === "idle" && wasRemote) void refreshHistory();
    });
  }, [applyStatus, configured, refreshHistory, threadId]);

  const resizeTextarea = useCallback(() => {
    const element = textareaRef.current;
    if (!element) return;
    element.style.height = "0px";
    element.style.height = `${Math.min(element.scrollHeight, 132)}px`;
  }, []);

  useEffect(resizeTextarea, [input, resizeTextarea]);

  /** 停止：走服务端中断（持久化取消信号），不本地 abort，等收口广播/事件。 */
  const stopStreaming = async () => {
    if (!threadId || stopping) return;
    setStopping(true);
    setError("");
    try {
      await interruptTurn(threadId, activeTurnRecordIdRef.current || undefined);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "停止失败，请稍后重试");
    } finally {
      setStopping(false);
    }
  };

  const handleApproval = async (approvalId: string, action: "approve" | "deny") => {
    if (!approvalId || approvalBusyId) return;
    setApprovalBusyId(approvalId);
    setError("");
    setMessages((current) => current.map((message) => ({
      ...message,
      approvals: message.approvals?.map((approval) => (
        approval.approvalId === approvalId ? { ...approval, status: "executing" } : approval
      )),
    })));
    try {
      const response = await respondApproval(approvalId, action);
      setMessages((current) => current.map((message) => ({
        ...message,
        approvals: message.approvals?.map((approval) => (
          approval.approvalId === approvalId
            ? {
              ...approval,
              status: response.action,
              result: response.tool_result,
              error: response.error,
            }
            : approval
        )),
      })));
      if (response.error) setError(response.error);
    } catch (cause) {
      setMessages((current) => current.map((message) => ({
        ...message,
        approvals: message.approvals?.map((approval) => (
          approval.approvalId === approvalId ? { ...approval, status: "pending" } : approval
        )),
      })));
      setError(cause instanceof Error ? cause.message : "审批失败，请稍后重试");
    } finally {
      setApprovalBusyId("");
    }
  };

  const send = async () => {
    const content = input.trim();
    if (!content || streaming || !configured) return;
    if (execState !== "idle") {
      // 忙状态下的消息不排队、不落库。
      setError(execState === "busy" ? "后台任务处理中，请稍候" : "Agent 正在回复，请稍候");
      return;
    }

    const userMessage: ChatMessage = { id: messageId(), role: "user", content };
    const replyId = messageId();
    const replyPlaceholder: ChatMessage = { id: replyId, role: "assistant", content: "" };
    const controller = new AbortController();
    abortRef.current = controller;
    setInput("");
    setError("");
    setExecState("running");
    setRemoteRunning(false);
    activeTurnRecordIdRef.current = "";
    setStreaming(true);
    streamingRef.current = true;
    setMessages((current) => [...current, userMessage, replyPlaceholder]);

    try {
      const result = await streamMessage(
        content,
        threadId || undefined,
        {
          onStarted: (nextThreadId, turnRecordId) => {
            setThreadId(nextThreadId);
            activeTurnRecordIdRef.current = turnRecordId;
          },
          onToken: (token) => {
            setMessages((current) => current.map((message) => (
              message.id === replyId ? { ...message, content: message.content + token } : message
            )));
          },
        },
        controller.signal,
      );
      if (result.cancelled) {
        // 服务端已丢弃本轮用户消息：本地移除乐观气泡并恢复服务器历史。
        setMessages((current) => current.filter(
          (message) => message.id !== userMessage.id && message.id !== replyId,
        ));
        setMessages(historyToMessages((await getThreadMessages(result.threadId || threadId)).messages));
      } else {
        setThreadId(result.threadId);
        setMessages((current) => current.map((message) => (
          message.id === replyId
            ? { ...message, content: result.reply || message.content, approvals: result.approvals }
            : message
        )));
      }
    } catch (cause) {
      if (!controller.signal.aborted) {
        setError(cause instanceof Error ? cause.message : "发送失败，请稍后重试");
        setMessages((current) => current.filter(
          (message) => message.id !== replyId || Boolean(message.content),
        ));
      } else {
        // 停止发生在首个 token 之前时，移除空白占位气泡；已有内容则保留。
        setMessages((current) => current.filter(
          (message) => message.id !== replyId || Boolean(message.content),
        ));
      }
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      setStreaming(false);
      streamingRef.current = false;
      // 本轮结束先恢复发送；若随后进入后台占用，WS 广播会再次收敛为处理中。
      setExecState("idle");
    }
  };

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="brand">
          <img src="./aiive-logo.png" alt="" className="brand-logo" />
          <div className="brand-copy">
            <strong>AIive</strong>
            <span className={configured ? "connection online" : "connection unconfigured"}>
              <i aria-hidden="true" />
              {configured ? "已连接" : "未配置服务器"}
            </span>
          </div>
        </div>
        <div className="header-actions">
          <span className="account-chip" title={username}>{username}</span>
          <button
            type="button"
            className="icon-button"
            onClick={onLogout}
            aria-label="退出登录"
            title="退出登录"
          >
            退出
          </button>
          <button
            type="button"
            className="icon-button"
            onClick={() => setTheme((current) => current === "dark" ? "light" : "dark")}
            aria-label={theme === "dark" ? "切换到浅色主题" : "切换到深色主题"}
            title={theme === "dark" ? "浅色主题" : "深色主题"}
          >
            {theme === "dark" ? <SunIcon /> : <MoonIcon />}
          </button>
        </div>
      </header>

      <main className="conversation" ref={scrollRef}>
        <div className="message-list">
          {messages.length === 0 && (
            <section className="empty-state">
              <img src="./aiive-logo.png" alt="AIive" className="empty-logo" />
              <h1>想聊点什么？</h1>
              <p>
                {configured
                  ? "我会在这里陪你继续每一次对话。"
                  : "请先在 apps/android/src/config.ts 中填写服务器地址，再重新构建 APK。"}
              </p>
            </section>
          )}

          {messages.map((message) => (
            <article key={message.id} className={`message-row ${message.role}`}>
              {message.role === "assistant" && (
                <img src="./aiive-logo.png" alt="" className="message-avatar" />
              )}
              <div className={`message-stack ${message.role}`}>
                <div className={`message-bubble ${message.role}`}>
                  {message.role === "assistant" && !message.content && streaming ? (
                    <span className="typing" aria-label="正在回复">
                      <i /><i /><i />
                    </span>
                  ) : message.role === "assistant" && !streaming ? (
                    <div className="markdown">
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
                    </div>
                  ) : (
                    <span className="plain-message">{message.content}</span>
                  )}
                </div>
                {message.approvals?.map((approval) => {
                  const busy = approval.status === "executing" || approvalBusyId === approval.approvalId;
                  const pending = approval.status === "pending";
                  const stateLabel = busy ? "执行中…"
                    : pending ? "等待确认"
                      : approval.status === "succeeded" ? "执行成功"
                        : approval.status === "denied" ? "已拒绝"
                          : approval.status === "interrupted_unknown" ? "结果未知"
                            : "执行失败";
                  return (
                    <section className="approval-card" key={approval.approvalId}>
                      <div className="approval-title">
                        <strong>需要确认：{approval.toolName}</strong>
                        <span>{stateLabel}</span>
                      </div>
                      <pre>{JSON.stringify(approval.params, null, 2)}</pre>
                      {approval.result !== undefined && (
                        <pre>{typeof approval.result === "string" ? approval.result : JSON.stringify(approval.result, null, 2)}</pre>
                      )}
                      {approval.error && <p className="approval-error">{approval.error}</p>}
                      {pending && (
                        <div className="approval-actions">
                          <button
                            type="button"
                            className="approve"
                            disabled={Boolean(approvalBusyId)}
                            onClick={() => void handleApproval(approval.approvalId, "approve")}
                          >确认执行</button>
                          <button
                            type="button"
                            disabled={Boolean(approvalBusyId)}
                            onClick={() => void handleApproval(approval.approvalId, "deny")}
                          >拒绝</button>
                        </div>
                      )}
                    </section>
                  );
                })}
              </div>
            </article>
          ))}
        </div>
      </main>

      <footer className="composer-area">
        <div className="composer-inner">
          {error && <div className="error-banner" role="alert">{error}</div>}
          {execState !== "idle" && (
            <div className="status-banner" role="status">
              {execState === "busy"
                ? "后台任务处理中，请稍候…"
                : remoteRunning ? "Agent 正在其他设备回复…" : "Agent 正在回复…"}
            </div>
          )}
          <div className={`composer ${streaming ? "is-streaming" : ""}`}>
            <textarea
              ref={textareaRef}
              rows={1}
              value={input}
              disabled={streaming || execState !== "idle" || !configured}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                  event.preventDefault();
                  void send();
                }
              }}
              placeholder={configured ? "发消息…" : "请先配置服务器地址"}
              aria-label="消息"
            />
            <button
              type="button"
              className={execState === "running" ? "send-button stop" : "send-button"}
              onClick={execState === "running" ? () => void stopStreaming() : send}
              disabled={
                execState === "busy"
                || stopping
                || (execState === "idle" && (!input.trim() || !configured))
              }
              aria-label={
                execState === "running" ? "停止生成" : execState === "busy" ? "处理中" : "发送消息"
              }
            >
              {execState === "running" ? <StopIcon /> : <SendIcon />}
            </button>
          </div>
          <p className="composer-note">AI 可能会出错，请核对重要信息</p>
        </div>
      </footer>
    </div>
  );
}


/**
 * 应用入口（V49）：先确定认证状态，再决定渲染登录界面还是聊天界面。
 * - 未初始化：显示一次性创建所有者账号表单
 * - 已初始化且本地有有效 Token：直接进入聊天
 * - 任意受保护请求返回 401：清除凭证并回到登录界面（不无限重试）
 */
export default function App() {
  const [phase, setPhase] = useState<"loading" | "anonymous" | "authenticated">("loading");
  const [initialized, setInitialized] = useState(true);
  const [username, setUsername] = useState("");

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
      setUsername("");
      setPhase("anonymous");
    };
    window.addEventListener(AUTH_EXPIRED_EVENT, handler);
    return () => window.removeEventListener(AUTH_EXPIRED_EVENT, handler);
  }, []);

  if (phase === "loading") {
    return (
      <div className="auth-shell">
        <p className="auth-hint">正在验证登录状态…</p>
      </div>
    );
  }

  if (phase === "anonymous") {
    return (
      <LoginScreen
        initialized={initialized}
        onAuthenticated={(name) => {
          setUsername(name);
          setPhase("authenticated");
        }}
        onInitialized={() => setInitialized(true)}
      />
    );
  }

  return (
    <ChatApp
      username={username}
      onLogout={() => {
        logout();
        setUsername("");
        setPhase("anonymous");
      }}
    />
  );
}
