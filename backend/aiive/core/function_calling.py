"""Shared LangChain transport, validation feedback and bounded correction policy.

No execution, database transactions, or hidden model retry loop lives here.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolCall, ToolMessage, message_to_dict
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from aiive.core.function_spec import FunctionSpec, FunctionSpecs
from aiive.config import settings
from aiive.core.llm_client import LLMClient
from aiive.core.assistant_protocol import assistant_message_kwargs, assistant_metadata
from aiive.core.deepseek_chat import AIiveChatDeepSeek
from aiive.tools.base import BaseTool
from aiive.tools.registry import ToolRegistry
from aiive.tools.tool_validation import canonical_tool_schema, validate_tool_params


def build_chat_model(client: LLMClient, *, streaming: bool = False) -> ChatOpenAI | AIiveChatDeepSeek:
    if client.provider == "deepseek":
        return AIiveChatDeepSeek(
            model=client.default_model, api_key=SecretStr(client.api_key), api_base=client.base_url,
            temperature=0, timeout=client.timeout_seconds, streaming=streaming,
            stream_usage=settings.aiive_llm_stream_usage, max_retries=0, use_responses_api=False, output_version="v0",
            thinking_mode=client.deepseek_thinking,
        )
    return ChatOpenAI(model=client.default_model, api_key=SecretStr(client.api_key),
                      base_url=client.base_url, temperature=0, timeout=client.timeout_seconds,
                      streaming=streaming, stream_usage=settings.aiive_llm_stream_usage,
                      max_retries=0)  # Infrastructure retries belong to the host/Outbox.


def function_definition(registration: BaseTool | FunctionSpec) -> dict[str, Any]:
    description = registration.description
    if registration.safety.writes_external_world:
        description += " (side_effect: true)"
    if registration.safety.risk_level in ("high", "critical"):
        description += f" (risk: {registration.safety.risk_level})"
    return {"name": registration.safety.capability_id, "description": description,
            "parameters": canonical_tool_schema(registration)}


def function_definitions(registry: ToolRegistry) -> list[dict[str, Any]]:
    """导出 OpenAI function 定义列表：模型可见工具 Schema 的唯一来源。"""
    return [
        {"type": "function", "function": function_definition(registration)}
        for registration in registry.registrations()
    ]


def correction_allowed(failures: int) -> bool:
    return failures <= settings.tool_argument_max_retries


def output_error(code: str, *, path: str = "$", expected: str = "one valid function call", received: str = "") -> dict[str, Any]:
    return {"ok": False, "error_type": "agent_output_validation_error", "retryable": True,
            "issues": [{"path": path, "code": code, "message": "模型函数调用不符合契约",
                        "expected": expected, "received": received}],
            "instruction": "只修正函数调用结构或参数，不重复无关内容；此前调用未执行。"}


def normalize_calls(message: AIMessage) -> AIMessage:
    """Convert LangChain invalid_tool_calls into paired, non-executable diagnostics.

    Malformed raw argument strings never enter feedback or persistent snapshots.
    """
    calls: list[Any] = []
    errors = dict(message.additional_kwargs.get("aiive_call_errors", {}))
    seen: set[str] = set()
    for raw, invalid in [(call, False) for call in message.tool_calls] + [(call, True) for call in message.invalid_tool_calls]:
        call = dict(raw)
        original_id = str(call.get("id") or "")
        call_id = original_id if original_id and original_id not in seen else f"call_{uuid.uuid4().hex}"
        seen.add(call_id)
        call.pop("error", None)
        call.update(id=call_id, type="tool_call")
        name = call.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name):
            call["name"] = "invalid_function"
            errors[call_id] = output_error("invalid_function_name", path="name", expected="function name",
                                           received=type(name).__name__)
        if invalid or not isinstance(call.get("args"), dict):
            issue = output_error("invalid_tool_arguments", path="arguments", expected="JSON object",
                                 received=type(call.get("args")).__name__)
            if call_id in errors:
                errors[call_id]["issues"].extend(issue["issues"])
            else:
                errors[call_id] = issue
            call["args"] = {}
        if not original_id or call_id != original_id:
            issue = output_error("invalid_tool_call_id", path="id", expected="unique nonempty tool_call_id",
                                 received=original_id)
            if call_id in errors:
                errors[call_id]["issues"].extend(issue["issues"])
            else:
                errors[call_id] = issue
        calls.append(call)
    return message.model_copy(update={"tool_calls": calls, "invalid_tool_calls": [],
                                      "additional_kwargs": {**message.additional_kwargs, "aiive_call_errors": errors}})


@dataclass
class CallValidation:
    errors: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def feedback(self, message: AIMessage) -> list[BaseMessage]:
        if not message.tool_calls:
            return [HumanMessage(content=json.dumps(self.errors["$"], ensure_ascii=False))]
        return [ToolMessage(content=json.dumps(self.errors[str(call["id"])], ensure_ascii=False),
                            tool_call_id=str(call["id"]), name=call["name"]) for call in message.tool_calls]

    def retry_turn(self, message: AIMessage) -> list[dict[str, Any]]:
        # Keep only a bounded repair exchange, never a conversation transcript.
        if not message.tool_calls:
            return [message_to_dict(item) for item in self.feedback(message)]
        calls: list[ToolCall] = []
        for call in message.tool_calls:
            error = self.errors.get(str(call["id"]))
            if error and error.get("error_type") != "batch_aborted_due_to_invalid_arguments":
                issues = error.get("issues", [])
                if not (len(issues) == 1 and issues[0]["code"] == "tool_call_count"):
                    call = call.copy()
                    call["args"] = {}
            calls.append(call)
        safe = AIMessage(content="", tool_calls=calls,
                         **assistant_message_kwargs(assistant_metadata(message)))
        return [message_to_dict(item) for item in [safe, *self.feedback(message)]]


def validate_calls(message: AIMessage, registry: ToolRegistry | FunctionSpecs, *, require_one: bool = False) -> CallValidation:
    errors = dict(message.additional_kwargs.get("aiive_call_errors", {}))
    calls = message.tool_calls
    for call in calls:
        call_id = str(call["id"])
        if call_id in errors:
            continue
        registration = registry.get(call["name"])
        if registration is None:
            errors[call_id] = output_error("unknown_function", path="name", expected="a currently bound function", received="str")
            continue
        result = validate_tool_params(registration, call["args"])
        if not result.ok:
            errors[call_id] = result.error_payload(call["name"])
    if require_one and len(calls) != 1:
        count = output_error("tool_call_count", received=str(len(calls)))
        errors = {
            str(call["id"]): {
                **count, "issues": [*count["issues"], *errors.get(str(call["id"]), {}).get("issues", [])],
            }
            for call in calls
        } or {"$": count}
    if errors:
        for call in calls:
            errors.setdefault(str(call["id"]), {
                "ok": False, "error_type": "batch_aborted_due_to_invalid_arguments", "retryable": True,
                "instruction": "同批其他调用无效，整批未执行；请重新生成完整调用。",
            })
    return CallValidation(errors)


def response_usage(message: AIMessage) -> dict[str, int]:
    usage = message.usage_metadata
    if usage:
        return {"prompt_tokens": int(usage.get("input_tokens", 0)),
                "completion_tokens": int(usage.get("output_tokens", 0)),
                "total_tokens": int(usage.get("total_tokens", 0))}
    raw = message.response_metadata.get("token_usage", message.response_metadata.get("usage", {}))
    return LLMClient._normalize_usage(raw) if isinstance(raw, dict) else {}  # pyright: ignore[reportPrivateUsage]
