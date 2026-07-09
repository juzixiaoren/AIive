"""Built-in tools registered in ToolRegistry. Each tool maps to a real service."""

import os
from datetime import datetime, timedelta, timezone

from aiive.db.base import SessionLocal
from aiive.tools.registry import (
    CapabilitySafetySchema,
    ToolRegistration,
    ToolRegistry,
    compute_descriptor_hash,
)


def _build_safety(capability_id: str, **overrides) -> CapabilitySafetySchema:
    base = dict(
        capability_id=capability_id,
        definition_source="local_builtin",
        definition_trust_level="trusted",
        risk_level="low",
        requires_confirmation=False,
        writes_external_world=False,
        can_access_secret=False,
        can_delete=False,
    )
    base.update(overrides)
    base["descriptor_hash"] = compute_descriptor_hash(base)
    fields = CapabilitySafetySchema.__dataclass_fields__
    return CapabilitySafetySchema(**{k: v for k, v in base.items() if k in fields})


# ═══════════════════════════════════════════════════════════════
# Handler functions
# ═══════════════════════════════════════════════════════════════

def _db_handler(fn):
    """Decorator: open/close DB session for handlers that need it."""
    def wrapper(**params):
        db = SessionLocal()
        try:
            result = fn(db, **params)
            db.commit()
            return result
        finally:
            db.close()
    return wrapper


# ── Echo ──
def _handle_echo(message: str = "") -> str:
    return message


# ── Reminder / Task ──
# Module-level context for passing thread_id from agent loop to tool handlers.
_current_thread_id: str | None = None


def set_thread_context(thread_id: str | None) -> None:
    global _current_thread_id
    _current_thread_id = thread_id


@_db_handler
def _handle_schedule_reminder(db, content: str, delay_minutes: int = 1):
    from aiive.runtime.task_manager import TaskManager
    next_check = datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)
    task = TaskManager(db).create(
        task_type="reminder",
        title=content,
        description=f"延迟{delay_minutes}分钟",
        next_check_at=next_check,
    )
    task.thread_id = _current_thread_id
    db.flush()
    return {"reminder_set": True, "task_id": task.id, "content": content, "delay_minutes": delay_minutes}


@_db_handler
def _handle_list_tasks(db, status: str = ""):
    from aiive.runtime.task_manager import TaskManager
    tasks = TaskManager(db).list_all(status or None)
    return [{"id": t.id, "task_type": t.task_type, "title": t.title, "status": t.status} for t in tasks]


@_db_handler
def _handle_cancel_task(db, task_id: str):
    from aiive.db.models import Task
    task = db.get(Task, task_id)
    if task:
        task.status = "canceled"
        return {"canceled": True, "task_id": task_id}
    return {"canceled": False, "error": "not found"}


@_db_handler
def _handle_show_notifications(db):
    from aiive.db.models import Event
    notifs = db.query(Event).filter(Event.event_type == "notification_created").order_by(Event.created_at.desc()).limit(20).all()
    return [{"id": e.id, "title": e.payload.get("title", ""), "message": e.payload.get("message", ""), "created_at": e.created_at.isoformat()} for e in notifs]


# ── Memory ──
@_db_handler
def _handle_remember_or_update(db, content: str, memory_type: str = "fact", memory_key: str = ""):
    """Create or update memory through MemoryGate + MemoryWriteService."""
    from aiive.memory.memory_gate import MemoryGate, MemoryGateInput
    from aiive.memory.memory_store import MemoryStore
    from aiive.memory.memory_write_service import MemoryWriteService

    store = MemoryStore(db)
    gate = MemoryGate()
    writer = MemoryWriteService(db)

    # Look up existing memory for the same key
    existing = None
    if memory_key:
        for old in store.get_active():
            if old.memory_key == memory_key:
                existing = {"id": old.id, "content": old.content, "memory_type": old.memory_type}
                break

    # Gate the write
    gate_input = MemoryGateInput(
        content=content,
        user_message=content,
        source="tool_call",
        intent_type="memory_update",
        execution_mode="execute",
        should_execute=True,
        evidence_source="trusted_user_message",
        extracted_memory_type=memory_type,
        extracted_memory_key=memory_key or None,
        confidence=0.95,
        existing_memory=existing,
    )
    decision = gate.decide(gate_input)

    if decision.decision == "reject":
        return {"ok": False, "error": f"Memory gate rejected: {decision.reason}"}

    # Write through service
    result = writer.write(decision, content)
    db.flush()
    return {
        "ok": True,
        "memory_id": result.get("memory_id", ""),
        "content": content,
        "memory_type": decision.memory_type or memory_type,
        "state": result.get("state", ""),
        "superseded_old": result.get("superseded_old", False),
    }


@_db_handler
def _handle_forget_memory(db, memory_id: str = "", reason: str = ""):
    from aiive.memory.memory_maintenance import MemoryMaintenance
    maint = MemoryMaintenance(db)
    if not memory_id:
        return {"ok": False, "error": "memory_id is required. LLM should provide the exact memory_id from previous search_memory results."}
    return maint.forget(memory_id, reason)


@_db_handler
def _handle_run_memory_maintenance(db):
    from aiive.memory.memory_maintenance import MemoryMaintenance
    return MemoryMaintenance(db).scan()


@_db_handler
def _handle_search_memory(db, query: str = ""):
    from aiive.memory.memory_store import MemoryStore
    records = MemoryStore(db).resolve_for_context() if not query else MemoryStore(db).list_all()
    return [{"id": r.id, "content": r.content, "memory_type": r.memory_type, "lifecycle_state": r.lifecycle_state} for r in records[:20]]


# ── File / Safe Delete ──
def _handle_safe_delete(path: str, scope_id: str = "test_artifacts", mode: str = "trash"):
    from aiive.tools.safe_delete import safe_delete as do_safe_delete
    decision = do_safe_delete(path, scope_id, mode)
    return {"allowed": decision.allowed, "reason": decision.reason, "resolved_path": decision.resolved_path}


def _handle_read_text_file(path: str, max_lines: int = 50):
    allowed_dir = os.path.expanduser("~/Documents")
    real_path = os.path.realpath(path)
    if not real_path.startswith(allowed_dir):
        return {"error": "Access denied", "path": path}
    if not os.path.isfile(real_path):
        return {"error": "File not found", "path": path}
    try:
        with open(real_path, "r") as f:
            lines = [line.rstrip("\n") for i, line in enumerate(f) if i < max_lines]
        return {"lines": lines, "total_read": len(lines)}
    except Exception as e:
        return {"error": str(e)}


# ── Knowledge ──
@_db_handler
def _handle_ingest_document(db, file_path: str):
    from aiive.knowledge.ingestor import KnowledgeIngestor
    from aiive.storage.object_store import put_text
    result = KnowledgeIngestor(db).ingest(file_path)
    # Save raw document to object store
    if result.get("ok") and not result.get("duplicate"):
        try:
            import os
            content = open(file_path).read()
            put_text("raw-documents", os.path.basename(file_path), content)
        except Exception:
            pass
    return result


@_db_handler
def _handle_search_knowledge(db, query: str, limit: int = 5):
    from aiive.knowledge.ingestor import search_chunks
    return search_chunks(db, query, limit)


# ── MCP ──
def _handle_search_mcp(goal: str = ""):
    from aiive.mcp.discovery import search_mcp_candidates
    candidates = search_mcp_candidates(goal)
    return [{"name": c.name, "source": c.source, "version": c.version, "description": c.description, "risk_notes": c.risk_notes, "declared_tools": c.declared_tools} for c in candidates]


@_db_handler
def _handle_install_mcp_sandbox(db, candidate_name: str):
    from aiive.mcp.discovery import search_mcp_candidates
    from aiive.mcp.installer import install_sandbox
    candidates = search_mcp_candidates(candidate_name)
    if not candidates:
        return {"ok": False, "error": "No candidate found"}
    c = candidates[0]
    return install_sandbox(db, c.name, c.package_ref, c.version, c.transport, c.declared_tools, {"name": c.name, "description": c.description})


# ── Self-Dev ──
def _handle_create_selfdev_plan(goal: str = ""):
    from aiive.config import settings
    from aiive.core.llm_client import LLMClient
    from aiive.selfdev.planner import SelfDevPlanner
    llm = LLMClient(base_url=settings.aiive_llm_base_url, api_key=settings.aiive_llm_api_key, default_model=settings.aiive_llm_model, timeout_seconds=settings.aiive_llm_timeout_seconds)
    return SelfDevPlanner(llm).plan(goal)


def _handle_apply_patch_to_inactive_slot():
    from aiive.selfdev.patch_executor import PatchExecutor
    return PatchExecutor().apply_to_inactive([])


def _handle_promote_slot():
    from aiive.selfdev.promote_rollback import PromoteRollback
    return PromoteRollback().promote()


def _handle_rollback_slot():
    from aiive.selfdev.promote_rollback import PromoteRollback
    pr = PromoteRollback()
    active = pr._manager.get_active_slot()
    previous = "B" if active == "A" else "A"
    return pr.rollback(previous)


# ── Rhythm / Attention ──
@_db_handler
def _handle_query_rhythm(db):
    from aiive.runtime.rhythm_manager import RhythmManager
    return RhythmManager(db).daily_summary()


@_db_handler
def _handle_query_attention(db, thread_id: str = ""):
    from aiive.runtime.attention_manager import AttentionManager
    return AttentionManager(db).recompute(thread_id, "query")


# ═══════════════════════════════════════════════════════════════
# Registration
# ═══════════════════════════════════════════════════════════════

def register_builtin_tools(registry: ToolRegistry) -> None:
    # Tuple format: (cap_id, handler, description, params, risk_level, writes_external_world, can_delete)
    tools = [
        # Basic
        ("echo", _handle_echo, "Echo back the input message", {"message": "str"}, "low", False, False),
        # Reminder / Task
        ("schedule_reminder", _handle_schedule_reminder, "Create a timed reminder. Worker polls DB and fires real notification.", {"content": "str", "delay_minutes": "int"}, "low", True, False),
        ("list_tasks", _handle_list_tasks, "List all tasks/reminders", {"status": "str"}, "low", False, False),
        ("cancel_task", _handle_cancel_task, "Cancel a task by ID", {"task_id": "str"}, "low", True, False),
        ("show_notifications", _handle_show_notifications, "Show triggered notifications", {}, "low", False, False),
        # Memory
        ("remember_or_update", _handle_remember_or_update, "Remember or update user info. Use consistent memory_key (e.g. 'user.name', 'user.pref'). Same key auto-supersedes old.", {"content": "str", "memory_type": "str", "memory_key": "str"}, "low", True, False),
        ("forget_memory", _handle_forget_memory, "Forget a memory by ID or content keyword", {"memory_id": "str", "reason": "str"}, "low", True, False),
        ("run_memory_maintenance", _handle_run_memory_maintenance, "Scan and report memory health", {}, "low", False, False),
        ("search_memory", _handle_search_memory, "Search current memories", {"query": "str"}, "low", False, False),
        # File
        ("safe_delete", _handle_safe_delete, "Safely delete a file within allowed scopes", {"path": "str", "scope_id": "str", "mode": "str"}, "high", True, True),
        ("read_text_file", _handle_read_text_file, "Read a text file within ~/Documents (max 50 lines)", {"path": "str", "max_lines": "int"}, "low", False, False),
        # Knowledge
        ("ingest_document", _handle_ingest_document, "Ingest a document into knowledge base", {"file_path": "str"}, "low", True, False),
        ("search_knowledge", _handle_search_knowledge, "Search ingested documents", {"query": "str", "limit": "int"}, "low", False, False),
        # MCP
        ("search_mcp", _handle_search_mcp, "Search MCP candidates by goal", {"goal": "str"}, "low", False, False),
        ("install_mcp_sandbox", _handle_install_mcp_sandbox, "Install MCP to sandbox", {"candidate_name": "str"}, "medium", True, False),
        # Self-Dev
        ("create_selfdev_plan", _handle_create_selfdev_plan, "Generate a self-dev patch plan", {"goal": "str"}, "low", False, False),
        ("apply_patch_to_inactive_slot", _handle_apply_patch_to_inactive_slot, "Apply patch to inactive slot only", {}, "high", True, False),
        ("promote_slot", _handle_promote_slot, "Promote inactive slot to active after health check", {}, "high", True, False),
        ("rollback_slot", _handle_rollback_slot, "Rollback to previous active slot", {}, "high", True, False),
        # Rhythm
        ("query_rhythm", _handle_query_rhythm, "Get daily rhythm summary", {}, "low", False, False),
        ("query_attention", _handle_query_attention, "Get current attention state", {"thread_id": "str"}, "low", False, False),
    ]

    for cap_id, handler, desc, params, risk, writes_ext, can_del in tools:
        safety = _build_safety(
            cap_id, risk_level=risk,
            requires_confirmation=(risk == "high"),
            writes_external_world=writes_ext,
            can_delete=can_del,
        )
        registry.register(ToolRegistration(safety=safety, handler=handler, description=desc, parameters=params))
