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
from aiive.db.models import ApprovalRequest, Thread
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
        # 仅当 thread 已持久化时才记录事件。前端可能在会话尚未产生任何
        # 消息（thread 未落库）时就发起重置，此时无对应 threads 行，
        # 直接跳过事件记录以避免外键违规。重置未持久化的会话是幂等成功操作。
        if body.thread_id and db.get(Thread, body.thread_id) is not None:
            event_logger = EventLogger(db)
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
def get_thread_messages(
    thread_id: str,
    page_size: int = 50,
    before_sequence: int | None = None,
    db: Session = Depends(get_db),
):
    """按 Turn 游标分页恢复前端历史消息。

    该接口只负责 UI 展示，不参与基于 token 的 LLM 上下文组装。
    """
    try:
        page = ThreadState(db).list_thread_messages_page(
            thread_id=thread_id,
            page_size=page_size,
            before_sequence=before_sequence,
        )
        approvals = {
            approval.id: approval.status
            for approval in db.query(ApprovalRequest).filter(
                ApprovalRequest.thread_id == thread_id,
            ).all()
        }
        for message in page.get("messages", []):
            for card in message.get("action_cards", []):
                if card.get("card_type") != "approval_required":
                    continue
                approval_id = str(
                    card.get("resource_refs", {}).get("approval_id")
                    or card.get("payload_preview", {}).get("approval_id")
                    or ""
                )
                approval_status = approvals.get(approval_id)
                if approval_status and approval_status != "pending":
                    card["status"] = "completed" if approval_status in ("succeeded", "denied") else "failed"
                    card["actions"] = []
                    card.setdefault("payload_preview", {})["approval_status"] = approval_status
        return page
    except Exception:
        app_logger.exception("获取会话消息失败: thread_id=%s", thread_id)
        raise
