import { useState, useEffect, useRef } from "react";
import { sendMessageStream, resetThread, ActionCard } from "../api/chat";

const STORAGE_KEY = "aiive_active_thread";

function loadStored(): { threadId: string | undefined; messages: Message[] } {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return JSON.parse(raw);
  } catch {}
  return { threadId: undefined, messages: [] };
}

function saveStored(threadId: string | undefined, messages: Message[]) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({ threadId, messages: messages.slice(-200) }));
}

interface Message {
  role: "user" | "agent" | "action";
  content: string;
  traceId: string;
  threadId: string;
  actionCards?: ActionCard[];
}

export default function ChatPage({ onInspectTrace }: { onInspectTrace?: (tid: string) => void }) {
  const stored = loadStored();
  const [messages, setMessages] = useState<Message[]>(stored.messages);
  const [input, setInput] = useState("");
  const [threadId, setThreadId] = useState<string | undefined>(stored.threadId);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  };

  // Persist on change
  useEffect(() => { saveStored(threadId, messages); }, [threadId, messages]);

  // Auto-scroll when messages update
  useEffect(() => { scrollToBottom(); }, [messages]);

  // Load messages from backend on mount (recovery)
  useEffect(() => {
    const tid = stored.threadId;
    if (tid && stored.messages.length === 0) {
      fetch(`/api/threads/${tid}/messages`)
        .then(r => r.json())
        .then((msgs: Array<{role: string; content: string}>) => {
          if (msgs.length > 0) {
            const loaded: Message[] = msgs.map(m => ({
              role: m.role === "user" ? "user" : "agent",
              content: m.content,
              traceId: "",
              threadId: tid,
            }));
            setMessages(loaded);
          }
        }).catch(() => {});
    }
  }, []);

  const handleReset = async () => {
    if (!window.confirm("确定要清空当前对话上下文吗？")) return;
    try {
      await resetThread(threadId);
    } catch {}
    setThreadId(undefined);
    setMessages([]);
    localStorage.removeItem(STORAGE_KEY);
  };

  const handleSend = async () => {
    const text = input.trim();
    if (!text || loading) return;

    setInput("");
    setError(null);
    setLoading(true);

    const userMsg: Message = {
      role: "user",
      content: text,
      traceId: "",
      threadId: threadId ?? "(new)",
    };

    // Placeholder for streaming agent response
    const placeholderIdx = messages.length + 1; // index after user msg
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
        // onToken
        (token) => {
          setMessages((prev) => {
            const updated = [...prev];
            if (updated.length > placeholderIdx) {
              // Find the agent message being streamed (the last one)
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
        // onToolCall
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
        // onToolResult
        () => {},
        // onActionCard
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
        // onError
        (msg) => { setError(msg); },
      );

      if (!threadId) {
        setThreadId(result.thread_id);
      }

      // Finalize the streaming agent message with trace_id
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
      // Remove placeholder on error
      setMessages((prev) => prev.filter((_m, i) => i !== placeholderIdx));
    } finally {
      setLoading(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div className="flex flex-col gap-4">
      {/* 消息区 */}
      <div className="flex flex-col gap-3 min-h-[450px] max-h-[65vh] overflow-y-auto rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
        {messages.length === 0 && (
          <div className="flex flex-col items-center justify-center flex-1 text-slate-400 mt-32 gap-2">
            <svg className="w-10 h-10 text-slate-300" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" /></svg>
            <p className="text-sm">发送消息开始对话</p>
          </div>
        )}
        {messages.map((msg, i) => (
          <div key={i} className={`flex flex-col ${msg.role === "user" ? "items-end" : "items-start"}`}>
            <div className={`max-w-[80%] rounded-2xl px-4 py-2.5 text-sm leading-relaxed whitespace-pre-wrap ${
              msg.role === "user"
                ? "bg-blue-500 text-white rounded-br-md"
                : msg.role === "action"
                  ? "bg-amber-50 text-amber-700 rounded-lg border border-amber-200 text-xs"
                  : "bg-slate-100 text-slate-800 rounded-bl-md"
            }`}>
              {msg.role === "agent" && msg.content === "" && loading ? (
                <span className="text-slate-400 animate-pulse">▊</span>
              ) : (
                msg.content
              )}
            </div>
            {/* Action Cards */}
            {msg.actionCards && msg.actionCards.length > 0 && (
              <div className="flex flex-wrap gap-1 mt-1">
                {msg.actionCards.map((card, j) => (
                  <span key={j} className={`text-[11px] px-2 py-0.5 rounded-full border ${
                    card.card_type === "task_created" ? "bg-blue-50 text-blue-600 border-blue-200" :
                    card.card_type === "memory_revised" ? "bg-purple-50 text-purple-600 border-purple-200" :
                    card.card_type === "forget_result" ? "bg-red-50 text-red-600 border-red-200" :
                    "bg-gray-50 text-gray-600 border-gray-200"
                  }`}>📌 {card.title}{card.summary ? `: ${card.summary.slice(0, 30)}` : ""}</span>
                ))}
              </div>
            )}
            {msg.role !== "action" && (
              <span className="text-[11px] text-slate-400 mt-1 px-1 select-none">
                {msg.role === "agent" && msg.traceId ? (
                  <span className="inline-flex items-center gap-1.5">
                    <button
                      onClick={() => onInspectTrace?.(msg.traceId)}
                      className="underline hover:text-blue-500 font-mono"
                    >trace:{msg.traceId.slice(0, 8)}</button>
                    <span className="text-slate-300">·</span>
                    <span className="font-mono">thread:{msg.threadId.slice(0, 8)}</span>
                  </span>
                ) : msg.role === "agent" ? (
                  <span className="text-slate-300 animate-pulse">生成中...</span>
                ) : (
                  <span className="font-mono">thread:{msg.threadId}</span>
                )}
              </span>
            )}
          </div>
        ))}
        <div ref={messagesEndRef} />
      </div>

      {/* 错误 */}
      {error && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-2.5 text-sm text-red-600">
          {error}
        </div>
      )}

      {/* 输入 */}
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
