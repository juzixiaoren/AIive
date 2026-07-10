/**
 * 聊天 API 接口层
 * - 定义前端与后端聊天接口的数据结构（ActionCard、ChatResponse、StreamEvent）
 * - 提供 sendMessage：非流式发送消息
 * - 提供 sendMessageStream：SSE 流式发送消息，支持逐 token 回调
 * - 提供 confirmReminder / snoozeReminder：提醒确认与延时操作
 * - 提供 resetThread：重置对话线程
 */

/** 操作卡片，用于在聊天界面中展示工具执行结果 */
export interface ActionCard {
  card_type: string;
  title: string;
  summary: string;
  trace_id: string;
  status: string;
  resource_refs: Record<string, string>;
  payload_preview: Record<string, unknown>;
  reminder_id?: string;
}

/** 聊天响应体，包含模型回复、线程 ID、追踪 ID 及可能的操作卡片 */
export interface ChatResponse {
  reply: string;
  thread_id: string;
  trace_id: string;
  action_cards?: ActionCard[];
  intent_type?: string;
}

/** SSE 流式事件类型定义 */
export interface StreamEvent {
  event: "token" | "tool_call" | "tool_result" | "action_card" | "done" | "error";
  data: Record<string, unknown>;
}

/**
 * 发送消息（非流式）
 * @param message - 用户输入的消息文本
 * @param threadId - 可选的对话线程 ID，用于多轮对话
 * @returns ChatResponse，包含模型回复和其他元信息
 */
export async function sendMessage(
  message: string,
  threadId?: string
): Promise<ChatResponse> {
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, thread_id: threadId ?? null }),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Chat API error ${res.status}: ${text}`);
  }
  return res.json();
}

/**
 * 确认提醒：通过向 LLM 发送自然语言指令，让 LLM 决定调用 confirm_reminder 工具
 * @param reminderId - 提醒 ID
 * @param threadId - 可选的对话线程 ID
 * @returns 包含模型回复的对象
 */
export async function confirmReminder(reminderId: string, threadId?: string): Promise<{ reply: string }> {
  const result = await sendMessageStream(
    `确认提醒 ${reminderId}`,
    threadId,
    () => {}, () => {}, () => {}, () => {}, () => {},
  );
  return { reply: result.reply };
}

/**
 * 延时提醒：通过向 LLM 发送自然语言指令，让 LLM 决定调用 snooze_reminder 工具
 * @param reminderId - 提醒 ID
 * @param delayMinutes - 延时分钟数
 * @param threadId - 可选的对话线程 ID
 * @returns 包含模型回复的对象
 */
export async function snoozeReminder(reminderId: string, delayMinutes: number, threadId?: string): Promise<{ reply: string }> {
  const result = await sendMessageStream(
    `延时提醒 ${reminderId} ${delayMinutes} 分钟`,
    threadId,
    () => {}, () => {}, () => {}, () => {}, () => {},
  );
  return { reply: result.reply };
}

/**
 * 流式发送消息并接收 SSE 事件回调
 * 返回一个 Promise，在流式传输完成时 resolve
 * @param message - 用户输入的消息文本
 * @param threadId - 可选的对话线程 ID
 * @param onToken - 收到 token 时的回调
 * @param onToolCall - 检测到工具调用时的回调（工具名 + 参数）
 * @param onToolResult - 工具执行完成时的回调（工具名 + 结果 + 状态）
 * @param onActionCard - 收到操作卡片时的回调
 * @param onError - 发生错误时的回调
 * @param signal - 可选的 AbortSignal，用于中断流式请求
 * @returns 包含 thread_id、trace_id、reply 和 action_cards 的结果对象
 */
export async function sendMessageStream(
  message: string,
  threadId: string | undefined,
  onToken: (text: string) => void,
  onToolCall: (name: string, params: Record<string, unknown>) => void,
  onToolResult: (name: string, result: unknown, status?: string) => void,
  onActionCard: (card: ActionCard) => void,
  onError: (msg: string) => void,
  signal?: AbortSignal,
): Promise<{
  thread_id: string;
  trace_id: string;
  reply: string;
  action_cards: ActionCard[];
  tool_calls?: Array<{ name: string; params?: Record<string, unknown>; status?: string }>;
  tool_results?: Array<{ name: string; params?: Record<string, unknown>; result?: unknown; status?: string }>;
}> {
  const res = await fetch("/api/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, thread_id: threadId ?? null }),
    signal,
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Stream API error ${res.status}: ${text}`);
  }

  const reader = res.body?.getReader();
  if (!reader) throw new Error("No response body");

  const decoder = new TextDecoder();
  let buffer = "";
  let currentEvent = "";
  let result = {
    thread_id: "",
    trace_id: "",
    reply: "",
    action_cards: [] as ActionCard[],
    tool_calls: [] as Array<{ name: string; params?: Record<string, unknown>; status?: string }>,
    tool_results: [] as Array<{ name: string; params?: Record<string, unknown>; result?: unknown; status?: string }>,
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      // 保留未完成的行到缓冲区，供下次拼接
      buffer = lines.pop() || "";

      for (const line of lines) {
        // 解析 SSE 事件类型行
        if (line.startsWith("event: ")) {
          currentEvent = line.slice(7).trim();
        } else if (line.startsWith("data: ")) {
          // 解析 SSE 数据行
          const dataStr = line.slice(6);
          try {
            const data = JSON.parse(dataStr);
            switch (currentEvent) {
              case "token":
                // 逐 token 回调，用于打字机效果
                onToken(data.text as string);
                break;
              case "tool_call":
                // 工具调用开始
                onToolCall(data.name as string, data.params as Record<string, unknown>);
                break;
              case "tool_result":
                // 工具执行结果（含状态：completed / failed）
                onToolResult(data.name as string, data.result, data.status as string);
                break;
              case "action_card":
                // 操作卡片（如任务创建、记忆修改等）
                onActionCard(data as unknown as ActionCard);
                break;
              case "done":
                // 流式传输完成，收集最终结果
                result = {
                  thread_id: data.thread_id as string,
                  trace_id: data.trace_id as string,
                  reply: data.reply as string,
                  action_cards: (data.action_cards || []) as ActionCard[],
                  tool_calls: (data.tool_calls || []) as Array<{ name: string; params?: Record<string, unknown>; status?: string }>,
                  tool_results: (data.tool_results || []) as Array<{ name: string; params?: Record<string, unknown>; result?: unknown; status?: string }>,
                };
                break;
              case "error":
                onError(data.message as string || "Unknown error");
                break;
            }
          } catch {
            // 跳过格式错误的 JSON 数据
          }
        }
      }
    }
  } catch (err) {
    // 用户主动中断（AbortSignal）时正常结束，不抛出错误
    if (signal?.aborted || (err instanceof Error && err.name === "AbortError")) {
      // 主动中断，忽略
    } else {
      throw err;
    }
  } finally {
    reader.releaseLock();
  }

  return result;
}

/**
 * 重置对话线程上下文
 * @param threadId - 可选的对话线程 ID
 */
export async function resetThread(threadId?: string): Promise<void> {
  await fetch("/api/thread/reset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ thread_id: threadId ?? null }),
  });
}
