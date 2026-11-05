"""Outbox Job Payload 字段契约（Forget Scrub / Verify / Worker 共用）。

按 job_type 区分正文、引用 ID 与状态/幂等元数据；Scrub 与 Verify 必须使用
同一份契约，避免清理遗漏与漏检同时发生。
"""
from __future__ import annotations

from typing import Any

_REDACTED = "[redacted]"

# 所有 Job 共用的正文键（递归匹配，保留结构）。
COMMON_CONTENT_FIELDS: frozenset[str] = frozenset({
    "user_message", "reply", "message", "content", "title", "task_title",
    "question", "goal", "reason", "failure_reason", "description", "summary",
    "query", "prompt", "result", "output", "input", "text", "body", "preview",
    "raw_text", "raw_payload", "arguments", "params", "tool_args",
    "input_snapshot", "output_summary", "inline_payload", "content_preview",
    "content_span", "error", "error_message", "notes", "decision_span",
    "verdict_reason", "resolution_reason", "old_content", "new_content",
    "event_payload", "trigger_event_payload", "plan", "state", "condition",
})

# job_type → 该类型实际携带正文的字段（生产结构）。
CONTENT_FIELDS_BY_JOB_TYPE: dict[str, frozenset[str]] = {
    "memory_extraction": frozenset({"user_message", "reply"}),
    "goal_event_dispatch": frozenset({"event_payload"}),
    "goal_trigger_decision": frozenset({
        "trigger_event_payload", "reason", "trigger_event_type",
    }),
    "reminder_delivery": frozenset({"title", "content"}),
    "agent_task_run": frozenset({
        "goal", "reason", "failure_reason", "task_title", "summary", "query",
    }),
    "memory_conflict_question": frozenset({
        "title", "content", "message", "question",
    }),
    "memory_conflict_reply": frozenset({"reason", "content", "message"}),
    "memory_conflict_cleanup": frozenset({"reason"}),
    "improvement_discovery": frozenset({
        "title", "content", "message", "goal", "content_preview",
    }),
    "tool_operation": frozenset({
        "name", "params", "result", "error", "tool_args", "arguments",
    }),
    "segment_sealing": frozenset({"summary", "content", "goal", "reason"}),
    "epoch_checkpoint": frozenset({"summary", "content", "goal", "reason"}),
    "epoch_rollover": frozenset({"reason", "summary"}),
    "memory_maintenance": frozenset({"content", "reason", "summary"}),
}

# 定位与幂等字段：任何情况下保留。
REFERENCE_FIELDS: frozenset[str] = frozenset({
    "operation_id", "event_id", "trigger_event_id", "assistant_event_id",
    "source_event_ids", "source_turn_record_id", "source_turn_id",
    "turn_id", "turn_record_id", "thread_id", "task_id", "step_id",
    "memory_id", "conflict_case_id", "case_id", "approval_id", "action_id",
    "tool_call_id", "tool_operation_id", "forget_operation_id", "operation_key",
    "idempotency_key", "trigger_key", "batch_index", "source_id", "source_type",
    "source_version", "record_version", "trace_id", "request_id", "run_id",
    "schema_version", "message_source", "extraction_mode", "event_type",
    "task_type", "status", "occurred_at", "fired_at", "scheduled_at",
    "pending_event_dedupe_key", "available_at", "max_retries",
})

# Forget Saga 自身的 Job 不清理（payload 全是 ID，破坏会中断 Saga）。
INTERNAL_JOB_PREFIXES: tuple[str, ...] = ("forget_", "retention_cleanup")


def is_internal_job(job_type: str) -> bool:
    return job_type.startswith(INTERNAL_JOB_PREFIXES)


def content_fields(job_type: str) -> frozenset[str]:
    return COMMON_CONTENT_FIELDS | CONTENT_FIELDS_BY_JOB_TYPE.get(
        job_type, frozenset()
    )


def _sentinel(value: Any) -> Any:
    if isinstance(value, dict):
        return {}
    if isinstance(value, list):
        return []
    if isinstance(value, str):
        return _REDACTED
    return None


def redact_job_payload(job_type: str, payload: Any) -> Any:
    """递归清理正文键；引用 ID / 状态 / 幂等字段保留。"""
    if is_internal_job(job_type) or not isinstance(payload, dict):
        return payload
    fields = content_fields(job_type)
    result: dict[str, Any] = dict(payload)
    for key, value in payload.items():
        if key in fields:
            result[key] = _sentinel(value)
        elif isinstance(value, (dict, list)):
            result[key] = redact_job_payload(job_type, value)
    result["forget_content_redacted"] = True
    return result


def payload_has_content(job_type: str, payload: Any) -> bool:
    """Verify 用：是否仍存在未清理的正文。"""
    if is_internal_job(job_type) or not isinstance(payload, dict):
        return False
    fields = content_fields(job_type)
    for key, value in payload.items():
        if key in fields:
            if value in (None, "", _REDACTED, [], {}):
                continue
            if isinstance(value, (dict, list)) and not value:
                continue
            return True
        if isinstance(value, dict) and payload_has_content(job_type, value):
            return True
        if isinstance(value, list) and any(
            isinstance(item, dict) and payload_has_content(job_type, item)
            for item in value
        ):
            return True
    return False


def is_redacted_payload(payload: Any) -> bool:
    return isinstance(payload, dict) and bool(payload.get("forget_content_redacted"))


def _collect_ids(value: Any, key: str, out: set[str]) -> None:
    if key.endswith("_id") or key in {
        "source_event_ids", "event_ids", "turn_tokens",
    }:
        if isinstance(value, str):
            out.add(value)
        elif isinstance(value, list):
            out.update(str(item) for item in value if isinstance(item, str))


def job_reference_ids(payload: Any) -> set[str]:
    """提取 payload 中用于目标发现的引用 ID（不提取正文）。"""
    result: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                _collect_ids(value, key, result)
                if isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return result
