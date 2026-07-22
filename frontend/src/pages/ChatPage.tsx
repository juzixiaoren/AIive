/**
 * 聊天页面
 * - 核心对话界面，支持流式多 token 打字机效果
 * - 消息持久化到 localStorage，支持页面刷新后恢复
 * - 支持工具调用展示（tool_call）和操作卡片（action_card）
 * - 通过 WebSocket 接收后端主动推送的事件（提醒触发等）
 * - 支持清空上下文、从后端恢复历史消息
 * - 消息内容支持 Markdown 渲染（GFM + 代码高亮）；流式输出期间回退为纯文本，避免不完整 Markdown 频繁重排
 */

import { useState, useEffect, useRef, useCallback } from "react";
import {
  sendMessageStream,
  resetThread,
  getThreadMessages,
  ActionCard,
  StreamIncompleteError,
} from "../api/chat";
import Markdown from "../components/Markdown";

const STORAGE_KEY = "aiive_active_thread";

/**
 * 从 localStorage 加载已保存的对话状态
 * @returns 包含 threadId 和 messages 的持久化状态
 */
function loadStored(): { threadId: string | undefined; messages: Message[] } {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as { threadId?: string; messages?: Message[] };
      // 清理上一次未完成的空占位消息（流式被中断、刷新页面导致的残留），不影响已完成内容
      const messages = (parsed.messages || [])
        .filter((m) => !(m.role === "agent" && !m.content && !m.traceId))
        .map((m) => (m.id ? m : { ...m, id: genMessageId() }));
      return { threadId: parsed.threadId, messages };
    }
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

/** 单条工具调用的展示模型，状态随流推进并保留无法确认的真实终态 */
interface ToolCallItem {
  id: string;
  name: string;
  params: Record<string, unknown>;
  status: "pending" | "completed" | "failed" | "execution_unknown" | "cancelled";
  result?: string;
}

/** 将后端及历史兼容状态收敛为 UI 状态；未识别状态不得声明完成。 */
function normalizeToolCallStatus(status?: string): ToolCallItem["status"] {
  switch (status) {
    case "completed":
    case "committed":
    case "succeeded":
      return "completed";
    case "failed":
    case "error":
    case "execution_failed":
      return "failed";
    case "pending":
    case "pending_approval":
      return "pending";
    case "cancelled":
    case "preempted":
      return "cancelled";
    case "queued":
    case "running":
    case "execution_unknown":
    case "interrupted_unknown":
    case "unknown":
    default:
      return "execution_unknown";
  }
}

/** 生成消息唯一标识，作为稳定的 React 列表 key，避免头部插入更早消息时整体重挂载 */
function genMessageId(): string {
  try {
    return crypto.randomUUID();
  } catch {
    return `m-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  }
}

/** 消息数据结构 */
interface Message {
  /** 稳定唯一标识：后端历史取 event_id，本地消息取 crypto.randomUUID */
  id: string;
  role: "user" | "agent" | "action";
  content: string;
  traceId: string;
  threadId: string;
  actionCards?: ActionCard[];
  /** 结构化工具调用列表，替代原先扁平的 action 文本气泡 */
  toolCalls?: ToolCallItem[];
}

/**
 * 单条工具调用的展示卡片
 * - pending：旋转 spinner，表示正在执行（同时作为占位，避免结果到达前抖动）
 * - completed / failed / cancelled：状态 chip + 可折叠的参数与结果
 */
function ToolCallCard({ call }: { call: ToolCallItem }) {
  const [open, setOpen] = useState(false);

  const statusMeta = {
    pending: {
      label: "执行中",
      chip: "bg-primary-soft text-primary-border border-primary-border",
      icon: <span className="w-3.5 h-3.5 rounded-full border-2 border-primary-ring border-t-primary animate-spin" />,
    },
    completed: {
      label: "完成",
      chip: "bg-success-soft text-success-text border-success-border",
      icon: (
        <svg className="w-3.5 h-3.5 text-success-text" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7" />
        </svg>
      ),
    },
    failed: {
      label: "失败",
      chip: "bg-danger-soft text-danger-hover border-danger-border",
      icon: (
        <svg className="w-3.5 h-3.5 text-danger" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M6 18L18 6M6 6l12 12" />
        </svg>
      ),
    },
    execution_unknown: {
      label: "待确认",
      chip: "bg-warning-soft text-warning-text border-warning-border",
      icon: <span className="w-3.5 h-3.5 rounded-full border-2 border-warning-border border-t-transparent animate-spin" />,
    },
    cancelled: {
      label: "已停止",
      chip: "bg-surface-muted text-muted border-divider",
      icon: (
        <svg className="w-3.5 h-3.5 text-faint" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M6 18L18 6M6 6l12 12" />
        </svg>
      ),
    },
  }[call.status];

  const paramStr = call.params ? JSON.stringify(call.params) : "";
  const resultStr = call.result ?? "";

  return (
    <div className="animate-tool-in rounded-xl border border-divider bg-surface px-3 py-2 shadow-sm">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center gap-2 text-left focus:outline-none"
      >
        <span className="shrink-0 flex items-center justify-center w-4 h-4">{statusMeta.icon}</span>
        <span className="text-sm font-medium text-title truncate">🔧 {call.name}</span>
        <span className={`ml-auto text-[11px] px-2 py-0.5 rounded-full border whitespace-nowrap ${statusMeta.chip}`}>
          {statusMeta.label}
        </span>
        <svg
          className={`w-3.5 h-3.5 text-faint transition-transform duration-200 ${open ? "rotate-180" : ""}`}
          fill="none" stroke="currentColor" viewBox="0 0 24 24"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>
      {open && (
        <div className="mt-2 flex flex-col gap-1.5 animate-fade-in">
          <div>
            <div className="text-[11px] font-medium text-faint mb-0.5">参数</div>
            <pre className="text-[11px] text-code bg-background rounded-lg px-2.5 py-1.5 whitespace-pre-wrap break-all font-mono max-h-40 overflow-auto">
              {paramStr ? (paramStr.length > 400 ? paramStr.slice(0, 400) + "…" : paramStr) : "（无）"}
            </pre>
          </div>
          {call.result !== undefined && (
            <div>
              <div className="text-[11px] font-medium text-faint mb-0.5">结果</div>
              <pre className="text-[11px] text-code bg-background rounded-lg px-2.5 py-1.5 whitespace-pre-wrap break-all font-mono max-h-48 overflow-auto">
                {resultStr.length > 600 ? resultStr.slice(0, 600) + "…" : resultStr}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
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
  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  /** 当前流式请求的 AbortController，用于“停止”按钮中断生成 */
  const abortRef = useRef<AbortController | null>(null);
  /** 同步记录请求占用状态，阻止 React 状态提交前的重复发送。 */
  const inFlightRef = useRef(false);
  /** 当前流已收到的真实线程 ID，用于断流后不受 React 状态闭包影响地恢复历史。 */
  const activeStreamThreadIdRef = useRef<string | undefined>(undefined);
  /** 用户是否贴近消息底部（决定自动滚动 & 是否显示回到底部按钮） */
  const atBottomRef = useRef(true);
  const [showScrollBtn, setShowScrollBtn] = useState(false);
  /** 离底期间累计的未读（新到达）消息数，回到底部后清零 */
  const [unreadCount, setUnreadCount] = useState(0);
  /** 上一轮渲染时的消息条数，用于增量计算新增消息 */
  const prevMsgCountRef = useRef(stored.messages.length);
  const [historyCursor, setHistoryCursor] = useState<number | null>(null);
  const [hasMoreHistory, setHasMoreHistory] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);

  /** 按占位消息 ID 更新当前流，避免并发或线程切换时写入其他消息。 */
  const updateStreamingAgent = useCallback((messageId: string, updater: (m: Message) => Message) => {
    setMessages((prev) => prev.map((message) => (
      message.id === messageId ? updater(message) : message
    )));
  }, []);

  /** 滚动到消息列表底部 */
  const scrollToBottom = () => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    atBottomRef.current = true;
    setShowScrollBtn(false);
    setUnreadCount(0);
  };

  /** 监听消息区滚动，更新“是否贴近底部”与悬浮按钮显隐 */
  const handleScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    const atBottom = distance < 80;
    atBottomRef.current = atBottom;
    setShowScrollBtn(!atBottom);
    if (atBottom) setUnreadCount(0);
  };

  /** 输入框高度随内容自适应（单行→多行），上限约 160px */
  const autoResize = () => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 160) + "px";
  };

  // 每次消息或线程 ID 变化时持久化到 localStorage
  useEffect(() => { saveStored(threadId, messages); }, [threadId, messages]);

  // 消息更新：贴底则自动滚动；离底时累计新增的 agent 消息数为未读
  useEffect(() => {
    const added = messages.length - prevMsgCountRef.current;
    if (added > 0 && !atBottomRef.current) {
      const newAgent = messages.slice(prevMsgCountRef.current).filter(
        (m) => m.role === "agent" && m.content.length > 0 && m.traceId.length > 0
      ).length;
      if (newAgent > 0) setUnreadCount((c) => c + newAgent);
    }
    prevMsgCountRef.current = messages.length;
    if (atBottomRef.current) scrollToBottom();
  }, [messages]);

  // 输入框内容变化时重算高度（含清空后回弹为单行）
  useEffect(() => { autoResize(); }, [input]);

  /** 从后端加载消息（含 trace、操作卡片和工具记录） */
  const loadRemoteMessages = useCallback(async (tid: string) => {
    try {
      const page = await getThreadMessages(tid);
      if (page.messages.length > 0) {
        const loaded: Message[] = page.messages.map((message) => ({
          id: message.event_id || genMessageId(),
          role: message.role === "user" ? "user" : "agent",
          content: message.content,
          traceId: message.trace_id || "",
          threadId: tid,
          actionCards: message.action_cards || [],
          toolCalls: message.tool_calls.map((call) => ({
            id: call.tool_call_id,
            name: call.name,
            params: call.params || {},
            status: normalizeToolCallStatus(call.status),
            result: call.result === undefined || call.result === null
              ? undefined
              : typeof call.result === "string" ? call.result : JSON.stringify(call.result),
          })),
        }));
        prevMsgCountRef.current = loaded.length;
        setUnreadCount(0);
        setMessages(loaded);
      }
      setHistoryCursor(page.next_cursor);
      setHasMoreHistory(page.has_more);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "历史消息加载失败");
    }
  }, []);

  // 组件挂载时始终以服务端历史为准恢复已有线程，避免本地残缺流式状态覆盖真实结果。
  useEffect(() => {
    const tid = stored.threadId;
    if (tid) {
      void loadRemoteMessages(tid);
    }
  }, [loadRemoteMessages, stored.threadId]);

  const loadOlderMessages = useCallback(async () => {
    if (!threadId || historyCursor === null || loadingHistory) return;
    const el = scrollRef.current;
    const previousHeight = el?.scrollHeight || 0;
    setLoadingHistory(true);
    try {
      const page = await getThreadMessages(threadId, historyCursor);
      const older: Message[] = page.messages.map((message) => ({
        id: message.event_id || genMessageId(),
        role: message.role === "user" ? "user" : "agent",
        content: message.content,
        traceId: message.trace_id || "",
        threadId,
        actionCards: message.action_cards || [],
        toolCalls: message.tool_calls.map((call) => ({
          id: call.tool_call_id,
          name: call.name,
          params: call.params || {},
          status: normalizeToolCallStatus(call.status),
          result: call.result === undefined || call.result === null
            ? undefined
            : typeof call.result === "string" ? call.result : JSON.stringify(call.result),
        })),
      }));
      setMessages((current) => {
        prevMsgCountRef.current = older.length + current.length;
        return [...older, ...current];
      });
      setHistoryCursor(page.next_cursor);
      setHasMoreHistory(page.has_more);
      requestAnimationFrame(() => {
        const current = scrollRef.current;
        if (current) current.scrollTop = current.scrollHeight - previousHeight;
      });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "更早消息加载失败");
    } finally {
      setLoadingHistory(false);
    }
  }, [historyCursor, loadingHistory, threadId]);

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
            const { event_id, reply, thread_id, trace_id, action_cards } = msg.data;
            if (event_id && reply) {
              setMessages(prev => {
                const messageId = String(event_id);
                if (prev.some(message => message.id === messageId)) return prev;
                const newMsg: Message = {
                  id: messageId,
                  role: "agent",
                  content: String(reply),
                  traceId: String(trace_id || ""),
                  threadId: String(thread_id || threadId),
                  actionCards: Array.isArray(action_cards) ? action_cards : [],
                };
                if (atBottomRef.current) setTimeout(scrollToBottom, 100);
                return [...prev, newMsg];
              });
            }
          } else if (msg.type === "maintenance_report" && msg.data?.operation_id && msg.data?.card) {
            const operationId = String(msg.data.operation_id);
            const terminalCard = msg.data.card as ActionCard;
            setMessages(prev => prev.map(message => ({
              ...message,
              actionCards: message.actionCards?.map(card => (
                card.card_type === "maintenance_report"
                && card.resource_refs?.operation_id === operationId
                  ? terminalCard
                  : card
              )),
            })));
          } else if (msg.type === "tool_operation" && msg.data?.operation_id && msg.data?.tool_call_id) {
            const operationId = String(msg.data.operation_id);
            const toolCallId = String(msg.data.tool_call_id);
            const rawStatus = String(msg.data.execution_status || "execution_unknown");
            const status: ToolCallItem["status"] = rawStatus === "committed"
              ? "completed"
              : rawStatus === "failed" ? "failed" : "execution_unknown";
            setMessages(prev => prev.map(message => ({
              ...message,
              actionCards: message.actionCards?.map(card => (
                card.resource_refs?.operation_id === operationId
                  ? { ...card, status, payload_preview: { ...card.payload_preview, result: msg.data.result } }
                  : card
              )),
              toolCalls: message.toolCalls?.map(call => (
                call.id === toolCallId
                  ? {
                    ...call,
                    status,
                    result: msg.data.result === undefined ? call.result : JSON.stringify(msg.data.result),
                  }
                  : call
              )),
            })));
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
    if (!threadId || !reminderId || inFlightRef.current) {
      if (!threadId) setError("当前对话线程尚未建立，无法处理提醒");
      return;
    }
    inFlightRef.current = true;
    setError(null);
    setLoading(true);
    const text = action === "confirm"
      ? `[系统指令] 请调用 confirm_reminder 工具，参数 reminder_id="${reminderId}"`
      : `[系统指令] 请调用 snooze_reminder 工具，参数 reminder_id="${reminderId}"，delay_minutes=${delayMin ?? 5}`;
    // 不添加用户消息到聊天列表，作为系统指令隐藏发送
    const placeholderMsg: Message = {
      id: genMessageId(),
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
      const responseText = await res.text();
      let result: Record<string, unknown> = {};
      if (responseText) {
        try {
          result = JSON.parse(responseText) as Record<string, unknown>;
        } catch {
          if (res.ok) throw new Error("系统指令响应格式无效");
        }
      }
      if (!res.ok) {
        const detail = result.detail;
        const message = typeof detail === "string"
          ? detail
          : typeof detail === "object" && detail !== null && "message" in detail
            ? String((detail as Record<string, unknown>).message)
            : responseText || `系统指令失败 HTTP ${res.status}`;
        throw new Error(message);
      }
      const reply = typeof result.reply === "string" ? result.reply : "";
      const traceId = typeof result.trace_id === "string" ? result.trace_id : "";
      const resultThreadId = typeof result.thread_id === "string" ? result.thread_id : (threadId || "");
      const actionCards = Array.isArray(result.action_cards) ? result.action_cards : [];
      if (!reply || !traceId) throw new Error("系统指令响应缺少 reply 或 trace_id");
      setMessages(prev => prev.map(message => {
        if (message.id === placeholderMsg.id) {
          return { ...message, content: reply, traceId, threadId: resultThreadId, actionCards };
        }
        const nextCards = message.actionCards?.filter(card => card.reminder_id !== reminderId);
        return nextCards?.length === message.actionCards?.length
          ? message
          : { ...message, actionCards: nextCards };
      }));
    } catch (e: unknown) {
      setMessages(prev => prev.filter(message => message.id !== placeholderMsg.id));
      setError(e instanceof Error ? e.message : "未知错误");
    } finally {
      inFlightRef.current = false;
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
    prevMsgCountRef.current = 0;
    setHistoryCursor(null);
    setHasMoreHistory(false);
    setUnreadCount(0);
    setMessages([]);
    localStorage.removeItem(STORAGE_KEY);
  };

  /** 发送消息并处理流式响应 */
  const handleSend = async () => {
    const text = input.trim();
    if (!text || inFlightRef.current) return;

    inFlightRef.current = true;
    setInput("");
    setError(null);
    setLoading(true);

    // 构造用户消息
    const userMsg: Message = {
      id: genMessageId(),
      role: "user",
      content: text,
      traceId: "",
      threadId: threadId ?? "(new)",
    };

    // 占位消息：用于流式接收 agent 回复时的占位
    const placeholderMsg: Message = {
      id: genMessageId(),
      role: "agent",
      content: "",
      traceId: "",
      threadId: threadId ?? "",
      actionCards: [],
    };

    setMessages((prev) => [...prev, userMsg, placeholderMsg]);

    try {
      const ac = new AbortController();
      abortRef.current = ac;
      const result = await sendMessageStream(
        text,
        threadId,
        // onStarted：Turn 已持久化后立即记录真实线程 ID，供首次对话断流恢复。
        (event) => {
          activeStreamThreadIdRef.current = event.thread_id;
          setThreadId(event.thread_id);
          setMessages((prev) => prev.map((message) => (
            message.id === userMsg.id || message.id === placeholderMsg.id
              ? { ...message, threadId: event.thread_id }
              : message
          )));
        },
        // onToken：逐 token 追加到占位消息中，实现打字机效果
        (token) => {
          updateStreamingAgent(placeholderMsg.id, (message) => ({
            ...message,
            content: message.content + token,
          }));
        },
        // onToolCall：在当前流式 agent 消息上追加一条 pending 工具卡片（即时可见反馈）
        (toolCallId, name, params) => {
          updateStreamingAgent(placeholderMsg.id, (m) => {
            const calls = m.toolCalls || [];
            if (toolCallId && calls.some((call) => call.id === toolCallId)) {
              return m;
            }
            return {
              ...m,
              toolCalls: [
                ...calls,
                {
                  id: toolCallId,
                  name,
                  params,
                  status: "pending",
                },
              ],
            };
          });
        },
        // onToolResult：将最近一条同名 pending 工具卡片收敛为 completed / failed
        (toolCallId, name, result, status) => {
          updateStreamingAgent(placeholderMsg.id, (m) => {
            const calls = m.toolCalls || [];
            for (let i = calls.length - 1; i >= 0; i--) {
              if (calls[i].id === toolCallId || (!calls[i].id && calls[i].name === name && calls[i].status === "pending")) {
                const next = [...calls];
                next[i] = {
                  ...next[i],
                  id: toolCallId || next[i].id,
                  status: status === "failed"
                    ? "failed"
                    : status === "execution_unknown" ? "execution_unknown" : "completed",
                  result: typeof result === "string" ? result : JSON.stringify(result),
                };
                return { ...m, toolCalls: next };
              }
            }
            return m;
          });
        },
        // onError：设置错误信息
        (msg) => { setError(msg); },
        ac.signal,
      );

      if (ac.signal.aborted) {
        // 用户主动中断：将未完成的工具调用标记为已停止，并清理空占位气泡
        setMessages((prev) => prev.flatMap((message) => {
          if (message.id !== placeholderMsg.id) return [message];
          const calls = message.toolCalls || [];
          if (!message.content) return [];
          return [{
            ...message,
            toolCalls: calls.map((call) => (
              call.status === "pending" ? { ...call, status: "cancelled" } : call
            )),
          }];
        }));
      } else {
        // 首次对话时记录返回的 thread_id
        if (!threadId && result.thread_id) {
          setThreadId(result.thread_id);
        }

        // 流式结束后：用最终结果收敛占位消息（含工具调用的完成状态）
        if (result.trace_id) {
          if (!atBottomRef.current && result.reply) {
            setUnreadCount((count) => count + 1);
          }
          setMessages((prev) => {
            const updated = [...prev];
            for (let i = updated.length - 1; i >= 0; i--) {
              if (updated[i].id === placeholderMsg.id) {
                const ex = updated[i].toolCalls || [];
                const finals = (
                  result.tool_results && result.tool_results.length
                    ? result.tool_results
                    : (result.tool_calls || [])
                ) as Array<{ tool_call_id: string; name: string; params?: Record<string, unknown>; result?: unknown; status?: string }>;
                const formatResult = (value: unknown): string | undefined => {
                  if (value === undefined) return undefined;
                  if (typeof value === "string") return value;
                  if (typeof value === "object" && value !== null && "result" in value) {
                    return String((value as Record<string, unknown>).result);
                  }
                  return JSON.stringify(value);
                };
                const statusFromFinal = (status?: string): ToolCallItem["status"] => (
                  normalizeToolCallStatus(status)
                );
                const finalById = new Map(
                  finals.filter((item) => item.tool_call_id).map((item) => [item.tool_call_id, item]),
                );
                let unnamedFinalIndex = 0;
                let toolCalls: ToolCallItem[] = ex.map((call): ToolCallItem => {
                  const final = call.id ? finalById.get(call.id) : finals[unnamedFinalIndex++];
                  if (!final) {
                    return call.status === "pending" ? { ...call, status: "execution_unknown" } : call;
                  }
                  return {
                    ...call,
                    id: final.tool_call_id || call.id,
                    name: final.name || call.name,
                    params: final.params || call.params,
                    status: statusFromFinal(final.status),
                    result: formatResult(final.result) ?? call.result,
                  };
                });
                if (ex.length === 0 && finals.length) {
                  toolCalls = finals.map((item, index): ToolCallItem => ({
                    id: item.tool_call_id || `${result.trace_id}-${index}`,
                    name: item.name,
                    params: item.params || {},
                    status: statusFromFinal(item.status),
                    result: formatResult(item.result),
                  }));
                }
                updated[i] = {
                  ...updated[i],
                  content: result.reply || updated[i].content,
                  traceId: result.trace_id,
                  threadId: result.thread_id,
                  actionCards: result.action_cards,
                  toolCalls,
                };
                break;
              }
            }
            return updated;
          });
        }
      }
    } catch (e: unknown) {
      if (!abortRef.current?.signal.aborted) {
        setError(e instanceof Error ? e.message : "未知错误");
        const recoveryThreadId = activeStreamThreadIdRef.current || threadId;
        if (e instanceof StreamIncompleteError && recoveryThreadId) {
          await loadRemoteMessages(recoveryThreadId);
        } else {
          // 发生错误时只移除当前请求的占位消息
          setMessages((prev) => prev.filter((message) => message.id !== placeholderMsg.id));
        }
      }
    } finally {
      activeStreamThreadIdRef.current = undefined;
      inFlightRef.current = false;
      setLoading(false);
      abortRef.current = null;
    }
  };

  /** 键盘事件处理：Enter 发送，Shift+Enter 换行 */
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  /** 粘贴多行时保留换行（浏览器原生行为），并在下一帧重算高度以即时撑开 */
  const handlePaste = () => {
    requestAnimationFrame(autoResize);
  };

  return (
    <div className="flex-1 min-h-0 flex flex-col py-4 relative">
      {/* 顶部标题栏：标题 + 当前线程 + 清空按钮 */}
      <div className="flex items-center justify-between pb-3 mb-1 border-b border-divider">
        <div className="flex items-baseline gap-2">
          <span className="text-sm font-semibold text-title">对话</span>
          {threadId && (
            <span className="text-[11px] font-mono text-faint">thread:{threadId.slice(0, 8)}</span>
          )}
        </div>
        {messages.length > 0 && (
          <button
            onClick={handleReset}
            disabled={loading}
            className="text-xs text-faint hover:text-danger transition-colors px-2 py-1 rounded hover:bg-danger-soft disabled:opacity-30"
          >
            清空上下文
          </button>
        )}
      </div>

      {/* 消息列表区域：占满剩余高度并独立滚动 */}
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        className="flex-1 min-h-0 flex flex-col gap-3 overflow-y-auto py-4 pr-1"
      >
        {hasMoreHistory && (
          <button
            type="button"
            onClick={loadOlderMessages}
            disabled={loadingHistory}
            className="self-center text-xs text-faint hover:text-primary px-3 py-1.5 rounded-lg border border-divider hover:border-primary-border disabled:opacity-40 transition-colors"
          >
            {loadingHistory ? "加载中…" : "加载更早消息"}
          </button>
        )}
        {messages.length === 0 && (
          <div className="flex flex-col items-center justify-center flex-1 text-faint gap-2">
            <svg className="w-10 h-10 text-subtle" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" /></svg>
            <p className="text-sm">发送消息开始对话</p>
          </div>
        )}
        {messages.map((msg) => (
          <div key={msg.id} className={`flex flex-col animate-fade-in ${msg.role === "user" ? "items-end" : "items-start"}`}>
            {/* 消息气泡，根据角色不同应用不同样式 */}
            <div className={`max-w-[80%] rounded-2xl px-4 py-2.5 text-sm leading-relaxed shadow-sm ${
              msg.role === "user"
                ? "bg-primary text-on-primary rounded-br-md"
                : msg.role === "action"
                  ? "bg-warning-soft text-warning-text rounded-lg border border-warning-border text-xs"
                  : "bg-surface text-content rounded-bl-md border border-divider"
            }`}>
              {/* 流式加载中时显示“正在分析”跳动省略号（即时反馈） */}
              {msg.role === "agent" && msg.content === "" && loading ? (
                <span className="inline-flex items-center gap-1 text-faint text-xs">
                  <span className="w-1.5 h-1.5 rounded-full bg-primary animate-thinking" style={{ animationDelay: "0ms" }} />
                  <span className="w-1.5 h-1.5 rounded-full bg-primary animate-thinking" style={{ animationDelay: "150ms" }} />
                  <span className="w-1.5 h-1.5 rounded-full bg-primary animate-thinking" style={{ animationDelay: "300ms" }} />
                  <span className="ml-1">正在分析</span>
                </span>
              ) : (
                // 流式输出中（agent 正在打字且尚未结束）用纯文本，避免不完整 Markdown 频繁重排/闪烁；
                // 其余情况（用户消息、已完成的 agent 消息）渲染为 Markdown。
                (() => {
                  const isLiveStreaming = msg.role === "agent" && msg.content !== "" && msg.traceId === "" && loading;
                  if (isLiveStreaming || msg.role === "action") {
                    return <span className="whitespace-pre-wrap">{msg.content}</span>;
                  }
                  if (!msg.content) return null;
                  return <Markdown content={msg.content} variant={msg.role === "user" ? "colored" : "surface"} />;
                })()
              )}
            </div>
            {/* 工具使用卡片：状态随时间推进由 pending 收敛为 completed/failed（可折叠） */}
            {msg.toolCalls && msg.toolCalls.length > 0 && (
              <div className="flex flex-col gap-1.5 mt-2 max-w-[80%]">
                {msg.toolCalls.map((call) => (
                  <ToolCallCard key={call.id} call={call} />
                ))}
              </div>
            )}
            {/* 操作卡片：展示任务创建、记忆修改等工具执行卡片 */}
            {msg.actionCards && msg.actionCards.length > 0 && (
              <div className="flex flex-col gap-1.5 mt-2">
                {msg.actionCards.map((card, j) => (
                  <div key={`${msg.id}-${card.card_type}-${card.reminder_id || j}`}>
                    {/* 提醒卡片：显示确认/延时按钮 */}
                    {card.card_type === "reminder_alert" && (
                      <div className="bg-gradient-to-r from-warning-soft to-warning-soft border border-warning-border rounded-xl px-4 py-3 shadow-sm">
                        {/* 标题行 */}
                        <div className="flex items-center gap-2 mb-2">
                          <div className="w-6 h-6 rounded-full bg-warning-soft flex items-center justify-center shrink-0">
                            <svg className="w-3.5 h-3.5 text-warning" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9" />
                            </svg>
                          </div>
                          <span className="text-sm font-medium text-warning-text">{card.summary || card.title}</span>
                        </div>
                        {/* 操作区 */}
                        <div className="flex items-center gap-2">
                          <button
                            disabled={loading}
                            onClick={() => handleReminderAction("confirm", card.reminder_id || "")}
                            className="flex items-center gap-1 text-xs px-3.5 py-1.5 rounded-lg bg-success text-on-primary font-medium hover:bg-success-text active:scale-95 disabled:opacity-40 transition-all shadow-sm"
                          >
                            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7" />
                            </svg>
                            确认
                          </button>
                          {/* 分隔 */}
                          <span className="text-subtle text-xs mx-0.5">|</span>
                          {/* 延时输入 */}
                          <div className="flex items-center gap-1.5 bg-surface/70 rounded-lg px-2.5 py-1.5 border border-warning-border">
                            <input
                              type="number"
                              min={1} max={120} defaultValue={5}
                              id={`snooze-${msg.id}-${j}`}
                              className="w-12 text-xs px-1.5 py-0.5 rounded border-0 bg-transparent text-title focus:outline-none text-center font-mono"
                            />
                            <span className="text-xs text-faint font-medium">分钟后</span>
                          </div>
                          <button
                            disabled={loading}
                            onClick={() => {
                              const el = document.getElementById(`snooze-${msg.id}-${j}`) as HTMLInputElement;
                              const mins = Math.max(1, Math.min(120, Number(el?.value) || 5));
                              handleReminderAction("snooze", card.reminder_id || "", mins);
                            }}
                            className="flex items-center gap-1 text-xs px-3.5 py-1.5 rounded-lg bg-muted text-on-primary font-medium hover:bg-code active:scale-95 disabled:opacity-40 transition-all shadow-sm"
                          >
                            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
                            </svg>
                            延时
                          </button>
                        </div>
                      </div>
                    )}
                    {card.card_type === "maintenance_report" && (() => {
                      const scan = (card.payload_preview?.pre_enqueue_scan || {}) as Record<string, unknown>;
                      const result = (card.payload_preview?.result || {}) as Record<string, unknown>;
                      const actionCounts = (result.action_counts || {}) as Record<string, unknown>;
                      const isPending = card.status === "pending";
                      const isFailed = card.status === "failed";
                      return (
                        <div className={`rounded-xl border px-4 py-3 shadow-sm ${
                          isFailed ? "bg-danger-soft border-danger-border" :
                          isPending ? "bg-warning-soft border-warning-border" :
                          "bg-success-soft border-success-border"
                        }`}>
                          <div className="flex items-center justify-between gap-3">
                            <span className="text-sm font-medium text-title">{card.title}</span>
                            <span className="text-[11px] font-mono text-muted">
                              {isPending ? "执行中" : isFailed ? "失败" : "已完成"}
                            </span>
                          </div>
                          <p className="text-xs text-muted mt-1">{card.summary}</p>
                          {isPending ? (
                            <div className="grid grid-cols-3 gap-2 mt-3 text-center">
                              <div><p className="text-[10px] text-faint">扫描</p><p className="text-sm font-mono text-title">{String(scan.total_scanned ?? 0)}</p></div>
                              <div><p className="text-[10px] text-faint">待睡眠</p><p className="text-sm font-mono text-title">{String(scan.sleep_candidates ?? 0)}</p></div>
                              <div><p className="text-[10px] text-faint">待归档</p><p className="text-sm font-mono text-title">{String(scan.archive_candidates ?? 0)}</p></div>
                            </div>
                          ) : (
                            <div className="grid grid-cols-4 gap-2 mt-3 text-center">
                              <div><p className="text-[10px] text-faint">候选</p><p className="text-sm font-mono text-title">{String(result.candidate_count ?? 0)}</p></div>
                              <div><p className="text-[10px] text-faint">已应用</p><p className="text-sm font-mono text-title">{String(result.applied_count ?? 0)}</p></div>
                              <div><p className="text-[10px] text-faint">睡眠</p><p className="text-sm font-mono text-title">{String(actionCounts.sleep ?? 0)}</p></div>
                              <div><p className="text-[10px] text-faint">归档</p><p className="text-sm font-mono text-title">{String((Number(actionCounts.archive_expired || 0) + Number(actionCounts.archive_candidate || 0)))}</p></div>
                            </div>
                          )}
                          {isFailed && Boolean(result.error) && <p className="text-xs text-danger-text mt-2 break-words">{String(result.error)}</p>}
                          <p className="text-[10px] font-mono text-faint mt-3 break-all">{card.resource_refs?.operation_id}</p>
                        </div>
                      );
                    })()}
                    {/* TODO: 用户审批当前有意停用；恢复时必须同步启用后端策略、审批节点、前端交互、测试和文档。 */}
                    {/* 其他卡片：保持原有 badge 样式 */}
                    {card.card_type !== "reminder_alert" && card.card_type !== "approval_required" && card.card_type !== "maintenance_report" && (
                      <span className={`text-[11px] px-2 py-0.5 rounded-full border ${
                        card.card_type === "task_created" ? "bg-primary-soft text-primary-hover border-primary-border" :
                        card.card_type === "memory_revised" ? "bg-accent-soft text-accent-text border-accent-border" :
                        card.card_type === "forget_result" ? "bg-danger-soft text-danger-hover border-danger-border" :
                        "bg-background text-code border-divider"
                      }`}>📌 {card.title}{card.summary ? `: ${card.summary.slice(0, 30)}` : ""}</span>
                    )}
                    {card.trace_id && (
                      <button
                        type="button"
                        onClick={() => onInspectTrace?.(card.trace_id)}
                        className="block mt-1 px-1 text-[11px] font-mono text-faint underline hover:text-primary transition-colors"
                      >
                        trace:{card.trace_id.slice(0, 8)}
                      </button>
                    )}
                  </div>
                ))}
              </div>
            )}
            {/* 元信息：trace_id（可点击跳转上下文检查器）、thread_id */}
            {msg.role !== "action" && (
              <span className="text-[11px] text-faint mt-1 px-1 select-none">
                {msg.role === "agent" ? (
                  msg.content === "" && loading ? (
                    <span className="text-subtle animate-pulse">生成中...</span>
                  ) : msg.traceId ? (
                    <span className="inline-flex items-center gap-1.5">
                      <button
                        onClick={() => onInspectTrace?.(msg.traceId)}
                        className="underline hover:text-primary font-mono"
                      >trace:{msg.traceId.slice(0, 8)}</button>
                      <span className="text-subtle">·</span>
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
      </div>

      {/* 回到底部悬浮按钮：用户向上浏览历史时显示 */}
      {showScrollBtn && (
        <button
          onClick={scrollToBottom}
          className="absolute bottom-24 right-6 z-10 flex items-center gap-1.5 rounded-full bg-surface border border-divider shadow-md px-3 py-1.5 text-xs text-muted hover:text-primary hover:border-primary-ring transition-all animate-fade-in"
        >
          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 14l-7 7-7-7M12 3v18" />
          </svg>
          回到底部
          {unreadCount > 0 && (
            <span className="ml-0.5 inline-flex min-w-[18px] h-[18px] px-1 items-center justify-center rounded-full bg-primary text-on-primary text-[10px] font-medium leading-none">
              {unreadCount > 99 ? "99+" : unreadCount}
            </span>
          )}
        </button>
      )}

      {/* 底部输入区：贴底固定，顶部分隔线 */}
      <div className="pt-3 border-t border-divider">
        {/* 错误提示 */}
        {error && (
          <div className="mb-2 rounded-lg border border-danger-border bg-danger-soft px-4 py-2.5 text-sm text-danger-hover animate-fade-in">
            {error}
          </div>
        )}

        {/* 输入区域 */}
        <div className="flex gap-2 items-end">
          <textarea
            ref={textareaRef}
            rows={1}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            placeholder="输入消息...（Enter 发送，Shift+Enter 换行）"
            disabled={loading}
            className="flex-1 resize-none rounded-xl border border-divider bg-surface px-4 py-2.5 text-sm text-content placeholder-faint focus:outline-none focus:border-primary focus:ring-2 focus:ring-primary-soft disabled:opacity-50 shadow-sm leading-relaxed max-h-40 overflow-y-auto"
          />
          {loading ? (
            <button
              onClick={() => {
                setError(null);
                abortRef.current?.abort();
              }}
              className="rounded-xl bg-danger px-6 py-2.5 text-sm font-medium text-on-primary hover:bg-danger-hover active:scale-95 transition-all shadow-sm"
            >
              停止
            </button>
          ) : (
            <button
              onClick={handleSend}
              disabled={!input.trim()}
              className="rounded-xl bg-primary px-6 py-2.5 text-sm font-medium text-on-primary hover:bg-primary-hover active:scale-95 disabled:opacity-40 disabled:cursor-not-allowed transition-all shadow-sm"
            >
              发送
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
