import type { Message } from "./types";
import { genMessageId } from "./messageModel";

const STORAGE_KEY = "aiive_active_thread";

/**
 * 从 localStorage 加载已保存的对话状态
 * @returns 包含 threadId 和 messages 的持久化状态
 */
export function loadStored(): { threadId: string | undefined; messages: Message[] } {
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
export function saveStored(threadId: string | undefined, messages: Message[]) {
  try {
    // 持久化前截断超大工具结果，避免大结果撑爆 localStorage 配额
    const trimmed = messages.slice(-200).map((m) => (
      m.toolCalls?.some((c) => c.result && c.result.length > 4000)
        ? {
          ...m,
          toolCalls: m.toolCalls.map((c) => (
            c.result && c.result.length > 4000
              ? { ...c, result: c.result.slice(0, 4000) + "…(已截断)" }
              : c
          )),
        }
        : m
    ));
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ threadId, messages: trimmed }));
  } catch {
    // 配额不足或序列化失败时放弃本次持久化，不能让异常冒泡到渲染层
  }
}
