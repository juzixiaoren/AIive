"""Outbox Job Payload 字段契约（Forget Scrub / Verify / Worker 共用）。

按 job_type 区分正文、结构化引用 ID 与状态/幂等元数据。Outbox 目标发现使用
精确集合匹配，禁止 JSON 全文 LIKE / 子串包含。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_REDACTED = "[redacted]"
_REDACTION_MARKER = "forget_content_redacted"

# 所有 Job 共用的正文键（递归匹配，保留结构）。
COMMON_CONTENT_FIELDS: frozenset[str] = frozenset({
    "user_message", "reply", "message", "content", "title", "task_title",
    "question", "goal", "reason", "failure_reason", "description", "summary",
    "query", "prompt", "result", "output", "input", "text", "body", "preview",
    "raw_text", "raw_payload", "arguments", "params", "tool_args",
    "input_snapshot", "output_summary", "inline_payload", "content_preview",
    "content_span", "error", "error_message", "notes", "decision_span",
    "verdict_reason", "resolution_reason", "old_content", "new_content",
    "event_payload", "trigger_event_payload", "condition",
})
# 明确不属于正文的执行控制字段（绝不能脱敏，否则状态机断链）：
#   trigger —— agent_task_run 的派发原因（dispatch / reminder / goal），
#   step_id / task_type / status 等见 REFERENCE_FIELDS。

# job_type → 该类型实际携带正文的字段（生产结构）。
CONTENT_FIELDS_BY_JOB_TYPE: dict[str, frozenset[str]] = {
    "memory_extraction": frozenset({"user_message", "reply"}),
    "goal_event_dispatch": frozenset({"event_payload"}),
    "goal_trigger_decision": frozenset({
        "trigger_event_payload", "reason", "trigger_event_type", "plan",
    }),
    "reminder_delivery": frozenset({"title", "content"}),
    "agent_task_run": frozenset({
        "goal", "reason", "failure_reason", "task_title", "summary", "query",
        # working-state 快照属于正文；trigger 是执行控制字段，保留。
        "state", "plan", "progress",
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
    "segment_sealing": frozenset({
        "summary", "content", "goal", "reason", "state", "details",
    }),
    "epoch_checkpoint": frozenset({"summary", "content", "goal", "reason"}),
    "epoch_rollover": frozenset({"reason", "summary", "state", "details"}),
    "memory_maintenance": frozenset({
        "content", "reason", "summary", "progress", "details", "state",
    }),
}

# 定位与幂等字段：任何情况下保留。
REFERENCE_FIELDS: frozenset[str] = frozenset({
    "operation_id", "event_id", "trigger_event_id", "assistant_event_id",
    "request_event_id", "message_event_id", "source_event_ids",
    "source_turn_record_id", "source_turn_id", "turn_id", "turn_record_id",
    "thread_id", "task_id", "step_id", "memory_id", "old_memory_id",
    "resolved_memory_id", "conflict_case_id", "case_id", "approval_id",
    "action_id", "run_id", "evidence_id", "artifact_id", "checkpoint_id",
    "tool_call_id", "tool_operation_id", "forget_operation_id", "operation_key",
    "idempotency_key", "trigger_key", "batch_index", "source_id", "source_type",
    "source_version", "record_version", "trace_id", "request_id",
    "schema_version", "message_source", "extraction_mode", "event_type",
    "task_type", "status", "occurred_at", "fired_at", "scheduled_at",
    "pending_event_dedupe_key", "available_at", "max_retries",
})

INTERNAL_JOB_PREFIXES: tuple[str, ...] = ("forget_", "retention_cleanup")

# 引用语义 → payload 键（用于精确集合匹配）。
_DEFAULT_REFERENCE_SCHEMA: dict[str, frozenset[str]] = {
    "event": frozenset({
        "event_id", "trigger_event_id", "assistant_event_id",
        "request_event_id", "message_event_id", "source_event_ids",
        "event_ids",
    }),
    "turn": frozenset({
        "source_turn_record_id", "source_turn_id", "turn_id", "turn_record_id",
    }),
    "memory": frozenset({
        "memory_id", "old_memory_id", "resolved_memory_id", "winner_id",
    }),
    "task": frozenset({"task_id"}),
    "child": frozenset({
        "run_id", "step_id", "action_id", "evidence_id", "artifact_id",
        "checkpoint_id", "approval_id", "conflict_case_id", "case_id",
        "tool_operation_id", "source_id",
    }),
    "thread": frozenset({"thread_id"}),
    "trace": frozenset({"trace_id"}),
    "operation": frozenset({"operation_id"}),
}

# 各 job_type 的真实 Payload Schema（仅列与默认不同的部分）。
_JOB_REFERENCE_SCHEMA: dict[str, dict[str, frozenset[str]]] = {
    "memory_extraction": {
        "event": frozenset({"source_event_ids", "assistant_event_id"}),
        "turn": frozenset({"source_turn_record_id", "source_turn_id"}),
        "thread": frozenset({"thread_id"}),
        "trace": frozenset({"trace_id"}),
    },
    "goal_event_dispatch": {
        "event": frozenset({"event_id"}),
        "thread": frozenset({"thread_id"}),
    },
    "goal_trigger_decision": {
        "event": frozenset({"trigger_event_id"}),
        "task": frozenset({"task_id"}),
        "child": frozenset({"step_id"}),
        "trace": frozenset({"trace_id"}),
    },
    "reminder_delivery": {
        "task": frozenset({"task_id"}),
        "thread": frozenset({"thread_id"}),
        "trace": frozenset({"trace_id"}),
    },
    "agent_task_run": {
        "task": frozenset({"task_id"}),
        "trace": frozenset({"trace_id"}),
    },
    "tool_operation": {
        "child": frozenset({"tool_operation_id", "tool_call_id"}),
        "operation": frozenset({"operation_id"}),
    },
}

# operation_id 是冒号分隔的业务键（<业务前缀>:<来源ID>:<版本/随机串>）。
# 发现时只做「整段 token 精确相等」匹配：既不整串相等，也不做子串包含，
# 因此 id 前缀相同的不同 token（如 <turn>EXTRA）不会误命中。
_OPERATION_ID_BUSINESS_PREFIXES: frozenset[str] = frozenset({
    "memory_extraction", "agent_task_run", "reminder_delivery", "retrieval_refresh",
    "epoch_checkpoint", "epoch_rollover", "core", "memory_vector_refresh",
    "memory_file_projection", "memory_graph_refresh", "memory_markdown_project",
    "segment_sealing", "compaction", "goal_event", "goal_trigger", "tool_operation",
})

# 版本/批次 token（v3、b0）不是来源引用，直接跳过。
_NON_SOURCE_TOKEN = re.compile(r"^(v\d+|b\d+)$")


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


def _redact_node(job_type: str, node: Any, fields: frozenset[str]) -> Any:
    """递归支持 dict / list / 嵌套 list / scalar。"""
    if isinstance(node, dict):
        result: dict[str, Any] = {}
        for key, value in node.items():
            if key in fields:
                result[key] = _sentinel(value)
            elif isinstance(value, (dict, list)):
                result[key] = _redact_node(job_type, value, fields)
            else:
                result[key] = value
        return result
    if isinstance(node, list):
        return [_redact_node(job_type, item, fields) for item in node]
    return node


def redact_job_payload(job_type: str, payload: Any) -> Any:
    """递归清理正文键；引用 ID / 状态 / 幂等字段保留。"""
    if is_internal_job(job_type):
        return payload
    fields = content_fields(job_type)
    redacted = _redact_node(job_type, payload, fields)
    if isinstance(redacted, dict):
        redacted[_REDACTION_MARKER] = True
    return redacted


def _content_in_node(job_type: str, node: Any, fields: frozenset[str]) -> bool:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in fields:
                if value in (None, "", _REDACTED, [], {}):
                    continue
                if isinstance(value, (dict, list)) and not value:
                    continue
                return True
            if isinstance(value, (dict, list)) and _content_in_node(
                job_type, value, fields,
            ):
                return True
        return False
    if isinstance(node, list):
        return any(
            _content_in_node(job_type, item, fields) for item in node
        )
    return False


def payload_has_content(job_type: str, payload: Any) -> bool:
    """Verify 用：是否仍存在未清理的正文（与 Scrub 同一递归规则）。"""
    if is_internal_job(job_type):
        return False
    return _content_in_node(job_type, payload, content_fields(job_type))


def is_redacted_payload(payload: Any) -> bool:
    return isinstance(payload, dict) and bool(payload.get(_REDACTION_MARKER))


@dataclass(frozen=True)
class JobReferences:
    event_ids: set[str] = field(default_factory=set)
    turn_ids: set[str] = field(default_factory=set)
    memory_ids: set[str] = field(default_factory=set)
    task_ids: set[str] = field(default_factory=set)
    child_ids: set[str] = field(default_factory=set)
    thread_ids: set[str] = field(default_factory=set)
    trace_ids: set[str] = field(default_factory=set)
    operation_ids: set[str] = field(default_factory=set)
    unparseable: bool = False

    @property
    def exact_source_ids(self) -> set[str]:
        return (
            self.event_ids | self.turn_ids | self.memory_ids | self.child_ids
            | self.trace_ids
        )


def _add_ids(value: Any, out: set[str]) -> bool:
    """收集引用字段里的字符串 ID；返回是否全部可解析。

    已知引用字段出现嵌套数组 / 对象 / 数字等异常结构时必须标记 unparseable
    （fail-closed），不能静默丢弃——否则该 Job 可能因解析不完整而漏检。
    """
    if value is None:
        return True
    if isinstance(value, str):
        if value:
            out.add(value)
        return True
    if isinstance(value, (list, tuple, set)):
        ok = True
        for item in value:
            if isinstance(item, str):
                if item:
                    out.add(item)
            else:
                ok = False
        return ok
    return False


def _schema_for(job_type: str) -> dict[str, frozenset[str]]:
    schema: dict[str, frozenset[str]] = {
        key: frozenset(keys) for key, keys in _DEFAULT_REFERENCE_SCHEMA.items()
    }
    for semantic, keys in _JOB_REFERENCE_SCHEMA.get(job_type, {}).items():
        schema[semantic] = frozenset(set(schema.get(semantic, set())) | set(keys))
    return schema


def job_reference_ids(job_type: str, payload: Any) -> JobReferences:
    """按 job_type 的真实 Payload Schema 提取结构化引用 ID。

    支持 dict / list / 嵌套 list；只做精确字段取值匹配，不使用 JSON 文本
    LIKE / 字符串包含。顶层非 dict/list 结构无法可靠解析，明确标记。
    """
    schema = _schema_for(job_type)
    known_keys = {key for keys in schema.values() for key in keys}
    refs = JobReferences()
    unparseable = False

    content_keys = content_fields(job_type)

    def parse_dict(node: dict[str, Any]) -> None:
        nonlocal unparseable
        for key in node:
            if (key.endswith("_id") and key not in known_keys
                    and key not in {"operation_id", "idempotency_key", "tool_call_id"}):
                unparseable = True
        for semantic, keys in schema.items():
            out = getattr(refs, f"{semantic}_ids", None)
            if out is None:
                continue
            for key in keys:
                if key in node and not _add_ids(node[key], out):
                    unparseable = True
        # 非契约字段中的嵌套结构继续递归；正文键与已知引用键不再深入。
        for key, value in node.items():
            if key in known_keys or key in content_keys:
                continue
            if isinstance(value, (dict, list)):
                walk(value)

    def walk(node: Any) -> None:
        nonlocal unparseable
        if isinstance(node, dict):
            parse_dict(node)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        else:
            unparseable = True

    walk(payload)
    return JobReferences(
        event_ids=refs.event_ids,
        turn_ids=refs.turn_ids,
        memory_ids=refs.memory_ids,
        task_ids=refs.task_ids,
        child_ids=refs.child_ids,
        thread_ids=refs.thread_ids,
        trace_ids=refs.trace_ids,
        operation_ids=refs.operation_ids,
        unparseable=unparseable,
    )


def operation_id_tokens(job_type: str, operation_id: str) -> set[str]:
    """把 operation_id 拆成来源 token 集合（整段精确相等，不做子串包含）。

    operation_id 形如 <业务前缀>:<来源ID>:<版本/随机串>。业务前缀与版本/批次
    token 被剔除；剩余 token 由调用方与目标 ID 集合做精确交集，因此
    <turn_id>EXTRA 这类前缀相同但实际不同的 token 不会命中。
    """
    if is_internal_job(job_type) or not operation_id:
        return set()
    tokens: set[str] = set()
    for token in str(operation_id).split(":"):
        token = token.strip()
        if not token:
            continue
        if token in _OPERATION_ID_BUSINESS_PREFIXES or _NON_SOURCE_TOKEN.match(token):
            continue
        tokens.add(token)
    return tokens
