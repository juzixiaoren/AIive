"""
API路由模块：通知管理
- 提供统一的通知和提醒查询接口
- 从事件表中获取最近的通知和提醒
- 支持按类别筛选和删除通知
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from aiive.api.ws_manager import ws_manager
from aiive.db.base import get_db
from aiive.db.models import Event, OutboxJob, Task

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

# 通知事件状态，仅用于通知读模型筛选。
PENDING_STATUSES = ["pending", "alerting", "snoozed"]
# 已执行状态：已确认、已取消
DONE_STATUSES = ["confirmed", "cancelled"]
# Task 状态机中仍可能被取消的状态。
CANCELLABLE_TASK_STATUSES = frozenset({"pending", "dispatching"})


def count_pending_notifications(db: Session) -> int:
    """统计当前处于 pending 状态的通知数量。

    Args:
        db: 数据库会话

    Returns:
        pending 状态的通知数量
    """
    events = (
        db.query(Event)
        .filter(Event.event_type.in_(["notification_created", "reminder_created"]))
        .all()
    )
    return sum(
        1 for e in events
        if not (e.payload or {}).get("dismissed")
        and (e.payload or {}).get("status") in PENDING_STATUSES
    )


def broadcast_pending_count(db: Session) -> None:
    """计算并推送最新 pending 数量到全局通知通道，供前端角标即时更新。

    Args:
        db: 数据库会话
    """
    ws_manager.broadcast_notification_sync(count_pending_notifications(db))


@router.get("/notifications")
def list_notifications(
    category: str = "all",
    db: Session = Depends(get_db),
):
    """获取最近的通知和提醒（从事件表统一查询）

    Args:
        category: 筛选类别，默认 all 返回全部
        db: 数据库会话

    Returns:
        通知和提醒列表，按创建时间降序排列，最多50条
    """
    events = (
        db.query(Event)
        .filter(Event.event_type.in_(["notification_created", "reminder_created"]))
        .order_by(Event.created_at.desc())
        .limit(50)
        .all()
    )

    events = [e for e in events if not (e.payload or {}).get("dismissed")]
    if category == "pending":
        events = [e for e in events if (e.payload or {}).get("status") in PENDING_STATUSES]
    elif category == "done":
        events = [e for e in events if (e.payload or {}).get("status") in DONE_STATUSES]

    result = []
    for e in events:
        try:
            p = e.payload or {}
            result.append({
                "id": e.id,
                "title": p.get("title", p.get("content", "")),
                "message": p.get("message", p.get("content", "")),
                "event_type": e.event_type,
                "status": p.get("status", ""),
                "thread_id": e.thread_id or "",
                "created_at": e.created_at.isoformat() if e.created_at else "",
            })
        except Exception:
            logger.warning("通知条目解析失败，已跳过: event_id=%s", e.id, exc_info=True)
            continue
    return result


@router.delete("/notifications/{notification_id}")
def delete_notification(notification_id: str, db: Session = Depends(get_db)):
    """删除指定通知，并同步取消关联的定时任务，避免提醒再次弹出。

    Args:
        notification_id: 通知 ID（即 Event.id）
        db: 数据库会话

    Returns:
        ok 为 True 表示成功，False 表示通知不存在
    """
    event = (
        db.query(Event)
        .filter(
            Event.id == notification_id,
            Event.event_type.in_(["notification_created", "reminder_created"]),
        )
        .first()
    )
    if event is None:
        raise HTTPException(status_code=404, detail="通知不存在")

    payload = dict(event.payload or {})
    task_id = payload.get("task_id")
    payload["dismissed"] = True
    event.payload = payload

    task_cancelled = False
    if task_id:
        task = db.query(Task).filter(Task.id == str(task_id)).with_for_update().first()
        if task is not None and task.status in CANCELLABLE_TASK_STATUSES:
            task.status = "cancelled"
            task_cancelled = True
            db.query(OutboxJob).filter(
                OutboxJob.operation_id == f"reminder_delivery:{task.id}",
                OutboxJob.status == "pending",
            ).update({
                OutboxJob.status: "cancelled",
                OutboxJob.terminal_reason: "task_cancelled",
                OutboxJob.terminal_at: func.now(),
            }, synchronize_session=False)

        related = (
            db.query(Event)
            .filter(
                Event.event_type == "reminder_created",
                Event.payload["task_id"].as_string() == str(task_id),
            )
            .all()
        )
        for related_event in related:
            related_payload = dict(related_event.payload or {})
            related_payload["status"] = "cancelled"
            related_payload["dismissed"] = True
            related_event.payload = related_payload

    db.commit()
    broadcast_pending_count(db)
    return {"ok": True, "task_cancelled": task_cancelled}
