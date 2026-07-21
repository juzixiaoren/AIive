"""Phase 3: Epoch / Segment 显式密封 API。

提供：
- GET  /api/epochs/{thread_id}                  查询当前 Epoch 与 Segment 状态
- POST /api/epochs/{thread_id}/seal-segment     显式密封当前 open Segment
- POST /api/epochs/{thread_id}/rollover         显式触发 Epoch rollover

所有写操作复用 EpochManager，事务内原子；密封本体由 OutboxWorker 异步完成。
"""
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from aiive.api.developer_security import require_local_developer
from aiive.db.base import get_db
from aiive.db.models import Epoch, Segment
from aiive.runtime.epoch_manager import EpochManager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/epochs")


class EpochStatus(BaseModel):
    """Epoch 状态响应。"""

    thread_id: str
    active_epoch_id: str | None = None
    active_epoch_no: int | None = None
    open_segment_id: str | None = None
    open_segment_status: str | None = None
    sealed_segment_count: int = 0


class SealResult(BaseModel):
    """密封/rollover 结果响应。"""

    ok: bool
    stale: bool = False
    reason: str = ""
    segment_id: str | None = None
    compaction_input_id: str | None = None
    successor_segment_id: str | None = None
    new_epoch_id: str | None = None
    new_segment_id: str | None = None


def _current_status(db: Session, thread_id: str) -> EpochStatus:
    epoch = db.query(Epoch).filter(
        Epoch.thread_id == thread_id, Epoch.status == "active",
    ).first()
    open_seg = None
    sealed_count = 0
    if epoch is not None:
        open_seg = db.query(Segment).filter(
            Segment.epoch_id == epoch.id, Segment.status == "open",
        ).first()
        sealed_count = db.query(Segment).filter(
            Segment.epoch_id == epoch.id, Segment.status == "sealed",
        ).count()
    return EpochStatus(
        thread_id=thread_id,
        active_epoch_id=epoch.id if epoch else None,
        active_epoch_no=epoch.epoch_no if epoch else None,
        open_segment_id=open_seg.id if open_seg else None,
        open_segment_status=open_seg.status if open_seg else None,
        sealed_segment_count=sealed_count,
    )


@router.get("/{thread_id}", dependencies=[Depends(require_local_developer)])
def get_epoch_status(thread_id: str, db: Session = Depends(get_db)) -> EpochStatus:
    """查询 Thread 当前 Epoch 与 Segment 状态。"""
    return _current_status(db, thread_id)


@router.post("/{thread_id}/seal-segment")
def seal_segment(thread_id: str, db: Session = Depends(get_db)) -> SealResult:
    """显式密封当前 open Segment（异步生成 Summary）。"""
    open_seg = (
        db.query(Segment)
        .filter(Segment.thread_id == thread_id, Segment.status == "open")
        .order_by(Segment.segment_no.desc())
        .first()
    )
    if open_seg is None:
        return SealResult(ok=False, reason="no_open_segment")
    mgr = EpochManager()
    result = mgr.begin_segment_sealing(db, thread_id, open_seg.id)
    db.commit()
    return SealResult(
        ok=not result.stale,
        stale=result.stale,
        reason=result.reason,
        segment_id=result.segment_id,
        compaction_input_id=result.compaction_input_id,
        successor_segment_id=result.successor_segment_id,
    )


@router.post("/{thread_id}/rollover")
def rollover(thread_id: str, db: Session = Depends(get_db)) -> SealResult:
    """显式触发 Epoch rollover（冻结旧 Epoch 并创建新 Epoch）。"""
    mgr = EpochManager()
    result = mgr.begin_epoch_sealing(db, thread_id)
    db.commit()
    return SealResult(
        ok=not result.stale,
        stale=result.stale,
        reason=result.reason,
        new_epoch_id=result.new_epoch_id,
        new_segment_id=result.new_segment_id,
        segment_id=result.old_epoch_id or None,
    )
