"""
内置工具注册模块：定义所有系统内置工具的处理函数并将其注册到 ToolRegistry。
每个工具映射到一个真实的服务或操作，涵盖提醒、记忆、文件、知识库、MCP、自进化等功能。

工具分类：
- 基础工具：echo, get_current_time
- 提醒/任务：schedule_reminder, remind_alert, confirm_reminder, snooze_reminder, list_tasks, cancel_task, show_notifications
- 记忆管理：remember_or_update, forget, run_memory_maintenance
- 记忆召回（V2 Agent-Initiated，只读，结果作证据返回）：memory_search, memory_timeline, memory_event_log
- 文件操作：safe_delete, read_text_file
- 知识库：ingest_document, reindex_document, search_knowledge
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
from aiive.runtime.working_state import WorkingStateService
from typing import Any

logger = logging.getLogger(__name__)
from aiive.tools.registry import (
    CapabilitySafetySchema,
    ToolRegistration,
    ToolRegistry,
)
from aiive.tools.forget_tool import handle_forget, handle_forget_status


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
    # 不在此处预计算 descriptor_hash：交由 registry.register 统一补算，
    # 使指纹覆盖 description/parameters，让 descriptor_changed 防护对内置工具生效。
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

    wrapper._aiive_db_handler = fn
    wrapper._aiive_accepts_ctx = _accepts_ctx
    return wrapper


# ── Echo（回显）──
def _handle_echo(message: str = "") -> str:
    """回显工具：原样返回输入消息。"""
    return message


# ── 当前时间（只读，供绝对时间点换算 delay_minutes）──
def _handle_get_current_time() -> dict[str, Any]:
    """获取当前时间（UTC 与服务器本地时区）。

    用途：用户给的是绝对时间点（如“14点提醒我”），但 schedule_reminder 只接受
    delay_minutes（相对分钟数）。Agent 先调用本工具拿到“现在几点”，再自行换算出
    到目标时间还有多少分钟，传给 schedule_reminder，避免乱填 delay。

    返回:
        utc:            当前 UTC 时间（ISO 8601，含 +00:00）
        local:          当前服务器本地时间（ISO 8601，含时区偏移）
        timezone_name:  本地时区名（未知时用 UTC±HH:MM 表示）
        offset_seconds: 本地相对 UTC 的偏移秒数（东八区为 28800）
        unix_timestamp: 当前 Unix 时间戳（秒）
    """
    now_utc = datetime.now(timezone.utc)
    now_local = datetime.now(timezone.utc).astimezone()
    offset = now_local.utcoffset()
    offset_seconds = int(offset.total_seconds()) if offset is not None else 0
    if now_local.tzname():
        tz_name = now_local.tzname()
    else:
        sign = "+" if offset_seconds >= 0 else "-"
        abs_off = abs(offset_seconds)
        tz_name = f"UTC{sign}{abs_off // 3600:02d}:{(abs_off % 3600) // 60:02d}"
    return {
        "utc": now_utc.isoformat(),
        "local": now_local.isoformat(),
        "timezone_name": tz_name,
        "offset_seconds": offset_seconds,
        "unix_timestamp": int(now_utc.timestamp()),
    }


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
    """创建定时提醒及其通知收件箱事件。

    参数:
        content: 提醒内容
        delay_minutes: 延迟分钟数，默认 1 分钟

    返回:
        包含 reminder_set、task_id、content、delay_minutes 等字段的字典
    """
    from aiive.db.models import Event
    from aiive.runtime.task_manager import TaskManager

    ctx = _require_ctx(ctx, "schedule_reminder")
    next_check = datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)
    task = TaskManager(db).create(
        task_type="reminder",
        title=content,
        description=f"延迟{delay_minutes}分钟",
        next_check_at=next_check,
    )
    task.thread_id = ctx.thread_id
    db.flush()
    db.add(Event(
        trace_id=task.id,
        thread_id=ctx.thread_id,
        event_type="reminder_created",
        payload={
            "task_id": task.id,
            "title": content,
            "content": content,
            "status": "pending",
            "scheduled_at": next_check.isoformat(),
        },
    ))
    db.flush()
    # 提醒创建后需要推送最新 pending 数量；广播由 operation_executor 在事务
    # commit 成功后统一执行（避免事务提交前广播造成幻影计数）。
    return {
        "reminder_set": True,
        "task_id": task.id,
        "content": content,
        "delay_minutes": delay_minutes,
        "needs_notification_broadcast": True,
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
    event = db.query(Event).filter(Event.id == reminder_id).with_for_update().one_or_none()
    if not event:
        return {"ok": False, "error": "Reminder not found"}
    if event.event_type != "reminder_created":
        return {"ok": False, "error": f"Not a reminder event (type={event.event_type})"}

    payload = dict(event.payload or {})
    current_status = str(payload.get("status", ""))
    if current_status == "confirmed":
        return {"ok": True, "reminder_id": reminder_id, "content": payload.get("content", ""), "status": "confirmed", "already_applied": True}
    if current_status != "alerting":
        return {"ok": False, "error": f"Reminder status does not allow confirmation (status={current_status})"}
    payload["status"] = "confirmed"
    event.payload = payload
    db.flush()
    # 确认后 pending 数量减少；广播延迟到 commit 成功后统一执行
    return {"ok": True, "reminder_id": reminder_id, "content": payload.get("content", ""), "status": "confirmed", "needs_notification_broadcast": True}


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
    event = db.query(Event).filter(Event.id == reminder_id).with_for_update().one_or_none()
    if not event:
        return {"ok": False, "error": "Reminder not found"}
    if event.event_type != "reminder_created":
        return {"ok": False, "error": f"Not a reminder event (type={event.event_type})"}

    payload = dict(event.payload or {})
    content = payload.get("content", "提醒")
    current_status = str(payload.get("status", ""))
    if current_status == "snoozed":
        return {
            "ok": True,
            "snoozed_reminder_id": reminder_id,
            "new_reminder_id": payload.get("snoozed_to"),
            "content": content,
            "delay_minutes": payload.get("snooze_delay_minutes", delay_minutes),
            "already_applied": True,
        }
    if current_status != "alerting":
        return {"ok": False, "error": f"Reminder status does not allow snooze (status={current_status})"}

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

    # 创建新的 pending 事件（thread_id 统一沿用原提醒事件的线程，与新 Task 一致）
    ctx = _require_ctx(ctx, "snooze_reminder")
    new_event = Event(
        id=str(_uuid.uuid4()),
        trace_id=task.id,
        thread_id=event.thread_id,
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
    event.payload = {
        **payload,
        "snoozed_to": new_event.id,
        "snooze_delay_minutes": delay_minutes,
    }
    db.flush()
    # 延时后新增一条 pending 事件；广播延迟到 commit 成功后统一执行

    return {
        "ok": True,
        "snoozed_reminder_id": reminder_id,
        "new_reminder_id": new_event.id,
        "content": content,
        "delay_minutes": delay_minutes,
        "needs_notification_broadcast": True,
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

    # 同步取消关联的 reminder_created 事件（通知也会消失）。
    # 不用 limit(50) + 内存过滤（会漏掉较旧事件）；为避免 JSON 查询方言差异，
    # 按 event_type 全量查询后在 Python 侧按 payload.task_id 过滤。
    related_events = (
        db.query(Event)
        .filter(Event.event_type == "reminder_created")
        .all()
    )
    updated = 0
    for e in related_events:
        if (e.payload or {}).get("task_id") == task_id:
            p = dict(e.payload or {})
            p["status"] = "cancelled"
            e.payload = p
            updated += 1

    return {"cancelled": True, "task_id": task_id, "events_updated": updated > 0, "events_cancelled": updated}


@_db_handler
def _handle_dismiss_notifications(db: Session, ctx: RunContext | None, scope: str = "thread"):
    """清除未执行的通知：将待提醒/提醒中/已延时的通知标记为 cancelled。

    已确认（confirmed）和已取消（cancelled）的通知不受影响，
    它们已属于"已执行"类别。

    参数:
        scope: "thread"（默认，仅当前线程）或 "all"（显式跨线程清除）

    返回:
        包含 dismissed_count 的字典
    """
    from aiive.db.models import Event
    pending_statuses = ["pending", "alerting", "snoozed"]
    query = db.query(Event).filter(
        Event.event_type.in_(["notification_created", "reminder_created"])
    )
    # 写操作默认限定当前线程（与 list_tasks 一致），scope="all" 显式跨线程
    if scope != "all" and ctx is not None and ctx.thread_id:
        query = query.filter(Event.thread_id == ctx.thread_id)
    events = query.order_by(Event.created_at.desc()).limit(200).all()
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
def _handle_remember_or_update(db: Session, ctx: RunContext | None, content: str, memory_type: str = "fact", memory_key: str = "", keywords: list[str] | None = None):
    """创建或更新记忆，通过 MemoryWriteService 统一写入（查重下沉到服务层）。

    execution_mode = "user_required"：写入失败时本工具返回 error，Turn 不得声称成功。
    """
    from aiive.memory.memory_types import EvidenceItem, TrustLevel
    from aiive.memory.proposal_normalizer import ProposalNormalizer
    from aiive.memory.memory_write_service import MemoryWriteService

    ctx = _require_ctx(ctx, "remember_or_update")
    # Phase 2: user_required 失败语义 — 写入失败必须明确返回 error
    ctx.execution_mode = "user_required"
    writer = MemoryWriteService(db)

    # 幂等键来源：优先用真实 Event.id；流式/工具上下文缺失时退化为 trace_id，
    # 保证同轮次稳定、跨轮次不互相冲突（彻底避免固定键冲突）。
    source_event_ids = list(ctx.source_event_ids) or ([ctx.trace_id] if ctx.trace_id else [])

    evidence = [EvidenceItem(
        # source_event_id 用于 reinforce 防重（同一来源事件不重复强化）
        source_event_id=source_event_ids[0] if source_event_ids else None,
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
        source_event_ids=source_event_ids,
        extractor_name="remember_or_update_tool",
        extractor_version="1.0",
        thread_id=ctx.thread_id,
        keywords=keywords,
    )

    if result.error or result.proposal is None:
        return {"ok": False, "error": result.error or "Normalization failed", "memory_write_failed": True}

    # Phase 2: provenance — 真实 Event.id + TurnRecord PK
    result.proposal.source_turn_record_id = ctx.turn_record_id
    result.proposal.source_turn_id = ctx.turn_id
    result.proposal.source_event_ids = source_event_ids
    result.proposal.execution_mode = ctx.execution_mode
    # 关键：provenance 注入后重算幂等键，确保键包含真实来源与内容，彻底避免固定键冲突
    result.proposal.compute_request_idempotency()

    try:
        write_result = writer.write(result.proposal, run_context=ctx)
    except Exception as e:
        return {"ok": False, "error": str(e), "memory_write_failed": True}

    db.flush()

    return {
        "ok": write_result.written,
        "memory_id": write_result.memory_id,
        "content": content,
        "memory_type": result.proposal.memory_type,
        "canonical_key": result.proposal.canonical_key,
        "keywords": result.proposal.keywords,
        "state": write_result.state,
        "operation": write_result.operation,
    }


@_db_handler
def _handle_run_memory_maintenance(db: Session, ctx: RunContext | None):
    """事务化入队一次手工记忆维护，并返回入队前只读诊断快照。"""
    from aiive.memory.memory_maintenance import MemoryMaintenance
    from aiive.worker.scheduler_daemon import enqueue_maintenance_job

    run_ctx = _require_ctx(ctx, "run_memory_maintenance")
    scan = MemoryMaintenance(db).scan()
    bucket_source = run_ctx.turn_record_id or run_ctx.turn_id or run_ctx.trace_id
    maintenance_operation_id = enqueue_maintenance_job(
        db,
        window_bucket=f"manual-{bucket_source}",
        source_context={
            "thread_id": run_ctx.thread_id,
            "turn_id": run_ctx.turn_id,
            "trace_id": run_ctx.trace_id,
        },
    )
    return {
        "ok": True,
        "enqueued": maintenance_operation_id is not None,
        "maintenance_operation_id": maintenance_operation_id,
        "pre_enqueue_scan": scan,
    }


@_db_handler
def _handle_memory_search(
    db: Session,
    ctx: RunContext | None,
    query: str = "",
    memory_types: list[str] | None = None,
    top_k: int | None = None,
    token_budget: int | None = None,
    include_archived: bool = False,
):
    """Agent-Initiated Recall: query-aware 搜索长期记忆（只读，不触发写入）。

    内部统一走 UnifiedRetriever（mode=SEARCH, source_types=["memory_record"]）；
    不再直接调用 AutomaticRecallEngine。AutomaticRecallEngine 仅作为
    UnifiedRetriever 内部的 Memory route adapter。

    参数:
        query: 自然语言查询
        memory_types: 可选类型过滤（MemoryType 取值）
        top_k: 返回条数上限
        token_budget: token 预算

    返回:
        包含 results 的字典（兼容旧输出格式）
    """
    from aiive.db.models import MemoryRecord
    from aiive.memory.memory_policy import MemoryPolicyEngine, MemoryReadChannel
    from aiive.memory.recall_config import RecallConfig, RetrievalConfig
    from aiive.memory.scope_resolver import build_scope_context
    from aiive.retrieval.retrieval_types import RetrievalMode, RetrievalRequest
    from aiive.retrieval.unified_retriever import UnifiedRetriever

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

    rcfg = RetrievalConfig()
    effective_top_k: int = top_k if top_k is not None else rcfg.search_max_results
    effective_token_budget: int = token_budget if token_budget is not None else rcfg.search_token_budget

    scope = build_scope_context(db, ctx, ctx.thread_id)
    retriever = UnifiedRetriever(db, rcfg)
    result = retriever.retrieve(RetrievalRequest(
        query=query,
        mode=RetrievalMode.SEARCH,
        scope_context=scope,
        thread_id=ctx.thread_id,
        source_types=["memory_record"],
        include_sleeping=True,
        include_archived=include_archived,
        max_results=effective_top_k,
        token_budget=effective_token_budget,
    ))

    # 兼容旧输出格式：RetrievalHit → dict
    items = result.hits
    if memory_types:
        items = [h for h in items if h.provenance.get("memory_type", "") in memory_types]

    results = []
    touched_ids: list[str] = []
    policy = MemoryPolicyEngine()
    memory_ids = [str(h.memory_record_id or h.source_id) for h in items]
    sensitivities = {
        row.id: row.sensitivity
        for row in db.query(MemoryRecord).filter(MemoryRecord.id.in_(memory_ids)).all()
    }
    for h in items:
        memory_id = str(h.memory_record_id or h.source_id)
        results.append({
            "memory_id": memory_id,
            "content": policy.render_content(
                h.snippet, sensitivities.get(memory_id), MemoryReadChannel.TOOL,
            ),
            "memory_type": h.provenance.get("memory_type", ""),
            "canonical_key": h.canonical_key or h.title,
            "scope_type": h.scope_type,
            "relevance_score": h.lexical_score,
            "validity_state": "valid",
            "lifecycle_state": h.lifecycle_state,
        })
        # archived / forgotten 不 touch（不得影响生命周期、不得 wake）
        if h.lifecycle_state in ("archived", "forgotten"):
            continue
        mid = h.memory_record_id or h.source_id
        if mid:
            touched_ids.append(str(mid))

    # touch 实际返回的记忆（best-effort，排除 archived/forgotten）
    try:
        if touched_ids:
            from aiive.memory.memory_access_tracker import MemoryAccessTracker
            MemoryAccessTracker().touch(touched_ids)
    except Exception:
        logger.exception("AccessTracker touch（memory_search 返回）失败")

    return {
        "ok": True,
        "query": query,
        "memory_types": memory_types or [],
        "results": results,
        "total": len(results),
        "degraded": result.degraded,
        "note": "统一检索（经 UnifiedRetriever）；retrieved historical memory; may be stale — current explicit user input overrides defaults",
    }


@_db_handler
def _handle_history_search(
    db: Session,
    ctx: RunContext | None,
    query: str = "",
    deep: bool = False,
    top_k: int | None = None,
    token_budget: int | None = None,
    include_archived: bool = False,
):
    """统一检索：跨记忆 / 阶段摘要 / 检查点检索历史（只读，不触发写入）。

    Runtime 注入合法 ScopeContext（thread 为下界，不得越权扩展）。deep=True 时
    额外二阶段回溯原始 Turn/Event（raw）。空查询返回空。

    参数:
        query: 自然语言查询
        deep: 是否回溯原始历史（默认 False）
        top_k: 返回条数上限
        token_budget: token 预算上限

    返回:
        包含 results（统一 RetrievalHit 列表）的字典
    """
    from aiive.memory.recall_config import RetrievalConfig
    from aiive.memory.scope_resolver import build_scope_context
    from aiive.retrieval.retrieval_types import RetrievalMode, RetrievalRequest
    from aiive.retrieval.unified_retriever import UnifiedRetriever

    ctx = _require_ctx(ctx, "history_search")
    if not query or not query.strip():
        return {"ok": True, "results": [], "hint": "Empty query. Provide a non-empty query."}

    rcfg = RetrievalConfig()
    effective_top_k: int = top_k if top_k is not None else rcfg.search_max_results
    effective_budget: int = token_budget if token_budget is not None else rcfg.search_token_budget

    scope = build_scope_context(db, ctx, ctx.thread_id)
    mode = RetrievalMode.DEEP if deep else RetrievalMode.SEARCH
    req = RetrievalRequest(
        query=query,
        mode=mode,
        scope_context=scope,
        thread_id=ctx.thread_id,
        include_archived=include_archived,
        max_results=effective_top_k,
        token_budget=effective_budget,
    )
    retriever = UnifiedRetriever(db, rcfg)
    result = retriever.retrieve(req)

    results = [h.model_dump(exclude_none=True) for h in result.hits]
    return {
        "ok": True,
        "query": query,
        "deep": deep,
        "results": results,
        "total": len(results),
        "degraded": result.degraded,
        "note": "统一检索结果（含记忆/摘要/检查点/原始）；当前明确用户输入始终覆盖这些默认值",
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
    from aiive.memory.memory_policy import MemoryPolicyEngine, MemoryReadChannel
    from aiive.memory.memory_types import LifecycleState, ValidityState

    ctx = _require_ctx(ctx, "memory_timeline")

    policy = MemoryPolicyEngine()

    # 辅助：从 MemoryRecord 提取详情
    def _record_detail(r: MemoryRecord) -> dict[str, Any]:
        d: dict[str, Any] = {
            "memory_id": r.id,
            "content": policy.render_content(r.content, r.sensitivity, MemoryReadChannel.TOOL),
            "sensitivity": r.sensitivity or "normal",
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
    from aiive.memory.memory_policy import MemoryPolicyEngine, MemoryReadChannel
    from aiive.memory.memory_types import LifecycleState, ValidityState

    ctx = _require_ctx(ctx, "memory_event_log")
    policy = MemoryPolicyEngine()
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
            "content": policy.render_content(
                r.content, r.sensitivity, MemoryReadChannel.TOOL,
            ),
            "sensitivity": r.sensitivity or "normal",
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
    # 关键：必须带 ok 键。operation_executor 依赖 ok/allowed 判定失败，
    # 否则删除被拒绝时会被记成 committed 成功（审计假成功）。
    return {
        "ok": decision.allowed,
        "allowed": decision.allowed,
        "reason": decision.reason,
        "resolved_path": decision.resolved_path,
    }


def _handle_read_text_file(path: str, max_lines: int = 50):
    """读取 ~/Documents 目录下的文本文件（最多 max_lines 行）。

    只允许读取 ~/Documents 范围内的文件，拒绝其他路径。

    参数:
        path: 文件路径
        max_lines: 最大读取行数，默认 50

    返回:
        包含 lines 和 total_read 的字典，或包含 error 的错误字典
    """
    allowed_dir = os.path.realpath(os.path.expanduser("~/Documents"))
    real_path = os.path.realpath(path)
    # 用 normcase + commonpath 校验目录包含关系：
    # 避免裸 startswith 被兄弟目录（如 ~/Documents2）绕过，且兼容 Windows 大小写
    try:
        allowed_nc = os.path.normcase(allowed_dir)
        real_nc = os.path.normcase(real_path)
        within_allowed = os.path.commonpath([real_nc, allowed_nc]) == allowed_nc
    except ValueError:
        within_allowed = False
    if not within_allowed:
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
    """通过统一摄取服务持久化原文并导入知识库。"""
    from aiive.knowledge.ingestor import KnowledgeIngestor

    return KnowledgeIngestor(db).ingest(file_path)


@_db_handler
def _handle_reindex_document(db: Session, document_id: str):
    """从持久原文重新生成知识文档分块。"""
    from aiive.knowledge.ingestor import KnowledgeIngestor

    return KnowledgeIngestor(db).reindex(document_id)


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


def _handle_apply_patch_to_inactive_slot(operations: list[dict[str, Any]] | None = None):
    """将补丁应用到非活跃槽位（不影响当前运行版本）。

    参数:
        operations: 操作列表，每项包含 operation、target_file、content 等字段。
                    来自 create_selfdev_plan 的 plan.operations。
    """
    from aiive.selfdev.patch_executor import PatchExecutor
    return PatchExecutor().apply_to_inactive(operations or [])


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
def _handle_query_attention(db: Session, ctx: RunContext | None, thread_id: str = ""):
    """只读查询当前注意力状态与空闲评估，不写入新的 AttentionState。"""
    from aiive.runtime.attention_manager import AttentionManager
    effective_thread_id = thread_id or (ctx.thread_id if ctx else "")
    exclude_turn_id = ctx.turn_id if ctx else ""
    return AttentionManager(db).inspect(effective_thread_id, exclude_turn_id=exclude_turn_id)


# ── Working State（显式语义字段维护）──
@_db_handler
def _handle_update_working_state(
    db: Session, ctx: RunContext | None,
    field: str, operation: str = "set",
    payload: dict[str, Any] | None = None, idempotency_key: str = "",
):
    """显式维护 WorkingState 的语义字段。

    仅用于 current_objective / open_loops / active_constraints 三种语义字段。
    确定性字段（pending_approvals / artifact_refs / verified_tool_states /
    uncommitted_side_effects / running_tool_state）由 Turn 生命周期自动维护，
    不通过此工具修改。

    idempotency_key 用于防止同一更新被重复应用（崩溃重试场景）。
    """
    ctx = _require_ctx(ctx, "update_working_state")
    payload = payload or {}
    ws_service = WorkingStateService()
    ws = ws_service.get_or_create(db, ctx.thread_id)

    # 幂等：同一 idempotency_key 仅应用一次
    applied_keys = list(ws.applied_idempotency_keys or [])
    if idempotency_key and idempotency_key in applied_keys:
        return {"ok": True, "applied": False, "reason": "idempotent_skip", "field": field}

    if field == "current_objective":
        ws.current_objective = payload.get("value", "")
        # 统一版本簿记：current_objective 分支也 bump version/updated_at
        ws.version = (ws.version or 0) + 1
        ws.updated_at = datetime.now(timezone.utc)
    elif field in ("open_loops", "active_constraints"):
        # update_semantic_field 内部已 bump version/updated_at，此处不再重复
        ws_service.update_semantic_field(
            db, ctx.thread_id, field, operation, payload,
            ctx.turn_id or "", idempotency_key or "",
        )
    else:
        return {"ok": False, "error": f"unsupported field: {field}"}

    if idempotency_key:
        applied_keys.append(idempotency_key)
        # 滑动窗口上限：仅保留最近 200 个幂等键，防止无限增长
        if len(applied_keys) > 200:
            applied_keys = applied_keys[-200:]
        ws.applied_idempotency_keys = applied_keys

    return {"ok": True, "applied": True, "field": field}


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
        ("get_current_time", _handle_get_current_time,
         "获取当前时间（UTC 与本地时区）。用户给的是绝对时间点（如“14点提醒我”）时，"
         "Agent 先调用本工具拿到现在几点，再换算成 schedule_reminder 所需的 delay_minutes，避免乱填。",
         {}, "low", False, False),
        # 提醒 / 任务
        ("schedule_reminder", _handle_schedule_reminder,
         "创建定时提醒并写入 Task 表，由后台可靠投递。注意：本工具只接受相对延迟 delay_minutes。"
         "若用户给的是绝对时间点（如“14点提醒我”“下午3点做某事”），必须先调用 get_current_time 获取当前时间，"
         "由你自行换算出到目标时间还剩多少分钟，再传入 delay_minutes，切勿凭空乱填。",
         {"content": "str", "delay_minutes": {"type": "int", "description": "相对当前时间的延迟分钟数；用户给绝对时间时需先用 get_current_time 换算，默认 1"}}, "low", True, False),
        ("remind_alert", _handle_remind_alert, "激活到期提醒警报，前端显示确认/延期操作按钮",
         {"reminder_id": "str"}, "low", True, False),
        ("confirm_reminder", _handle_confirm_reminder, "确认提醒已完成",
         {"reminder_id": "str"}, "low", True, False),
        ("snooze_reminder", _handle_snooze_reminder, "延迟提醒 N 分钟，创建新的延时任务",
         {"reminder_id": "str", "delay_minutes": {"type": "int", "description": "默认 5"}}, "low", True, False),
        ("list_tasks", _handle_list_tasks, "列出所有任务/提醒，status='all'/'pending'/'completed'",
         {"status": {"type": "str", "description": "空或 all=全部; pending/completed 可选"}}, "low", False, False),
        ("cancel_task", _handle_cancel_task, "按 ID 取消任务",
         {"task_id": "str"}, "low", True, False),
        ("dismiss_notifications", _handle_dismiss_notifications,
         "清除未执行的通知（待提醒/提醒中/已延时），将其标记为已取消。已确认和已取消的不受影响。默认仅当前线程",
         {"scope": {"type": "str", "description": "thread(默认，仅当前线程) / all(跨线程清除)"}}, "low", True, False),
        ("show_notifications", _handle_show_notifications, "显示已触发的通知", {}, "low", False, False),
        # 记忆管理 —— 写入
        ("remember_or_update", _handle_remember_or_update,
         "记住或更新一条长期记忆。相同 memory_key 自动覆盖旧值，无需手动查重。\n"
         + MEMORY_KEY_GUIDE,
         {
             "content": {"type": "str", "description": "记忆内容文本；身份键只填纯值"},
             "memory_type": {"type": "str", "description": "user_profile / agent_self / project / policy / procedural / episodic / knowledge / environment"},
             "memory_key": {"type": "str", "description": "稳定键，推荐格式: user.preference.<topic> / agent.persona.<trait> / project.<name>.<topic>"},
             "keywords": {"type": "list", "description": "可选检索关键词（同义词/上位词）。用于词汇召回命中；例如记「喜欢霸王茶姬」附 ['奶茶','茶饮']，查询「想喝奶茶」即可召回。不写入 content 文本"},
         }, "low", True, False),
        # Phase 6A: 统一 forget 工具
        ("forget", handle_forget,
         "执行 Forget Saga — Phase A 立即屏蔽。长期记忆、原始聊天、派生摘要全部清理。\n"
         + "mode: everywhere(默认/忘记一切) / memory_only(仅删记忆保留聊天) / history_only(仅删聊天及派生)",
         {
             "mode": {"type": "str", "description": "memory_only / history_only / everywhere (默认 everywhere)"},
             "memory_ids": {"type": "list", "description": "memory_only/everywhere: 精确记忆 ID"},
             "turn_ids": {"type": "list", "description": "history_only/everywhere: 精确 Turn ID"},
             "event_ids": {"type": "list", "description": "history_only/everywhere: 精确 Event ID"},
             "thread_id": {"type": "str", "description": "history_only/everywhere: 整个 Thread"},
             "canonical_key": {"type": "str", "description": "memory_only/everywhere: 按内容键全删"},
             "reason": {"type": "str", "description": "遗忘原因"},
         }, "high", True, False),
        ("forget_status", handle_forget_status,
         "查询 forget Operation 的各阶段进度：shield/cascade/rebuild/purge/verify 的完成状态，不返回已删除内容。",
         {
             "operation_key": {"type": "str", "description": "forget 工具返回的 operation_key"},
         }, "low", False, False),
        ("run_memory_maintenance", _handle_run_memory_maintenance,
         "扫描记忆库健康状态并启动真实后台维护；返回入队前诊断快照，最终结果稍后更新",
         {}, "low", True, False),
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
        ("history_search", _handle_history_search,
         "统一检索历史（记忆/阶段摘要/检查点，可选回溯原始事件）。根据查询语义跨三类源检索，" +
         "deep=True 时二阶段回溯原始 Turn/Event。受 Runtime ScopeContext 约束。空查询返回空。",
         {
             "query": {"type": "str", "description": "自然语言查询"},
             "deep": {"type": "bool", "description": "是否回溯原始历史，默认 false"},
             "top_k": {"type": "int", "description": "返回条数，默认 20"},
             "token_budget": {"type": "int", "description": "结果 token 上限，默认 2000"},
         }, "low", False, False),
        # 文件操作
        ("safe_delete", _handle_safe_delete, "在允许范围内安全删除文件",
         {"path": "str",
          "scope_id": {"type": "str", "description": "默认 test_artifacts"},
          "mode": {"type": "str", "description": "trash / quarantine / hard_delete_for_test_only"}}, "high", True, True),
        ("read_text_file", _handle_read_text_file, "读取 ~/Documents 下的文本文件（最多 50 行）",
         {"path": {"type": "str", "description": "限 ~/Documents"}, "max_lines": {"type": "int", "description": "默认 50"}}, "low", False, False),
        # 知识库
        ("ingest_document", _handle_ingest_document, "导入文档到知识库并持久化原文",
         {"file_path": "str"}, "low", True, False),
        ("reindex_document", _handle_reindex_document, "从持久原文重新生成知识文档索引",
         {"document_id": "str"}, "low", True, False),
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
        ("apply_patch_to_inactive_slot", _handle_apply_patch_to_inactive_slot, "仅将补丁应用到非活跃槽位",
         {"operations": {"type": "list", "description": "补丁操作列表，来自 create_selfdev_plan 返回的 plan.operations"}}, "high", True, False),
        ("promote_slot", _handle_promote_slot, "健康检查通过后提升非活跃槽位为活跃", {}, "high", True, False),
        ("rollback_slot", _handle_rollback_slot, "回滚到上一个活跃槽位", {}, "high", True, False),
        # 节奏 / 注意力
        ("query_rhythm", _handle_query_rhythm, "获取每日节奏摘要", {}, "low", False, False),
        ("query_attention", _handle_query_attention, "只读查询当前注意力状态与空闲评估",
         {"thread_id": {"type": "str", "description": "可选, 默认当前线程"}}, "low", False, False),
        # Working State（仅显式语义字段）
        ("update_working_state", _handle_update_working_state,
        "显式维护 WorkingState 语义字段: current_objective（设置当前目标）/ open_loops / active_constraints。"
        + "确定性字段（pending_approvals/artifact_refs/verified_tool_states/uncommitted_side_effects/running_tool_state）由系统自动维护，不要通过此工具修改。",
         {
             "field": {"type": "str", "description": "current_objective / open_loops / active_constraints"},
             "operation": {"type": "str", "description": "add / remove / update；current_objective 忽略此参数"},
             "payload": {"type": "dict", "description": "字段内容。open_loops/active_constraints 单项需带 id；current_objective 为 {'value': '...'}"},
             "idempotency_key": {"type": "str", "description": "可选，重复提交保护"},
         }, "low", True, False),
    ]

    external_effect_modes = {
        "forget": "externally_reconcilable",
        "safe_delete": "non_repeatable_external",
        "apply_patch_to_inactive_slot": "non_repeatable_external",
        "promote_slot": "non_repeatable_external",
        "rollback_slot": "non_repeatable_external",
    }
    for cap_id, handler, desc, params, risk, writes_ext, can_del in tools:
        safety = _build_safety(
            cap_id, risk_level=risk,
            requires_confirmation=(risk == "high"),
            writes_external_world=writes_ext,
            can_delete=can_del,
            effect_mode=external_effect_modes.get(cap_id, "db_transactional"),
        )
        registry.register(ToolRegistration(safety=safety, handler=handler, description=desc, parameters=params))
