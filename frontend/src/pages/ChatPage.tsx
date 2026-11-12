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
  getCurrentConversation,
  getExecutionStatus,
  interruptTurn,
  getThreadMessages,
  respondApproval,
  ActionCard,
  ApiError,
  ExecutionStatus,
  StreamIncompleteError,
} from "../api/chat";
import { BASE_PATH } from "../lib/base";
import { apiFetch, sendWsAuth } from "../api/client";
import { workCardsFrom } from "../../../shared/work/types";
import Markdown from "../components/Markdown";
import ToolCallCard from "../features/chat/ToolCallCard";
import type { Message, ToolCallItem } from "../features/chat/types";
import { loadStored, saveStored } from "../features/chat/messageStorage";
import { normalizeToolCallStatus, genMessageId } from "../features/chat/messageModel";

export default function ChatPage({ onInspectTrace, onOpenWork, active = true }: { onInspectTrace?: (tid: string) => void; onOpenWork?: (taskId: string) => void; active?: boolean }) {
  // 惰性初始化：只在首次挂载读取 localStorage。若每次渲染都重读，
  // 新线程首条消息流式期间 threadId 持久化会使挂载恢复 effect 重新触发，
  // 服务端历史整体覆盖消息列表，正在流式接收的占位消息被抹掉（回复丢失）。
  const [stored] = useState(loadStored);
  const [messages, setMessages] = useState<Message[]>(stored.messages);
  const [input, setInput] = useState("");
  // localStorage 只提供展示缓存：Thread 权威来源是服务器，启动阶段不信任本地 ID。
  const [threadId, setThreadId] = useState<string | undefined>(undefined);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** 启动握手（current Thread → 历史 → 执行状态）完成前不允许发送。 */
  const [conversationReady, setConversationReady] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  /** 当前流式请求的 AbortController，用于“停止”按钮中断生成 */
  const abortRef = useRef<AbortController | null>(null);
  /** 同步记录请求占用状态，阻止 React 状态提交前的重复发送。 */
  const inFlightRef = useRef(false);
  /** 本地 Turn 期间收到的历史同步请求，在 finally 中合并执行。 */
  const historyRefreshPendingRef = useRef(false);
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
  const [approvalBusyId, setApprovalBusyId] = useState<string | null>(null);
  /** 服务端线程级执行状态：idle / running（可中断）/ busy（后台占用）。 */
  const [execState, setExecState] = useState<"idle" | "running" | "busy">("idle");
  /** 当前 Turn 由其他设备发起（用于提示"Agent 正在回复"）。 */
  const [remoteRunning, setRemoteRunning] = useState(false);
  /** 远端 Turn 是否仍活跃：只在 idle 时清除（interrupting 不清除）。 */
  const remoteTurnActiveRef = useRef(false);
  const [stopping, setStopping] = useState(false);
  /** 当前前台 Turn 的 TurnRecord 主键：中断请求必须携带，防止误伤新 Turn。 */
  const activeTurnRecordIdRef = useRef<string | undefined>(undefined);

  /** 用服务端状态统一收敛发送/停止按钮与提示（初始化、WS 广播、重连共用）。 */
  const applyExecutionStatus = useCallback((status: ExecutionStatus) => {
    setExecState(status.state);
    if (status.state === "running" && status.active_turn?.turn_record_id) {
      activeTurnRecordIdRef.current = status.active_turn.turn_record_id;
      // 本地请求在途时不算远端执行；interrupting 只影响提示文案，
      // 不能清除「远端仍活跃」标记（否则取消收口后不会刷新历史）。
      const remote = !inFlightRef.current;
      if (remote) remoteTurnActiveRef.current = true;
      setRemoteRunning(remote);
    } else if (status.state === "idle") {
      activeTurnRecordIdRef.current = undefined;
      remoteTurnActiveRef.current = false;
      setRemoteRunning(false);
    } else {
      // busy（后台占用）：前台 Turn 已不在执行，但远端活跃标记只在 idle 清除。
      activeTurnRecordIdRef.current = undefined;
      setRemoteRunning(false);
    }
  }, []);

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

  // 消息或线程 ID 变化时持久化到 localStorage（300ms 去抖：
  // 流式期间每个 token 都会触发本 effect，全量 JSON.stringify 会造成打字卡顿）
  useEffect(() => {
    const timer = setTimeout(() => saveStored(threadId, messages), 300);
    return () => clearTimeout(timer);
  }, [threadId, messages]);

  // 组件卸载时中止进行中的流式请求，避免后台 fetch 空转
  useEffect(() => () => { abortRef.current?.abort(); }, []);

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
      // 服务器历史是事实来源：即使为空也覆盖本地缓存（取消的轮次必须消失）。
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
      setHistoryCursor(page.next_cursor);
      setHasMoreHistory(page.has_more);
      return true;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "历史消息加载失败");
      return false;
    }
  }, []);

  /** 启动握手 / stale 恢复：current Thread → 服务器历史 → 执行状态。 */
  const syncConversation = useCallback(async (): Promise<boolean> => {
    try {
      const conversation = await getCurrentConversation();
      setThreadId(conversation.thread_id);
      if (!(await loadRemoteMessages(conversation.thread_id))) return false;
      applyExecutionStatus(await getExecutionStatus(conversation.thread_id));
      return true;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "当前对话获取失败");
      return false;
    }
  }, [applyExecutionStatus, loadRemoteMessages]);

  // 启动时从服务器获取唯一长期 Thread：三端共用同一 ID，本地缓存仅用于展示加速。
  useEffect(() => {
    let disposed = false;
    const bootstrap = async () => {
      setConversationReady(false);
      const ok = await syncConversation();
      if (!disposed && ok) setConversationReady(true);
    };
    void bootstrap();
    return () => { disposed = true; };
  }, [syncConversation]);

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
    if (!threadId || !active) return;
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${protocol}//${window.location.host}${BASE_PATH}ws/${threadId}`;
    let ws: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    // effect 已清理标记：ws.close() 触发的 onclose 是异步回调，会在 cleanup 之后执行，
    // 若不拦截会再排一个重连定时器，产生指向旧线程的僵尸连接
    let disposed = false;

    const connect = () => {
      let receivedInitialStatus = false;
      ws = new WebSocket(wsUrl);
      // 首帧认证：认证完成前后端不会推送任何消息事件。
      ws.onopen = () => {
        if (ws) sendWsAuth(ws);
      };
      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === "new_message" && msg.data) {
            const { event_id, reply, thread_id, trace_id, action_cards, tool_calls } = msg.data;
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
                  // 后台提醒等推送 turn 携带的工具调用记录（如 remind_alert），映射逻辑与历史加载一致
                  toolCalls: Array.isArray(tool_calls)
                    ? tool_calls.map((call: { tool_call_id: string; name: string; params?: Record<string, unknown>; status?: string; result?: unknown }) => ({
                      id: call.tool_call_id,
                      name: call.name,
                      params: call.params || {},
                      status: normalizeToolCallStatus(call.status),
                      result: call.result === undefined || call.result === null
                        ? undefined
                        : typeof call.result === "string" ? call.result : JSON.stringify(call.result),
                    }))
                    : [],
                };
                if (atBottomRef.current) setTimeout(scrollToBottom, 100);
                return [...prev, newMsg];
              });
            }
          } else if (msg.type === "execution_status" && msg.data) {
            const status = msg.data as ExecutionStatus;
            const initialSnapshot = !receivedInitialStatus;
            receivedInitialStatus = true;
            // 先取「此前是否仍有远端 Turn」再应用状态：idle 会清除该标记。
            const hadRemoteTurn = remoteTurnActiveRef.current;
            applyExecutionStatus(status);
            if (initialSnapshot || (status.state === "idle" && hadRemoteTurn)) {
              // 首状态快照补齐断线期间错过的完整 Turn；本地流结束前不能覆盖气泡。
              if (inFlightRef.current) {
                historyRefreshPendingRef.current = true;
              } else {
                void loadRemoteMessages(String(status.thread_id || threadId));
              }
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
        if (disposed) return;
        // 5 秒后重连
        reconnectTimer = setTimeout(connect, 5000);
      };
      ws.onerror = () => {
        ws?.close();
      };
    };

    const recover = () => {
      if (document.hidden || disposed) return;
      if (inFlightRef.current) historyRefreshPendingRef.current = true;
      else void loadRemoteMessages(threadId);
      void getExecutionStatus(threadId).then(applyExecutionStatus).catch(() => undefined);
      if (ws?.readyState === WebSocket.OPEN) return;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      connect();
    };
    window.addEventListener("online", recover);
    window.addEventListener("focus", recover);
    document.addEventListener("visibilitychange", recover);
    connect();
    return () => {
      window.removeEventListener("online", recover);
      window.removeEventListener("focus", recover);
      document.removeEventListener("visibilitychange", recover);
      disposed = true;
      ws?.close();
      if (reconnectTimer) clearTimeout(reconnectTimer);
    };
  }, [active, applyExecutionStatus, loadRemoteMessages, threadId]);

  /** 执行或拒绝服务端冻结的工具调用；客户端永远不回传可篡改的工具参数。 */
  const handleApprovalAction = async (approvalId: string, action: "approve" | "deny") => {
    if (!approvalId || approvalBusyId) return;
    setApprovalBusyId(approvalId);
    setError(null);
    try {
      const response = await respondApproval(approvalId, action);
      const status = response.action === "succeeded"
        ? "completed"
        : response.action === "denied"
          ? "cancelled"
          : response.action === "interrupted_unknown"
            ? "execution_unknown"
            : "failed";
      const serializedResult = response.tool_result === undefined
        ? response.error
        : typeof response.tool_result === "string"
          ? response.tool_result
          : JSON.stringify(response.tool_result);
      setMessages((current) => current.map((message) => {
        const matchingCards = (message.actionCards || []).filter((card) => (
          card.card_type === "approval_required"
          && (card.resource_refs?.approval_id === approvalId
            || card.payload_preview?.approval_id === approvalId)
        ));
        if (matchingCards.length === 0) return message;
        const toolCallIds = new Set(matchingCards.map((card) => String(
          card.resource_refs?.tool_call_id || card.payload_preview?.tool_call_id || "",
        )).filter(Boolean));
        return {
          ...message,
          actionCards: message.actionCards?.map((card) => (
            card.card_type === "approval_required"
            && (card.resource_refs?.approval_id === approvalId
              || card.payload_preview?.approval_id === approvalId)
              ? {
                ...card,
                status,
                actions: [],
                payload_preview: {
                  ...card.payload_preview,
                  approval_status: response.action,
                  result: response.tool_result,
                  error: response.error,
                },
              }
              : card
          )),
          toolCalls: message.toolCalls?.map((call) => (
            toolCallIds.has(call.id)
              ? { ...call, status, result: serializedResult }
              : call
          )),
        };
      }));
      if (response.error) setError(response.error);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "审批请求失败");
    } finally {
      setApprovalBusyId(null);
    }
  };

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
      const res = await apiFetch("api/chat/system", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, thread_id: threadId }),
      });
      // 后端用 202 表达"已有对话轮次进行中"，不能落入成功分支
      if (res.status === 202) throw new Error("当前有对话正在进行，请稍后再试");
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
      if (historyRefreshPendingRef.current && threadId) {
        historyRefreshPendingRef.current = false;
        await loadRemoteMessages(threadId);
      }
    }
  };

  /** 发送消息并处理流式响应 */
  const handleSend = async () => {
    const text = input.trim();
    if (!text || inFlightRef.current || !conversationReady) return;
    if (execState !== "idle") {
      // 忙状态下的消息不排队、不落库：明确提示，保留用户输入。
      setError(execState === "busy" ? "后台任务正在处理中，请稍候" : "Agent 正在回复，请稍候");
      return;
    }

    inFlightRef.current = true;
    setInput("");
    setError(null);
    setLoading(true);
    setExecState("running");
    setRemoteRunning(false);
    activeTurnRecordIdRef.current = undefined;

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

    /** 是否已收到 started（服务端已创建 Turn）；用于区分「未落库的 Busy」回滚。 */
    let started = false;
    /** catch/finally 是否已重新同步过执行状态，避免重复或覆盖新状态。 */
    let statusSynced = false;

    try {
      const ac = new AbortController();
      abortRef.current = ac;
      const result = await sendMessageStream(
        text,
        threadId,
        // onStarted：Turn 已持久化后立即记录真实线程 ID，供首次对话断流恢复。
        (event) => {
          started = true;
          activeStreamThreadIdRef.current = event.thread_id;
          activeTurnRecordIdRef.current = event.turn_record_id || undefined;
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

      if (result.cancelled) {
        // 服务端已完成取消收口：本轮用户消息不进入历史，本地同步移除并恢复服务器历史。
        setMessages((prev) => prev.filter(
          (message) => message.id !== userMsg.id && message.id !== placeholderMsg.id,
        ));
        setError(null);
        if (threadId) await loadRemoteMessages(threadId);
      } else if (ac.signal.aborted) {
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
        if (result.thread_id) {
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
                  traceId: result.trace_id || "",
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
      const errorCode = e instanceof ApiError ? e.code : "";
      const busyBeforeStart = !started && errorCode === "turn_in_progress";
      const staleBeforeStart = !started && errorCode === "stale_thread_id";
      if (!abortRef.current?.signal.aborted) {
        if (busyBeforeStart || staleBeforeStart) {
          // 服务端没有创建 Turn / 已切换到新 Thread：撤回两个乐观气泡，
          // 恢复输入（不覆盖用户已经重新输入的内容）。
          setMessages((prev) => prev.filter(
            (message) => message.id !== userMsg.id && message.id !== placeholderMsg.id,
          ));
          setInput((prev) => (prev.trim() ? prev : text));
          setError(staleBeforeStart ? "对话已切换，正在重新同步…" : "Agent 正在回复，请稍候");
          if (staleBeforeStart) {
            const ok = await syncConversation();
            setConversationReady(ok);
          } else {
            try {
              applyExecutionStatus(await getExecutionStatus(threadId));
            } catch {
              // 状态查询失败时保留提示，后续 WS 广播会恢复按钮。
            }
          }
          statusSynced = true;
        } else {
          setError(e instanceof Error ? e.message : "未知错误");
          const recoveryThreadId = activeStreamThreadIdRef.current || threadId;
          if (e instanceof StreamIncompleteError && recoveryThreadId) {
            await loadRemoteMessages(recoveryThreadId);
          } else if (!started) {
            // 未收到 started 说明服务端没有为本轮创建 Turn：
            // 不能留下数据库里不存在的幻影用户消息。
            setMessages((prev) => prev.filter(
              (message) => message.id !== userMsg.id && message.id !== placeholderMsg.id,
            ));
            setInput((prev) => (prev.trim() ? prev : text));
            if (recoveryThreadId) await loadRemoteMessages(recoveryThreadId);
            try {
              applyExecutionStatus(await getExecutionStatus(recoveryThreadId));
            } catch {
              // 状态查询失败时保留现有提示。
            }
            statusSynced = true;
          } else {
            // 已创建 Turn：保留用户消息，只移除未完成的占位气泡。
            setMessages((prev) => prev.filter((message) => message.id !== placeholderMsg.id));
          }
        }
      }
    } finally {
      const resolvedThreadId = activeStreamThreadIdRef.current || threadId;
      const wasAborted = abortRef.current?.signal.aborted ?? false;
      activeStreamThreadIdRef.current = undefined;
      inFlightRef.current = false;
      setLoading(false);
      abortRef.current = null;
      if (historyRefreshPendingRef.current && resolvedThreadId) {
        historyRefreshPendingRef.current = false;
        await loadRemoteMessages(resolvedThreadId);
      }
      // 本轮结束后不要无条件置 idle：后台 GoalDecision 可能已接手占用。
      // 重新读取服务端状态；查询失败时才退回 idle 以保证按钮可用。
      if (!wasAborted && !statusSynced) {
        try {
          applyExecutionStatus(await getExecutionStatus(resolvedThreadId));
        } catch {
          setExecState("idle");
        }
      }
    }
  };

  /** 停止：走服务端中断（持久化取消信号），不 abort 本地流，等收口广播。 */
  const handleStop = async () => {
    if (!threadId || stopping) return;
    setStopping(true);
    setError(null);
    try {
      const result = await interruptTurn(threadId, activeTurnRecordIdRef.current);
      if (result.status === "idle" || result.status === "completed" || result.status === "cancelled") {
        activeTurnRecordIdRef.current = undefined;
        try {
          applyExecutionStatus(await getExecutionStatus(threadId));
        } catch {
          setExecState("idle");
        }
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "中断请求失败");
    } finally {
      setStopping(false);
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
            {workCardsFrom(msg.actionCards || []).map(card => (
              <section key={card.taskId} className="rounded-xl border border-divider bg-surface p-4 my-3">
                <strong className="text-sm text-title">{card.title}</strong>
                <p className="text-xs text-muted mt-1">{card.summary}</p>
                <button className="mt-3 rounded-lg border border-divider px-3 py-2 text-sm" onClick={() => onOpenWork?.(card.taskId)}>查看工作</button>
              </section>
            ))}
            {msg.actionCards && msg.actionCards.length > 0 && (
              <div className="flex flex-col gap-1.5 mt-2">
                {msg.actionCards.filter(card => card.card_type !== "agent_task_created").map((card, j) => (
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
                    {card.card_type === "approval_required" && (() => {
                      const approvalId = String(
                        card.resource_refs?.approval_id || card.payload_preview?.approval_id || "",
                      );
                      const params = card.payload_preview?.tool_params;
                      const pending = card.status === "needs_review" && (card.actions || []).length > 0;
                      const busy = approvalBusyId === approvalId;
                      return (
                        <div className="rounded-xl border border-warning-border bg-warning-soft px-4 py-3 shadow-sm max-w-xl">
                          <div className="flex items-center justify-between gap-3">
                            <span className="text-sm font-semibold text-warning-text">{card.title}</span>
                            <span className="text-[11px] font-mono text-muted">
                              {busy ? "执行中…" : pending ? "等待确认" : card.status === "completed" ? "已处理" : "执行失败"}
                            </span>
                          </div>
                          <p className="mt-1 text-xs text-muted">{card.summary}</p>
                          {params !== undefined && (
                            <pre className="mt-2 max-h-40 overflow-auto rounded-lg bg-background px-2.5 py-2 text-[11px] text-code whitespace-pre-wrap break-all font-mono">
                              {JSON.stringify(params, null, 2)}
                            </pre>
                          )}
                          {pending && (
                            <div className="mt-3 flex items-center gap-2">
                              <button
                                type="button"
                                disabled={busy || !approvalId}
                                onClick={() => void handleApprovalAction(approvalId, "approve")}
                                className="rounded-lg bg-danger px-3.5 py-1.5 text-xs font-medium text-on-primary hover:bg-danger-hover disabled:opacity-40 transition-colors"
                              >
                                {busy ? "正在执行" : "确认执行"}
                              </button>
                              <button
                                type="button"
                                disabled={busy || !approvalId}
                                onClick={() => void handleApprovalAction(approvalId, "deny")}
                                className="rounded-lg border border-divider bg-surface px-3.5 py-1.5 text-xs font-medium text-muted hover:text-title disabled:opacity-40 transition-colors"
                              >
                                拒绝
                              </button>
                            </div>
                          )}
                        </div>
                      );
                    })()}
                    {/* 其他卡片：保持原有 badge 样式 */}
                    {card.card_type !== "agent_task_created" && card.card_type !== "reminder_alert" && card.card_type !== "approval_required" && card.card_type !== "maintenance_report" && (
                      <span className={`text-[11px] px-2 py-0.5 rounded-full border ${
                        card.card_type === "task_created" ? "bg-primary-soft text-primary-hover border-primary-border" :
                        card.card_type === "memory_revised" ? "bg-accent-soft text-accent-text border-accent-border" :
                        card.card_type === "forget_result" ? "bg-danger-soft text-danger-hover border-danger-border" :
                        "bg-background text-code border-divider"
                      }`}>{card.title}{card.summary ? `: ${card.summary.slice(0, 30)}` : ""}</span>
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

        {/* 执行状态提示：其他设备正在回复 / 后台占用（不可中断） */}
        {execState !== "idle" && (
          <div className="mb-2 flex items-center justify-between rounded-lg border border-divider bg-background px-3 py-2 text-xs text-muted">
            <span className="inline-flex items-center gap-1.5">
              <span className="w-1.5 h-1.5 rounded-full bg-primary animate-pulse" />
              {execState === "busy"
                ? "后台任务处理中，请稍候…"
                : remoteRunning ? "Agent 正在其他设备回复…" : "Agent 正在回复…"}
            </span>
            {execState === "running" && <span className="text-faint">点击停止可中断</span>}
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
            placeholder={conversationReady ? "输入消息...（Enter 发送，Shift+Enter 换行）" : "正在同步对话…"}
            disabled={!conversationReady || loading || execState !== "idle"}
            className="flex-1 resize-none rounded-xl border border-divider bg-surface px-4 py-2.5 text-sm text-content placeholder-faint focus:outline-none focus:border-primary focus:ring-2 focus:ring-primary-soft disabled:opacity-50 shadow-sm leading-relaxed max-h-40 overflow-y-auto"
          />
          {execState === "running" ? (
            <button
              onClick={handleStop}
              disabled={stopping}
              className="rounded-xl bg-danger px-6 py-2.5 text-sm font-medium text-on-primary hover:bg-danger-hover active:scale-95 disabled:opacity-60 transition-all shadow-sm"
            >
              {stopping ? "停止中…" : "停止"}
            </button>
          ) : execState === "busy" ? (
            <button
              type="button"
              disabled
              className="rounded-xl bg-muted px-6 py-2.5 text-sm font-medium text-on-primary opacity-60 cursor-not-allowed shadow-sm"
            >
              处理中
            </button>
          ) : (
            <button
              onClick={handleSend}
              disabled={!conversationReady || !input.trim()}
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
