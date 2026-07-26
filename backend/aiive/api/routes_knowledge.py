"""
API路由模块：知识库
- 提供知识文件导入（ingest）接口
- 提供知识块检索搜索接口
"""
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aiive.db.base import get_db
from aiive.knowledge.ingestor import KnowledgeIngestor, search_chunks

router = APIRouter(prefix="/api")

# 仓库根目录（.../<repo>/backend/aiive/api/routes_knowledge.py → parents[3]）
_REPO_ROOT = Path(__file__).resolve().parents[3]

# 允许导入的根目录列表通过环境变量 AIIVE_KNOWLEDGE_ROOTS 配置
# （多个目录以 os.pathsep 分隔）；未配置时默认仓库根下 .data/knowledge。
# 说明：不放 config.py 是为了避免与全局配置模块产生耦合（该模块另有归属）。
_KNOWLEDGE_ROOTS_ENV = "AIIVE_KNOWLEDGE_ROOTS"


def _allowed_knowledge_roots() -> list[Path]:
    """解析允许导入的根目录（每次请求读取环境变量，便于测试与热配置）。"""
    raw = os.environ.get(_KNOWLEDGE_ROOTS_ENV, "")
    roots: list[Path] = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if part:
            roots.append(Path(part).expanduser().resolve())
    if not roots:
        roots.append((_REPO_ROOT / ".data" / "knowledge").resolve())
    return roots


def _resolve_ingest_path(file_path: str) -> Path:
    """把用户传入的路径 resolve 到允许根目录内，越界一律 400。

    - 相对路径相对第一个允许根解释；
    - 绝对路径与相对路径 resolve（含符号链接/..）后都必须位于允许根之内。
    """
    roots = _allowed_knowledge_roots()
    candidate = Path(file_path).expanduser()
    if not candidate.is_absolute():
        candidate = roots[0] / candidate
    resolved = candidate.resolve()
    for root in roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    allowed = os.pathsep.join(str(r) for r in roots)
    raise HTTPException(
        status_code=400,
        detail=(
            "知识导入路径不在允许的根目录内。"
            f"允许的根目录: {allowed}"
            f"（可通过环境变量 {_KNOWLEDGE_ROOTS_ENV} 配置，多个目录以 '{os.pathsep}' 分隔）"
        ),
    )


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
    resolved = _resolve_ingest_path(request.file_path)
    ingestor = KnowledgeIngestor(db)
    result = ingestor.ingest(str(resolved))
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
