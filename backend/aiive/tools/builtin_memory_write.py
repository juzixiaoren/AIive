"""Domain tool handlers (implementation) split from builtin_tools."""

from dataclasses import replace
from typing import override

from aiive.tools.base import DatabaseTool

from sqlalchemy.orm import Session

from aiive.context.run_context import RunContext, require_ctx


class RememberOrUpdateTool(DatabaseTool):
    @override
    def _run(self, db: Session, ctx: RunContext | None, content: str, memory_type: str = "fact", memory_key: str = "", keywords: list[str] | None = None):
        """创建或更新记忆，通过 MemoryWriteService 统一写入（查重下沉到服务层）。

        execution_mode = "user_required"：写入失败时本工具返回 error，Turn 不得声称成功。
        """
        from aiive.memory.memory_types import TrustLevel, WriteOutcome
        from aiive.memory.memory_write_service import MemoryWriteService, WriteResult
        from aiive.memory.proposal_normalizer import ProposalNormalizer

        ctx = require_ctx(ctx, "remember_or_update")
        source_event = ctx.load_user_request_event(db)
        if source_event is None or ctx.source_event_ids != [source_event.id]:
            reason = "user_request_event_required"
            return {**WriteResult(WriteOutcome.FAILED, reason=reason).to_dict(),
                    "error": reason, "canonical_key": ""}
        ctx = replace(ctx, execution_mode="user_required")

        normalizer = ProposalNormalizer()
        result = normalizer.normalize(
            content=content,
            memory_type_hint=memory_type or None,
            memory_key_hint=memory_key or None,
            confidence=0.95,
            importance=0.8,
            trust_level=TrustLevel.TRUSTED.value,
            source_event_ids=ctx.source_event_ids,
            extractor_name="remember_or_update_tool",
            extractor_version="1.0",
            thread_id=ctx.thread_id,
            keywords=keywords,
        )

        if result.error or result.proposal is None:
            reason = result.error or "Normalization failed"
            return {**WriteResult(WriteOutcome.FAILED, reason=reason).to_dict(),
                    "error": reason, "canonical_key": ""}

        # Phase 2: provenance — 真实 Event.id + TurnRecord PK
        result.proposal.source_turn_record_id = ctx.turn_record_id
        result.proposal.source_turn_id = ctx.turn_id
        result.proposal.execution_mode = ctx.execution_mode
        # 关键：provenance 注入后重算幂等键，确保键包含真实来源与内容，彻底避免固定键冲突
        result.proposal.compute_request_idempotency()

        write_result = MemoryWriteService(db).write(result.proposal, run_context=ctx)
        payload = {
            **write_result.to_dict(),
            "memory_type": result.proposal.memory_type,
            "canonical_key": result.proposal.canonical_key,
            "keywords": result.proposal.keywords,
        }
        if write_result.outcome == WriteOutcome.FAILED:
            payload["error"] = write_result.reason
        return payload


class RunMemoryMaintenanceTool(DatabaseTool):
    @override
    def _run(self, db: Session, ctx: RunContext | None):
        """事务化入队一次手工记忆维护，并返回入队前只读诊断快照。"""
        from aiive.memory.memory_maintenance import MemoryMaintenance
        from aiive.worker.scheduler_daemon import enqueue_maintenance_job

        run_ctx = require_ctx(ctx, "run_memory_maintenance")
        scan = MemoryMaintenance(db).scan()
        # 一次显式请求使用一个稳定 attempt ID。换一轮发起补跑才会创建新任务。
        attempt_id = run_ctx.turn_record_id or run_ctx.turn_id or run_ctx.trace_id
        maintenance_operation_id = enqueue_maintenance_job(
            db,
            window_bucket=f"manual-{attempt_id}",
            source_context={
                "thread_id": run_ctx.thread_id,
                "turn_id": run_ctx.turn_id,
                "trace_id": run_ctx.trace_id,
            },
        )
        return {
            "ok": True,
            "enqueued": maintenance_operation_id is not None,
            "maintenance_operation_id": maintenance_operation_id,
            "pre_enqueue_scan": scan,
        }
