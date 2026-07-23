"""
API路由模块：注意力管理与节奏管理
- 提供注意力状态只读查询接口（状态写入统一由主流程 resolve_for_turn 负责）
- 提供每日/每周节奏摘要接口
"""
import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from aiive.db.base import get_db
from aiive.runtime.attention_manager import AttentionManager
from aiive.runtime.rhythm_manager import RhythmManager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


@router.get("/attention/current")
def get_attention(thread_id: str = Query(...), db: Session = Depends(get_db)):
    """获取当前会话的注意力状态

    Args:
        thread_id: 会话ID
        db: 数据库会话

    Returns:
        注意力决策信息，包含决策结果、建议、焦点主题和最近话题
    """
    try:
        mgr = AttentionManager(db)
        state = mgr.get_current(thread_id)
        if not state:
            return {
                "thread_id": thread_id,
                "decision": "continue",
                "suggestion": None,
                "focus_topic": None,
                "recent_topics": [],
            }
        return {
            "thread_id": state.thread_id,
            "decision": state.decision,
            "suggestion": state.suggestion,
            "focus_topic": state.focus_topic,
            "recent_topics": state.recent_topics,
        }
    except Exception:
        logger.exception("获取注意力状态失败: thread_id=%s", thread_id)
        raise


@router.get("/rhythm/daily")
def daily_rhythm(db: Session = Depends(get_db)):
    """获取每日节奏摘要

    Args:
        db: 数据库会话

    Returns:
        当日工作节奏摘要数据
    """
    try:
        mgr = RhythmManager(db)
        return mgr.daily_summary()
    except Exception:
        logger.exception("获取每日节奏摘要失败")
        raise


@router.get("/rhythm/weekly")
def weekly_rhythm(db: Session = Depends(get_db)):
    """获取每周节奏摘要

    Args:
        db: 数据库会话

    Returns:
        本周工作节奏摘要数据
    """
    try:
        mgr = RhythmManager(db)
        return mgr.weekly_summary()
    except Exception:
        logger.exception("获取每周节奏摘要失败")
        raise
