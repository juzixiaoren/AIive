"""
API路由模块：记忆管理
- 提供记忆的增删改查 REST API 接口
- 支持记忆的遗忘、休眠、归档等生命周期操作
- 提供记忆扫描和投影维护接口
"""
import uuid as _uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aiive.context.run_context import (
    RunContext,
    RUN_CTX_API,
    RUN_CTX_MANUAL_MEMORY_API,
)
from aiive.db.base import get_db
from aiive.memory.memory_maintenance import MemoryMaintenance
from aiive.memory.memory_policy import MemoryPolicyEngine, MemoryReadChannel
from aiive.memory.memory_store import MemoryStore
from aiive.memory.memory_types import MemoryProposal, TrustLevel, WriteOutcome
from aiive.memory.memory_write_service import MemoryWriteService, WriteResult
from aiive.memory.projection import MemoryProjection
from aiive.memory.proposal_normalizer import ProposalNormalizer

router = APIRouter(prefix="/api")


class CreateMemoryRequest(BaseModel):
    """创建记忆请求体"""
    content: str = Field(..., min_length=1)
    memory_type: str = "fact"
    pinned: bool = False


class CreateMemoryResponse(BaseModel):
    """手动写入记忆的结构化结果。"""

    ok: bool
    outcome: WriteOutcome
    operation: str = ""
    id: str = ""
    content: str
    lifecycle_state: str = ""
    memory_type: str
    canonical_key: str
    reason: str = ""
    idempotent: bool = False


def _create_memory_response(
    request: CreateMemoryRequest,
    memory_type: str,
    canonical_key: str,
    result: WriteResult,
    *,
    idempotent: bool = False,
) -> CreateMemoryResponse:
    """将领域写入结果转换为稳定的 API 响应。"""
    return CreateMemoryResponse(
        ok=result.written,
        outcome=result.outcome,
        operation=result.operation,
        id=result.memory_id,
        content=request.content,
        lifecycle_state=result.state,
        memory_type=memory_type,
        canonical_key=canonical_key,
        reason=result.reason,
        idempotent=idempotent,
    )


@router.get("/memories")
def list_memories(db: Session = Depends(get_db)):
    """获取所有记忆列表

    Args:
        db: 数据库会话

    Returns:
        记忆列表，包含类型、生命周期状态、内容、置信度等
    """
    from aiive.forget.visibility_service import ForgetVisibilityService

    store = MemoryStore(db)
    policy = MemoryPolicyEngine()
    records = store.list_all()
    # Phase 6A fail-closed：过滤被 forget 屏蔽的记忆（lifecycle=forgotten 已是子集，
    # 此处再排除被 active Shield / Tombstone 覆盖的记忆）
    blocked = ForgetVisibilityService.blocked_target_ids(
        db, "memory_record",
        [(r.id, r.created_at) for r in records],
    )
    return [
        {
            "id": r.id,
            "memory_type": r.memory_type,
            "lifecycle_state": r.lifecycle_state,
            "content": policy.render_content(r.content, r.sensitivity, MemoryReadChannel.API),
            "sensitivity": r.sensitivity or "normal",
            "source_event_id": r.source_event_id,
            "confidence": r.confidence,
            "pinned": r.pinned,
            "created_at": r.created_at.isoformat(),
            "updated_at": r.updated_at.isoformat(),
        }
        for r in records
        if r.id not in blocked
    ]


@router.get("/memories/{memory_id}/relations")
def get_memory_relations(memory_id: str, db: Session = Depends(get_db)):
    """查询一条记忆支撑的实体关系（含有效期与来源证据）。

    只读、复用既有 /api/memories 路由前缀；不新建图谱管理后台。
    关系是派生投影，因此这里同时回查来源记忆状态，已失效/遗忘的关系不会被
    当作当前事实返回。
    """
    from aiive.db.models import GraphEntity, GraphRelation, MemoryRecord
    from aiive.forget.visibility_service import ForgetVisibilityService
    from aiive.memory.graph_predicates import (
        cardinality,
        conflict_group_key,
        conflicted_group_keys,
        is_single_valued,
    )

    record = db.get(MemoryRecord, memory_id)
    if record is None:
        raise HTTPException(status_code=404, detail="记忆不存在")

    blocked = ForgetVisibilityService.filter_memory_ids(db, [memory_id])
    visible = memory_id in blocked
    policy = MemoryPolicyEngine()
    relations = (
        db.query(GraphRelation)
        .filter(GraphRelation.source_memory_id == memory_id)
        .order_by(
            GraphRelation.status.asc(),
            GraphRelation.recorded_at.desc(),
            GraphRelation.id.asc(),
        )
        .limit(200)
        .all()
    )
    # 单值谓词出现多个有效客体 = 冲突：如实返回标记，不隐藏、也不替记忆层裁决。
    # 冲突对方可能属于别的记忆，因此按同主体做一次有界批查询再判定。
    single_subjects = {
        r.subject_entity_id
        for r in relations
        if r.status == "active" and is_single_valued(r.predicate)
    }
    peers = (
        db.query(GraphRelation)
        .filter(
            GraphRelation.status == "active",
            GraphRelation.subject_entity_id.in_(single_subjects),
        )
        .limit(1000)
        .all()
    ) if single_subjects else []
    conflict_keys = conflicted_group_keys([
        (
            r.subject_entity_id, r.predicate, r.object_entity_id,
            r.status, r.scope_type, r.scope_id,
        )
        for r in peers
    ])
    entity_ids = {r.subject_entity_id for r in relations} | {r.object_entity_id for r in relations}
    entities = {
        entity.id: entity
        for entity in db.query(GraphEntity).filter(GraphEntity.id.in_(entity_ids)).all()
    } if entity_ids else {}

    return {
        "memory_id": memory_id,
        "memory_type": record.memory_type,
        "lifecycle_state": record.lifecycle_state,
        "validity_state": record.validity_state,
        "record_version": record.record_version,
        "visible": visible and (record.lifecycle_state or "") != "forgotten",
        "entities": [
            {
                "id": entity.id,
                "display_name": entity.display_name,
                "normalized_name": entity.normalized_name,
                "aliases": entity.aliases or [],
                "scope_type": entity.scope_type,
                "scope_id": entity.scope_id,
            }
            for entity in entities.values()
        ],
        "relations": [
            {
                "id": relation.id,
                "subject": getattr(entities.get(relation.subject_entity_id), "display_name", ""),
                "predicate": relation.predicate,
                "object": getattr(entities.get(relation.object_entity_id), "display_name", ""),
                "status": relation.status,
                "confidence": relation.confidence,
                "scope_type": relation.scope_type,
                "scope_id": relation.scope_id,
                "valid_from": relation.valid_from.isoformat() if relation.valid_from else None,
                "valid_to": relation.valid_to.isoformat() if relation.valid_to else None,
                "recorded_at": relation.recorded_at.isoformat() if relation.recorded_at else None,
                "source_record_version": relation.source_record_version,
                "source_span": policy.render_content(
                    relation.source_span or "", record.sensitivity, MemoryReadChannel.API,
                ),
                "closed_reason": relation.closed_reason,
                "superseded_by_id": relation.superseded_by_id,
                "cardinality": cardinality(relation.predicate),
                "conflicted": relation.status == "active" and conflict_group_key(
                    relation.subject_entity_id, relation.predicate,
                    relation.scope_type, relation.scope_id,
                ) in conflict_keys,
            }
            for relation in relations
        ],
    }


@router.get("/memories/conflicts")
def list_memory_conflicts(
    thread_id: str = "",
    status: str = "",
    limit: int = 50,
    db: Session = Depends(get_db),
):
    """列出记忆冲突待确认案例（只读）。

    待确认期间这些案例**不是**记忆：不会出现在 /memories 列表、不会被召回、
    也不会产生图谱关系。这里只暴露"用户需要决定什么"，不含任何内部模型调用细节。
    """
    from aiive.memory.conflict_confirmation import MemoryConflictService

    policy = MemoryPolicyEngine()
    cases = MemoryConflictService.list_cases(
        db, thread_id=thread_id, status=status, limit=limit,
    )
    return [
        {
            "id": case.id,
            "status": case.status,
            "thread_id": case.thread_id,
            "canonical_key": case.canonical_key,
            "memory_type": case.memory_type,
            "old_memory_id": case.old_memory_id,
            "old_content": policy.render_content(
                case.old_content, case.sensitivity, MemoryReadChannel.API,
            ),
            "new_content": policy.render_content(
                case.new_content, case.sensitivity, MemoryReadChannel.API,
            ),
            "verdict": case.verdict,
            "question": case.question,
            "clarify_rounds": case.clarify_rounds,
            "decision": case.decision,
            "resolved_memory_id": case.resolved_memory_id,
            "created_at": case.created_at.isoformat() if case.created_at else "",
            "expires_at": case.expires_at.isoformat() if case.expires_at else "",
            "resolved_at": case.resolved_at.isoformat() if case.resolved_at else "",
        }
        for case in cases
    ]


@router.post("/maintenance/graph-rebuild")
def rebuild_graph(db: Session = Depends(get_db)):
    """从 canonical 记忆记录重建图谱投影（可重复执行、幂等）。

    用于关系索引丢失、Outbox 重试失败或维护中断后的恢复；不调用 LLM。
    """
    from aiive.memory.graph_rebuild import GraphRebuildService

    report = GraphRebuildService(db).rebuild()
    db.commit()
    return report


@router.post("/memories", response_model=CreateMemoryResponse)
def create_memory(request: CreateMemoryRequest,
                   db: Session = Depends(get_db),
                   idempotency_key: str = Header(None, alias="Idempotency-Key")):
    """创建新记忆 — 走完整写入链路（ProposalNormalizer → MemoryWriteService）。

    支持 Idempotency-Key header 去重。
    execution_mode = "user_required"：拒绝返回 422，失败返回 409。
    """
    from aiive.db.models import MemoryProposal as MemoryProposalModel, MemoryRecord

    # 幂等检查：重复请求必须重放原领域结果，不能再次执行写入链路
    if idempotency_key:
        existing = db.query(MemoryProposalModel).filter(
            MemoryProposalModel.idempotency_key == idempotency_key
        ).first()
        if existing is not None:
            final_operation = existing.final_operation or ""
            if final_operation == "reject":
                raise HTTPException(
                    status_code=422,
                    detail=existing.gate_reason or "记忆未通过写入规则",
                )

            if final_operation == "ignore" and not existing.final_memory_id:
                return CreateMemoryResponse(
                    ok=False,
                    outcome=WriteOutcome.IGNORED,
                    operation="ignore",
                    content=existing.content or request.content,
                    memory_type=existing.memory_type or request.memory_type,
                    canonical_key=existing.canonical_key or "",
                    reason=existing.gate_reason or "该请求未产生记忆变更",
                    idempotent=True,
                )

            if not existing.final_memory_id:
                raise HTTPException(
                    status_code=409,
                    detail="幂等请求没有可重放的记忆写入结果",
                )

            record = db.get(MemoryRecord, existing.final_memory_id)
            if record is None:
                raise HTTPException(
                    status_code=409,
                    detail="幂等写入记录指向的记忆不存在",
                )
            replay_outcome = (
                WriteOutcome.REINFORCE_SKIPPED
                if final_operation == "ignore"
                else WriteOutcome.WRITTEN
            )
            return CreateMemoryResponse(
                ok=replay_outcome == WriteOutcome.WRITTEN,
                outcome=replay_outcome,
                operation=final_operation,
                id=record.id,
                content=record.content,
                lifecycle_state=record.lifecycle_state,
                memory_type=record.memory_type,
                canonical_key=record.canonical_key or "",
                reason=(
                    "所有 source_event 均已计入，不重复强化"
                    if replay_outcome == WriteOutcome.REINFORCE_SKIPPED
                    else ""
                ),
                idempotent=True,
            )

    normalizer = ProposalNormalizer()
    result = normalizer.normalize(
        content=request.content,
        memory_type_hint=request.memory_type or None,
        memory_key_hint=None,
        confidence=0.95,
        importance=0.8,
        trust_level=TrustLevel.TRUSTED.value,
        evidence=[],
        source_event_ids=[],
        extractor_name="manual_memory_api",
        extractor_version="1.0",
        thread_id="",
    )

    if result.error or result.proposal is None:
        raise HTTPException(status_code=422, detail=result.error or "Normalization failed")

    proposal = result.proposal
    proposal.execution_mode = "user_required"
    proposal.retention_policy = "pinned" if request.pinned else "normal"
    proposal.source_turn_record_id = ""  # API 无 Turn 上下文
    if idempotency_key:
        proposal.idempotency_key = idempotency_key

    ctx = RunContext(
        thread_id="manual_api",
        trace_id=str(_uuid.uuid4()),
        source=RUN_CTX_MANUAL_MEMORY_API,
        execution_mode="user_required",
    )

    writer = MemoryWriteService(db)
    try:
        write_result = writer.write(proposal, run_context=ctx)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"记忆写入失败: {exc}") from exc

    if write_result.outcome == WriteOutcome.GATE_REJECTED:
        db.commit()
        raise HTTPException(status_code=422, detail=write_result.reason or "记忆未通过写入规则")

    if write_result.outcome == WriteOutcome.FAILED:
        db.rollback()
        raise HTTPException(status_code=409, detail=write_result.reason or "记忆写入失败")

    db.commit()
    return _create_memory_response(
        request,
        proposal.memory_type,
        proposal.canonical_key,
        write_result,
    )


class ForgetRequest(BaseModel):
    """遗忘记忆请求体"""
    reason: str = ""


@router.post("/memories/{memory_id}/forget")
def forget_memory(memory_id: str, request: ForgetRequest, db: Session = Depends(get_db)):
    """遗忘指定记忆（标记为遗忘状态）

    Args:
        memory_id: 记忆ID
        request: 包含遗忘原因的请求体
        db: 数据库会话

    Returns:
        遗忘操作结果
    """
    writer = MemoryWriteService(db)
    result = writer.forget(memory_id, reason=request.reason)
    db.commit()
    return {"ok": result.written, "memory_id": result.memory_id, "reason": result.reason}


@router.post("/memories/{memory_id}/sleep")
def sleep_memory(memory_id: str, db: Session = Depends(get_db)):
    """将指定记忆置为休眠状态

    Args:
        memory_id: 记忆ID
        db: 数据库会话

    Returns:
        休眠操作结果
    """
    writer = MemoryWriteService(db)
    proposal = MemoryProposal(
        source_event_ids=[memory_id],
        memory_type="",
        canonical_key="",
        proposed_operation="sleep",
    )
    ctx = RunContext(thread_id="api", trace_id=memory_id, source=RUN_CTX_API)
    result = writer.execute_maintenance(proposal, ctx)
    db.commit()
    return {"ok": result.written, "memory_id": result.memory_id, "reason": result.reason}


@router.post("/memories/{memory_id}/archive")
def archive_memory(memory_id: str, db: Session = Depends(get_db)):
    """将指定记忆归档

    Args:
        memory_id: 记忆ID
        db: 数据库会话

    Returns:
        归档操作结果
    """
    writer = MemoryWriteService(db)
    proposal = MemoryProposal(
        source_event_ids=[memory_id],
        memory_type="",
        canonical_key="",
        proposed_operation="archive",
    )
    ctx = RunContext(thread_id="api", trace_id=memory_id, source=RUN_CTX_API)
    result = writer.execute_maintenance(proposal, ctx)
    db.commit()
    return {"ok": result.written, "memory_id": result.memory_id, "reason": result.reason}


@router.post("/maintenance/memory-scan")
def scan_memories(db: Session = Depends(get_db)):
    """触发记忆维护扫描

    扫描所有记忆并执行必要的生命周期操作。

    Args:
        db: 数据库会话

    Returns:
        扫描结果
    """
    maint = MemoryMaintenance(db)
    return maint.scan()


@router.get("/maintenance/projection")
def get_projection(db: Session = Depends(get_db)):
    """获取记忆投影的 JSON 表示

    Args:
        db: 数据库会话

    Returns:
        记忆投影的 JSON 数据
    """
    proj = MemoryProjection(db)
    return proj.to_json()
