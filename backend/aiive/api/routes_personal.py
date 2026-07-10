"""
API路由模块：个人信号管理
- 提供个人偏好和个人特征的查询接口
- 从记忆记录中筛选出属于个人信号类型的条目
"""
import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from aiive.db.base import get_db
from aiive.db.models import MemoryRecord

# Personal signal types mapped to canonical: user_profile + keys with user.* patterns
_PERSONAL_SIGNAL_TYPES = frozenset({"user_profile", "agent_self"})

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


@router.get("/personal-signals")
def list_personal_signals(db: Session = Depends(get_db)):
    """获取个人信号列表

    从记忆记录中筛选出属于个人信号类型（如偏好、特征等）的条目。

    Args:
        db: 数据库会话

    Returns:
        个人信号列表，按更新时间降序排列，最多50条
    """
    try:
        records = (
            db.query(MemoryRecord)
            .filter(MemoryRecord.memory_type.in_(_PERSONAL_SIGNAL_TYPES))
            .order_by(MemoryRecord.updated_at.desc())
            .limit(50)
            .all()
        )
        return [
            {
                "id": r.id,
                "memory_type": r.memory_type,
                "content": r.content,
                "lifecycle_state": r.lifecycle_state,
                "confidence": r.confidence,
                "lineage": r.lineage,
                "created_at": r.created_at.isoformat(),
            }
            for r in records
        ]
    except Exception:
        logger.exception("获取个人信号列表失败")
        raise
