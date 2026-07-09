from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aiive.db.base import get_db
from aiive.knowledge.ingestor import KnowledgeIngestor, search_chunks

router = APIRouter(prefix="/api")


class IngestRequest(BaseModel):
    file_path: str = Field(..., min_length=1)


@router.post("/knowledge/ingest")
def ingest(request: IngestRequest, db: Session = Depends(get_db)):
    ingestor = KnowledgeIngestor(db)
    result = ingestor.ingest(request.file_path)
    db.commit()
    return result


@router.get("/search")
def search(q: str = Query(...), limit: int = Query(5), db: Session = Depends(get_db)):
    return search_chunks(db, q, limit)
