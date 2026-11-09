"""单用户唯一长期对话：当前 Thread 指针与执行状态视图。

后端是 Thread ID 的唯一事实来源：Web / Android / Desktop 启动时都读取同一行
ConversationState，不允许通过缺省 thread_id 隐式创建新的长期会话。

执行状态只有三种，直接由线程级租约（thread_execution_leases）派生：
- idle：无人持有占用；
- running：前台 Turn 持有占用（可中断）；
- busy：后台目标决策持有占用（不可中断，客户端显示处理中）。
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aiive.api.ws_manager import ws_manager
from aiive.db.base import SessionLocal
from aiive.db.models import ConversationState, Thread, TurnRecord
from aiive.runtime.thread_bootstrap import SYSTEM_THREAD_ID
from aiive.runtime.thread_lease import OWNER_TURN, lease_holder

logger = logging.getLogger(__name__)

CONVERSATION_STATE_ID = "default"
EXECUTION_STATUS_EVENT = "execution_status"

STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_BUSY = "busy"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _select_primary_thread(db: Session) -> Thread:
    """确定性地选择一个既有主 Thread，保留其他旧数据不合并、不删除。

    规则：排除系统线程；优先 TurnRecord 数最多者；数量相同按创建时间最早，
    再按 id 字典序兜底，保证多进程/多次调用得到同一个结果。
    库内没有任何用户线程时才创建新线程。
    """
    counts = (
        db.query(
            TurnRecord.thread_id.label("thread_id"),
            func.count(TurnRecord.id).label("turn_count"),
        )
        .group_by(TurnRecord.thread_id)
        .subquery()
    )
    row = (
        db.query(Thread, func.coalesce(counts.c.turn_count, 0).label("turn_count"))
        .outerjoin(counts, counts.c.thread_id == Thread.id)
        .filter(Thread.id != SYSTEM_THREAD_ID)
        .order_by(
            func.coalesce(counts.c.turn_count, 0).desc(),
            Thread.created_at.asc(),
            Thread.id.asc(),
        )
        .first()
    )
    if row is not None:
        return row[0]

    thread = Thread(id=str(uuid.uuid4()))
    db.add(thread)
    db.flush()
    logger.info("当前对话初始化：库内无可用 Thread，已创建 %s", thread.id)
    return thread


def _read_state(db: Session) -> ConversationState | None:
    return (
        db.query(ConversationState)
        .filter(ConversationState.id == CONVERSATION_STATE_ID)
        .with_for_update()
        .one_or_none()
    )


def get_current_thread_id(db: Session) -> str:
    """读取或初始化唯一 current_thread_id，返回已持久化的 Thread ID。

    幂等且并发安全：首次初始化与并发请求都可能同时插入同一主键，后者回滚后
    重读已提交的指针，保证所有客户端最终拿到同一个 Thread。
    """
    state = _read_state(db)
    if state is not None and db.get(Thread, state.current_thread_id) is not None:
        return state.current_thread_id

    thread = _select_primary_thread(db)
    if state is None:
        db.add(ConversationState(
            id=CONVERSATION_STATE_ID,
            current_thread_id=thread.id,
        ))
    else:
        # 指针指向的 Thread 已不存在（例如被外部清理）：重新选择并修正。
        state.current_thread_id = thread.id
        state.updated_at = _utcnow()
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = _read_state(db)
        if existing is not None:
            return existing.current_thread_id
        raise
    logger.info("当前对话 Thread: %s", thread.id)
    return thread.id


def ensure_current_thread() -> str:
    """启动时用独立短事务确保 ConversationState 已初始化。"""
    db = SessionLocal()
    try:
        return get_current_thread_id(db)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def describe_execution_status(db: Session, thread_id: str) -> dict[str, object]:
    """返回线程当前执行状态，供客户端初始化与断线重连后同步。"""
    payload: dict[str, object] = {
        "thread_id": thread_id,
        "state": STATE_IDLE,
        "cancellable": False,
        "active_turn": None,
        "owner_kind": None,
        "interrupting": False,
        "server_time": _iso(_utcnow()),
    }
    if not thread_id:
        return payload

    holder = lease_holder(db, thread_id)
    if holder is None:
        return payload

    owner_kind = str(holder.get("owner_kind") or "")
    owner_id = str(holder.get("owner_id") or "")
    if owner_kind == OWNER_TURN:
        turn = db.get(TurnRecord, owner_id)
        if turn is None or turn.status != "running":
            # 租约行还在但 Turn 已收口（崩溃/收尾竞态）：对外仍视为空闲，
            # 真正的写入互斥由租约自身保证。
            return payload
        cancel_requested = turn.cancel_requested_at is not None
        payload.update({
            "state": STATE_RUNNING,
            "cancellable": True,
            "interrupting": cancel_requested,
            "active_turn": {
                "turn_record_id": turn.id,
                "turn_id": turn.turn_id,
                "started_at": _iso(turn.updated_at),
                "cancel_requested_at": _iso(turn.cancel_requested_at),
            },
        })
        return payload

    # 后台目标决策：不可中断，客户端只显示处理中，不允许提交新消息。
    payload.update({
        "state": STATE_BUSY,
        "cancellable": False,
        "owner_kind": owner_kind or "background",
        "active_turn": {
            "turn_record_id": None,
            "turn_id": None,
            "started_at": None,
            "cancel_requested_at": None,
        },
    })
    return payload


def broadcast_execution_status(thread_id: str) -> None:
    """在 Turn 开始/完成/取消后向该线程的 WebSocket 订阅者广播状态。

    后台线程（收尾、Outbox）调用；未绑定事件循环时静默跳过。
    """
    if not thread_id or not ws_manager.is_bound:
        return
    db = SessionLocal()
    try:
        payload = describe_execution_status(db, thread_id)
    except Exception:
        logger.exception("执行状态查询失败: thread_id=%s", thread_id)
        return
    finally:
        db.close()
    try:
        ws_manager.broadcast_to_thread_sync(thread_id, EXECUTION_STATUS_EVENT, payload)
    except Exception:
        # 状态广播是尽力而为：任何推送失败都不能影响 Turn 收口。
        logger.exception("执行状态广播失败: thread_id=%s", thread_id)
