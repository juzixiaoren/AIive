/**
 * 聊天页面
 * - 核心对话界面，支持流式多 token 打字机效果
 * - 消息持久化到 localStorage，支持页面刷新后恢复
 * - 支持工具调用展示（tool_call）和操作卡片（action_card）
 * - 通过 WebSocket 接收后端主动推送的事件（提醒触发等）
 * - 支持清空上下文、从后端恢复历史消息
 */

import { useState, useEffect, useRef, useCallback } from "react";
import { sendMessageStream, resetThread, ActionCard } from "../api/chat";

const STORAGE_KEY = "aiive_active_thread";

/**
 * 从 localStorage 加载已保存的对话状态
 * @returns 包含 threadId 和 messages 的持久化状态
 */
function loadStored(): { threadId: string | undefined; messages: Message[] } {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return JSON.parse(raw);
  } catch {}
  return { threadId: undefined, messages: [] };
}

/**
 * 将当前对话状态保存到 localStorage（最多保留最近 200 条消息）
 * @param threadId - 当前对话线程 ID
 * @param messages - 消息列表
 */
function saveStored(threadId: string | undefined, messages: Message[]) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({ threadId, messages: messages.slice(-200) }));
}

/** 消息数据结构 */
interface Message {
  role: "user" | "agent" | "action";
  content: string;
  traceId: string;
  threadId: string;
  actionCards?: ActionCard[];
}

/**
 * 聊天页面组件
 * @param onInspectTrace - 点击 trace_id 时回调，用于跳转到上下文检查器
 */
export default function ChatPage({ onInspectTrace }: { onInspectTrace?: (tid: string) => void }) {
  const stored = loadStored();
  const [messages, setMessages] = useState<Message[]>(stored.messages);
  const [input, setInput] = useState("");
  const [threadId, setThreadId] = useState<string | undefined>(stored.threadId);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  /** 滚动到消息列表底部 */
  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  };

  // 每次消息或线程 ID 变化时持久化到 localStorage
  useEffect(() => { saveStored(threadId, messages); }, [threadId, messages]);

  // 消息更新时自动滚动到最新消息
  useEffect(() => { scrollToBottom(); }, [messages]);

  // 组件挂载时，如果本地无消息但有 threadId，从后端恢复历史消息
  useEffect(() => {
    const tid = stored.threadId;
    if (tid && stored.messages.length === 0) {
      loadRemoteMessages(tid);
    }
  }, []);

  /** 从后端加载消息（含 action_cards） */
  const loadRemoteMessages = useCallback((tid: string) => {
    fetch(`/api/threads/${tid}/messages`)
      .then(r => r.json())
      .then((msgs: Array<{role: string; content: string; action_cards?: ActionCard[]; event_id?: string; trace_id?: string}>) => {
        if (msgs.length > 0) {
          const loaded: Message[] = msgs.map(m => ({
            role: m.role === "user" ? "user" : "agent",
            content: m.content,
            traceId: m.trace_id || "",
            threadId: tid,
            actionCards: m.action_cards || [],
          }));
          setMessages(loaded);
        }
      }).catch(() => {});
  }, []);

  // WebSocket 连接：接收后端主动推送的事件（提醒触发等）
  useEffect(() => {
    if (!threadId) return;
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${protocol}//${window.location.host}/ws/${threadId}`;
    let ws: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

    const connect = () => {
      ws = new WebSocket(wsUrl);
      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === "new_message" && msg.data) {
            const { reply, thread_id, trace_id, action_cards } = msg.data;
            if (reply) {
              setMessages(prev => {
                const exists = prev.some(m => m.content === reply);
                if (exists) return prev;
                const newMsg: Message = {
                  role: "agent",
                  content: reply,
                  traceId: trace_id || "",
                  threadId: thread_id || threadId,
                  actionCards: action_cards || [],
                };
                setTimeout(scrollToBottom, 100);
                return [...prev, newMsg];
              });
            }
          }
        } catch {}
      };
      ws.onclose = () => {
        // 5 秒后重连
        reconnectTimer = setTimeout(connect, 5000);
      };
      ws.onerror = () => {
        ws?.close();
      };
    };

    connect();
    return () => {
      ws?.close();
      if (reconnectTimer) clearTimeout(reconnectTimer);
    };
  }, [threadId]);

  /** 处理提醒操作：通过系统指令让 LLM 调用 confirm_reminder / snooze_reminder */
  const handleReminderAction = async (action: "confirm" | "snooze", reminderId: string, delayMin?: number) => {
    setError(null);
    setLoading(true);
    const text = action === "confirm"
      ? `[系统指令] 请调用 confirm_reminder 工具，参数 reminder_id="${reminderId}"`
      : `[系统指令] 请调用 snooze_reminder 工具，参数 reminder_id="${reminderId}"，delay_minutes=${delayMin ?? 5}`;
    // 不添加用户消息到聊天列表，作为系统指令隐藏发送
    const placeholderMsg: Message = {
      role: "agent", content: "", traceId: "", threadId: threadId ?? "",
      actionCards: [],
    };
    setMessages(prev => [...prev, placeholderMsg]);
    try {
      const res = await fetch("/api/chat/system", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, thread_id: threadId }),
      });
      const result = await res.json();
      setMessages(prev => {
        const updated = [...prev];
        for (let i = updated.length - 1; i >= 0; i--) {
          if (updated[i].role === "agent" && updated[i].traceId === "") {
            updated[i] = {
              ...updated[i],
              content: result.reply || "",
              traceId: result.trace_id,
              threadId: result.thread_id,
              actionCards: result.action_cards || [],
            };
            break;
          }
        }
        return updated;
      });
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "未知错误");
    } finally {
      setLoading(false);
    }
  };

  /** 重置对话上下文 */
  const handleReset = async () => {
    if (!window.confirm("确定要清空当前对话上下文吗？")) return;
    try {
      await resetThread(threadId);
    } catch {}
    setThreadId(undefined);
    setMessages([]);
    localStorage.removeItem(STORAGE_KEY);
  };

  /** 发送消息并处理流式响应 */
  const handleSend = async () => {
    const text = input.trim();
    if (!text || loading) return;

    setInput("");
    setError(null);
    setLoading(true);

    // 构造用户消息
    const userMsg: Message = {
      role: "user",
      content: text,
      traceId: "",
      threadId: threadId ?? "(new)",
    };

    // 占位消息：用于流式接收 agent 回复时的占位
    const placeholderIdx = messages.length + 1; // 用户消息之后的索引位置
    const placeholderMsg: Message = {
      role: "agent",
      content: "",
      traceId: "",
      threadId: threadId ?? "",
      actionCards: [],
    };

    setMessages((prev) => [...prev, userMsg, placeholderMsg]);

    try {
      const result = await sendMessageStream(
        text,
        threadId,
        // onToken：逐 token 追加到占位消息中，实现打字机效果
        (token) => {
          setMessages((prev) => {
            const updated = [...prev];
            if (updated.length > placeholderIdx) {
              // 找到正在流式输出的 agent 消息（traceId 为空的最后一条 agent 消息）
              for (let i = updated.length - 1; i >= 0; i--) {
                if (updated[i].role === "agent" && updated[i].traceId === "") {
                  updated[i] = { ...updated[i], content: updated[i].content + token };
                  return updated;
                }
              }
            }
            return updated;
          });
        },
        // onToolCall：在消息列表中插入一条工具调用提示
        (name, params) => {
          setMessages((prev) => {
            const updated = [...prev];
            updated.push({
              role: "action",
              content: `🔧 调用工具: ${name}(${JSON.stringify(params).slice(0, 80)})`,
              traceId: "",
              threadId: threadId ?? "",
            });
            return updated;
          });
        },
        // onToolResult：当前无需特殊处理
        () => {},
        // onActionCard：将操作卡片附加到当前流式 agent 消息上
        (card) => {
          setMessages((prev) => {
            const updated = [...prev];
            for (let i = updated.length - 1; i >= 0; i--) {
              if (updated[i].role === "agent" && updated[i].traceId === "") {
                updated[i] = {
                  ...updated[i],
                  actionCards: [...(updated[i].actionCards || []), card],
                };
                break;
              }
            }
            return updated;
          });
        },
        // onError：设置错误信息
        (msg) => { setError(msg); },
      );

      // 首次对话时记录返回的 thread_id
      if (!threadId) {
        setThreadId(result.thread_id);
      }

      // 流式结束后，用最终 trace_id 和 action_cards 替换占位消息
      setMessages((prev) => {
        const updated = [...prev];
        for (let i = updated.length - 1; i >= 0; i--) {
          if (updated[i].role === "agent" && updated[i].traceId === "") {
            updated[i] = {
              ...updated[i],
              traceId: result.trace_id,
              threadId: result.thread_id,
              actionCards: result.action_cards,
            };
            break;
          }
        }
        return updated;
      });
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "未知错误");
      // 发生错误时移除占位消息（按特征匹配，避免索引漂移）
      setMessages((prev) => prev.filter(
        (m) => !(m.role === "agent" && m.traceId === "" && m.content === "")
      ));
    } finally {
      setLoading(false);
    }
  };

  /** 键盘事件处理：Enter 发送，Shift+Enter 换行 */
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div className="flex flex-col gap-4">
      {/* 消息列表区域 */}
      <div className="flex flex-col gap-3 min-h-[450px] max-h-[65vh] overflow-y-auto rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
        {messages.length === 0 && (
          <div className="flex flex-col items-center justify-center flex-1 text-slate-400 mt-32 gap-2">
            <svg className="w-10 h-10 text-slate-300" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" /></svg>
            <p className="text-sm">发送消息开始对话</p>
          </div>
        )}
        {messages.map((msg, i) => (
          <div key={i} className={`flex flex-col ${msg.role === "user" ? "items-end" : "items-start"}`}>
            {/* 消息气泡，根据角色不同应用不同样式 */}
            <div className={`max-w-[80%] rounded-2xl px-4 py-2.5 text-sm leading-relaxed whitespace-pre-wrap ${
              msg.role === "user"
                ? "bg-blue-500 text-white rounded-br-md"
                : msg.role === "action"
                  ? "bg-amber-50 text-amber-700 rounded-lg border border-amber-200 text-xs"
                  : "bg-slate-100 text-slate-800 rounded-bl-md"
            }`}>
              {/* 流式加载中时显示闪烁光标 */}
              {msg.role === "agent" && msg.content === "" && loading ? (
                <span className="text-slate-400 animate-pulse">▊</span>
              ) : (
                msg.content
              )}
            </div>
            {/* 操作卡片：展示任务创建、记忆修改等工具执行卡片 */}
            {msg.actionCards && msg.actionCards.length > 0 && (
              <div className="flex flex-col gap-1.5 mt-2">
                {msg.actionCards.map((card, j) => (
                  <div key={j}>
                    {/* 提醒卡片：显示确认/延时按钮 */}
                    {card.card_type === "reminder_alert" && (
                      <div className="bg-gradient-to-r from-amber-50 to-orange-50 border border-amber-200 rounded-xl px-4 py-3 shadow-sm">
                        {/* 标题行 */}
                        <div className="flex items-center gap-2 mb-2">
                          <div className="w-6 h-6 rounded-full bg-amber-100 flex items-center justify-center shrink-0">
                            <svg className="w-3.5 h-3.5 text-amber-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9" />
                            </svg>
                          </div>
                          <span className="text-sm font-medium text-amber-800">{card.summary || card.title}</span>
                        </div>
                        {/* 操作区 */}
                        <div className="flex items-center gap-2">
                          <button
                            disabled={loading}
                            onClick={() => handleReminderAction("confirm", card.reminder_id || "")}
                            className="flex items-center gap-1 text-xs px-3.5 py-1.5 rounded-lg bg-emerald-500 text-white font-medium hover:bg-emerald-600 active:scale-95 disabled:opacity-40 transition-all shadow-sm"
                          >
                            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7" />
                            </svg>
                            确认
                          </button>
                          {/* 分隔 */}
                          <span className="text-slate-300 text-xs mx-0.5">|</span>
                          {/* 延时输入 */}
                          <div className="flex items-center gap-1.5 bg-white/70 rounded-lg px-2.5 py-1.5 border border-amber-200">
                            <input
                              type="number"
                              min={1} max={120} defaultValue={5}
                              id={`snooze-${j}-${i}`}
                              className="w-12 text-xs px-1.5 py-0.5 rounded border-0 bg-transparent text-slate-700 focus:outline-none text-center font-mono"
                            />
                            <span className="text-xs text-slate-400 font-medium">分钟后</span>
                          </div>
                          <button
                            disabled={loading}
                            onClick={() => {
                              const el = document.getElementById(`snooze-${j}-${i}`) as HTMLInputElement;
                              const mins = Math.max(1, Math.min(120, Number(el?.value) || 5));
                              handleReminderAction("snooze", card.reminder_id || "", mins);
                            }}
                            className="flex items-center gap-1 text-xs px-3.5 py-1.5 rounded-lg bg-slate-500 text-white font-medium hover:bg-slate-600 active:scale-95 disabled:opacity-40 transition-all shadow-sm"
                          >
                            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
                            </svg>
                            延时
                          </button>
                        </div>
                      </div>
                    )}
                    {/* 其他卡片：保持原有 badge 样式 */}
                    {card.card_type !== "reminder_alert" && (
                      <span className={`text-[11px] px-2 py-0.5 rounded-full border ${
                        card.card_type === "task_created" ? "bg-blue-50 text-blue-600 border-blue-200" :
                        card.card_type === "memory_revised" ? "bg-purple-50 text-purple-600 border-purple-200" :
                        card.card_type === "forget_result" ? "bg-red-50 text-red-600 border-red-200" :
                        "bg-gray-50 text-gray-600 border-gray-200"
                      }`}>📌 {card.title}{card.summary ? `: ${card.summary.slice(0, 30)}` : ""}</span>
                    )}
                  </div>
                ))}
              </div>
            )}
            {/* 元信息：trace_id（可点击跳转上下文检查器）、thread_id */}
            {msg.role !== "action" && (
              <span className="text-[11px] text-slate-400 mt-1 px-1 select-none">
                {msg.role === "agent" ? (
                  msg.content === "" && loading ? (
                    <span className="text-slate-300 animate-pulse">生成中...</span>
                  ) : msg.traceId ? (
                    <span className="inline-flex items-center gap-1.5">
                      <button
                        onClick={() => onInspectTrace?.(msg.traceId)}
                        className="underline hover:text-blue-500 font-mono"
                      >trace:{msg.traceId.slice(0, 8)}</button>
                      <span className="text-slate-300">·</span>
                      <span className="font-mono">thread:{msg.threadId.slice(0, 8)}</span>
                    </span>
                  ) : (
                    <span className="font-mono">thread:{msg.threadId.slice(0, 8)}</span>
                  )
                ) : (
                  <span className="font-mono">thread:{msg.threadId}</span>
                )}
              </span>
            )}
          </div>
        ))}
        <div ref={messagesEndRef} />
      </div>

      {/* 错误提示 */}
      {error && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-2.5 text-sm text-red-600">
          {error}
        </div>
      )}

      {/* 输入区域 */}
      <div className="flex gap-2">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="输入消息..."
          disabled={loading}
          className="flex-1 rounded-xl border border-slate-200 bg-white px-4 py-2.5 text-sm text-slate-800 placeholder-slate-400 focus:outline-none focus:border-blue-400 focus:ring-2 focus:ring-blue-100 disabled:opacity-50 shadow-sm"
        />
        <button
          onClick={handleSend}
          disabled={loading || !input.trim()}
          className="rounded-xl bg-blue-500 px-6 py-2.5 text-sm font-medium text-white hover:bg-blue-600 disabled:opacity-40 disabled:cursor-not-allowed transition-colors shadow-sm"
        >
          发送
        </button>
      </div>
      {/* 清空上下文按钮 */}
      {messages.length > 0 && (
        <div className="flex justify-end">
          <button
            onClick={handleReset}
            disabled={loading}
            className="text-xs text-slate-400 hover:text-red-500 transition-colors px-2 py-1 rounded hover:bg-red-50 disabled:opacity-30"
          >
            清空上下文
          </button>
        </div>
      )}
    </div>
  );
}
