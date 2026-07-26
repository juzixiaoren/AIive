"""
API路由模块：通知管理
- 提供统一的通知和提醒查询接口
- 从事件表中获取最近的通知和提醒
- 支持按类别筛选和删除通知
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from aiive.api.ws_manager import ws_manager
from aiive.db.base import get_db
from aiive.db.models import Event
from aiive.runtime.task_manager import TaskManager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

# 通知事件状态，仅用于通知读模型筛选。
PENDING_STATUSES = ["pending", "alerting", "snoozed"]
# 已执行状态：已确认、已取消
DONE_STATUSES = ["confirmed", "cancelled"]

NOTIFICATION_EVENT_TYPES = ["notification_created", "reminder_created"]


def _not_dismissed_criterion():
    """SQL 侧「未被 dismissed」条件。

    payload 为 JSON 列；SQLAlchemy 的 JSON 路径操作在两种方言下均可下推：
    - PostgreSQL: CAST(payload ->> 'dismissed' AS BOOLEAN)（缺失键 → SQL NULL）
    - SQLite:     JSON_EXTRACT(payload, '$."dismissed"')（缺失键 → SQL NULL）
    注意 IS NULL 判断必须作用在 as_boolean() 的 CAST 结果上：直接对 JSON
    索引表达式 .is_(None) 在部分方言会按「JSON null 字面量」比较而非 SQL NULL，
    导致缺失键的行被漏掉。此处「键缺失(IS NULL) 或 显式为 false」视为未删除，
    与旧内存过滤 `not payload.get("dismissed")` 语义一致（写入侧只会写布尔值）。
    """
    dismissed = Event.payload["dismissed"].as_boolean()
    return or_(
        dismissed.is_(None),
        dismissed == False,  # noqa: E712
    )


def _status_criterion(statuses: list[str]):
    """SQL 侧 payload.status ∈ statuses 条件（兼容 PostgreSQL 与 SQLite）。"""
    return Event.payload["status"].as_string().in_(statuses)


def count_pending_notifications(db: Session) -> int:
    """统计当前处于 pending 状态的通知数量。

    过滤全部下推到 SQL（只做 COUNT，不再全表拉取到内存计数），
    随事件表增长保持稳定开销。

    Args:
        db: 数据库会话

    Returns:
        pending 状态的通知数量
    """
    return int(
        db.query(func.count(Event.id))
        .filter(
            Event.event_type.in_(NOTIFICATION_EVENT_TYPES),
            _not_dismissed_criterion(),
            _status_criterion(PENDING_STATUSES),
        )
        .scalar()
        or 0
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
    # dismissed / status 过滤下推到 SQL 后再 limit(50)：
    # 旧实现先 limit(50) 再内存过滤，一旦最近 50 条全被 dismissed，
    # 接口恒返回空列表（更早的有效通知永远取不到）。
    query = (
        db.query(Event)
        .filter(
            Event.event_type.in_(NOTIFICATION_EVENT_TYPES),
            _not_dismissed_criterion(),
        )
    )
    if category == "pending":
        query = query.filter(_status_criterion(PENDING_STATUSES))
    elif category == "done":
        query = query.filter(_status_criterion(DONE_STATUSES))
    events = query.order_by(Event.created_at.desc()).limit(50).all()

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
            Event.event_type.in_(NOTIFICATION_EVENT_TYPES),
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
        cancellation = TaskManager(db).cancel(str(task_id))
        task_cancelled = bool(cancellation.get("task_cancelled"))

    db.commit()
    broadcast_pending_count(db)
    return {"ok": True, "task_cancelled": task_cancelled}
