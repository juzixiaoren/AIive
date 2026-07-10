"""
内置工具注册模块：定义所有系统内置工具的处理函数并将其注册到 ToolRegistry。
每个工具映射到一个真实的服务或操作，涵盖提醒、记忆、文件、知识库、MCP、自进化等功能。

工具分类：
- 基础工具：echo
- 提醒/任务：schedule_reminder, remind_alert, confirm_reminder, snooze_reminder, list_tasks, cancel_task, show_notifications
- 记忆管理：remember_or_update, forget_memory, run_memory_maintenance, search_memory, list_memories
- 文件操作：safe_delete, read_text_file
- 知识库：ingest_document, search_knowledge
- MCP 集成：search_mcp, install_mcp_sandbox
- 自进化：create_selfdev_plan, apply_patch_to_inactive_slot, promote_slot, rollback_slot
- 节奏/注意力：query_rhythm, query_attention
"""

import inspect
import logging
import os
import uuid as _uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from aiive.context.run_context import RunContext
from aiive.db.base import SessionLocal
from aiive.memory.memory_types import MEMORY_KEY_GUIDE
from typing import Any

logger = logging.getLogger(__name__)
from aiive.tools.registry import (
    CapabilitySafetySchema,
    ToolRegistration,
    ToolRegistry,
    compute_descriptor_hash,
)


def _build_safety(capability_id: str, **overrides: Any) -> CapabilitySafetySchema:
    """构建工具的安全配置 schema。

    使用默认安全配置作为基础，允许通过 overrides 覆盖特定字段。

    参数:
        capability_id: 工具能力标识符
        **overrides: 需要覆盖的安全字段（如 risk_level、writes_external_world 等）

    返回:
        构建好的 CapabilitySafetySchema 实例
    """
    base: dict[str, Any] = dict(
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
# 处理函数
# ═══════════════════════════════════════════════════════════════

def _db_handler(fn: Callable[..., Any]):
    """装饰器：为需要数据库会话的处理函数自动管理会话生命周期（开启/提交/关闭）。

    提取 ctx 命名参数，仅在被装饰函数声明 ctx 时传递给 fn(db, ctx, **params)。
    RunContext 通过 registry.execute() 注入，不经过 LLM schema。
    """
    _accepts_ctx = "ctx" in inspect.signature(fn).parameters

    def wrapper(ctx: RunContext | None = None, **params: Any):
        logger.info("[TRACE:_db_handler] ENTER fn=%s params=%s", fn.__name__, params)
        db = SessionLocal()
        try:
            if _accepts_ctx:
                result = fn(db, ctx, **params)
            else:
                result = fn(db, **params)
            db.commit()
            logger.info("[TRACE:_db_handler] COMMIT fn=%s result=%s", fn.__name__, result)
            return result
        except Exception:
            logger.exception("[TRACE:_db_handler] ROLLBACK fn=%s", fn.__name__)
            db.rollback()
            raise
        finally:
            db.close()
    return wrapper


# ── Echo（回显）──
def _handle_echo(message: str = "") -> str:
    """回显工具：原样返回输入消息。"""
    return message


# ── Reminder / Task（提醒与任务）──


def _require_ctx(ctx: RunContext | None, tool_name: str) -> RunContext:
    """side-effect 工具需要 RunContext，缺失时 fast-fail。"""
    if ctx is None:
        raise RuntimeError(
            f"工具 {tool_name} 需要 RunContext，但未传入。请确认 AgentGraph 已通过 build_langchain_tools 传入 RunContext。"
        )
    return ctx


@_db_handler
def _handle_schedule_reminder(db: Session, ctx: RunContext | None, content: str, delay_minutes: int = 1):
    """创建定时提醒，只负责写入 Task 记录。

    Event（reminder_created）由 AgentGraph._finalize() 在主 DB 会话中统一写入，
    避免工具独立会话与主会话之间的 FK 约束冲突。

    参数:
        content: 提醒内容
        delay_minutes: 延迟分钟数，默认 1 分钟

    返回:
        包含 reminder_set、task_id、content、delay_minutes 等字段的字典
    """
    from aiive.runtime.task_manager import TaskManager
    next_check = datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)
    task = TaskManager(db).create(
        task_type="reminder",
        title=content,
        description=f"延迟{delay_minutes}分钟",
        next_check_at=next_check,
    )
    ctx = _require_ctx(ctx, "schedule_reminder")
    task.thread_id = ctx.thread_id
    db.flush()
    return {
        "reminder_set": True,
        "task_id": task.id,
        "content": content,
        "delay_minutes": delay_minutes,
    }


@_db_handler
def _handle_remind_alert(db: Session, reminder_id: str):
    """LLM 在提醒到期时调用此工具，激活提醒警报。

    将事件状态标记为 alerting，返回操作信息供前端显示确认/延期按钮。

    参数:
        reminder_id: reminder_created 事件 ID

    返回:
        包含 ok、reminder_id、content、status 的字典
    """
    from aiive.db.models import Event
    event = db.get(Event, reminder_id)
    if not event:
        return {"ok": False, "error": "Reminder not found"}
    if event.event_type != "reminder_created":
        return {"ok": False, "error": f"Not a reminder event (type={event.event_type})"}

    payload = dict(event.payload or {})
    payload["status"] = "alerting"
    event.payload = payload
    db.flush()

    return {
        "ok": True,
        "reminder_id": reminder_id,
        "content": payload.get("content", ""),
        "status": "alerting",
    }


@_db_handler
def _handle_confirm_reminder(db: Session, reminder_id: str):
    """用户确认提醒完成，将状态标记为 confirmed。

    参数:
        reminder_id: reminder_created 事件 ID

    返回:
        包含 ok、reminder_id、content、status 的字典
    """
    from aiive.db.models import Event
    event = db.get(Event, reminder_id)
    if not event:
        return {"ok": False, "error": "Reminder not found"}

    payload = dict(event.payload or {})
    payload["status"] = "confirmed"
    event.payload = payload
    db.flush()
    return {"ok": True, "reminder_id": reminder_id, "content": payload.get("content", ""), "status": "confirmed"}


@_db_handler
def _handle_snooze_reminder(db: Session, ctx: RunContext | None, reminder_id: str, delay_minutes: int = 5):
    """用户延迟提醒，创建新的延时任务和事件。

    流程:
    1. 将当前提醒标记为 snoozed
    2. 创建新的 Task（延迟 delay_minutes 分钟后检查）
    3. 创建新的 reminder_created 事件

    参数:
        reminder_id: 要延期的提醒事件 ID
        delay_minutes: 延期分钟数，默认 5 分钟

    返回:
        包含 snoozed_reminder_id、new_reminder_id、content、delay_minutes 的字典
    """
    from aiive.db.models import Event, Task
    event = db.get(Event, reminder_id)
    if not event:
        return {"ok": False, "error": "Reminder not found"}

    payload = dict(event.payload or {})
    content = payload.get("content", "提醒")

    # 将当前事件标记为已延期
    payload["status"] = "snoozed"
    event.payload = payload
    db.flush()

    # 创建新的延时任务
    next_check = datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)
    task = Task(
        id=str(_uuid.uuid4()),
        task_type="reminder",
        title=content,
        status="pending",
        description=f"延时{delay_minutes}分钟 (原提醒: {reminder_id[:8]})",
        next_check_at=next_check,
        thread_id=event.thread_id,
    )
    db.add(task)
    db.flush()

    # 创建新的 pending 事件
    ctx = _require_ctx(ctx, "snooze_reminder")
    new_event = Event(
        id=str(_uuid.uuid4()),
        trace_id=task.id,
        thread_id=ctx.thread_id,
        event_type="reminder_created",
        payload={
            "task_id": task.id,
            "status": "pending",
            "content": content,
            "delay_minutes": delay_minutes,
            "snoozed_from": reminder_id,
        },
    )
    db.add(new_event)
    db.flush()

    return {
        "ok": True,
        "snoozed_reminder_id": reminder_id,
        "new_reminder_id": new_event.id,
        "content": content,
        "delay_minutes": delay_minutes,
    }


@_db_handler
def _handle_list_tasks(db: Session, ctx: RunContext | None, status: str = "", thread_id: str = ""):
    """列出所有任务/提醒。

    参数:
        status: 状态筛选，支持空/"all"/"全部"（不筛选）、"pending"、"completed"
        thread_id: 线程 ID，默认使用当前线程上下文

    返回:
        任务字典列表，每项包含 id、task_type、title、status、next_check_at
    """
    from aiive.runtime.task_manager import TaskManager
    # "all" / 空 / "全部" → 不施加状态过滤
    normalized = status.strip().lower() if status else ""
    effective = normalized if normalized not in ("", "all", "全部") else None
    # 默认限定为当前线程，确保用户只看到自己的提醒
    tid = thread_id or (ctx.thread_id if ctx else None)
    tasks = TaskManager(db).list_all(effective, tid or None)
    return [{"id": t.id, "task_type": t.task_type, "title": t.title, "status": t.status, "next_check_at": t.next_check_at.isoformat() if t.next_check_at else None} for t in tasks]


@_db_handler
def _handle_cancel_task(db: Session, task_id: str):
    """按 ID 取消一个任务，同时将关联的 reminder_created 事件标记为 cancelled。

    参数:
        task_id: 要取消的任务 ID

    返回:
        包含 cancelled 标志的字典
    """
    from aiive.db.models import Event, Task
    task = db.get(Task, task_id)
    if not task:
        return {"cancelled": False, "error": "not found"}

    task.status = "cancelled"

    # 同步取消关联的 reminder_created 事件（通知也会消失）
    related_events = (
        db.query(Event)
        .filter(Event.event_type == "reminder_created")
        .order_by(Event.created_at.desc())
        .limit(50)
        .all()
    )
    for e in related_events:
        if (e.payload or {}).get("task_id") == task_id:
            p = dict(e.payload or {})
            p["status"] = "cancelled"
            e.payload = p

    return {"cancelled": True, "task_id": task_id, "events_updated": True}


@_db_handler
def _handle_dismiss_notifications(db: Session):
    """清除未执行的通知：将待提醒/提醒中/已延时的通知标记为 cancelled。

    已确认（confirmed）和已取消（cancelled）的通知不受影响，
    它们已属于"已执行"类别。

    返回:
        包含 dismissed_count 的字典
    """
    from aiive.db.models import Event
    pending_statuses = ["pending", "alerting", "snoozed"]
    events = (
        db.query(Event)
        .filter(Event.event_type.in_(["notification_created", "reminder_created"]))
        .order_by(Event.created_at.desc())
        .limit(200)
        .all()
    )
    count = 0
    for e in events:
        status = (e.payload or {}).get("status", "")
        if status in pending_statuses:
            p = dict(e.payload or {})
            p["status"] = "cancelled"
            e.payload = p
            count += 1

    return {"ok": True, "dismissed_count": count}


@_db_handler
def _handle_show_notifications(db: Session):
    """显示已触发的通知（notification_created 和 reminder_created 事件）。

    返回:
        最近 20 条通知的列表，每项包含 id、event_type、title、message、status、created_at
    """
    from aiive.db.models import Event
    notifs = (
        db.query(Event)
        .filter(Event.event_type.in_(["notification_created", "reminder_created"]))
        .order_by(Event.created_at.desc())
        .limit(20)
        .all()
    )
    result = []
    for e in notifs:
        try:
            p = e.payload or {}
            result.append({
                "id": e.id, "event_type": e.event_type,
                "title": p.get("title", p.get("content", "")),
                "message": p.get("message", p.get("content", "")),
                "status": p.get("status", ""),
                "created_at": e.created_at.isoformat() if e.created_at else "",
            })
        except Exception:
            logger.warning("解析通知条目失败: event_id=%s", e.id, exc_info=True)
            continue
    return result


# ── Memory（记忆管理）──
@_db_handler
def _handle_remember_or_update(db: Session, ctx: RunContext | None, content: str, memory_type: str = "fact", memory_key: str = ""):
    """创建或更新记忆，通过 MemoryGate + MemoryWriteService 写入。

    流程:
    1. 如果提供了 memory_key，查找已有的同 key 记忆
    2. 通过 MemoryGate 决策（接受/拒绝/更新）
    3. 使用 MemoryWriteService 写入

    参数:
        content: 记忆内容
        memory_type: 记忆类型（fact / preference / steward_signal 等）
        memory_key: 记忆键（如 "user.name"），同 key 自动覆盖旧记忆

    返回:
        包含 ok、memory_id、content、memory_type、state 的字典
    """
    from aiive.memory.memory_gate import MemoryGate, MemoryGateInput
    from aiive.memory.memory_store import MemoryStore
    from aiive.memory.memory_write_service import MemoryWriteService

    store = MemoryStore(db)
    gate = MemoryGate()
    writer = MemoryWriteService(db)

    # 查找同 key 的已有记忆（用于更新而非重复创建）
    existing = None
    if memory_key:
        for old in store.get_active():
            if old.memory_key == memory_key:
                existing = {"id": old.id, "content": old.content, "memory_type": old.memory_type}
                break

    # 通过 Gate 决策
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

    # 通过 write service 写入
    ctx = _require_ctx(ctx, "remember_or_update")
    result = writer.write(decision, content, run_context=ctx)
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
def _handle_forget_memory(db: Session, memory_id: str = "", reason: str = "", scope: str = "memory_id", target: str = ""):
    """遗忘/删除存储的记忆。

    支持四种操作模式（由 scope 参数决定）:
    - memory_id（默认）: 按 ID 删除单条记录（使用 memory_id 参数）
    - memory_key: 删除所有匹配 memory_key 的活跃记录（使用 target 参数）
    - topic: 搜索并删除内容中包含 target 文本的记录
    - all: 清空所有活跃记忆（需要显式确认）

    参数:
        memory_id: 单条记忆 ID（scope=memory_id 时使用）
        reason: 删除原因
        scope: 操作范围（memory_id / memory_key / topic / all）
        target: 目标关键词（scope=memory_key 或 topic 时使用）

    返回:
        包含 ok、deleted_count、deleted_ids 等字段的字典
    """
    from aiive.memory.memory_maintenance import MemoryMaintenance
    from aiive.memory.memory_store import MemoryStore

    logger.info(
        "[TRACE:forget_memory] ENTER scope=%s memory_id=%s target=%s reason=%s",
        scope, memory_id[:16] if memory_id else "(empty)", target, reason,
    )

    maint = MemoryMaintenance(db)
    store = MemoryStore(db)
    deleted: list[str] = []

    if scope == "all":
        records = [r for r in store.get_active()]
        logger.info("[TRACE:forget_memory] scope=all: found %d active records", len(records))
        for r in records:
            result = maint.forget(r.id, reason)
            if result.get("ok"):
                deleted.append(r.id)
            else:
                logger.warning(
                    "[TRACE:forget_memory] scope=all: forget failed for %s: %s",
                    r.id[:16], result.get("error", "unknown"),
                )
        logger.info("[TRACE:forget_memory] scope=all: deleted %d/%d records", len(deleted), len(records))
        return {
            "ok": True,
            "deleted_count": len(deleted),
            "deleted_ids": deleted,
            "scope": "all",
            "reason": reason,
        }

    if scope == "memory_key":
        if not target:
            logger.warning("[TRACE:forget_memory] scope=memory_key: missing target")
            return {"ok": False, "error": "target is required for scope=memory_key"}
        records = [r for r in store.get_active() if r.memory_key == target]
        logger.info("[TRACE:forget_memory] scope=memory_key target=%s: found %d records", target, len(records))
        for r in records:
            result = maint.forget(r.id, reason)
            if result.get("ok"):
                deleted.append(r.id)
            else:
                logger.warning(
                    "[TRACE:forget_memory] scope=memory_key: forget failed for %s: %s",
                    r.id[:16], result.get("error", "unknown"),
                )
        return {
            "ok": True,
            "deleted_count": len(deleted),
            "deleted_ids": deleted,
            "scope": "memory_key",
            "target": target,
        }

    if scope == "topic":
        if not target:
            logger.warning("[TRACE:forget_memory] scope=topic: missing target")
            return {"ok": False, "error": "target is required for scope=topic"}
        records = [r for r in store.get_active() if target.lower() in r.content.lower()]
        logger.info("[TRACE:forget_memory] scope=topic target=%s: found %d records", target, len(records))
        for r in records:
            result = maint.forget(r.id, reason)
            if result.get("ok"):
                deleted.append(r.id)
            else:
                logger.warning(
                    "[TRACE:forget_memory] scope=topic: forget failed for %s: %s",
                    r.id[:16], result.get("error", "unknown"),
                )
        return {
            "ok": True,
            "deleted_count": len(deleted),
            "deleted_ids": deleted,
            "scope": "topic",
            "target": target,
        }

    # 默认：按 memory_id 删除
    if not memory_id:
        logger.warning("[TRACE:forget_memory] scope=memory_id: missing memory_id")
        return {"ok": False, "error": "memory_id is required for scope=memory_id (or use scope=all/topic/memory_key)"}
    result = maint.forget(memory_id, reason)
    logger.info("[TRACE:forget_memory] scope=memory_id: result=%s", result)
    return result


@_db_handler
def _handle_run_memory_maintenance(db: Session):
    """运行记忆维护扫描，检查并报告记忆健康状况。"""
    from aiive.memory.memory_maintenance import MemoryMaintenance
    return MemoryMaintenance(db).scan()


@_db_handler
def _handle_search_memory(db: Session, query: str = ""):
    """按内容文本搜索记忆。

    空查询返回空结果；使用 list_memories 可列出全部。

    参数:
        query: 搜索关键词（大小写不敏感）

    返回:
        包含 results 和 total 的字典，results 最多 20 条
    """
    from aiive.memory.memory_store import MemoryStore
    if not query or not query.strip():
        return {"ok": True, "results": [], "hint": "Empty query. Use list_memories to list all active memories."}
    # 搜索内容包含 query 的活跃记录（大小写不敏感）
    all_active = MemoryStore(db).get_active()
    q = query.lower()
    matched = [r for r in all_active if q in r.content.lower()]
    return {
        "ok": True,
        "results": [{"id": r.id, "content": r.content, "memory_type": r.memory_type, "lifecycle_state": r.lifecycle_state} for r in matched[:20]],
        "total": len(matched),
        "query": query,
    }


@_db_handler
def _handle_list_memories(db: Session):
    """列出所有活跃记忆（无搜索过滤）。

    返回:
        包含 results（最多 50 条）和 total 的字典
    """
    from aiive.memory.memory_store import MemoryStore
    records = MemoryStore(db).get_active()
    return {
        "ok": True,
        "results": [{"id": r.id, "content": r.content, "memory_type": r.memory_type, "lifecycle_state": r.lifecycle_state} for r in records[:50]],
        "total": len(records),
    }


# ── File / Safe Delete（文件与安全删除）──
def _handle_safe_delete(path: str, scope_id: str = "test_artifacts", mode: str = "trash"):
    """安全删除文件，仅允许删除已注册 scope 内的文件。

    参数:
        path: 要删除的文件路径
        scope_id: 范围标识符，默认 "test_artifacts"
        mode: 删除模式（trash / quarantine / hard_delete_for_test_only）

    返回:
        包含 allowed、reason、resolved_path 的字典
    """
    from aiive.tools.safe_delete import safe_delete as do_safe_delete
    decision = do_safe_delete(path, scope_id, mode)
    return {"allowed": decision.allowed, "reason": decision.reason, "resolved_path": decision.resolved_path}


def _handle_read_text_file(path: str, max_lines: int = 50):
    """读取 ~/Documents 目录下的文本文件（最多 max_lines 行）。

    只允许读取 ~/Documents 范围内的文件，拒绝其他路径。

    参数:
        path: 文件路径
        max_lines: 最大读取行数，默认 50

    返回:
        包含 lines 和 total_read 的字典，或包含 error 的错误字典
    """
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
        logger.warning("读取文本文件失败: path=%s, error=%s", real_path, e)
        return {"error": str(e)}


# ── Knowledge（知识库）──
@_db_handler
def _handle_ingest_document(db: Session, file_path: str):
    """将文档导入知识库，同时将原始内容保存到对象存储。

    参数:
        file_path: 文档文件路径

    返回:
        KnowledgeIngestor 的导入结果字典
    """
    from aiive.knowledge.ingestor import KnowledgeIngestor
    from aiive.storage.object_store import put_text
    result = KnowledgeIngestor(db).ingest(file_path)
    # 将原始文档保存到对象存储供后续检索
    if result.get("ok") and not result.get("duplicate"):
        try:
            content = open(file_path).read()
            put_text("raw-documents", os.path.basename(file_path), content)
        except Exception:
            logger.warning("对象存储写入失败: file_path=%s", file_path, exc_info=True)
    return result


@_db_handler
def _handle_search_knowledge(db: Session, query: str, limit: int = 5):
    """搜索已导入的文档知识。

    参数:
        query: 搜索查询
        limit: 返回结果数量上限，默认 5

    返回:
        search_chunks 的搜索结果
    """
    from aiive.knowledge.ingestor import search_chunks
    return search_chunks(db, query, limit)


# ── MCP 集成 ──
def _handle_search_mcp(goal: str = ""):
    """根据目标搜索匹配的 MCP 候选服务器。

    参数:
        goal: 用户目标描述文本

    返回:
        MCP 候选服务器信息列表，每项包含 name、source、version、description 等
    """
    from aiive.mcp.discovery import search_mcp_candidates
    candidates = search_mcp_candidates(goal)
    return [{"name": c.name, "source": c.source, "version": c.version, "description": c.description, "risk_notes": c.risk_notes, "declared_tools": c.declared_tools} for c in candidates]


@_db_handler
def _handle_install_mcp_sandbox(db: Session, candidate_name: str):
    """将 MCP 候选服务器安装到沙箱环境。

    流程:
    1. 搜索匹配的候选
    2. 调用 install_sandbox 安装到数据库

    参数:
        candidate_name: 候选服务器名称

    返回:
        安装结果字典
    """
    from aiive.mcp.discovery import search_mcp_candidates
    from aiive.mcp.installer import install_sandbox
    candidates = search_mcp_candidates(candidate_name)
    if not candidates:
        return {"ok": False, "error": "No candidate found"}
    c = candidates[0]
    return install_sandbox(db, c.name, c.package_ref, c.version, c.transport, c.declared_tools, {"name": c.name, "description": c.description})


# ── Self-Dev（自进化）──
def _handle_create_selfdev_plan(goal: str = ""):
    """生成自进化补丁计划。

    参数:
        goal: 改进目标描述

    返回:
        SelfDevPlanner 的计划结果
    """
    from aiive.core.llm_client import default_llm_client
    from aiive.selfdev.planner import SelfDevPlanner
    llm = default_llm_client()
    return SelfDevPlanner(llm).plan(goal)


def _handle_apply_patch_to_inactive_slot():
    """将补丁应用到非活跃槽位（不影响当前运行版本）。"""
    from aiive.selfdev.patch_executor import PatchExecutor
    return PatchExecutor().apply_to_inactive([])


def _handle_promote_slot():
    """健康检查通过后将非活跃槽位提升为活跃版本。"""
    from aiive.selfdev.promote_rollback import PromoteRollback
    return PromoteRollback().promote()


def _handle_rollback_slot():
    """回滚到上一个活跃槽位版本。

    自动检测当前活跃槽位（A/B），回退到另一个槽位。
    """
    from aiive.selfdev.promote_rollback import PromoteRollback
    pr = PromoteRollback()
    active = pr.get_active_slot()
    previous = "B" if active == "A" else "A"
    return pr.rollback(previous)


# ── Rhythm / Attention（节奏/注意力）──
@_db_handler
def _handle_query_rhythm(db: Session):
    """获取每日节奏摘要。"""
    from aiive.runtime.rhythm_manager import RhythmManager
    return RhythmManager(db).daily_summary()


@_db_handler
def _handle_query_attention(db: Session, thread_id: str = ""):
    """获取当前注意力状态。"""
    from aiive.runtime.attention_manager import AttentionManager
    return AttentionManager(db).recompute(thread_id, "query")


# ═══════════════════════════════════════════════════════════════
# 注册
# ═══════════════════════════════════════════════════════════════

def register_builtin_tools(registry: ToolRegistry) -> None:
    """将所有内置工具注册到 ToolRegistry。

    工具元组格式: (cap_id, handler, description, params, risk_level, writes_external_world, can_delete)

    参数:
        registry: 目标 ToolRegistry 实例
    """
    tools = [
        # 基础工具
        ("echo", _handle_echo, "回显输入消息", {"message": "str"}, "low", False, False),
        # 提醒 / 任务
        ("schedule_reminder", _handle_schedule_reminder, "创建定时提醒，立即写入 events 表",
         {"content": "str", "delay_minutes": {"type": "int", "description": "默认 1"}}, "low", True, False),
        ("remind_alert", _handle_remind_alert, "激活到期提醒警报，前端显示确认/延期操作按钮",
         {"reminder_id": "str"}, "low", False, False),
        ("confirm_reminder", _handle_confirm_reminder, "确认提醒已完成",
         {"reminder_id": "str"}, "low", True, False),
        ("snooze_reminder", _handle_snooze_reminder, "延迟提醒 N 分钟，创建新的延时任务",
         {"reminder_id": "str", "delay_minutes": {"type": "int", "description": "默认 5"}}, "low", True, False),
        ("list_tasks", _handle_list_tasks, "列出所有任务/提醒，status='all'/'pending'/'completed'",
         {"status": {"type": "str", "description": "空或 all=全部; pending/completed 可选"}}, "low", False, False),
        ("cancel_task", _handle_cancel_task, "按 ID 取消任务",
         {"task_id": "str"}, "low", True, False),
        ("dismiss_notifications", _handle_dismiss_notifications, "清除未执行的通知（待提醒/提醒中/已延时），将其标记为已取消。已确认和已取消的不受影响", {}, "low", True, False),
        ("show_notifications", _handle_show_notifications, "显示已触发的通知", {}, "low", False, False),
        # 记忆管理
        ("remember_or_update", _handle_remember_or_update,
        "记住或更新用户信息（长期记忆）。用一致的 memory_key 写入，同 key 自动覆盖旧记忆。\n"
        + MEMORY_KEY_GUIDE,
         {
             "content": {"type": "str", "description": "身份键(user.name/user.display_name/agent.display_name/agent.persona.*)的 content 只填纯值"},
             "memory_type": {"type": "str", "description": "user_profile / agent_self / preference 等"},
             "memory_key": {"type": "str", "description": "稳定键: user.name/user.display_name/agent.display_name/agent.persona.relationship/user.preference.<topic>，同 key 自动覆盖"},
         }, "low", True, False),
        ("forget_memory", _handle_forget_memory, "遗忘/删除记忆：按 ID、memory_key、主题，或使用 scope=all 清空全部",
         {"memory_id": {"type": "str", "description": "scope=memory_id 时必填"},
          "reason": {"type": "str", "description": "可选"},
          "scope": {"type": "str", "description": "memory_id / memory_key / topic / all"},
          "target": {"type": "str", "description": "scope=memory_key 或 topic 时必填"}}, "medium", True, False),
        ("run_memory_maintenance", _handle_run_memory_maintenance, "扫描并报告记忆健康状况", {}, "low", False, False),
        ("search_memory", _handle_search_memory, "按内容文本搜索记忆，需要非空查询词",
         {"query": {"type": "str", "description": "非空"}}, "low", False, False),
        ("list_memories", _handle_list_memories, "列出所有活跃记忆（无过滤）", {}, "low", False, False),
        # 文件操作
        ("safe_delete", _handle_safe_delete, "在允许范围内安全删除文件",
         {"path": "str",
          "scope_id": {"type": "str", "description": "默认 test_artifacts"},
          "mode": {"type": "str", "description": "trash / quarantine / hard_delete_for_test_only"}}, "high", True, True),
        ("read_text_file", _handle_read_text_file, "读取 ~/Documents 下的文本文件（最多 50 行）",
         {"path": {"type": "str", "description": "限 ~/Documents"}, "max_lines": {"type": "int", "description": "默认 50"}}, "low", False, False),
        # 知识库
        ("ingest_document", _handle_ingest_document, "导入文档到知识库",
         {"file_path": "str"}, "low", True, False),
        ("search_knowledge", _handle_search_knowledge, "搜索已导入的文档",
         {"query": "str", "limit": {"type": "int", "description": "默认 5"}}, "low", False, False),
        # MCP 集成
        ("search_mcp", _handle_search_mcp, "按目标搜索 MCP 候选服务器",
         {"goal": "str"}, "low", False, False),
        ("install_mcp_sandbox", _handle_install_mcp_sandbox, "将 MCP 安装到沙箱",
         {"candidate_name": "str"}, "medium", True, False),
        # 自进化
        ("create_selfdev_plan", _handle_create_selfdev_plan, "生成自进化补丁计划",
         {"goal": "str"}, "low", False, False),
        ("apply_patch_to_inactive_slot", _handle_apply_patch_to_inactive_slot, "仅将补丁应用到非活跃槽位", {}, "high", True, False),
        ("promote_slot", _handle_promote_slot, "健康检查通过后提升非活跃槽位为活跃", {}, "high", True, False),
        ("rollback_slot", _handle_rollback_slot, "回滚到上一个活跃槽位", {}, "high", True, False),
        # 节奏 / 注意力
        ("query_rhythm", _handle_query_rhythm, "获取每日节奏摘要", {}, "low", False, False),
        ("query_attention", _handle_query_attention, "获取当前注意力状态",
         {"thread_id": {"type": "str", "description": "可选, 默认当前线程"}}, "low", False, False),
    ]

    for cap_id, handler, desc, params, risk, writes_ext, can_del in tools:
        safety = _build_safety(
            cap_id, risk_level=risk,
            requires_confirmation=(risk == "high"),
            writes_external_world=writes_ext,
            can_delete=can_del,
        )
        registry.register(ToolRegistration(safety=safety, handler=handler, description=desc, parameters=params))
