import { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  type ChatMessage,
  hasBackendConfig,
  respondApproval,
  resetConversation,
  streamMessage,
} from "./api/chat";
import { appConfig } from "./config";

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

function NewChatIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M12 20H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h9" />
      <path d="M16 3v6m-3-3h6M8 11h8M8 15h5" />
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

export default function App() {
  const [initialChat] = useState(loadChat);
  const [messages, setMessages] = useState<ChatMessage[]>(initialChat.messages);
  const [threadId, setThreadId] = useState(initialChat.threadId);
  const [input, setInput] = useState("");
  const [theme, setTheme] = useState<Theme>(initialTheme);
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState("");
  const [approvalBusyId, setApprovalBusyId] = useState("");
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const configured = hasBackendConfig();

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

  const resizeTextarea = useCallback(() => {
    const element = textareaRef.current;
    if (!element) return;
    element.style.height = "0px";
    element.style.height = `${Math.min(element.scrollHeight, 132)}px`;
  }, []);

  useEffect(resizeTextarea, [input, resizeTextarea]);

  const startNewChat = async () => {
    if (streaming) return;
    setError("");
    try {
      await resetConversation(threadId || undefined);
      setThreadId("");
      setMessages([]);
      setInput("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "新建对话失败");
    }
  };

  const stopStreaming = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    setStreaming(false);
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

    const userMessage: ChatMessage = { id: messageId(), role: "user", content };
    const replyId = messageId();
    const replyPlaceholder: ChatMessage = { id: replyId, role: "assistant", content: "" };
    const controller = new AbortController();
    abortRef.current = controller;
    setInput("");
    setError("");
    setStreaming(true);
    setMessages((current) => [...current, userMessage, replyPlaceholder]);

    try {
      const result = await streamMessage(
        content,
        threadId || undefined,
        {
          onStarted: setThreadId,
          onToken: (token) => {
            setMessages((current) => current.map((message) => (
              message.id === replyId ? { ...message, content: message.content + token } : message
            )));
          },
        },
        controller.signal,
      );
      setThreadId(result.threadId);
      setMessages((current) => current.map((message) => (
        message.id === replyId
          ? { ...message, content: result.reply || message.content, approvals: result.approvals }
          : message
      )));
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
          <button
            type="button"
            className="icon-button"
            onClick={startNewChat}
            disabled={streaming || messages.length === 0}
            aria-label="新建对话"
            title="新建对话"
          >
            <NewChatIcon />
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
          <div className={`composer ${streaming ? "is-streaming" : ""}`}>
            <textarea
              ref={textareaRef}
              rows={1}
              value={input}
              disabled={streaming || !configured}
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
              className={streaming ? "send-button stop" : "send-button"}
              onClick={streaming ? stopStreaming : send}
              disabled={!streaming && (!input.trim() || !configured)}
              aria-label={streaming ? "停止生成" : "发送消息"}
            >
              {streaming ? <StopIcon /> : <SendIcon />}
            </button>
          </div>
          <p className="composer-note">AI 可能会出错，请核对重要信息</p>
        </div>
      </footer>
    </div>
  );
}
