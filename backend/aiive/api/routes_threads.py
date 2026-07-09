"""
API路由模块：会话（Thread）管理
- 提供会话上下文重置接口
- 提供会话历史消息查询接口
"""
import logging

from pydantic import BaseModel

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from aiive.db.base import get_db
from aiive.runtime.event_logger import EventLogger
from aiive.runtime.thread_state import ThreadState

app_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


class ResetRequest(BaseModel):
    """重置请求体"""
    thread_id: str | None = None


@router.post("/thread/reset")
def reset_thread(body: ResetRequest, db: Session = Depends(get_db)):
    """重置会话上下文

    记录 context_reset 事件并返回成功。前端收到响应后清除本地状态，
    下一次 /api/chat 调用将创建新的会话。

    Args:
        body: 包含可选 thread_id 的重置请求
        db: 数据库会话

    Returns:
        操作结果，ok 为 True 表示成功
    """
    try:
        event_logger = EventLogger(db)
        if body.thread_id:
            event_logger.log_event(
                trace_id=body.thread_id,
                thread_id=body.thread_id,
                event_type="context_reset",
                payload={"reason": "user_requested"},
            )
        db.commit()
        return {"ok": True}
    except Exception:
        app_logger.exception("重置会话上下文失败: thread_id=%s", body.thread_id)
        db.rollback()
        raise


@router.get("/threads/{thread_id}/messages")
def get_thread_messages(thread_id: str, db: Session = Depends(get_db)):
    """从后端事件中恢复会话消息，用于前端展示

    Args:
        thread_id: 会话ID
        db: 数据库会话

    Returns:
        消息列表，每条消息包含 role 和 content
    """
    try:
        ts = ThreadState(db)
        msgs = ts.get_recent_messages(thread_id, limit=200)
        return [
            {"role": m["role"], "content": m["content"]}
            for m in msgs
        ]
    except Exception:
        app_logger.exception("获取会话消息失败: thread_id=%s", thread_id)
        raise
