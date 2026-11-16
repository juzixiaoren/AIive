/** 聊天错误契约：HTTP 非 2xx 与 SSE error 事件共用同一结构化载荷。 */

export const DEFAULT_CHAT_ERROR_CODE = "chat_stream_failed";

/** 后端结构化错误字段（snake_case 与 SSE 载荷一致）。 */
export interface ChatErrorPayload {
  message?: unknown;
  code?: unknown;
  status?: unknown;
  retryable?: unknown;
  trace_id?: unknown;
  retry_after_seconds?: unknown;
  draft_recoverable?: unknown;
}

/** 统一聊天错误：两端 UI 只依赖这些字段判断回滚、草稿恢复与重试。 */
export class ChatApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly retryable: boolean;
  readonly traceId?: string;
  readonly draftRecoverable: boolean;
  readonly retryAfterSeconds?: number;

  constructor(
    message: string,
    code: string,
    status: number,
    retryable: boolean,
    traceId?: string,
    draftRecoverable: boolean = false,
    retryAfterSeconds?: number,
  ) {
    super(message);
    this.name = "ChatApiError";
    this.code = code;
    this.status = status;
    this.retryable = retryable;
    this.traceId = traceId;
    this.draftRecoverable = draftRecoverable;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

function asText(value: unknown, fallback: string): string {
  return typeof value === "string" && value.trim() ? value : fallback;
}

/** 从 SSE error 事件（或同构载荷）构造统一错误。 */
export function chatApiErrorFromPayload(
  payload: ChatErrorPayload,
  fallbackStatus: number = 500,
  fallbackMessage: string = "对话请求失败",
): ChatApiError {
  const code = asText(payload.code, DEFAULT_CHAT_ERROR_CODE);
  return new ChatApiError(
    asText(payload.message, fallbackMessage),
    code,
    Number(payload.status) || fallbackStatus,
    Boolean(payload.retryable),
    typeof payload.trace_id === "string" ? payload.trace_id : undefined,
    Boolean(payload.draft_recoverable),
    typeof payload.retry_after_seconds === "number" ? payload.retry_after_seconds : undefined,
  );
}

function errorBody(payload: unknown): ChatErrorPayload {
  if (!payload || typeof payload !== "object") return {};
  const body = payload as Record<string, unknown>;
  const detail = body.detail;
  if (detail && typeof detail === "object") return detail as ChatErrorPayload;
  if (typeof detail === "string") return { message: detail, code: body.code };
  return body as ChatErrorPayload;
}

/** 解析 HTTP 非 2xx：兼容 FastAPI detail 包装、纯文本与结构化载荷。 */
export async function chatApiErrorFromResponse(
  response: Response,
): Promise<ChatApiError> {
  const text = await response.text();
  let payload: unknown = {};
  try {
    payload = JSON.parse(text);
  } catch {
    payload = {};
  }
  const error = chatApiErrorFromPayload(
    errorBody(payload),
    response.status,
    text ? "服务器请求失败 (" + response.status + ")" : "服务器请求失败",
  );
  return error;
}
