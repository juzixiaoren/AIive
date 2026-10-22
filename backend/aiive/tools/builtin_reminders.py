"""Domain tool handlers (implementation) split from builtin_tools."""

import logging

from sqlalchemy.orm import Session

from aiive.context.run_context import RunContext
from aiive.tools.builtin_tools import db_handler, require_ctx

logger = logging.getLogger(__name__)

import uuid as _uuid
from datetime import datetime, timedelta, timezone


@db_handler
def handle_schedule_reminder(db: Session, ctx: RunContext | None, content: str, delay_minutes: int = 1):
    """创建定时提醒及其通知收件箱事件。

    参数:
        content: 提醒内容
        delay_minutes: 延迟分钟数，默认 1 分钟

    返回:
        包含 reminder_set、task_id、content、delay_minutes 等字段的字典
    """
    from aiive.db.models import Event
    from aiive.runtime.task_manager import TaskManager

    ctx = require_ctx(ctx, "schedule_reminder")
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

@db_handler
def handle_remind_alert(db: Session, reminder_id: str):
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

@db_handler
def handle_confirm_reminder(db: Session, reminder_id: str):
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

@db_handler
def handle_snooze_reminder(db: Session, ctx: RunContext | None, reminder_id: str, delay_minutes: int = 5):
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
    ctx = require_ctx(ctx, "snooze_reminder")
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

@db_handler
def handle_list_tasks(db: Session, ctx: RunContext | None, status: str = "", thread_id: str = ""):
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

@db_handler
def handle_cancel_task(db: Session, task_id: str):
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

@db_handler
def handle_dismiss_notifications(db: Session, ctx: RunContext | None, scope: str = "thread"):
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

@db_handler
def handle_show_notifications(db: Session):
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
