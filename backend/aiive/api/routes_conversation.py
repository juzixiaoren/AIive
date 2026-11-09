"""当前对话（单用户唯一长期 Thread）与执行状态/中断接口。

设计要点：
- 后端是 Thread ID 的唯一事实来源，客户端启动时读取 /conversation/current；
- 执行状态由线程级租约派生，与 Turn 开始/完成/取消的广播共用同一视图；
- 中断只登记持久化取消信号（cancel_requested_at），不在这里释放占用：
  真正停 LLM / 工具、标记 cancelled、原子释放租约由执行者完成收口。
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aiive.db.base import get_db
from aiive.db.models import TurnRecord
from aiive.runtime.conversation_state import (
    broadcast_execution_status,
    describe_execution_status,
    get_current_thread_id,
)
from aiive.runtime.thread_lease import OWNER_TURN, lease_holder

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


class InterruptRequest(BaseModel):
    """中断请求：必须绑定当前线程与客户端看到的 active turn_record_id。"""

    thread_id: str | None = Field(default=None)
    turn_record_id: str | None = Field(default=None)


def _conflict(code: str, message: str, status_code: int = 409) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
            "retryable": False,
            "trace_id": None,
            "retry_after_seconds": None,
            "status": status_code,
        },
    )


@router.get("/conversation/current")
def get_current_conversation(db: Session = Depends(get_db)) -> dict[str, str]:
    """获取或初始化唯一长期对话的 Thread ID（幂等）。"""
    return {"thread_id": get_current_thread_id(db)}


@router.get("/conversation/status")
def get_execution_status(
    thread_id: str | None = None,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """当前执行状态：idle / running（可中断）/ busy（后台占用，不可中断）。"""
    tid = thread_id or get_current_thread_id(db)
    return describe_execution_status(db, tid)


@router.post("/conversation/interrupt")
def interrupt_turn(
    body: InterruptRequest,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """请求中断当前前台 Turn。

    只允许取消“实际持有执行锁的前台 Turn”，并用客户端回传的 turn_record_id
    做旧请求防护：旧停止请求不会误伤后续新 Turn。取消信号落库后由执行者
    在安全边界收口，接口立即返回 cancelling。
    """
    tid = body.thread_id or get_current_thread_id(db)

    # 先按客户端回传的 turn_record_id 判定：旧停止请求要么得到终态回执，
    # 要么被拒绝，绝不能落到当前正在执行的新 Turn 上。
    if body.turn_record_id:
        target = db.get(TurnRecord, body.turn_record_id)
        if target is None or target.thread_id != tid:
            raise _conflict("stale_turn", "停止请求对应的 Turn 不属于当前对话")
        if target.status == "completed":
            return {
                "ok": True, "status": "completed", "thread_id": tid,
                "turn_record_id": target.id,
            }
        if target.status == "cancelled":
            return {
                "ok": True, "status": "cancelled", "thread_id": tid,
                "turn_record_id": target.id,
            }
        if target.status != "running":
            return {
                "ok": True, "status": "idle", "thread_id": tid,
                "turn_record_id": target.id,
            }

    holder = lease_holder(db, tid)
    if holder is None:
        return {"ok": True, "status": "idle", "thread_id": tid, "turn_record_id": None}

    if str(holder.get("owner_kind") or "") != OWNER_TURN:
        raise _conflict("not_cancellable", "后台任务正在占用对话，无法中断")

    turn = db.get(TurnRecord, str(holder.get("owner_id") or ""))
    if turn is None or turn.status != "running":
        return {"ok": True, "status": "idle", "thread_id": tid, "turn_record_id": None}
    if body.turn_record_id and body.turn_record_id != turn.id:
        raise _conflict("stale_turn", "该 Turn 已结束，停止请求已过期")
    if turn.cancel_requested_at is not None:
        return {
            "ok": True, "status": "cancelling", "thread_id": tid,
            "turn_record_id": turn.id,
        }

    now = datetime.now(UTC)
    affected = db.query(TurnRecord).filter(
        TurnRecord.id == turn.id,
        TurnRecord.status == "running",
        TurnRecord.cancel_requested_at.is_(None),
    ).update({
        TurnRecord.cancel_requested_at: now,
        TurnRecord.updated_at: now,
    }, synchronize_session=False)
    db.commit()
    if affected != 1:
        current = db.get(TurnRecord, turn.id)
        if current is not None and current.status == "completed":
            return {
                "ok": True, "status": "completed", "thread_id": tid,
                "turn_record_id": current.id,
            }
        if current is not None and current.status == "cancelled":
            return {
                "ok": True, "status": "cancelled", "thread_id": tid,
                "turn_record_id": current.id,
            }
        raise _conflict("turn_state_changed", "Turn 状态已变化，请刷新后重试")

    broadcast_execution_status(tid)
    logger.info("已登记中断请求: thread_id=%s turn_record_id=%s", tid, turn.id)
    return {
        "ok": True, "status": "cancelling", "thread_id": tid,
        "turn_record_id": turn.id,
    }
