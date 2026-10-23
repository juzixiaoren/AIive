"""
API路由模块：知识库
- 提供知识文件导入（ingest）接口
- 提供文档删除、重索引与维护报告接口
- 提供知识块检索搜索接口
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from typing import Any

from aiive.db.base import get_db
from aiive.db.models import Chunk, Document
from aiive.knowledge.access import allowed_knowledge_roots, resolve_knowledge_path
from aiive.knowledge.ingestor import INACTIVE_STATUSES, KnowledgeIngestor, search_chunks
from aiive.knowledge.maintenance import (
    DEFAULT_BATCH_LIMIT,
    apply_report,
    scan_knowledge_base,
)

router = APIRouter(prefix="/api")

class IngestRequest(BaseModel):
    """知识导入请求体"""
    file_path: str = Field(..., min_length=1)


class MaintenanceRequest(BaseModel):
    """知识库维护请求体"""
    apply: bool = Field(True, description="是否执行清理；false 仅返回扫描报告")
    limit: int = Field(DEFAULT_BATCH_LIMIT, ge=1, le=500, description="单类别处理上限")


@router.post("/knowledge/ingest")
def ingest(request: IngestRequest, db: Session = Depends(get_db)):
    """导入知识文件到知识库（仅允许根目录内的文件，防任意本地文件读取）

    同一路径内容变化时，旧版本会被标记为 superseded 且分块立即清空，
    因此检索只会命中最新内容。

    Args:
        request: 包含 file_path 的导入请求
        db: 数据库会话

    Returns:
        导入结果
    """
    try:
        resolved = resolve_knowledge_path(request.file_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ingestor = KnowledgeIngestor(db)
    try:
        result = ingestor.ingest(str(resolved))
    except IntegrityError as exc:
        # 并发导入同一 source_path：部分唯一索引 ix_documents_active_source_path
        # 只允许先提交方胜出，本事务回滚；ingest 幂等，客户端重试即可收敛。
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="并发导入冲突：同一路径正在被其他请求更新，请重试",
        ) from exc
    db.commit()
    return result


@router.get("/knowledge")
def list_documents(
    limit: int = Query(50, ge=1, le=200),
    include_inactive: bool = Query(False, description="是否包含已取代/已删除的墓碑记录"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """列出已导入的持久文档、索引状态和分块数量。

    默认只返回活跃文档（indexed / index_failed / source_unavailable）；
    `include_inactive=true` 时额外返回 superseded / deleted 墓碑记录，用于审计。
    """
    query = db.query(Document)
    if not include_inactive:
        query = query.filter(Document.status.notin_(tuple(INACTIVE_STATUSES)))
    documents = query.order_by(Document.created_at.desc()).limit(limit).all()
    count_rows = (
        db.query(Chunk.document_id, func.count(Chunk.id))
        .filter(Chunk.document_id.in_([document.id for document in documents]))
        .group_by(Chunk.document_id)
        .all()
    ) if documents else []
    counts: dict[str, int] = {str(document_id): int(count) for document_id, count in count_rows}
    return {
        "documents": [
            {
                "document_id": document.id,
                "title": document.title,
                "source_path": document.source_path,
                "doc_type": document.doc_type,
                "mime_type": document.mime_type,
                "status": document.status,
                "content_size": document.content_size,
                "content_hash": document.content_hash,
                "chunks": int(counts.get(document.id, 0)),
                "superseded_by": document.superseded_by,
                "created_at": document.created_at,
                "updated_at": document.updated_at,
                "deleted_at": document.deleted_at,
            }
            for document in documents
        ],
        "allowed_roots": [str(root) for root in allowed_knowledge_roots()],
    }


@router.get("/knowledge/maintenance")
def maintenance_report(db: Session = Depends(get_db)) -> dict[str, Any]:
    """只读扫描知识库一致性，返回维护报告（不做任何修改）。"""
    return scan_knowledge_base(db).to_dict()


@router.post("/knowledge/maintenance")
def run_maintenance(
    request: MaintenanceRequest | None = None, db: Session = Depends(get_db),
) -> dict[str, Any]:
    """扫描并（可选）执行幂等清理，返回报告与本次动作计数。

    清理范围仅限 AIive 管理的记录/分块/对象，不会删除用户原始文件。
    """
    body = request or MaintenanceRequest(apply=True, limit=DEFAULT_BATCH_LIMIT)
    report = scan_knowledge_base(db)
    if not body.apply:
        return {"ok": True, "applied": False, "actions": {}, "report": report.to_dict()}
    actions = apply_report(db, report, limit=body.limit)
    db.commit()
    refreshed = scan_knowledge_base(db)
    return {
        "ok": True,
        "applied": True,
        "actions": {key: value for key, value in actions.items() if key != "report"},
        "report": refreshed.to_dict(),
    }


@router.get("/knowledge/{document_id}/source")
def read_source(document_id: str, db: Session = Depends(get_db)):
    """从对象存储读取经过哈希校验的知识原文。"""
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="知识文档不存在")
    try:
        content = KnowledgeIngestor(db).read_source(document_id)
    except (LookupError, ValueError, OSError) as exc:
        # 无持久原文 / 对象文件丢失 / 哈希校验失败：原文不可用，非服务端错误
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(content=content, media_type=document.mime_type)


@router.post("/knowledge/{document_id}/reindex")
def reindex(document_id: str, db: Session = Depends(get_db)):
    """从持久原文重新生成知识文档分块。"""
    try:
        result = KnowledgeIngestor(db).reindex(document_id)
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, OSError) as exc:
        # 文档已失效 / 原文对象丢失 / 哈希校验失败：冲突态而非服务端错误
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return result


@router.delete("/knowledge/{document_id}")
def delete_document(document_id: str, db: Session = Depends(get_db)):
    """删除知识文档：清空分块、释放原文对象，保留墓碑记录。

    只清理 AIive 管理的派生数据，不会删除 source_path 指向的用户原始文件。
    重复删除返回 200 且 `already_deleted=True`（幂等）。
    """
    try:
        result = KnowledgeIngestor(db).delete(document_id)
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.commit()
    return result


@router.get("/search")
def search(q: str = Query(...), limit: int = Query(5), db: Session = Depends(get_db)):
    """知识库检索搜索

    Args:
        q: 搜索关键词
        limit: 返回结果数量，默认5
        db: 数据库会话

    Returns:
        匹配的知识块列表
    """
    return search_chunks(db, q, limit)


@router.get("/knowledge/search")
def search_knowledge(
    q: str = Query(..., min_length=1),
    limit: int = Query(5, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """名称明确的知识库检索端点；`/api/search` 继续兼容旧客户端。"""
    return search_chunks(db, q, limit)
