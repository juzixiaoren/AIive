"""
API路由模块：通知管理
- 提供统一的通知和提醒查询接口
- 从事件表中获取最近的通知和提醒
- 支持按类别筛选和删除通知
"""
import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from aiive.db.base import get_db
from aiive.db.models import Event

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

# 未执行状态：待提醒、提醒中、已延时
PENDING_STATUSES = ["pending", "alerting", "snoozed"]
# 已执行状态：已确认、已取消
DONE_STATUSES = ["confirmed", "cancelled"]


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
            continue
    return result


@router.delete("/notifications/{notification_id}")
def delete_notification(notification_id: str, db: Session = Depends(get_db)):
    """删除指定通知（从数据库完整删除 event 记录）。

    Args:
        notification_id: 通知 ID（即 Event.id）
        db: 数据库会话

    Returns:
        ok 为 True 表示成功，False 表示通知不存在
    """
    event = db.get(Event, notification_id)
    if event is None:
        return {"ok": False, "error": "通知不存在"}
    db.delete(event)
    db.commit()
    return {"ok": True}
