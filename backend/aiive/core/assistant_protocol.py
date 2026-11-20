"""Private Assistant message metadata shared by transport and runtime history."""

from typing import Any

from langchain_core.messages import AIMessage

PROVIDER_METADATA = "provider_metadata"
REASONING_CONTENT = "reasoning_content"
LEGACY_FALLBACK = "disabled_legacy_fallback"


def assistant_metadata(message: AIMessage) -> dict[str, str]:
    metadata = message.response_metadata
    provider = metadata.get("provider")
    if not provider:
        provider = {"openai": "openai_compatible", "deepseek": "deepseek"}.get(str(metadata.get("model_provider", "")))
    fields = {"provider": provider, "thinking_mode": metadata.get("thinking_mode")}
    if provider == "deepseek":
        fields[REASONING_CONTENT] = message.additional_kwargs.get(REASONING_CONTENT)
    return {key: value for key, value in fields.items() if isinstance(value, str) and value}


def assistant_message_kwargs(metadata: dict[str, Any]) -> dict[str, Any]:
    reasoning = metadata.get(REASONING_CONTENT)
    return {
        "response_metadata": {key: metadata[key] for key in ("provider", "thinking_mode") if key in metadata},
        "additional_kwargs": {REASONING_CONTENT: reasoning} if isinstance(reasoning, str) and reasoning else {},
    }


def public_event_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    return {
        key: value for key, value in (payload or {}).items()
        if key not in {PROVIDER_METADATA, "assistant_content", "protocol_only", "protocol_status"}
    }
