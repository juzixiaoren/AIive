import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import httpx


@dataclass
class LLMResponse:
    content: str
    model: str
    latency_ms: float
    usage: dict[str, int]
    raw_preview: str
    trace_id: str


class LLMClientError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None, trace_id: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.trace_id = trace_id


class LLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        default_model: str,
        timeout_seconds: int = 30,
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._default_model = default_model
        self._timeout_seconds = timeout_seconds

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        timeout: Optional[int] = None,
        trace_id: Optional[str] = None,
    ) -> LLMResponse:
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
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        timeout: Optional[int] = None,
        trace_id: Optional[str] = None,
    ):
        """Stream chat completions. Yields text chunks."""
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
                    raise LLMClientError(
                        message=f"LLM API error: {response.status_code} - {response.text[:500]}",
                        status_code=response.status_code,
                        trace_id=trace_id,
                    )
                for line in response.iter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                        except Exception:
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


class FakeLLMClient(LLMClient):
    def __init__(
        self,
        fixed_content: str = "Hello from FakeLLM",
        fixed_model: str = "fake-model",
        fixed_usage: Optional[dict[str, int]] = None,
        latency_ms: float = 10.0,
    ):
        super().__init__(
            base_url="http://fake",
            api_key="fake-key",
            default_model=fixed_model,
            timeout_seconds=1,
        )
        self._fixed_content = fixed_content
        self._fixed_model = fixed_model
        self._fixed_usage = fixed_usage or {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        }
        self._latency_ms = latency_ms
        self._call_history: list[dict[str, Any]] = []

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        timeout: Optional[int] = None,
        trace_id: Optional[str] = None,
    ) -> LLMResponse:
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
