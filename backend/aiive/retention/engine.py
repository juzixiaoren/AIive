"""
Phase 6B 保留清理引擎。

职责：
- 接收 claimed OutboxJob，管理 RetentionCleanupRun 生命周期
- 按确定性顺序推进各 lane，使用 composite cursor 分批
- 每批执行前回源校验安全谓词
- 返回 HandlerResult（COMPLETED/CONTINUE/错误）给 OutboxWorker
- cutoff 使用 per-lane 配置（RetentionPolicyConfig.lane.retention_days / delete_days）
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from aiive.db.models import (
    CompactionInput,
    Epoch,
    EpochCheckpoint,
    EpochCompactionInput,
    MemoryMaintenanceInput,
    OutboxJob,
    RetrievalIndexEntry,
    RetrievalIndexGeneration,
    RetrievalIndexToken,
    Segment,
    SegmentSummary,
)
from aiive.db.forget_models import (
    ForgetAction,
    ForgetBatch,
    ForgetDependency,
    ForgetOperation,
    FORGET_CLEANUP_READY_STATUSES,
    ForgetStageRun,
    ForgetTarget,
)
from aiive.db.retention_models import RetentionCleanupBatch, RetentionCleanupRun
from aiive.retention.config import RETENTION_POLICY_V1, RetentionPolicyConfig
from aiive.retention.safety_predicates import is_forget_operation_clearable
from aiive.worker.outbox_dto import HandlerOutcome, HandlerResult

logger = logging.getLogger(__name__)

# lane 确定性的处理顺序
LANE_ORDER: tuple[str, ...] = (
    "outbox_job",
    "maintenance_input",
    "retrieval_generation",
    "segment_summary",
    "epoch_checkpoint",
    "compaction_input",
    "forget_action",
    "retention_batch",
    "retention_run",
)

DEFAULT_BATCH_SIZE = 500
DEFAULT_RETENTION_DAYS = 30


class RetentionEngine:
    """保留清理引擎：管理 Run/Batch 生命周期，驱动各 lane 清理。"""

    def __init__(
        self,
        db: Session,
        run: RetentionCleanupRun,
        policy: RetentionPolicyConfig,
    ) -> None:
        self._db: Session = db
        self._run: RetentionCleanupRun = run
        self._policy: RetentionPolicyConfig = policy

    # ------------------------------------------------------------------
    # Run 生命周期管理
    # ------------------------------------------------------------------

    @classmethod
    def start_or_resume(
        cls,
        db: Session,
        outbox_job_id: str,
        operation_id: str,
    ) -> RetentionCleanupRun:
        """启动或恢复一个 RetentionCleanupRun。

        若已存在同 operation_id 的 Run 则续跑，否则创建新 Run。
        cutoff_at 设为全局默认（各 lane 独立按 per-lane 配置计算 cutoff）。
        """
        existing = (
            db.query(RetentionCleanupRun)
            .filter(RetentionCleanupRun.operation_id == operation_id)
            .first()
        )
        if existing and existing.status != "done":
            existing.execution_token = operation_id
            existing.claim_count += 1
            return existing

        policy = RETENTION_POLICY_V1
        cutoff_at = datetime.now(timezone.utc) - timedelta(days=DEFAULT_RETENTION_DAYS)
        run = RetentionCleanupRun(
            outbox_job_id=outbox_job_id,
            operation_id=operation_id,
            policy_version=policy.policy_version,
            policy_snapshot=policy.to_snapshot(),
            status="running",
            cutoff_at=cutoff_at,
            current_lane=None,
        )
        db.add(run)
        return run

    # ------------------------------------------------------------------
    # 每 lane 独立的 cutoff 计算
    # ------------------------------------------------------------------

    def _lane_cutoff(self, lane: str, stage: str = "scrub") -> datetime:
        """计算指定 lane 的 cutoff 时间。

        stage: "scrub" 使用 retention_days，"delete" 使用 delete_days。
        """
        now = datetime.now(timezone.utc)
        lane_cfg = self._policy.lane_config(lane)
        if lane_cfg is None:
            return now - timedelta(days=DEFAULT_RETENTION_DAYS)

        days = lane_cfg.retention_days if stage == "scrub" else (lane_cfg.delete_days or 365)
        return now - timedelta(days=days)

    # ------------------------------------------------------------------
    # 主执行入口
    # ------------------------------------------------------------------

    def execute(self) -> HandlerResult:
        """执行保留清理主循环。

        返回 COMPLETED 表示所有 lane 已完成；CONTINUE 表示还有 lane 待处理。
        """
        current_lane = self._run.current_lane
        start_idx = 0
        if current_lane and current_lane in LANE_ORDER:
            start_idx = LANE_ORDER.index(current_lane)

        for lane_idx in range(start_idx, len(LANE_ORDER)):
            lane = LANE_ORDER[lane_idx]
            lane_cfg = self._policy.lane_config(lane)
            if lane_cfg is None or not lane_cfg.enabled:
                continue

            batch_size = lane_cfg.batch_size or DEFAULT_BATCH_SIZE
            result = self._process_lane(lane, batch_size)

            if result.outcome == HandlerOutcome.CONTINUE:
                self._run.current_lane = lane
                return result
            if result.outcome == HandlerOutcome.RETRYABLE_ERROR:
                return result

        self._run.status = "done"
        return HandlerResult(outcome=HandlerOutcome.COMPLETED, reason="全部 lane 已完成")

    # ------------------------------------------------------------------
    # 各 lane 处理
    # ------------------------------------------------------------------

    def _process_lane(self, lane: str, batch_size: int) -> HandlerResult:
        """处理单个 lane 的一批数据。"""
        handlers = {
            "outbox_job": self._clean_outbox_jobs,
            "maintenance_input": self._clean_maintenance_inputs,
            "retrieval_generation": self._clean_retrieval_generation,
            "segment_summary": self._clean_segment_summaries,
            "epoch_checkpoint": self._clean_epoch_checkpoints,
            "compaction_input": self._clean_compaction_inputs,
            "forget_action": self._clean_forget_actions,
            "retention_batch": self._clean_retention_batches,
            "retention_run": self._clean_retention_runs,
        }
        handler = handlers.get(lane)
        if handler is None:
            return HandlerResult(outcome=HandlerOutcome.COMPLETED)
        return handler(batch_size)

    # ------------------------------------------------------------------
    # Batch 生命周期
    # ------------------------------------------------------------------

    def _get_next_batch_no(self, lane: str) -> int:
        max_batch = (
            self._db.query(RetentionCleanupBatch.batch_no)
            .filter(
                RetentionCleanupBatch.run_id == self._run.id,
                RetentionCleanupBatch.lane == lane,
            )
            .order_by(RetentionCleanupBatch.batch_no.desc())
            .first()
        )
        return (max_batch[0] + 1) if max_batch else 1

    def _get_last_cursor(self, lane: str) -> dict[str, Any]:
        """获取上次完成 batch 的 cursor_end（用于续跑）。"""
        last_batch = (
            self._db.query(RetentionCleanupBatch)
            .filter(
                RetentionCleanupBatch.run_id == self._run.id,
                RetentionCleanupBatch.lane == lane,
                RetentionCleanupBatch.status == "done",
            )
            .order_by(RetentionCleanupBatch.batch_no.desc())
            .first()
        )
        return last_batch.cursor_end_json if (last_batch and last_batch.cursor_end_json) else {}

    def _start_batch(
        self, lane: str, batch_no: int, cursor_start: dict[str, Any],
    ) -> RetentionCleanupBatch:
        batch = RetentionCleanupBatch(
            run_id=self._run.id,
            lane=lane,
            batch_no=batch_no,
            cursor_start_json=cursor_start,
            cutoff_at=self._run.cutoff_at,
            status="running",
        )
        self._db.add(batch)
        self._db.flush()
        return batch

    def _finish_batch(
        self,
        batch: RetentionCleanupBatch,
        scanned: int,
        scrubbed: int,
        deleted: int,
        cursor_end: dict[str, Any] | None = None,
    ) -> None:
        batch.status = "done"
        batch.scanned_count = scanned
        batch.scrubbed_count = scrubbed
        batch.deleted_count = deleted
        if cursor_end:
            batch.cursor_end_json = cursor_end
        self._run.scanned_count += scanned
        self._run.scrubbed_count += scrubbed
        self._run.deleted_count += deleted

    def _compute_input_hash(self, cursor_start: dict[str, Any], lane: str, batch_no: int) -> str:
        raw = json.dumps(
            {"cursor_start": cursor_start, "lane": lane, "batch_no": batch_no},
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    # ═════════════════════════════════════════════════════════════════
    # Lane: outbox_job
    # ═════════════════════════════════════════════════════════════════

    def _clean_outbox_jobs(self, batch_size: int) -> HandlerResult:
        """OutboxJob 清理。

        - deadletter：不 scrub、不自动删除（仅 completed 参与）
        - Phase 1：30 天 scrub payload/error_message
        - Phase 2：180 天在无 FK 引用时删除最小行
        - cutoff 使用 terminal_at（不可变）；未设置时回退到 created_at
        """
        lane = "outbox_job"
        cutoff_scrub = self._lane_cutoff(lane, "scrub")
        cutoff_delete = self._lane_cutoff(lane, "delete")
        cursor = self._get_last_cursor(lane)
        batch_no = self._get_next_batch_no(lane)

        # Phase 1: scrub（仅 completed，不碰 deadletter）
        query = self._db.query(OutboxJob).filter(
            OutboxJob.status == "completed",
            func.coalesce(OutboxJob.terminal_at, OutboxJob.created_at) <= cutoff_scrub,
            OutboxJob.id > cursor.get("last_id", ""),
        ).order_by(OutboxJob.id).limit(batch_size)

        batch = self._start_batch(lane, batch_no, cursor)
        batch.input_hash = self._compute_input_hash(cursor, lane, batch_no)

        rows = query.all()
        last_id = cursor.get("last_id", "")
        for job in rows:
            if job.payload:
                job.payload = {}
            if job.error_message and job.error_message != (job.terminal_reason or ""):
                job.error_message = job.terminal_reason or ""
            last_id = job.id

        self._finish_batch(
            batch, scanned=len(rows), scrubbed=len(rows),
            deleted=0, cursor_end={"last_id": last_id, "phase": "scrub"},
        )

        if len(rows) == batch_size:
            return HandlerResult(outcome=HandlerOutcome.CONTINUE, reason=f"{lane} scrub 分批")

        # Phase 2: delete（仅 completed，不碰 deadletter；游标续跑）
        delete_cursor = self._get_last_cursor(lane)
        last_id_from = delete_cursor.get("last_id", "") if delete_cursor.get("phase") == "delete" else ""

        deletable = (
            self._db.query(OutboxJob)
            .filter(
                OutboxJob.status == "completed",
                func.coalesce(OutboxJob.terminal_at, OutboxJob.created_at) <= cutoff_delete,
                OutboxJob.id > last_id_from,
            )
            .order_by(OutboxJob.id)
            .limit(batch_size)
            .all()
        )

        del_batch_no = self._get_next_batch_no(lane)
        del_batch = self._start_batch(lane, del_batch_no, {"last_id": last_id_from, "phase": "delete"})
        del_batch.input_hash = self._compute_input_hash({"last_id": last_id_from, "phase": "delete"}, lane, del_batch_no)

        deleted = 0
        last_deleted_id = last_id_from
        for job in deletable:
            ref_count = self._count_references(job.id)
            if ref_count > 0:
                continue
            self._db.delete(job)
            deleted += 1
            last_deleted_id = job.id

        self._finish_batch(
            del_batch, scanned=len(deletable), scrubbed=0,
            deleted=deleted, cursor_end={"last_id": last_deleted_id, "phase": "delete"},
        )

        if len(deletable) == batch_size:
            return HandlerResult(outcome=HandlerOutcome.CONTINUE, reason=f"{lane} delete 分批")

        return HandlerResult(outcome=HandlerOutcome.COMPLETED, reason=f"{lane} 完成")

    def _count_references(self, outbox_job_id: str) -> int:
        """统计 OutboxJob 的 FK 引用数。"""
        from aiive.db.models import CheckpointRun, CompactionRun, MemoryMaintenanceRun, RetrievalIndexRun

        count = 0
        for model in (CompactionRun, CheckpointRun, MemoryMaintenanceRun, RetrievalIndexRun):
            count += self._db.query(model).filter(
                model.outbox_job_id == outbox_job_id,
            ).count()
        count += self._db.query(ForgetStageRun).filter(
            ForgetStageRun.outbox_job_id == outbox_job_id,
        ).count()
        count += self._db.query(RetentionCleanupRun).filter(
            RetentionCleanupRun.outbox_job_id == outbox_job_id,
        ).count()
        return count

    # ═════════════════════════════════════════════════════════════════
    # Lane: maintenance_input
    # ═════════════════════════════════════════════════════════════════

    def _clean_maintenance_inputs(self, batch_size: int) -> HandlerResult:
        """MemoryMaintenanceInput 清理：scrub snapshot_json，有界续跑。"""
        lane = "maintenance_input"
        cutoff = self._lane_cutoff(lane, "scrub")
        cursor = self._get_last_cursor(lane)
        batch_no = self._get_next_batch_no(lane)

        query = (
            self._db.query(MemoryMaintenanceInput)
            .filter(
                MemoryMaintenanceInput.created_at <= cutoff,
                MemoryMaintenanceInput.id > cursor.get("last_id", ""),
            )
            .order_by(MemoryMaintenanceInput.id)
            .limit(batch_size)
        )

        batch = self._start_batch(lane, batch_no, cursor)
        batch.input_hash = self._compute_input_hash(cursor, lane, batch_no)

        rows = query.all()
        scrubbed = 0
        last_id = cursor.get("last_id", "")
        for row in rows:
            if row.snapshot_json:
                row.snapshot_json = {}
                scrubbed += 1
            last_id = row.id

        self._finish_batch(
            batch, scanned=len(rows), scrubbed=scrubbed,
            deleted=0, cursor_end={"last_id": last_id},
        )

        if len(rows) == batch_size:
            return HandlerResult(outcome=HandlerOutcome.CONTINUE, reason=f"{lane} 分批")
        return HandlerResult(outcome=HandlerOutcome.COMPLETED, reason=f"{lane} scrub={scrubbed}")

    # ═════════════════════════════════════════════════════════════════
    # Lane: retrieval_generation (Q.7 分批清理)
    # ═════════════════════════════════════════════════════════════════

    def _clean_retrieval_generation(self, batch_size: int) -> HandlerResult:
        """Retrieval generation 分批清理。

        顺序：Token → Entry → 确认 0 → Generation。
        每批重新确认 generation 仍为 retired/failed。
        """
        lane = "retrieval_generation"
        cutoff = self._lane_cutoff(lane, "scrub")

        gen = (
            self._db.query(RetrievalIndexGeneration)
            .filter(
                RetrievalIndexGeneration.status.in_(["retired", "failed"]),
                RetrievalIndexGeneration.status_changed_at <= cutoff,
            )
            .order_by(RetrievalIndexGeneration.status_changed_at.asc())
            .first()
        )
        if gen is None:
            return HandlerResult(outcome=HandlerOutcome.COMPLETED, reason=f"{lane} 无可清理 generation")

        if gen.status not in ("retired", "failed"):
            return HandlerResult(outcome=HandlerOutcome.CONTINUE, reason=f"{lane} generation 已被复用，跳过")

        # safety: 无正在运行的 rebuild
        from aiive.db.models import RetrievalIndexRun
        running_rebuild = (
            self._db.query(RetrievalIndexRun)
            .filter(RetrievalIndexRun.status == "running")
            .count()
        )
        if running_rebuild > 0:
            return HandlerResult(outcome=HandlerOutcome.CONTINUE, reason=f"{lane} 存在运行中 rebuild，暂缓清理")

        steps = self._get_cleanup_step(gen.id)
        batch_no = self._get_next_batch_no(lane)

        if steps == "token":
            return self._delete_tokens_batch(gen, batch_size, lane, batch_no)
        elif steps == "entry":
            return self._delete_entries_batch(gen, batch_size, lane, batch_no)
        elif steps == "confirm":
            return self._confirm_and_delete_gen(gen, lane)
        return HandlerResult(outcome=HandlerOutcome.COMPLETED)

    def _get_cleanup_step(self, _gen_id: str) -> str:
        last_batch = (
            self._db.query(RetentionCleanupBatch)
            .filter(
                RetentionCleanupBatch.run_id == self._run.id,
                RetentionCleanupBatch.lane == "retrieval_generation",
                RetentionCleanupBatch.status == "done",
            )
            .order_by(RetentionCleanupBatch.batch_no.desc())
            .first()
        )
        if last_batch and last_batch.cursor_end_json:
            step = last_batch.cursor_end_json.get("step", "token")
            if step in ("token", "entry", "confirm"):
                return step
        return "token"

    def _delete_tokens_batch(
        self, gen: RetrievalIndexGeneration, batch_size: int, lane: str, batch_no: int,
    ) -> HandlerResult:
        count = (
            self._db.query(RetrievalIndexToken)
            .filter(RetrievalIndexToken.index_version == gen.index_version)
            .count()
        )
        if count == 0:
            batch = self._start_batch(lane, batch_no, {"gen_id": gen.id, "step": "token"})
            self._finish_batch(batch, 0, 0, 0, {"gen_id": gen.id, "step": "entry"})
            return HandlerResult(outcome=HandlerOutcome.CONTINUE, reason=f"{lane} token 已清，进入 entry")

        gen_current = self._db.get(RetrievalIndexGeneration, gen.id)
        if gen_current and gen_current.status not in ("retired", "failed"):
            return HandlerResult(outcome=HandlerOutcome.CONTINUE, reason=f"{lane} generation 已被复用")

        tokens = (
            self._db.query(RetrievalIndexToken)
            .filter(RetrievalIndexToken.index_version == gen.index_version)
            .limit(batch_size)
            .all()
        )
        for token in tokens:
            self._db.delete(token)

        batch = self._start_batch(lane, batch_no, {"gen_id": gen.id, "step": "token"})
        next_step = "entry" if count <= batch_size else "token"
        self._finish_batch(batch, len(tokens), 0, 0, {"gen_id": gen.id, "step": next_step})
        return HandlerResult(outcome=HandlerOutcome.CONTINUE, reason=f"{lane} token 分批")

    def _delete_entries_batch(
        self, gen: RetrievalIndexGeneration, batch_size: int, lane: str, batch_no: int,
    ) -> HandlerResult:
        count = (
            self._db.query(RetrievalIndexEntry)
            .filter(RetrievalIndexEntry.index_version == gen.index_version)
            .count()
        )
        if count == 0:
            batch = self._start_batch(lane, batch_no, {"gen_id": gen.id, "step": "entry"})
            self._finish_batch(batch, 0, 0, 0, {"gen_id": gen.id, "step": "confirm"})
            return HandlerResult(outcome=HandlerOutcome.CONTINUE, reason=f"{lane} entry 已清，进入确认")

        gen_current = self._db.get(RetrievalIndexGeneration, gen.id)
        if gen_current and gen_current.status not in ("retired", "failed"):
            return HandlerResult(outcome=HandlerOutcome.CONTINUE, reason=f"{lane} generation 已被复用")

        entries = (
            self._db.query(RetrievalIndexEntry)
            .filter(RetrievalIndexEntry.index_version == gen.index_version)
            .limit(batch_size)
            .all()
        )
        for entry in entries:
            self._db.delete(entry)

        batch = self._start_batch(lane, batch_no, {"gen_id": gen.id, "step": "entry"})
        next_step = "confirm" if count <= batch_size else "entry"
        self._finish_batch(batch, len(entries), 0, 0, {"gen_id": gen.id, "step": next_step})
        return HandlerResult(outcome=HandlerOutcome.CONTINUE, reason=f"{lane} entry 分批")

    def _confirm_and_delete_gen(
        self, gen: RetrievalIndexGeneration, lane: str,
    ) -> HandlerResult:
        token_count = (
            self._db.query(RetrievalIndexToken)
            .filter(RetrievalIndexToken.index_version == gen.index_version)
            .count()
        )
        entry_count = (
            self._db.query(RetrievalIndexEntry)
            .filter(RetrievalIndexEntry.index_version == gen.index_version)
            .count()
        )
        if token_count > 0 or entry_count > 0:
            return HandlerResult(
                outcome=HandlerOutcome.RETRYABLE_ERROR,
                reason=f"Generation {gen.id} 仍有残余 Token={token_count} Entry={entry_count}",
            )

        gen_current = self._db.get(RetrievalIndexGeneration, gen.id)
        if gen_current and gen_current.status not in ("retired", "failed"):
            return HandlerResult(outcome=HandlerOutcome.COMPLETED, reason=f"{lane} generation 已被复用，跳过删除")

        self._db.delete(gen)
        return HandlerResult(outcome=HandlerOutcome.COMPLETED, reason=f"{lane} generation 已删除")

    # ═════════════════════════════════════════════════════════════════
    # Lane: segment_summary
    # ═════════════════════════════════════════════════════════════════

    def _clean_segment_summaries(self, batch_size: int) -> HandlerResult:
        lane = "segment_summary"
        cutoff = self._lane_cutoff(lane, "scrub")
        cursor = self._get_last_cursor(lane)
        batch_no = self._get_next_batch_no(lane)

        current_ids = {
            s[0] for s in self._db.query(Segment.summary_id).filter(
                Segment.summary_id.isnot(None)
            ).all()
        }

        filters = [
            SegmentSummary.created_at <= cutoff,
            SegmentSummary.id > cursor.get("last_id", ""),
        ]
        if current_ids:
            filters.append(~SegmentSummary.id.in_(current_ids))
        query = (
            self._db.query(SegmentSummary)
            .filter(*filters)
            .order_by(SegmentSummary.id)
            .limit(batch_size)
        )

        batch = self._start_batch(lane, batch_no, cursor)
        batch.input_hash = self._compute_input_hash(cursor, lane, batch_no)

        rows = query.all()
        last_id = cursor.get("last_id", "")
        for s in rows:
            s.goal = None
            s.outcome = None
            s.decisions = None
            s.open_loops = None
            s.entities = None
            s.artifacts = None
            s.important_tool_results = None
            s.active_constraints = None
            s.unresolved_failures = None
            s.omitted_artifact_refs = None
            last_id = s.id

        self._finish_batch(
            batch, scanned=len(rows), scrubbed=len(rows),
            deleted=0, cursor_end={"last_id": last_id},
        )

        if len(rows) == batch_size:
            return HandlerResult(outcome=HandlerOutcome.CONTINUE, reason=f"{lane} 分批")
        return HandlerResult(outcome=HandlerOutcome.COMPLETED, reason=f"{lane} scrub={len(rows)}")

    # ═════════════════════════════════════════════════════════════════
    # Lane: epoch_checkpoint
    # ═════════════════════════════════════════════════════════════════

    def _clean_epoch_checkpoints(self, batch_size: int) -> HandlerResult:
        lane = "epoch_checkpoint"
        cutoff = self._lane_cutoff(lane, "scrub")
        cursor = self._get_last_cursor(lane)
        batch_no = self._get_next_batch_no(lane)

        current_ids = {
            c[0] for c in self._db.query(Epoch.checkpoint_id).filter(
                Epoch.checkpoint_id.isnot(None)
            ).all()
        }

        filters: list[Any] = [
            EpochCheckpoint.created_at <= cutoff,
            EpochCheckpoint.id > cursor.get("last_id", ""),
        ]
        if current_ids:
            filters.append(~EpochCheckpoint.id.in_(current_ids))
        query = (
            self._db.query(EpochCheckpoint)
            .filter(*filters)
            .order_by(EpochCheckpoint.id)
            .limit(batch_size)
        )

        batch = self._start_batch(lane, batch_no, cursor)
        batch.input_hash = self._compute_input_hash(cursor, lane, batch_no)

        rows = query.all()
        last_id = cursor.get("last_id", "")
        for c in rows:
            c.current_goal = None
            c.completed_milestones = None
            c.open_loops = None
            c.active_constraints = None
            c.current_decisions = None
            c.referenced_artifacts = None
            c.relevant_entities = None
            c.latest_verified_tool_states = None
            last_id = c.id

        self._finish_batch(
            batch, scanned=len(rows), scrubbed=len(rows),
            deleted=0, cursor_end={"last_id": last_id},
        )

        if len(rows) == batch_size:
            return HandlerResult(outcome=HandlerOutcome.CONTINUE, reason=f"{lane} 分批")
        return HandlerResult(outcome=HandlerOutcome.COMPLETED, reason=f"{lane} scrub={len(rows)}")

    # ═════════════════════════════════════════════════════════════════
    # Lane: compaction_input (Q.6)
    # ═════════════════════════════════════════════════════════════════

    def _clean_compaction_inputs(self, batch_size: int) -> HandlerResult:
        lane = "compaction_input"
        cutoff = self._lane_cutoff(lane, "scrub")
        cursor = self._get_last_cursor(lane)
        batch_no = self._get_next_batch_no(lane)

        half = max(1, batch_size // 2)

        # CompactionInput
        compactions = (
            self._db.query(CompactionInput)
            .filter(
                CompactionInput.created_at <= cutoff,
                CompactionInput.id > cursor.get("compaction_last_id", ""),
            )
            .order_by(CompactionInput.id)
            .limit(half)
            .all()
        )
        comp_scrubbed = 0
        comp_last_id = cursor.get("compaction_last_id", "")
        for ci in compactions:
            if ci.working_state_snapshot:
                ci.working_state_snapshot = {}
                comp_scrubbed += 1
            comp_last_id = ci.id

        # EpochCompactionInput
        epoch_compactions = (
            self._db.query(EpochCompactionInput)
            .filter(
                EpochCompactionInput.created_at <= cutoff,
                EpochCompactionInput.id > cursor.get("epoch_last_id", ""),
            )
            .order_by(EpochCompactionInput.id)
            .limit(half)
            .all()
        )
        epoch_scrubbed = 0
        epoch_last_id = cursor.get("epoch_last_id", "")
        for eci in epoch_compactions:
            if eci.current_objective:
                eci.current_objective = None
                epoch_scrubbed += 1
            if eci.open_loops:
                eci.open_loops = []
            if eci.active_constraints:
                eci.active_constraints = []
            if eci.artifact_refs:
                eci.artifact_refs = []
            if eci.verified_tool_states:
                eci.verified_tool_states = []
            epoch_last_id = eci.id

        batch = self._start_batch(lane, batch_no, cursor)
        batch.input_hash = self._compute_input_hash(cursor, lane, batch_no)
        self._finish_batch(
            batch, scanned=len(compactions) + len(epoch_compactions),
            scrubbed=comp_scrubbed + epoch_scrubbed, deleted=0,
            cursor_end={"compaction_last_id": comp_last_id, "epoch_last_id": epoch_last_id},
        )

        has_more = (len(compactions) == half) or (len(epoch_compactions) == half)
        if has_more:
            return HandlerResult(outcome=HandlerOutcome.CONTINUE, reason=f"{lane} 分批")
        return HandlerResult(
            outcome=HandlerOutcome.COMPLETED,
            reason=f"{lane} CI={comp_scrubbed}, ECI={epoch_scrubbed}",
        )

    # ═════════════════════════════════════════════════════════════════
    # Lane: forget_action (Q.8)
    # ═════════════════════════════════════════════════════════════════

    def _clean_forget_actions(self, batch_size: int) -> HandlerResult:
        lane = "forget_action"
        cutoff = self._lane_cutoff(lane, "scrub")
        cursor = self._get_last_cursor(lane)
        batch_no = self._get_next_batch_no(lane)

        purged_ops = (
            self._db.query(ForgetOperation)
            .filter(
                ForgetOperation.status.in_(FORGET_CLEANUP_READY_STATUSES),
                ForgetOperation.purged_at <= cutoff,
                ForgetOperation.id > cursor.get("last_id", ""),
            )
            .order_by(ForgetOperation.id)
            .limit(batch_size)
            .all()
        )

        batch = self._start_batch(lane, batch_no, cursor)
        batch.input_hash = self._compute_input_hash(cursor, lane, batch_no)

        deleted_actions = 0
        deleted_batches = 0
        deleted_deps = 0
        deleted_targets = 0
        last_id = cursor.get("last_id", "")
        for op in purged_ops:
            # 安全谓词：Verifier 必须已通过
            if not is_forget_operation_clearable(self._db, op.id):
                continue
            last_id = op.id

            actions = (
                self._db.query(ForgetAction)
                .filter(ForgetAction.forget_operation_id == op.id)
                .all()
            )
            for a in actions:
                a.details = None
                self._db.delete(a)
                deleted_actions += 1

            batches = (
                self._db.query(ForgetBatch)
                .filter(ForgetBatch.forget_operation_id == op.id)
                .all()
            )
            for b in batches:
                self._db.delete(b)
                deleted_batches += 1

            deps = (
                self._db.query(ForgetDependency)
                .filter(ForgetDependency.forget_operation_id == op.id)
                .all()
            )
            for d in deps:
                self._db.delete(d)
                deleted_deps += 1

            targets = (
                self._db.query(ForgetTarget)
                .filter(ForgetTarget.forget_operation_id == op.id)
                .all()
            )
            for t in targets:
                self._db.delete(t)
                deleted_targets += 1

        self._finish_batch(
            batch, scanned=len(purged_ops), scrubbed=0,
            deleted=deleted_actions + deleted_batches + deleted_deps + deleted_targets,
            cursor_end={"last_id": last_id},
        )

        if len(purged_ops) == batch_size:
            return HandlerResult(outcome=HandlerOutcome.CONTINUE, reason=f"{lane} 分批")
        return HandlerResult(
            outcome=HandlerOutcome.COMPLETED,
            reason=f"{lane} ops={len(purged_ops)} actions={deleted_actions} batches={deleted_batches}",
        )

    # ═════════════════════════════════════════════════════════════════
    # Lane: retention_run / retention_batch (自身 retention lane)
    # ═════════════════════════════════════════════════════════════════

    def _clean_retention_batches(self, batch_size: int) -> HandlerResult:
        lane = "retention_batch"
        cutoff = self._lane_cutoff(lane, "scrub")
        cursor = self._get_last_cursor(lane)
        batch_no = self._get_next_batch_no(lane)

        old_batches = (
            self._db.query(RetentionCleanupBatch)
            .filter(
                RetentionCleanupBatch.run_id != self._run.id,
                RetentionCleanupBatch.status == "done",
                RetentionCleanupBatch.updated_at <= cutoff,
                RetentionCleanupBatch.id > cursor.get("last_id", ""),
            )
            .order_by(RetentionCleanupBatch.id)
            .limit(batch_size)
            .all()
        )

        batch = self._start_batch(lane, batch_no, cursor)
        batch.input_hash = self._compute_input_hash(cursor, lane, batch_no)

        last_id = cursor.get("last_id", "")
        for b in old_batches:
            self._db.delete(b)
            last_id = b.id

        self._finish_batch(
            batch, scanned=len(old_batches), scrubbed=0,
            deleted=len(old_batches), cursor_end={"last_id": last_id},
        )

        if len(old_batches) == batch_size:
            return HandlerResult(outcome=HandlerOutcome.CONTINUE, reason=f"{lane} 分批")
        return HandlerResult(outcome=HandlerOutcome.COMPLETED, reason=f"{lane} deleted={len(old_batches)}")

    def _clean_retention_runs(self, batch_size: int) -> HandlerResult:
        lane = "retention_run"
        cutoff = self._lane_cutoff(lane, "scrub")
        cursor = self._get_last_cursor(lane)
        batch_no = self._get_next_batch_no(lane)

        old_runs = (
            self._db.query(RetentionCleanupRun)
            .filter(
                RetentionCleanupRun.id != self._run.id,
                RetentionCleanupRun.status == "done",
                RetentionCleanupRun.updated_at <= cutoff,
                RetentionCleanupRun.id > cursor.get("last_id", ""),
            )
            .order_by(RetentionCleanupRun.id)
            .limit(batch_size)
            .all()
        )

        batch = self._start_batch(lane, batch_no, cursor)
        batch.input_hash = self._compute_input_hash(cursor, lane, batch_no)

        deleted = 0
        last_id = cursor.get("last_id", "")
        for run in old_runs:
            remaining = (
                self._db.query(RetentionCleanupBatch)
                .filter(RetentionCleanupBatch.run_id == run.id)
                .count()
            )
            if remaining > 0:
                continue
            self._db.delete(run)
            deleted += 1
            last_id = run.id

        self._finish_batch(
            batch, scanned=len(old_runs), scrubbed=0,
            deleted=deleted, cursor_end={"last_id": last_id},
        )

        if len(old_runs) == batch_size:
            return HandlerResult(outcome=HandlerOutcome.CONTINUE, reason=f"{lane} 分批")
        return HandlerResult(outcome=HandlerOutcome.COMPLETED, reason=f"{lane} deleted={deleted}")
