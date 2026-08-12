"""
API路由模块：知识库
- 提供知识文件导入（ingest）接口
- 提供知识块检索搜索接口
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session
from typing import Any

from aiive.db.base import get_db
from aiive.db.models import Chunk, Document
from aiive.knowledge.access import allowed_knowledge_roots, resolve_knowledge_path
from aiive.knowledge.ingestor import KnowledgeIngestor, search_chunks

router = APIRouter(prefix="/api")

class IngestRequest(BaseModel):
    """知识导入请求体"""
    file_path: str = Field(..., min_length=1)


@router.post("/knowledge/ingest")
def ingest(request: IngestRequest, db: Session = Depends(get_db)):
    """导入知识文件到知识库（仅允许根目录内的文件，防任意本地文件读取）

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
    result = ingestor.ingest(str(resolved))
    db.commit()
    return result


@router.get("/knowledge")
def list_documents(
    limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db),
) -> dict[str, Any]:
    """列出已导入的持久文档、索引状态和分块数量。"""
    documents = db.query(Document).order_by(Document.created_at.desc()).limit(limit).all()
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
                "doc_type": document.doc_type,
                "mime_type": document.mime_type,
                "status": document.status,
                "content_size": document.content_size,
                "content_hash": document.content_hash,
                "chunks": int(counts.get(document.id, 0)),
                "created_at": document.created_at,
            }
            for document in documents
        ],
        "allowed_roots": [str(root) for root in allowed_knowledge_roots()],
    }


@router.get("/knowledge/{document_id}/source")
def read_source(document_id: str, db: Session = Depends(get_db)):
    """从对象存储读取经过哈希校验的知识原文。"""
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="知识文档不存在")
    try:
        content = KnowledgeIngestor(db).read_source(document_id)
    except LookupError as exc:
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
