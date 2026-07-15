"""Phase 1: EpochManager — 惰性 Epoch/Segment 创建与不可变 Turn 归属。

Phase 1 不自动密封 Epoch，也不生成 SegmentSummary/EpochCheckpoint。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from aiive.db.models import Epoch, Segment, TurnRecord, WorkingState

logger = logging.getLogger(__name__)


class SegmentNotSealableError(Exception):
    """Segment 因存在活跃状态而无法密封。"""


class EpochManager:
    """惰性 Epoch/Segment 生命周期管理。Phase 1 不使用 LLM 摘要。"""

    def ensure_epoch_and_segment(
        self, db: Session, thread_id: str, turn_sequence: int,
    ) -> tuple[Epoch, Segment]:
        """获取或创建 active Epoch 和 open Segment。

        必须在持有 Thread 行锁（FOR UPDATE）的短事务中调用。
        """
        epoch = db.query(Epoch).filter(
            Epoch.thread_id == thread_id,
            Epoch.status == "active",
        ).first()

        if epoch is None:
            epoch_no = 1
            last_epoch = (
                db.query(Epoch)
                .filter(Epoch.thread_id == thread_id)
                .order_by(Epoch.epoch_no.desc())
                .first()
            )
            if last_epoch:
                epoch_no = last_epoch.epoch_no + 1

            epoch = Epoch(
                thread_id=thread_id,
                epoch_no=epoch_no,
                status="active",
                start_turn_sequence=turn_sequence,
            )
            db.add(epoch)
            db.flush()

        segment = db.query(Segment).filter(
            Segment.epoch_id == epoch.id,
            Segment.status == "open",
        ).first()

        if segment is None:
            segment_no = 1
            last_seg = (
                db.query(Segment)
                .filter(Segment.epoch_id == epoch.id)
                .order_by(Segment.segment_no.desc())
                .first()
            )
            if last_seg:
                segment_no = last_seg.segment_no + 1
                # Turn range 无重叠检查
                if last_seg.end_turn_sequence is not None and turn_sequence <= last_seg.end_turn_sequence:
                    raise SegmentNotSealableError(
                        f"Turn {turn_sequence} 与已密封 Segment {last_seg.id} 重叠"
                    )

            segment = Segment(
                epoch_id=epoch.id,
                thread_id=thread_id,
                segment_no=segment_no,
                status="open",
                start_turn_sequence=turn_sequence,
            )
            db.add(segment)
            db.flush()

        return epoch, segment

    def check_sealable(self, db: Session, thread_id: str) -> list[str]:
        """返回当前 Segment 不可密封的原因列表。空列表 = 可密封。"""
        reasons: list[str] = []

        # 存在 running Turn
        running_count = (
            db.query(TurnRecord)
            .filter(
                TurnRecord.thread_id == thread_id,
                TurnRecord.status == "running",
            )
            .count()
        )
        if running_count > 0:
            reasons.append(f"running_turns={running_count}")

        # 存在 interrupted_unknown Turn
        interrupted_count = (
            db.query(TurnRecord)
            .filter(
                TurnRecord.thread_id == thread_id,
                TurnRecord.status == "interrupted_unknown",
            )
            .count()
        )
        if interrupted_count > 0:
            reasons.append(f"interrupted_turns={interrupted_count}")

        # WorkingState 阻塞项
        ws = db.query(WorkingState).filter(WorkingState.thread_id == thread_id).first()
        if ws:
            if ws.uncommitted_side_effects:
                reasons.append("uncommitted_side_effects")
            if ws.pending_approvals:
                reasons.append("pending_approvals")
            if ws.running_tool_state:
                reasons.append("running_tool_state")

        # 条件 6：当前 open Segment 内无新建 completed Turn → 禁止密封
        open_segment = (
            db.query(Segment)
            .filter(Segment.thread_id == thread_id, Segment.status == "open")
            .first()
        )
        if open_segment is not None:
            completed_in_segment = (
                db.query(TurnRecord)
                .filter(
                    TurnRecord.thread_id == thread_id,
                    TurnRecord.segment_id == open_segment.id,
                    TurnRecord.status == "completed",
                )
                .count()
            )
            if completed_in_segment == 0:
                reasons.append("no_completed_turn_in_segment")

        return reasons

    def mark_pending_seal(self, db: Session, thread_id: str, turn_sequence: int) -> None:
        """标记当前 open Segment 为待密封（非阻塞）。"""
        segment = (
            db.query(Segment)
            .filter(Segment.thread_id == thread_id, Segment.status == "open")
            .first()
        )
        if segment is None:
            return
        segment.pending_seal_at = datetime.now(timezone.utc)
        segment.sealed_by_turn = turn_sequence
        db.flush()

    def try_seal_segment(
        self, db: Session, thread_id: str, end_turn_sequence: int, source_hash: str,
    ) -> Segment:
        """密封当前 open Segment。受阻时抛出 SegmentNotSealableError。"""
        reasons = self.check_sealable(db, thread_id)
        if reasons:
            raise SegmentNotSealableError(f"无法密封: {', '.join(reasons)}")

        segment = (
            db.query(Segment)
            .filter(Segment.thread_id == thread_id, Segment.status == "open")
            .with_for_update()
            .first()
        )
        if segment is None:
            raise SegmentNotSealableError("未找到 open segment")

        segment.status = "sealed"
        segment.end_turn_sequence = end_turn_sequence
        segment.source_hash = source_hash
        segment.sealed_at = datetime.now(timezone.utc)
        db.flush()
        return segment
