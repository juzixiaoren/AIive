"""DeepSeek chat-completions transport for AIive's tool-calling models."""

from collections.abc import AsyncIterator, Iterator
from typing import Any, Literal, override

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_deepseek import ChatDeepSeek

from aiive.core.assistant_protocol import LEGACY_FALLBACK, REASONING_CONTENT


def _missing_reasoning(message: AIMessage) -> bool:
    metadata = message.response_metadata
    provider = metadata.get("provider")
    if not provider and metadata.get("model_provider") == "openai":
        provider = "openai_compatible"
    reasoning = message.additional_kwargs.get(REASONING_CONTENT)
    return (
        provider in (None, "deepseek")
        and metadata.get("thinking_mode") not in ("disabled", LEGACY_FALLBACK)
        and not (isinstance(reasoning, str) and reasoning)
    )


class AIiveChatDeepSeek(ChatDeepSeek):
    thinking_mode: Literal["enabled", "disabled"] = "enabled"

    def _request_options(self, messages: list[BaseMessage], kwargs: dict[str, Any]) -> dict[str, str]:
        # This dictionary belongs to this invocation, including overlapping async streams.
        sticky = kwargs.pop("legacy_thinking_fallback", False)
        mode: str = self.thinking_mode
        if mode == "enabled" and kwargs.get("tools") and (
            sticky or any(isinstance(msg, AIMessage) and _missing_reasoning(msg) for msg in messages)
        ):
            mode = LEGACY_FALLBACK
        kwargs["extra_body"] = {
            **(self.extra_body or {}), **(kwargs.get("extra_body") or {}),
            "thinking": {"type": "enabled" if mode == "enabled" else "disabled"},
        }
        return {"provider": "deepseek", "thinking_mode": mode}

    @override
    def _get_request_payload(
        self, input_: LanguageModelInput, *, stop: list[str] | None = None, **kwargs: Any,
    ) -> dict[str, Any]:
        messages = self._convert_input(input_).to_messages()
        payload = super()._get_request_payload(messages, stop=stop, **kwargs)
        if payload.get("tools") and (
            (payload.get("extra_body") or {}).get("thinking", {}).get("type") == "enabled"
        ):
            for source, wire in zip(messages, payload["messages"], strict=True):
                if not isinstance(source, AIMessage):
                    continue
                if source.response_metadata.get("provider") not in (None, "deepseek"):
                    continue
                reasoning = source.additional_kwargs.get(REASONING_CONTENT)
                if isinstance(reasoning, str) and reasoning:
                    wire[REASONING_CONTENT] = reasoning
        return payload

    @override
    def _generate(
        self, messages: list[BaseMessage], stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None, **kwargs: Any,
    ) -> ChatResult:
        metadata = self._request_options(messages, kwargs)
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        for generation in result.generations:
            generation.message.response_metadata.update(metadata)
        return result

    @override
    async def _agenerate(
        self, messages: list[BaseMessage], stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None, **kwargs: Any,
    ) -> ChatResult:
        metadata = self._request_options(messages, kwargs)
        result = await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
        for generation in result.generations:
            generation.message.response_metadata.update(metadata)
        return result

    @override
    def _stream(
        self, messages: list[BaseMessage], stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None, **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        metadata = self._request_options(messages, kwargs)
        for index, chunk in enumerate(super()._stream(messages, stop=stop, run_manager=run_manager, **kwargs)):
            # Strings in custom response metadata concatenate during chunk merging.
            if index == 0:
                chunk.message.response_metadata.update(metadata)
            yield chunk

    @override
    async def _astream(
        self, messages: list[BaseMessage], stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None, **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        metadata = self._request_options(messages, kwargs)
        async for chunk in super()._astream(messages, stop=stop, run_manager=run_manager, **kwargs):
            chunk.message.response_metadata.update(metadata)
            metadata = {}
            yield chunk
