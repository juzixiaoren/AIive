"""Replay Eval scenario definitions (offline, deterministic).

Each scenario is a list of steps executed through the REAL execution chain:
  - ToolStep            -> ToolRegistry.execute (real tool handler + policy gating)
  - SafeDeleteStep      -> tools.safe_delete (real guard, no file deletion outside scope)
  - MemoryWriteStep     -> MemoryStore.create_record (the memory single-source writer)
  - MemoryAssertStep    -> query the active memory store
  - Event*/Outbox*/Snapshot* -> write + assert the real persistence models

No LLM, no network, no API key. Scenarios use unique keys/ids so they never
collide and the run is order-independent.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ToolStep:
    call: str
    args: dict[str, object] = field(default_factory=dict)
    expect_ok: bool = True
    expect_error_type: str | None = None
    result_contains: str | None = None
    source: str = "trusted_user_command"


@dataclass
class ExpectFailToolStep:
    call: str
    args: dict[str, object] = field(default_factory=dict)
    expect_error_type: str | None = None


@dataclass
class SafeDeleteStep:
    path: str
    expect_allowed: bool = True
    dangerous: bool = False
    mode: str = "trash"


@dataclass
class MemoryWriteStep:
    canonical_key: str
    content: str
    memory_type: str = "user_profile"


@dataclass
class MemoryAssertStep:
    canonical_key: str
    exists: bool = True


@dataclass
class EventEmitStep:
    event_type: str
    thread_id: str = "eval-thread"
    turn_id: str = "eval-turn"


@dataclass
class EventAssertStep:
    event_type: str


@dataclass
class OutboxEmitStep:
    job_type: str
    operation_id: str | None = None


@dataclass
class OutboxAssertStep:
    job_type: str


@dataclass
class SnapshotEmitStep:
    thread_id: str = "eval-thread"
    trace_id: str = "eval-trace"


@dataclass
class SnapshotAssertStep:
    thread_id: str = "eval-thread"


@dataclass
class Scenario:
    id: str
    description: str
    steps: list[object] = field(default_factory=list)


SCENARIOS: list[Scenario] = []


def _scn(scenario_id: str, description: str, steps: list[object]) -> None:
    SCENARIOS.append(Scenario(id=scenario_id, description=description, steps=steps))


# --- tool call + result -----------------------------------------------------
_scn("eval.s01_echo", "echo returns the echoed message", [
    ToolStep("echo", {"message": "hello-eval-s01"}, expect_ok=True, result_contains="hello-eval-s01"),
])
_scn("eval.s02_current_time", "get_current_time returns without error", [
    ToolStep("get_current_time", {}, expect_ok=True),
])
_scn("eval.s03_list_tasks", "list_tasks tool returns without error", [
    ToolStep("list_tasks", {}, expect_ok=True),
])
_scn("eval.s04_show_notifications", "show_notifications tool returns without error", [
    ToolStep("show_notifications", {}, expect_ok=True),
])
_scn("eval.s05_query_attention", "query_attention tool returns without error", [
    ToolStep("query_attention", {}, expect_ok=True),
])
_scn("eval.s06_memory_search", "memory_search tool returns without error", [
    ToolStep("memory_search", {"query": "coffee"}, expect_ok=True),
])

# --- failure handling -------------------------------------------------------
_scn("eval.s07_unknown_tool_refused", "unknown tool is refused by the registry", [
    ToolStep("nonexistent_tool_xyz", {}, expect_ok=False),
])
_scn("eval.s08_unauthorized_source", "tool refused when instruction source is not authorized", [
    ToolStep("echo", {"message": "x"}, expect_ok=False, source="untrusted_bot"),
])
_scn("eval.s09_invalid_params_refused", "tool refused when params violate its schema", [
    ToolStep("echo", {"wrong_arg": 1}, expect_ok=False),
])
_scn("eval.s10_safe_delete_dangerous_blocked", "safe_delete refuses a dangerous absolute path", [
    SafeDeleteStep("/etc/passwd", expect_allowed=False, dangerous=True),
])
_scn("eval.s11_safe_delete_out_of_scope_blocked", "safe_delete refuses a path outside any scope", [
    SafeDeleteStep("/tmp/evil-escape.txt", expect_allowed=False),
])
_scn("eval.s12_safe_delete_in_scope_allowed", "safe_delete allows a scoped deletion", [
    SafeDeleteStep("allowed-s12.txt", expect_allowed=True),
])

# --- memory: change + assertions --------------------------------------------
_scn("eval.s13_memory_write_read", "memory write lands in the single-source store and is readable", [
    MemoryWriteStep("eval.mem.s13", "User prefers dark mode"),
    MemoryAssertStep("eval.mem.s13", exists=True),
])
_scn("eval.s14_memory_two_keys", "two independent memories persist separately", [
    MemoryWriteStep("eval.mem.s14a", "alpha"),
    MemoryWriteStep("eval.mem.s14b", "beta"),
    MemoryAssertStep("eval.mem.s14a", exists=True),
    MemoryAssertStep("eval.mem.s14b", exists=True),
])
_scn("eval.s15_memory_update_path", "re-writing a key keeps the active record present", [
    MemoryWriteStep("eval.mem.s15", "v1"),
    MemoryAssertStep("eval.mem.s15", exists=True),
    MemoryWriteStep("eval.mem.s15", "v2"),
    MemoryAssertStep("eval.mem.s15", exists=True),
])

# --- events: persistence ----------------------------------------------------
_scn("eval.s16_event_emit_assert", "user_message event is persisted and queryable", [
    EventEmitStep("user_message", thread_id="eval-thread-s16", turn_id="eval-turn-s16"),
    EventAssertStep("user_message"),
])
_scn("eval.s17_event_tool_result", "tool_call event is persisted and queryable", [
    EventEmitStep("tool_call", thread_id="eval-thread-s17", turn_id="eval-turn-s17"),
    EventAssertStep("tool_call"),
])

# --- outbox: idempotency anchor ---------------------------------------------
_scn("eval.s18_outbox_emit_assert", "outbox job is persisted and queryable", [
    OutboxEmitStep("memory_extraction", operation_id="eval-out-s18"),
    OutboxAssertStep("memory_extraction"),
])

# --- context snapshot: persistence ------------------------------------------
_scn("eval.s19_snapshot_emit_assert", "context snapshot is persisted and queryable", [
    SnapshotEmitStep(thread_id="eval-thread-s19", trace_id="eval-trace-s19"),
    SnapshotAssertStep("eval-thread-s19"),
])

# --- composed / state-change scenarios --------------------------------------
_scn("eval.s20_tool_then_event", "tool execution followed by an event keeps both observable", [
    ToolStep("echo", {"message": "composed-s20"}, expect_ok=True),
    EventEmitStep("llm_response", thread_id="eval-thread-s20", turn_id="eval-turn-s20"),
    EventAssertStep("llm_response"),
])
_scn("eval.s21_safe_delete_guard_combo", "guard allows in-scope but blocks dangerous in one flow", [
    SafeDeleteStep("allowed-s21.txt", expect_allowed=True),
    SafeDeleteStep("/root/.ssh/id_rsa", expect_allowed=False, dangerous=True),
])
_scn("eval.s22_memory_and_snapshot", "memory and snapshot state are both captured", [
    MemoryWriteStep("eval.mem.s22", "composed memory"),
    SnapshotEmitStep(thread_id="eval-thread-s22", trace_id="eval-trace-s22"),
    MemoryAssertStep("eval.mem.s22", exists=True),
    SnapshotAssertStep("eval-thread-s22"),
])
_scn("eval.s23_failure_then_recovery", "a refused tool does not corrupt subsequent success", [
    ToolStep("nonexistent_tool_xyz", {}, expect_ok=False),
    ToolStep("echo", {"message": "recovered-s23"}, expect_ok=True, result_contains="recovered-s23"),
])
_scn("eval.s24_memory_search_after_write", "written memory is discoverable via the recall tool", [
    MemoryWriteStep("eval.mem.s24", "s24 prefer tea"),
    ToolStep("memory_search", {"query": "tea"}, expect_ok=True),
])
