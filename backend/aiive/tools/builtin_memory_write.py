"""Domain tool handlers (implementation) split from builtin_tools."""

from sqlalchemy.orm import Session

from aiive.context.run_context import RunContext
from aiive.tools.builtin_tools import db_handler, require_ctx


@db_handler
def handle_remember_or_update(db: Session, ctx: RunContext | None, content: str, memory_type: str = "fact", memory_key: str = "", keywords: list[str] | None = None):
    """创建或更新记忆，通过 MemoryWriteService 统一写入（查重下沉到服务层）。

    execution_mode = "user_required"：写入失败时本工具返回 error，Turn 不得声称成功。
    """
    from aiive.memory.memory_types import EvidenceItem, TrustLevel
    from aiive.memory.memory_write_service import MemoryWriteService
    from aiive.memory.proposal_normalizer import ProposalNormalizer

    ctx = require_ctx(ctx, "remember_or_update")
    # Phase 2: user_required 失败语义 — 写入失败必须明确返回 error
    ctx.execution_mode = "user_required"
    writer = MemoryWriteService(db)

    # 幂等键来源：优先用真实 Event.id；流式/工具上下文缺失时退化为 trace_id，
    # 保证同轮次稳定、跨轮次不互相冲突（彻底避免固定键冲突）。
    source_event_ids = list(ctx.source_event_ids) or ([ctx.trace_id] if ctx.trace_id else [])

    evidence = [EvidenceItem(
        # source_event_id 用于 reinforce 防重（同一来源事件不重复强化）
        source_event_id=source_event_ids[0] if source_event_ids else None,
        source_type="user_message",
        trust_level=TrustLevel.TRUSTED.value,
        relation="supports",
        content_span=content,
    )]

    normalizer = ProposalNormalizer()
    result = normalizer.normalize(
        content=content,
        memory_type_hint=memory_type or None,
        memory_key_hint=memory_key or None,
        confidence=0.95,
        importance=0.8,
        trust_level=TrustLevel.TRUSTED.value,
        evidence=[e.model_dump() for e in evidence],
        source_event_ids=source_event_ids,
        extractor_name="remember_or_update_tool",
        extractor_version="1.0",
        thread_id=ctx.thread_id,
        keywords=keywords,
    )

    if result.error or result.proposal is None:
        return {"ok": False, "error": result.error or "Normalization failed", "memory_write_failed": True}

    # Phase 2: provenance — 真实 Event.id + TurnRecord PK
    result.proposal.source_turn_record_id = ctx.turn_record_id
    result.proposal.source_turn_id = ctx.turn_id
    result.proposal.source_event_ids = source_event_ids
    result.proposal.execution_mode = ctx.execution_mode
    # 关键：provenance 注入后重算幂等键，确保键包含真实来源与内容，彻底避免固定键冲突
    result.proposal.compute_request_idempotency()

    try:
        write_result = writer.write(result.proposal, run_context=ctx)
    except Exception as e:
        return {"ok": False, "error": str(e), "memory_write_failed": True}

    db.flush()

    return {
        "ok": write_result.written,
        "memory_id": write_result.memory_id,
        "content": content,
        "memory_type": result.proposal.memory_type,
        "canonical_key": result.proposal.canonical_key,
        "keywords": result.proposal.keywords,
        "state": write_result.state,
        "operation": write_result.operation,
    }

@db_handler
def handle_run_memory_maintenance(db: Session, ctx: RunContext | None):
    """事务化入队一次手工记忆维护，并返回入队前只读诊断快照。"""
    from aiive.memory.memory_maintenance import MemoryMaintenance
    from aiive.worker.scheduler_daemon import enqueue_maintenance_job

    run_ctx = require_ctx(ctx, "run_memory_maintenance")
    scan = MemoryMaintenance(db).scan()
    bucket_source = run_ctx.turn_record_id or run_ctx.turn_id or run_ctx.trace_id
    maintenance_operation_id = enqueue_maintenance_job(
        db,
        window_bucket=f"manual-{bucket_source}",
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
