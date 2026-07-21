"""
API路由模块：知识库
- 提供知识文件导入（ingest）接口
- 提供知识块检索搜索接口
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aiive.db.base import get_db
from aiive.knowledge.ingestor import KnowledgeIngestor, search_chunks

router = APIRouter(prefix="/api")


class IngestRequest(BaseModel):
    """知识导入请求体"""
    file_path: str = Field(..., min_length=1)


@router.post("/knowledge/ingest")
def ingest(request: IngestRequest, db: Session = Depends(get_db)):
    """导入知识文件到知识库

    Args:
        request: 包含 file_path 的导入请求
        db: 数据库会话

    Returns:
        导入结果
    """
    ingestor = KnowledgeIngestor(db)
    result = ingestor.ingest(request.file_path)
    db.commit()
    return result


@router.get("/knowledge/{document_id}/source")
def read_source(document_id: str, db: Session = Depends(get_db)):
    """从对象存储读取经过哈希校验的知识原文。"""
    from aiive.db.models import Document

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
