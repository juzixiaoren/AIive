/** Chat SSE 事件的唯一解析实现（Web 与 Android 共用）。 */

/** 一个已解析的 SSE 事件：事件名 + JSON data。 */
export interface ChatStreamEvent {
  event: string;
  data: Record<string, unknown>;
}

/**
 * 逐事件读取 chat/stream 的 SSE 响应。
 *
 * 只负责协议帧：CRLF 归一、event/data 行、JSON 解析容错；不解释业务语义、
 * 不维护完成/取消状态机，也不吞掉调用方回调抛出的异常。
 */
export async function readChatEventStream(
  response: Response,
  onEvent: (event: ChatStreamEvent) => void,
): Promise<void> {
  const reader = response.body?.getReader();
  if (!reader) throw new Error("服务器没有返回流式响应");
  const decoder = new TextDecoder();
  let buffer = "";
  let currentEvent = "";

  const handleLine = (line: string) => {
    if (line.startsWith("event:")) {
      currentEvent = line.slice(6).trim();
      return;
    }
    if (!line.startsWith("data:")) return;
    const payload = line.slice(5).trim();
    if (!payload) return;
    let data: Record<string, unknown>;
    try {
      data = JSON.parse(payload) as Record<string, unknown>;
    } catch {
      return;
    }
    onEvent({ event: currentEvent, data });
  };

  try {
    for (;;) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true }).replace(/\r\n/g, "\n");
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) handleLine(line);
    }
    if (buffer) handleLine(buffer);
  } finally {
    reader.releaseLock();
  }
}
