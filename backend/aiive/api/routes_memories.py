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

from aiive.context.run_context import RunContext
from aiive.db.base import get_db
from aiive.memory.memory_maintenance import MemoryMaintenance
from aiive.memory.memory_store import MemoryStore
from aiive.memory.memory_types import MemoryProposal, TrustLevel
from aiive.memory.memory_write_service import MemoryWriteService
from aiive.memory.projection import MemoryProjection
from aiive.memory.proposal_normalizer import ProposalNormalizer

router = APIRouter(prefix="/api")


class CreateMemoryRequest(BaseModel):
    """创建记忆请求体"""
    content: str = Field(..., min_length=1)
    memory_type: str = "fact"
    pinned: bool = False


@router.get("/memories")
def list_memories(db: Session = Depends(get_db)):
    """获取所有记忆列表

    Args:
        db: 数据库会话

    Returns:
        记忆列表，包含类型、生命周期状态、内容、置信度等
    """
    store = MemoryStore(db)
    records = store.list_all()
    return [
        {
            "id": r.id,
            "memory_type": r.memory_type,
            "lifecycle_state": r.lifecycle_state,
            "content": r.content,
            "source_event_id": r.source_event_id,
            "confidence": r.confidence,
            "lineage": r.lineage,
            "pinned": r.pinned,
            "created_at": r.created_at.isoformat(),
            "updated_at": r.updated_at.isoformat(),
        }
        for r in records
    ]


@router.post("/memories")
def create_memory(request: CreateMemoryRequest,
                   db: Session = Depends(get_db),
                   idempotency_key: str = Header(None, alias="Idempotency-Key")):
    """创建新记忆 — 走完整写入链路（ProposalNormalizer → MemoryWriteService）。

    支持 Idempotency-Key header 去重。
    execution_mode = "user_required"：写入失败返回 409。
    """
    from aiive.db.models import MemoryProposal as MemoryProposalModel

    # 幂等检查：已存在相同 idempotency_key 的 proposal → 返回已有结果
    if idempotency_key:
        existing = db.query(MemoryProposalModel).filter(
            MemoryProposalModel.idempotency_key == idempotency_key
        ).first()
        if existing is not None and existing.final_memory_id:
            return {
                "id": existing.final_memory_id,
                "content": request.content,
                "lifecycle_state": "active",
                "_idempotent": True,
            }

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
        source="manual_memory_api",
        execution_mode="user_required",
    )

    writer = MemoryWriteService(db)
    try:
        write_result = writer.write(proposal, run_context=ctx)
    except Exception as e:
        raise HTTPException(status_code=409, detail=f"Memory write failed: {e}")

    db.commit()

    return {
        "id": write_result.memory_id,
        "content": request.content,
        "lifecycle_state": write_result.state,
        "memory_type": proposal.memory_type,
        "canonical_key": proposal.canonical_key,
    }


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
    ctx = RunContext(thread_id="api", trace_id=memory_id, source="api")
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
    ctx = RunContext(thread_id="api", trace_id=memory_id, source="api")
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
