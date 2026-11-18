"""Domain tool handlers (implementation) split from builtin_tools."""

from typing import override

from aiive.tools.base import DatabaseTool

import logging

from sqlalchemy import tuple_
from sqlalchemy.orm import Session

from aiive.context.run_context import RunContext, require_ctx

logger = logging.getLogger(__name__)

import uuid as _uuid
from datetime import datetime, timedelta, timezone


class ScheduleReminderTool(DatabaseTool):
    @override
    def _run(self, db: Session, ctx: RunContext | None, content: str, delay_minutes: int | None = None,
             at: str | None = None, timezone_name: str | None = None):
        """创建定时提醒及其通知收件箱事件。

        参数:
            content: 提醒内容
            delay_minutes: 延迟分钟数，默认 1 分钟

        返回:
            包含 reminder_set、task_id、content、delay_minutes 等字段的字典
        """
        from aiive.core.scheduling_time import schedule_instant, user_timezone
        from aiive.db.models import Event
        from aiive.runtime.task_manager import TaskManager

        ctx = require_ctx(ctx, "schedule_reminder")
        if at is not None and delay_minutes is not None:
            return {"ok": False, "error_type": "choose_absolute_or_relative_reminder"}
        now = datetime.now(timezone.utc)
        zone = timezone_name or user_timezone(ctx.user_timezone)
        if at is not None:
            next_check = schedule_instant(at, zone)
            if next_check <= now:
                return {"ok": False, "error_type": "reminder_time_has_passed"}
        else:
            delay_minutes = delay_minutes if delay_minutes is not None else 1
            next_check = now + timedelta(minutes=delay_minutes)
        task = TaskManager(db).create(
            task_type="reminder",
            title=content[:255],
            description=f"提醒时间 {next_check.isoformat()}",
            next_check_at=next_check,
        )
        task.thread_id = ctx.thread_id
        db.flush()
        event = Event(
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
        )
        db.add(event)
        db.flush()
        # 提醒创建后需要推送最新 pending 数量；广播由 operation_executor 在事务
        # commit 成功后统一执行（避免事务提交前广播造成幻影计数）。
        return {
            "reminder_set": True,
            "task_id": task.id,
            "reminder_id": event.id,
            "content": content,
            "delay_minutes": delay_minutes,
            "scheduled_at": next_check.isoformat(),
            "timezone": zone,
            "needs_notification_broadcast": True,
        }


class RemindAlertTool(DatabaseTool):
    @override
    def _run(self, db: Session, reminder_id: str):
        """LLM 在提醒到期时调用此工具，激活提醒警报。

        将事件状态标记为 alerting，返回操作信息供前端显示确认/延期按钮。

        参数:
            reminder_id: reminder_created 事件 ID

        返回:
            包含 ok、reminder_id、content、status 的字典
        """
        from aiive.db.models import Event, Task

        # Read identity first, then lock Task -> Event, matching cancellation. Never
        # hold an Event lock while waiting for the Task lock held by cancel().
        event = db.get(Event, reminder_id)
        if not event:
            return {"ok": False, "error": "Reminder not found"}
        if event.event_type != "reminder_created":
            return {"ok": False, "error": f"Not a reminder event (type={event.event_type})"}
        task_id = (event.payload or {}).get("task_id") or event.trace_id
        task = db.query(Task).filter(Task.id == task_id).populate_existing().with_for_update().one_or_none()
        if task is None or task.task_type != "reminder":
            return {"ok": False, "reminder_id": reminder_id, "error": "Reminder task not found"}
        event = db.query(Event).filter(Event.id == reminder_id).populate_existing().with_for_update().one_or_none()
        if event is None:
            return {"ok": False, "error": "Reminder not found"}

        payload = dict(event.payload or {})
        status = str(payload.get("status", ""))
        if task.status in {"cancelled", "failed"} or payload.get("dismissed") or status not in {"pending", "alerting"}:
            return {
                "ok": False, "reminder_id": reminder_id, "status": status,
                "task_status": task.status, "error": "Reminder no longer allows activation",
            }
        if status == "alerting":
            return {
                "ok": True, "reminder_id": reminder_id, "content": payload.get("content", ""),
                "status": status, "already_applied": True,
            }
        if task.status not in {"pending", "dispatching"}:
            return {
                "ok": False, "reminder_id": reminder_id, "status": status,
                "task_status": task.status, "error": "Reminder task no longer allows activation",
            }
        payload["status"] = "alerting"
        event.payload = payload
        db.flush()

        return {
            "ok": True,
            "reminder_id": reminder_id,
            "content": payload.get("content", ""),
            "status": "alerting",
        }


class ConfirmReminderTool(DatabaseTool):
    @override
    def _run(self, db: Session, reminder_id: str, ctx: RunContext | None = None):
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
        if ctx and ctx.thread_id != event.thread_id:
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


class SnoozeReminderTool(DatabaseTool):
    @override
    def _run(self, db: Session, ctx: RunContext | None, reminder_id: str, delay_minutes: int = 5):
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
        ctx = require_ctx(ctx, "snooze_reminder")
        if event.thread_id != ctx.thread_id:
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
            title=content[:255],
            status="pending",
            description=f"延时{delay_minutes}分钟 (原提醒: {reminder_id[:8]})",
            next_check_at=next_check,
            thread_id=event.thread_id,
        )
        db.add(task)
        db.flush()

        # 创建新的 pending 事件（thread_id 统一沿用原提醒事件的线程，与新 Task 一致）
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
                "scheduled_at": next_check.isoformat(),
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


class ListTasksTool(DatabaseTool):
    @override
    def _run(self, db: Session, ctx: RunContext | None, status: str = "", thread_id: str = ""):
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


class ListRemindersTool(DatabaseTool):
    def _run(self, db: Session, ctx: RunContext | None, status: str = "", cursor: str = "", limit: int = 25):
        from aiive.db.models import Event, Task

        thread_id = require_ctx(ctx, "list_reminders").thread_id
        query = db.query(Task).filter_by(thread_id=thread_id, task_type="reminder")
        if status:
            query = query.filter_by(status=status)
        if cursor:
            instant, separator, task_id = cursor.partition("|")
            if not separator or not task_id:
                raise ValueError("invalid_reminder_cursor")
            query = query.filter(tuple_(Task.created_at, Task.id) < (datetime.fromisoformat(instant), task_id))
        rows = query.order_by(Task.created_at.desc(), Task.id.desc()).limit(limit + 1).all()
        page = rows[:limit]
        events = db.query(Event).filter(
            Event.event_type == "reminder_created", Event.trace_id.in_([task.id for task in page]),
            Event.thread_id == thread_id,
        ).order_by(Event.created_at.asc()).all()
        by_task = {event.trace_id: event for event in events}
        return {
            "items": [
                {"task_id": task.id, "title": task.title, "status": task.status,
                 "scheduled_at": ((event.payload or {}).get("scheduled_at") if (event := by_task.get(task.id)) else None)
                 or (task.next_check_at.isoformat() if task.next_check_at else None),
                 "reminder_id": event.id if event else None,
                 "notification_status": (event.payload or {}).get("status") if event else None}
                for task in page
            ],
            "has_more": len(rows) > limit,
            "next_cursor": f"{page[-1].created_at.isoformat()}|{page[-1].id}" if len(rows) > limit else None,
        }


class CancelTaskTool(DatabaseTool):
    @override
    def _run(self, db: Session, task_id: str):
        """按 ID 取消任务，委托 TaskManager.cancel 的唯一取消规则。

        参数:
            task_id: 要取消的任务 ID

        返回:
            包含 cancelled 标志的字典
        """
        from aiive.runtime.task_manager import TaskManager
        result = TaskManager(db).cancel(task_id)
        return {
            **result,
            "cancelled": result["task_cancelled"],
            "events_updated": result["events_cancelled"] > 0,
        }


class CancelReminderTool(DatabaseTool):
    def _run(self, db: Session, ctx: RunContext | None, task_id: str):
        from aiive.db.models import Event, Task
        from aiive.runtime.task_manager import TaskManager

        thread_id = require_ctx(ctx, "cancel_reminder").thread_id
        task = db.query(Task).filter_by(id=task_id, task_type="reminder", thread_id=thread_id).with_for_update().first()
        if task is None:
            return {"ok": False, "error_type": "reminder_unavailable"}
        result = TaskManager(db).cancel(task_id)
        if task.status == "completed":
            event = db.query(Event).filter_by(thread_id=thread_id, event_type="reminder_created", trace_id=task_id).with_for_update().first()
            if event and (event.payload or {}).get("status") == "alerting":
                event.payload = {**event.payload, "status": "cancelled", "dismissed": True}
                return {"ok": True, "task_id": task_id, "status": "dismissed", "needs_notification_broadcast": True}
        return {**result, "needs_notification_broadcast": bool(result.get("changed"))}



class DismissNotificationsTool(DatabaseTool):
    @override
    def _run(self, db: Session, ctx: RunContext | None, scope: str = "thread"):
        """清除未执行的通知：将待提醒/提醒中/已延时的通知标记为 cancelled。

        已确认（confirmed）和已取消（cancelled）的通知不受影响，
        它们已属于"已执行"类别。

        参数:
            scope: "thread"（默认，仅当前线程）或 "all"（显式跨线程清除）

        返回:
            包含 dismissed_count 的字典
        """
        from aiive.db.models import Event, Task
        from aiive.runtime.task_manager import TaskManager
        pending_statuses = ["pending", "alerting", "snoozed"]
        query = db.query(Event).filter(
            Event.event_type.in_(["notification_created", "reminder_created"])
        )
        # 写操作默认限定当前线程（与 list_tasks 一致），scope="all" 显式跨线程
        if scope != "all":
            query = query.filter(Event.thread_id == require_ctx(ctx, "dismiss_notifications").thread_id)
        query = query.filter(Event.payload["status"].as_string().in_(pending_statuses))
        events = query.order_by(Event.id).all()
        related = {}
        for event in events:
            payload = event.payload or {}
            if not payload.get("goal_key") and not payload.get("agent_task_id"):
                task_id = payload.get("reminder_task_id") or payload.get("task_id")
                if task_id:
                    related[str(task_id)] = event.thread_id
        manager = TaskManager(db)
        for task_id, thread_id in sorted(related.items()):
            task = db.get(Task, task_id)
            if task and task.task_type == "reminder" and task.thread_id == thread_id:
                manager.cancel(task_id)
        count = 0
        for e in events:
            current = db.query(Event).filter_by(id=e.id).populate_existing().with_for_update().first()
            if current and (current.payload or {}).get("status") in pending_statuses:
                current.payload = {**current.payload, "status": "cancelled", "dismissed": True}
                count += 1
            elif current and (current.payload or {}).get("status") == "cancelled":
                count += 1
        return {"ok": True, "dismissed_count": count, "needs_notification_broadcast": bool(count)}


class ShowNotificationsTool(DatabaseTool):
    @override
    def _run(self, db: Session):
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
