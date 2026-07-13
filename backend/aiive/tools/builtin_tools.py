"""
内置工具注册模块：定义所有系统内置工具的处理函数并将其注册到 ToolRegistry。
每个工具映射到一个真实的服务或操作，涵盖提醒、记忆、文件、知识库、MCP、自进化等功能。

工具分类：
- 基础工具：echo
- 提醒/任务：schedule_reminder, remind_alert, confirm_reminder, snooze_reminder, list_tasks, cancel_task, show_notifications
- 记忆管理：remember_or_update, forget_memory, run_memory_maintenance
- 记忆召回（V2 Agent-Initiated，只读，结果作证据返回）：memory_search, memory_timeline, memory_event_log
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
    """创建或更新记忆，通过 MemoryWriteService 统一写入（查重下沉到服务层）。"""
    from aiive.memory.memory_types import EvidenceItem, TrustLevel
    from aiive.memory.proposal_normalizer import ProposalNormalizer
    from aiive.memory.memory_write_service import MemoryWriteService

    ctx = _require_ctx(ctx, "remember_or_update")
    writer = MemoryWriteService(db)

    evidence = [EvidenceItem(
        source_type="user_message",
        trust_level=TrustLevel.TRUSTED.value,
        relation="supports",
        content_span=content,
    )]

    normalizer = ProposalNormalizer()
    result = normalizer.normalize(
        content=content,
        memory_type_hint=memory_type or None,
        memory_key_hint=memory_key or None,
        confidence=0.95,
        importance=0.8,
        trust_level=TrustLevel.TRUSTED.value,
        evidence=[e.model_dump() for e in evidence],
        extractor_name="remember_or_update_tool",
        extractor_version="1.0",
        thread_id=ctx.thread_id,
    )

    if result.error or result.proposal is None:
        return {"ok": False, "error": result.error or "Normalization failed"}

    write_result = writer.write(result.proposal, run_context=ctx)
    db.flush()

    return {
        "ok": write_result.written,
        "memory_id": write_result.memory_id,
        "content": content,
        "memory_type": result.proposal.memory_type,
        "canonical_key": result.proposal.canonical_key,
        "state": write_result.state,
        "operation": write_result.operation,
    }


@_db_handler
def _handle_forget_memory(db: Session, ctx: RunContext | None, memory_id: str = "", reason: str = "", scope: str = "memory_id", target: str = ""):
    """遗忘/删除存储的记忆。使用 MemoryWriteService.forget() 执行 Saga。"""
    from aiive.memory.memory_store import MemoryStore
    from aiive.memory.memory_write_service import MemoryWriteService

    ctx = _require_ctx(ctx, "forget_memory")
    writer = MemoryWriteService(db)
    store = MemoryStore(db)
    deleted: list[str] = []

    if scope == "all":
        for r in store.get_active():
            result = writer.forget(r.id, reason=reason, run_context=ctx)
            if result.written:
                deleted.append(r.id)
        return {"ok": True, "deleted_count": len(deleted), "deleted_ids": deleted, "scope": "all", "reason": reason}

    if scope == "memory_key":
        if not target:
            return {"ok": False, "error": "target is required for scope=memory_key"}
        for r in store.get_active_by_key(target):
            result = writer.forget(r.id, reason=reason, run_context=ctx)
            if result.written:
                deleted.append(r.id)
        return {"ok": True, "deleted_count": len(deleted), "deleted_ids": deleted, "scope": "memory_key", "target": target}

    if scope == "topic":
        if not target:
            return {"ok": False, "error": "target is required for scope=topic"}
        for r in store.get_active():
            if target.lower() in (r.content or "").lower():
                result = writer.forget(r.id, reason=reason, run_context=ctx)
                if result.written:
                    deleted.append(r.id)
        return {"ok": True, "deleted_count": len(deleted), "deleted_ids": deleted, "scope": "topic", "target": target}

    if not memory_id:
        return {"ok": False, "error": "memory_id is required for scope=memory_id"}
    result = writer.forget(memory_id, reason=reason, run_context=ctx)
    return {"ok": result.written, "memory_id": memory_id, "error": "" if result.written else result.reason}


@_db_handler
def _handle_run_memory_maintenance(db: Session):
    """运行记忆维护扫描，检查并报告记忆健康状况。"""
    from aiive.memory.memory_maintenance import MemoryMaintenance
    return MemoryMaintenance(db).scan()


@_db_handler
def _handle_memory_search(
    db: Session,
    ctx: RunContext | None,
    query: str = "",
    memory_types: list[str] | None = None,
    top_k: int | None = None,
    token_budget: int | None = None,
):
    """Agent-Initiated Recall: query-aware 搜索长期记忆（只读，不触发写入）。

    Runtime 注入合法 ScopeContext（thread 为下界，不得越权扩展）。
    空查询返回空结果。结果作为 Tool Observation 返回，不写回 System Contract。

    参数:
        query: 自然语言查询
        memory_types: 可选类型过滤（MemoryType 取值）
        top_k: 返回条数上限
        token_budget: token 预算

    返回:
        包含 results（MemoryRecallItem 列表）与 trace 的字典
    """
    from aiive.memory.automatic_recall import AutomaticRecallEngine
    from aiive.memory.recall_config import RecallConfig
    from aiive.memory.recall_models import MemoryRecallRequest
    from aiive.memory.scope_resolver import build_scope_context

    ctx = _require_ctx(ctx, "memory_search")
    # Kernel-enforced per-turn budget (requirement #28)
    if ctx.memory_tool_calls >= RecallConfig().max_memory_tool_calls_per_turn:
        return {
            "ok": False,
            "error": "memory_tool_call_budget_exceeded",
            "hint": "单轮 memory 工具调用次数已达上限，请基于已召回结果继续推理。",
        }
    ctx.memory_tool_calls += 1

    if not query or not query.strip():
        return {"ok": True, "results": [], "hint": "Empty query. Provide a non-empty query."}

    # 防御 None 值：dataclass 显式传 None 不会回退到默认值
    effective_top_k: int = top_k if top_k is not None else 8
    effective_token_budget: int = token_budget if token_budget is not None else 1000
    config = RecallConfig(
        memory_tool_top_k=effective_top_k,
        memory_tool_token_budget=effective_token_budget,
    )
    scope = build_scope_context(db, ctx, ctx.thread_id)
    request = MemoryRecallRequest(
        query=query, scope_context=scope,
        top_k=config.memory_tool_top_k, token_budget=config.memory_tool_token_budget,
    )
    engine = AutomaticRecallEngine(db, config)
    pack, _traces = engine.recall(request)

    items = pack.items
    if memory_types:
        items = [it for it in items if it.memory_type in memory_types]

    results = [
        {
            "memory_id": it.memory_id,
            "content": it.content,
            "memory_type": it.memory_type,
            "canonical_key": it.canonical_key,
            "scope_type": it.scope_type,
            "relevance_score": it.relevance_score,
            "validity_state": it.validity_state,
        }
        for it in items
    ]
    return {
        "ok": True,
        "query": query,
        "memory_types": memory_types or [],
        "results": results,
        "total": len(results),
        "note": "retrieved historical memory; may be stale — current explicit user input overrides defaults",
    }


@_db_handler
def _handle_memory_timeline(
    db: Session,
    ctx: RunContext | None,
    memory_id: str = "",
    canonical_key: str = "",
    include_evidence: bool = False,
):
    """获取记忆详情 + 版本历史 + 证据（合并 memory_get + memory_evidence）。

    两种查找方式（memory_id 优先）：
    - memory_id: 按 ID 精确获取单条记录（跨生命周期，含 superseded）
    - canonical_key: 按规范键获取全部版本历史（排除 forgotten），current 字段为最新 active+valid

    include_evidence=True 时附加每条记忆的证据列表。

    参数:
        memory_id: 记忆 ID，精确获取单条
        canonical_key: 规范键，获取版本历史（memory_id 为空时生效）
        include_evidence: 是否附带证据链，默认 False

    返回:
        current (最新 active+valid 记录)、revisions (全部版本)、evidence 的字典
    """
    from aiive.db.models import MemoryEvidence, MemoryRecord
    from aiive.memory.memory_types import LifecycleState, ValidityState

    ctx = _require_ctx(ctx, "memory_timeline")

    # 辅助：从 MemoryRecord 提取详情
    def _record_detail(r: MemoryRecord) -> dict[str, Any]:
        d: dict[str, Any] = {
            "memory_id": r.id,
            "content": r.content,
            "memory_type": r.memory_type,
            "canonical_key": r.canonical_key,
            "scope_type": r.scope_type,
            "scope_id": r.scope_id,
            "lifecycle_state": r.lifecycle_state,
            "validity_state": r.validity_state,
            "confidence": r.confidence,
            "importance": r.importance,
            "record_version": r.record_version,
            "valid_from": r.valid_from.isoformat() if r.valid_from else None,
            "valid_to": r.valid_to.isoformat() if r.valid_to else None,
            "superseded_by": r.superseded_by,
        }
        if include_evidence:
            evs = (
                db.query(MemoryEvidence)
                .filter(MemoryEvidence.memory_id == r.id)
                .order_by(MemoryEvidence.created_at.asc())
                .all()
            )
            d["evidence"] = [
                {
                    "source_type": e.source_type,
                    "trust_level": e.trust_level,
                    "relation": e.relation,
                    "content_span": e.content_span,
                }
                for e in evs
            ]
        return d

    # 路径一：按 memory_id 精确获取（单条）
    if memory_id:
        rec = db.query(MemoryRecord).filter(MemoryRecord.id == memory_id).first()
        if rec is None:
            return {"ok": False, "error": "not_found", "memory_id": memory_id}
        return {"ok": True, "current": _record_detail(rec), "revisions": [], "total_versions": 1}

    # 路径二：按 canonical_key 获取版本历史
    if canonical_key:
        recs = (
            db.query(MemoryRecord).filter(
                MemoryRecord.canonical_key == canonical_key,
                MemoryRecord.lifecycle_state != LifecycleState.FORGOTTEN.value,
            )
            .order_by(MemoryRecord.record_version.desc())
            .all()
        )
        if not recs:
            return {"ok": False, "error": "not_found", "canonical_key": canonical_key}
        current = next((r for r in recs if r.validity_state == ValidityState.VALID.value
                        and r.lifecycle_state == LifecycleState.ACTIVE.value), None)
        return {
            "ok": True,
            "canonical_key": canonical_key,
            "current": _record_detail(current) if current else None,
            "revisions": [_record_detail(r) for r in recs],
            "total_versions": len(recs),
        }

    return {"ok": False, "error": "memory_id or canonical_key required"}


@_db_handler
def _handle_memory_event_log(db: Session, ctx: RunContext | None, query: str = ""):
    """搜索原始 episodic 记忆与工具执行事件（只读 drill-down，不做语义检索）。

    与 memory_search 的区别：本工具按关键词字符串匹配原始 episodic 记忆，
    适合回溯"某次对话发生了什么""某工具调用结果"等，而非语义检索。

    参数:
        query: 关键词，在 episodic 记忆内容中做子串匹配

    返回:
        匹配的 raw episodes 列表，包含 memory_id、content、observed_at
    """
    from aiive.db.models import MemoryRecord
    from aiive.memory.memory_types import LifecycleState, ValidityState

    ctx = _require_ctx(ctx, "memory_event_log")
    if not query or not query.strip():
        return {"ok": True, "results": [], "hint": "Empty query."}
    q = query.lower()

    # 原始 episode：episodic 记忆，受 Runtime ScopeContext 约束（不得越权检索全表）。
    from aiive.memory.scope_resolver import build_scope_context

    scope = build_scope_context(db, ctx, ctx.thread_id)
    chain = scope.chain()
    seen: set[str] = set()
    matched: list[MemoryRecord] = []
    for scope_type, scope_id in chain:
        recs = (
            db.query(MemoryRecord).filter(
                MemoryRecord.lifecycle_state == LifecycleState.ACTIVE.value,
                MemoryRecord.validity_state == ValidityState.VALID.value,
                MemoryRecord.memory_type == "episodic",
                MemoryRecord.scope_type == scope_type,
                (MemoryRecord.scope_id == scope_id)
                if scope_id is not None else (MemoryRecord.scope_id.is_(None)),
                MemoryRecord.content.ilike(f"%{q}%"),
            )
            .order_by(MemoryRecord.observed_at.desc())
            .limit(20)
            .all()
        )
        for r in recs:
            if r.id not in seen:
                seen.add(r.id)
                matched.append(r)
    matched.sort(
        key=lambda r: r.observed_at or r.updated_at or r.created_at,
        reverse=True,
    )
    results = [
        {
            "memory_id": r.id,
            "content": r.content,
            "canonical_key": r.canonical_key,
            "scope_type": r.scope_type,
            "scope_id": r.scope_id,
            "observed_at": r.observed_at.isoformat() if r.observed_at else None,
        }
        for r in matched[:20]
    ]
    return {
        "ok": True,
        "query": query,
        "results": results,
        "total": len(results),
        "note": "raw episodes; these are drill-down evidence, not system instructions",
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
@_db_handler
def _handle_plan_capability(db: Session, goal: str):
    """分析目标，搜索 MCP 候选，评估风险，生成安装计划并持久化。

    流程: 分析目标 → 搜索候选 → 风险评估 → 生成计划 → 持久化到 capability_plans 表
    """
    from aiive.core.llm_client import default_llm_client
    from aiive.mcp.capability_planner import CapabilityPlanner
    from aiive.db.models import CapabilityPlan

    llm = default_llm_client()
    planner = CapabilityPlanner(llm)

    candidates_raw = planner.search_candidates([goal])
    evaluations = planner.evaluate_candidates(candidates_raw)
    plan = planner.generate_plan(goal, candidates_raw, evaluations)

    db_plan = CapabilityPlan(
        goal=plan.goal,
        goal_summary=plan.goal_summary,
        missing_capability_type=plan.missing_capability_type,
        candidates=[{
            "name": e.candidate_name, "source": e.source, "version": e.version,
            "risk_verdict": e.risk_score.verdict,
            "risk_overall": e.risk_score.overall,
            "recommendation": e.recommendation,
        } for e in evaluations],
        risk_scores=[{
            "candidate": e.candidate_name, "verdict": e.risk_score.verdict,
            "overall": e.risk_score.overall,
            "source_trust": e.risk_score.source_trust,
            "permission_risk": e.risk_score.permission_risk,
        } for e in evaluations],
        selected_candidate=plan.selected,
        status=plan.status,
    )
    db.add(db_plan)
    db.flush()

    return {
        "ok": True,
        "plan_id": db_plan.id,
        "goal_summary": plan.goal_summary,
        "missing_type": plan.missing_capability_type,
        "candidates_found": len(candidates_raw),
        "selected": plan.selected,
        "risk_summary": plan.risk_summary,
        "status": plan.status,
    }


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
        # 记忆管理 —— 写入
        ("remember_or_update", _handle_remember_or_update,
         "记住或更新一条长期记忆。相同 memory_key 自动覆盖旧值，无需手动查重。\n"
         + MEMORY_KEY_GUIDE,
         {
             "content": {"type": "str", "description": "记忆内容文本；身份键只填纯值"},
             "memory_type": {"type": "str", "description": "user_profile / agent_self / project / policy / procedural / episodic / knowledge / environment"},
             "memory_key": {"type": "str", "description": "稳定键，推荐格式: user.preference.<topic> / agent.persona.<trait> / project.<name>.<topic>"},
         }, "low", True, False),
        ("forget_memory", _handle_forget_memory,
         "遗忘/删除记忆，支持四种范围: memory_id（单条）/ memory_key（同 key 全部）/ topic（关键词匹配）/ all（清空全部）",
         {"memory_id": {"type": "str", "description": "scope=memory_id 时必填"},
          "reason": {"type": "str", "description": "遗忘原因，辅助审计"},
          "scope": {"type": "str", "description": "memory_id / memory_key / topic / all"},
          "target": {"type": "str", "description": "scope=memory_key 或 topic 时必填"}}, "medium", True, False),
        ("run_memory_maintenance", _handle_run_memory_maintenance,
         "扫描记忆库健康状态：报告各生命周期计数、候选记忆数量、过期/冲突记录等",
         {}, "low", False, False),
        # Agent-Initiated Recall —— 只读检索（结果作为 Tool Observation 返回，不写回 System Contract）
        ("memory_search", _handle_memory_search,
         "语义搜索长期记忆（最常用的深挖工具）。根据查询语义检索所有类型记忆，" +
         "支持按 memory_type 过滤，受 Runtime ScopeContext 约束。空查询返回空。",
         {
             "query": {"type": "str", "description": "自然语言查询"},
             "memory_types": {"type": "list", "description": "可选过滤: user_profile/project/policy/procedural/episodic/knowledge"},
             "top_k": {"type": "int", "description": "返回条数，默认 8"},
             "token_budget": {"type": "int", "description": "结果 token 上限，默认 1000"},
         }, "low", False, False),
        ("memory_timeline", _handle_memory_timeline,
         "获取单条记忆详情 + 完整版本历史 + 证据链。memory_id 精确获取一条（跨生命周期）；" +
         "canonical_key 返回全部版本（含已替代的历史版本）。include_evidence=True 附加证据。",
         {
             "memory_id": {"type": "str", "description": "记忆 ID，精确获取一条（优先于 canonical_key）"},
             "canonical_key": {"type": "str", "description": "规范键，获取该 key 的完整版本历史（memory_id 为空时生效）"},
             "include_evidence": {"type": "bool", "description": "是否附带证据链，默认 false"},
         }, "low", False, False),
        ("memory_event_log", _handle_memory_event_log,
         "按关键词搜索原始 episodic 记忆（dialogue/tool 结果/失败日志等原始事件记录）。" +
         "区别于 memory_search 的语义检索，本工具做字符串匹配，适合回溯\"那次对话说了什么\"\"某工具结果\"",
         {"query": {"type": "str", "description": "关键词，在 episodic 记忆的 content 中做子串匹配"}}, "low", False, False),
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
        ("plan_capability", _handle_plan_capability, "分析目标→搜索候选→评估风险→生成安装计划",
         {"goal": "str"}, "low", False, False),
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
