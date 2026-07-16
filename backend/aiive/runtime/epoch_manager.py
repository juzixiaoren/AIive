"""Phase 1/3: EpochManager — 惰性 Epoch/Segment 创建与不可变 Turn 归属。

Phase 3 新增：
- begin_segment_sealing / begin_epoch_sealing（复用 freeze_segment_for_sealing）
- peek_next_turn_sequence / allocate_turn_sequence（只读不消费 vs 建 TurnRecord 时推进）
- 移除 try_seal_segment 直接 sealed 旁路（唯一合法 sealed 路径为 Handler Phase C）

唯一合法状态转换：
  open → sealing：仅 begin_segment_sealing
  sealing → sealed：仅 SegmentSummary Handler Phase C（同一事务）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from aiive.db.models import (
    Epoch,
    EpochCompactionInput,
    OutboxJob,
    Segment,
    Thread,
    TurnRecord,
    WorkingState,
)
from aiive.runtime.compaction import (
    build_working_state_snapshot,
    freeze_compaction_input,
    working_state_snapshot_hash,
)

logger = logging.getLogger(__name__)

# 阻断密封的非终态 Turn
NON_TERMINAL_TURN_STATUSES = {"not_started", "running", "interrupted_unknown"}


class SegmentNotSealableError(Exception):
    """Segment 因存在活跃状态而无法密封。"""


@dataclass
class SegmentSealingResult:
    """begin_segment_sealing 的结果。"""

    segment_id: str
    compaction_input_id: str | None = None
    outbox_job_id: str | None = None
    successor_segment_id: str | None = None
    stale: bool = False
    reason: str = ""


@dataclass
class EpochSealingResult:
    """begin_epoch_sealing 的结果。"""

    old_epoch_id: str
    new_epoch_id: str | None = None
    new_segment_id: str | None = None
    epoch_compaction_input_id: str | None = None
    outbox_job_id: str | None = None
    stale: bool = False
    reason: str = ""


def peek_next_turn_sequence(db: Session, thread_id: str) -> int:
    """Thread 行锁内只读，返回 max(TurnRecord.turn_sequence)+1，无副作用，不消费。"""
    max_seq = db.query(func.max(TurnRecord.turn_sequence)).filter(
        TurnRecord.thread_id == thread_id,
    ).scalar()
    return (max_seq or 0) + 1


def allocate_turn_sequence(db: Session, thread_id: str) -> int:
    """返回下一个 turn_sequence；消费发生在调用方创建 TurnRecord 落库时（max 自然推进）。"""
    return peek_next_turn_sequence(db, thread_id)


class EpochManager:
    """惰性 Epoch/Segment 生命周期管理。Phase 3 接入密封与 rollover。"""

    # =====================================================================
    # Phase 1: 惰性创建
    # =====================================================================

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

        # 存在 running / interrupted_unknown Turn
        non_terminal = (
            db.query(TurnRecord)
            .filter(
                TurnRecord.thread_id == thread_id,
                TurnRecord.status.in_(NON_TERMINAL_TURN_STATUSES),
            )
            .count()
        )
        if non_terminal > 0:
            reasons.append(f"non_terminal_turns={non_terminal}")

        # WorkingState 阻塞项
        ws = db.query(WorkingState).filter(WorkingState.thread_id == thread_id).first()
        if ws:
            if ws.uncommitted_side_effects:
                reasons.append("uncommitted_side_effects")
            if ws.pending_approvals:
                reasons.append("pending_approvals")
            if ws.running_tool_state:
                reasons.append("running_tool_state")

        # 空 Segment（turn_record_count == 0）禁止密封；
        # failed/cancelled/preempted 终态 Segment 视为非空，允许密封
        open_segment = (
            db.query(Segment)
            .filter(Segment.thread_id == thread_id, Segment.status == "open")
            .first()
        )
        if open_segment is not None:
            turn_record_count = (
                db.query(TurnRecord)
                .filter(TurnRecord.segment_id == open_segment.id)
                .count()
            )
            if turn_record_count == 0:
                reasons.append("empty_segment")

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

    # =====================================================================
    # Phase 3: 冻结（权威逻辑，普通 sealing 与 epoch rollover 共用）
    # =====================================================================

    def freeze_segment_for_sealing(
        self,
        db: Session,
        thread_id: str,
        segment: Segment,
        create_successor: bool,
        boundary_working_state: WorkingState | None,
    ) -> tuple[str, str, str | None]:
        """冻结一个 Segment 为 sealing 态。

        步骤：
          1. 非终态 Turn 检查
          2. range 固定（max turn_sequence）
          3. Turn/Event manifest（含 content_hash）
          4. CompactionInput 持久化 + source_hash
          5. Segment.status = "sealing" + range + source_hash
          6. segment_sealing OutboxJob（operation_id 幂等）
          7. if create_successor：创建 successor open Segment（peek 起点，只读不消费）

        返回 (segment_id, compaction_input_id, successor_segment_id)。
        调用方负责提交事务。
        """
        # 1. 非终态 Turn 检查
        non_terminal = (
            db.query(TurnRecord)
            .filter(
                TurnRecord.segment_id == segment.id,
                TurnRecord.status.in_(NON_TERMINAL_TURN_STATUSES),
            )
            .count()
        )
        if non_terminal > 0:
            raise SegmentNotSealableError(
                f"Segment {segment.id} 含 {non_terminal} 个非终态 Turn，无法密封"
            )

        # 2. range 固定
        end_seq = (
            db.query(func.max(TurnRecord.turn_sequence))
            .filter(TurnRecord.segment_id == segment.id)
            .scalar()
        ) or 0

        # 3-4. CompactionInput + source_hash
        ws = boundary_working_state or db.query(WorkingState).filter(
            WorkingState.thread_id == thread_id,
        ).first()
        ci, source_hash = freeze_compaction_input(
            db,
            segment_id=segment.id,
            thread_id=thread_id,
            epoch_id=segment.epoch_id,
            start_turn_sequence=segment.start_turn_sequence or 0,
            end_turn_sequence=end_seq,
            boundary_working_state=ws,
        )
        db.add(ci)
        db.flush()

        # 5. Segment → sealing
        segment.status = "sealing"
        segment.end_turn_sequence = end_seq
        segment.source_hash = source_hash

        # 6. segment_sealing OutboxJob
        operation_id = f"segment_sealing:{segment.id}:{source_hash}:{ci.summary_version}"
        job = OutboxJob(
            operation_id=operation_id,
            job_type="segment_sealing",
            status="pending",
            payload={
                "schema_version": 1,
                "segment_id": segment.id,
                "compaction_input_id": ci.id,
                "source_hash": source_hash,
                "summary_version": ci.summary_version,
            },
            max_retries=3,
        )
        db.add(job)
        db.flush()

        # 7. successor（普通 sealing 时创建；rollover 时由调用方另建 Epoch）
        successor_id: str | None = None
        if create_successor:
            next_seq = peek_next_turn_sequence(db, thread_id)
            last_seg = (
                db.query(Segment)
                .filter(Segment.epoch_id == segment.epoch_id)
                .order_by(Segment.segment_no.desc())
                .first()
            )
            seg_no = (last_seg.segment_no + 1) if last_seg else 1
            successor = Segment(
                epoch_id=segment.epoch_id,
                thread_id=thread_id,
                segment_no=seg_no,
                status="open",
                start_turn_sequence=next_seq,
            )
            db.add(successor)
            db.flush()
            successor_id = successor.id

        return segment.id, ci.id, successor_id

    # =====================================================================
    # Phase 3: 普通 Segment 密封
    # =====================================================================

    def begin_segment_sealing(
        self,
        db: Session,
        thread_id: str,
        expected_segment_id: str,
        expected_idle_cutoff: datetime | None = None,
    ) -> SegmentSealingResult:
        """开始密封指定 open Segment。

        必须接收 expected_segment_id；事务内若当前 open Segment 已变化则 stale/no-op。
        expected_idle_cutoff 提供时（Idle Scanner 场景）重新验证 Thread 仍空闲，
        用户重新活跃则返回 stale/no-op（J.3）。
        """
        thread = db.query(Thread).with_for_update().filter(Thread.id == thread_id).one()

        if expected_idle_cutoff is not None:
            last = thread.last_activity_at
            if last is None or last > expected_idle_cutoff:
                return SegmentSealingResult(
                    segment_id=expected_segment_id, stale=True, reason="thread_active",
                )

        segment = (
            db.query(Segment)
            .filter(Segment.id == expected_segment_id, Segment.status == "open")
            .with_for_update()
            .first()
        )
        if segment is None:
            return SegmentSealingResult(
                segment_id=expected_segment_id, stale=True, reason="segment_not_open_or_changed",
            )

        # 不可密封条件
        reasons = self.check_sealable(db, thread_id)
        if reasons:
            return SegmentSealingResult(
                segment_id=expected_segment_id, stale=True, reason=", ".join(reasons),
            )

        # 已存在另一个 sealing Segment
        other_sealing = (
            db.query(Segment)
            .filter(Segment.thread_id == thread_id, Segment.status == "sealing")
            .first()
        )
        if other_sealing is not None and other_sealing.id != segment.id:
            return SegmentSealingResult(
                segment_id=expected_segment_id, stale=True, reason="compaction_in_progress",
            )

        ws = db.query(WorkingState).filter(WorkingState.thread_id == thread_id).first()
        seg_id, ci_id, successor_id = self.freeze_segment_for_sealing(
            db, thread_id, segment, create_successor=True, boundary_working_state=ws,
        )
        db.commit()
        return SegmentSealingResult(
            segment_id=seg_id,
            compaction_input_id=ci_id,
            successor_segment_id=successor_id,
        )

    # =====================================================================
    # Phase 3: Epoch rollover
    # =====================================================================

    def begin_epoch_sealing(self, db: Session, thread_id: str) -> EpochSealingResult:
        """冻结当前 active Epoch 的 open Segment 并创建新 active Epoch + open Segment。

        复用 freeze_segment_for_sealing（create_successor=false）。
        """
        db.query(Thread).with_for_update().filter(Thread.id == thread_id).one()

        active_epoch = (
            db.query(Epoch)
            .filter(Epoch.thread_id == thread_id, Epoch.status == "active")
            .with_for_update()
            .first()
        )
        if active_epoch is None:
            return EpochSealingResult(old_epoch_id="", stale=True, reason="no_active_epoch")

        # 已存在 sealing Segment → 进行中
        other_sealing = (
            db.query(Segment)
            .filter(Segment.thread_id == thread_id, Segment.status == "sealing")
            .first()
        )
        if other_sealing is not None:
            return EpochSealingResult(
                old_epoch_id=active_epoch.id, stale=True, reason="compaction_in_progress",
            )

        ws = db.query(WorkingState).filter(WorkingState.thread_id == thread_id).first()
        open_seg = (
            db.query(Segment)
            .filter(Segment.epoch_id == active_epoch.id, Segment.status == "open")
            .with_for_update()
            .first()
        )

        # 冻结旧 Epoch 当前 open Segment（rollover 不复用 successor）
        if open_seg is not None:
            turn_record_count = (
                db.query(TurnRecord)
                .filter(TurnRecord.segment_id == open_seg.id)
                .count()
            )
            if turn_record_count == 0:
                # 空 Segment：直接关闭，不消耗 Turn sequence，不创建 Job
                db.delete(open_seg)
            else:
                self.freeze_segment_for_sealing(
                    db, thread_id, open_seg,
                    create_successor=False, boundary_working_state=ws,
                )

        # Epoch boundary WorkingState 快照
        snapshot = build_working_state_snapshot(ws) if ws else {}
        snapshot_hash = working_state_snapshot_hash(snapshot)
        boundary_turn_sequence = peek_next_turn_sequence(db, thread_id) - 1

        source_hashes = [
            s.source_hash
            for s in db.query(Segment).filter(
                Segment.epoch_id == active_epoch.id,
                Segment.status == "sealed",
                Segment.source_hash.isnot(None),
            ).all()
        ]

        eci = EpochCompactionInput(
            epoch_id=active_epoch.id,
            boundary_turn_sequence=boundary_turn_sequence,
            working_state_version=snapshot.get("version", 0) or 0,
            current_objective=snapshot.get("current_objective"),
            open_loops=snapshot.get("open_loops", []),
            active_constraints=snapshot.get("active_constraints", []),
            artifact_refs=snapshot.get("artifact_refs", []),
            verified_tool_states=snapshot.get("verified_tool_states", []),
            source_segment_ids=[
                s.id for s in db.query(Segment).filter(
                    Segment.epoch_id == active_epoch.id,
                ).all()
            ],
            source_hashes=source_hashes,
            snapshot_hash=snapshot_hash,
            checkpoint_version=1,
        )
        db.add(eci)
        db.flush()

        active_epoch.status = "sealing"

        # 新 active Epoch + open Segment（peek 起点，只读不消费）
        new_start = peek_next_turn_sequence(db, thread_id)
        new_epoch = Epoch(
            thread_id=thread_id,
            epoch_no=active_epoch.epoch_no + 1,
            status="active",
            start_turn_sequence=new_start,
        )
        db.add(new_epoch)
        db.flush()
        new_segment = Segment(
            epoch_id=new_epoch.id,
            thread_id=thread_id,
            segment_no=1,
            status="open",
            start_turn_sequence=new_start,
        )
        db.add(new_segment)
        db.flush()

        if ws is not None:
            ws.epoch_id = new_epoch.id

        db.commit()
        return EpochSealingResult(
            old_epoch_id=active_epoch.id,
            new_epoch_id=new_epoch.id,
            new_segment_id=new_segment.id,
            epoch_compaction_input_id=eci.id,
        )
