"""
模块功能说明：
- LLM 客户端模块，封装与 OpenAI 兼容的大模型 API 的通信
- 提供同步聊天（chat）和流式聊天（chat_stream）两种调用方式
- 包含 LLMClient（生产客户端）、FakeLLMClient（测试替身）和 LLMResponse（响应模型）
- 通过 default_llm_client() 工厂函数从项目配置创建统一的客户端实例
"""
import json
import logging
import time
import uuid
from dataclasses import dataclass

logger = logging.getLogger(__name__)
from collections.abc import Sequence
from typing import Any, override

import httpx


@dataclass
class LLMResponse:
    """LLM 响应数据类，封装一次 API 调用的完整结果。

    属性:
        content: LLM 返回的文本内容
        model: 实际使用的模型名称
        latency_ms: 调用延迟（毫秒）
        usage: token 用量信息（prompt_tokens, completion_tokens, total_tokens）
        raw_preview: 原始响应的前 2000 字符预览
        trace_id: 追踪 ID，用于日志关联
    """
    content: str
    model: str
    latency_ms: float
    usage: dict[str, int]
    raw_preview: str
    trace_id: str


class LLMClientError(Exception):
    """LLM 客户端异常，携带稳定错误码、重试语义和追踪 ID。"""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        trace_id: str | None = None,
        code: str = "llm_unavailable",
        retryable: bool = True,
        retry_after_seconds: int | None = None,
    ):
        super().__init__(message)
        self.status_code: int | None = status_code
        self.trace_id: str | None = trace_id
        self.code = code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


def normalize_llm_error(error: Exception, trace_id: str | None = None) -> LLMClientError:
    """将不同 LLM SDK 的异常归一化为安全、稳定的错误契约。"""
    if isinstance(error, LLMClientError):
        if not error.trace_id:
            error.trace_id = trace_id
        return error

    response = getattr(error, "response", None)
    status_code = getattr(error, "status_code", None) or getattr(response, "status_code", None)
    error_name = type(error).__name__.lower()
    if status_code == 429 or "ratelimit" in error_name:
        return LLMClientError(
            "模型服务请求过于频繁，请稍后重试",
            status_code=429,
            trace_id=trace_id,
            code="llm_rate_limited",
            retryable=True,
        )
    if "timeout" in error_name:
        return LLMClientError(
            "模型服务响应超时，请稍后重试",
            status_code=504,
            trace_id=trace_id,
            code="llm_timeout",
            retryable=True,
        )
    if status_code in (401, 403):
        return LLMClientError(
            "模型服务配置不可用",
            status_code=503,
            trace_id=trace_id,
            code="llm_configuration_error",
            retryable=False,
        )
    if isinstance(status_code, int) and status_code >= 500:
        return LLMClientError(
            "模型服务暂时不可用，请稍后重试",
            status_code=503,
            trace_id=trace_id,
            code="llm_unavailable",
            retryable=True,
        )
    return LLMClientError(
        "模型调用失败，请稍后重试",
        status_code=502,
        trace_id=trace_id,
        code="llm_request_failed",
        retryable=True,
    )


class LLMClient:
    """LLM API 客户端，封装与 OpenAI 兼容接口的 HTTP 通信。

    支持同步聊天完成和流式聊天完成两种模式，内置超时和错误处理。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        default_model: str,
        timeout_seconds: int = 30,
    ):
        """初始化 LLM 客户端。

        参数:
            base_url: API 基础 URL（如 https://api.deepseek.com/v1）
            api_key: API 密钥
            default_model: 默认模型名称
            timeout_seconds: 默认超时时间（秒）
        """
        self._base_url: str = base_url.rstrip("/")
        self._api_key: str = api_key
        self._default_model: str = default_model
        self._timeout_seconds: int = timeout_seconds

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        model: str | None = None,
        temperature: float | None = None,
        timeout: int | None = None,
        trace_id: str | None = None,
    ) -> LLMResponse:
        """发送同步聊天完成请求，返回完整的 LLMResponse。

        参数:
            messages: 对话消息列表，每条消息包含 role 和 content
            model: 指定模型名称，为 None 时使用默认模型
            temperature: 生成温度参数，控制随机性
            timeout: 超时时间（秒），为 None 时使用默认值
            trace_id: 追踪 ID，为 None 时自动生成

        返回值:
            LLMResponse: 包含生成内容和元信息的响应对象

        异常:
            LLMClientError: API 返回非 200、请求超时或其他网络异常
        """
        if trace_id is None:
            trace_id = str(uuid.uuid4())

        model = model or self._default_model
        timeout_s = timeout or self._timeout_seconds

        payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
        }
        if temperature is not None:
            payload["temperature"] = temperature

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        url = f"{self._base_url}/chat/completions"

        start = time.monotonic()
        try:
            response = httpx.post(
                url,
                json=payload,
                headers=headers,
                timeout=httpx.Timeout(timeout_s, connect=10.0),
            )
            elapsed = (time.monotonic() - start) * 1000

            # 非正常响应：抛出 LLMClientError
            if response.status_code != 200:
                raise LLMClientError(
                    message=f"LLM API error: {response.status_code} - {response.text[:500]}",
                    status_code=response.status_code,
                    trace_id=trace_id,
                )

            data = response.json()
            choice = data["choices"][0]
            message = choice["message"]
            content = message.get("content", "") or ""

            usage = {
                "prompt_tokens": data.get("usage", {}).get("prompt_tokens", 0),
                "completion_tokens": data.get("usage", {}).get("completion_tokens", 0),
                "total_tokens": data.get("usage", {}).get("total_tokens", 0),
            }

            return LLMResponse(
                content=content,
                model=data.get("model", model),
                latency_ms=round(elapsed, 2),
                usage=usage,
                raw_preview=response.text[:2000],
                trace_id=trace_id,
            )

        except httpx.TimeoutException:
            elapsed = (time.monotonic() - start) * 1000
            raise LLMClientError(
                message=f"LLM request timed out after {timeout_s}s",
                status_code=None,
                trace_id=trace_id,
            )
        except LLMClientError:
            raise
        except Exception as e:
            raise LLMClientError(
                message=f"LLM request failed: {e}",
                status_code=None,
                trace_id=trace_id,
            )

    def chat_stream(
        self,
        messages: Sequence[dict[str, Any]],
        model: str | None = None,
        temperature: float | None = None,
        timeout: int | None = None,
        trace_id: str | None = None,
    ):
        """流式聊天完成请求，逐块 yield 文本内容。

        参数:
            messages: 对话消息列表，每条消息包含 role 和 content
            model: 指定模型名称，为 None 时使用默认模型
            temperature: 生成温度参数
            timeout: 超时时间（秒）
            trace_id: 追踪 ID

        Yields:
            str: 每次 yield 一段文本增量

        异常:
            LLMClientError: API 错误、超时或网络异常
        """
        if trace_id is None:
            trace_id = str(uuid.uuid4())

        model = model or self._default_model
        timeout_s = timeout or self._timeout_seconds

        payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "stream": True,
        }
        if temperature is not None:
            payload["temperature"] = temperature

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        url = f"{self._base_url}/chat/completions"

        try:
            with httpx.stream(
                "POST",
                url,
                json=payload,
                headers=headers,
                timeout=httpx.Timeout(timeout_s, connect=10.0),
            ) as response:
                if response.status_code != 200:
                    response.read()
                    raise LLMClientError(
                        message=f"LLM API error: {response.status_code} - {response.text[:500]}",
                        status_code=response.status_code,
                        trace_id=trace_id,
                    )
                # 逐行解析 SSE 流
                for line in response.iter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                        except json.JSONDecodeError:
                            logger.warning("流式响应 JSON 解析失败: trace_id=%s line=%s", trace_id, data_str[:200])
                            continue
                        choices = data.get("choices", [])
                        if choices:
                            delta = choices[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                yield content
        except httpx.TimeoutException:
            raise LLMClientError(
                message=f"LLM request timed out after {timeout_s}s",
                status_code=None,
                trace_id=trace_id,
            )
        except LLMClientError:
            raise
        except Exception as e:
            raise LLMClientError(
                message=f"LLM stream failed: {e}",
                status_code=None,
                trace_id=trace_id,
            )

    @property
    def default_model(self) -> str:
        return self._default_model

    @property
    def api_key(self) -> str:
        return self._api_key

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def timeout_seconds(self) -> int:
        return self._timeout_seconds


def default_llm_client() -> "LLMClient":
    """基于项目配置构架 LLMClient 实例的工厂函数。

    统一工厂函数确保意图识别、工具调用、主对话等所有路径使用相同的端点
    和温度默认值。在函数内部惰性导入 settings，避免模块加载时的循环导入。
    """
    from aiive.config import settings

    return LLMClient(
        base_url=settings.aiive_llm_base_url,
        api_key=settings.aiive_llm_api_key,
        default_model=settings.aiive_llm_model,
        timeout_seconds=settings.aiive_llm_timeout_seconds,
    )


class FakeLLMClient(LLMClient):
    """测试用假 LLM 客户端，不做真实 API 调用，返回预设的固定内容。

    用于单元测试和集成测试，避免依赖外部 LLM 服务。每次调用 chat() 会记录调用历史，
    便于测试断言。
    """
    def __init__(
        self,
        fixed_content: str = "Hello from FakeLLM",
        fixed_model: str = "fake-model",
        fixed_usage: dict[str, int] | None = None,
        latency_ms: float = 10.0,
    ):
        """初始化假 LLM 客户端。

        参数:
            fixed_content: 每次调用返回的固定文本内容
            fixed_model: 模拟的模型名称
            fixed_usage: 模拟的 token 用量
            latency_ms: 模拟的调用延迟（毫秒）
        """
        super().__init__(
            base_url="http://fake",
            api_key="fake-key",
            default_model=fixed_model,
            timeout_seconds=1,
        )
        self._fixed_content: str = fixed_content
        self._fixed_model: str = fixed_model
        self._fixed_usage: dict[str, int] = fixed_usage or {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        }
        self._latency_ms: float = latency_ms
        self._call_history: list[dict[str, Any]] = []

    @override
    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        model: str | None = None,
        temperature: float | None = None,
        timeout: int | None = None,
        trace_id: str | None = None,
    ) -> LLMResponse:
        """模拟同步聊天完成，记录调用历史并返回固定的预设内容。

        参数:
            messages: 对话消息列表
            model: 模型名称
            temperature: 温度参数
            timeout: 超时时间
            trace_id: 追踪 ID

        返回值:
            LLMResponse: 包含预设内容和元信息的响应对象
        """
        if trace_id is None:
            trace_id = str(uuid.uuid4())

        self._call_history.append({
            "messages": list(messages),
            "model": model,
            "temperature": temperature,
            "trace_id": trace_id,
        })

        return LLMResponse(
            content=self._fixed_content,
            model=model or self._fixed_model,
            latency_ms=self._latency_ms,
            usage=dict(self._fixed_usage),
            raw_preview='{"choices":[{"message":{"content":"' + self._fixed_content + '"}}]}',
            trace_id=trace_id,
        )
