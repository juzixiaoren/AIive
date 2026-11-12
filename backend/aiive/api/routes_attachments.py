"""Authenticated, bounded raw-body uploads (no multipart buffering)."""

from urllib.parse import quote, unquote
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session
from aiive.config import settings
from aiive.db.base import get_db
from aiive.db.models import Attachment, Event
from aiive.runtime.conversation_state import get_current_thread_id
from aiive.runtime import attachments as service
from aiive.storage import object_store

router = APIRouter(prefix="/api/attachments")


@router.post("")
async def upload(request: Request, db: Session = Depends(get_db)):
    thread_id = get_current_thread_id(db)
    service.cleanup(db)
    db.commit()
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > settings.aiive_attachment_max_bytes:
            raise HTTPException(413, "附件超过上传限制")
        data.extend(chunk)
    filename = unquote(request.headers.get("x-filename", ""))
    try:
        mime = service.validate(
            bytes(data), filename, request.headers.get("content-type", "")
        )
    except Exception as error:
        raise HTTPException(422, f"附件无法读取：{error}") from error
    # Metadata first: a failed object write never leaves an untracked object.
    import hashlib

    digest = hashlib.sha256(data).hexdigest()
    key = f"sha256/{digest[:2]}/{digest}"
    upload_event = Event(
        thread_id=thread_id,
        trace_id="",
        event_type="attachment_uploaded",
        payload={"sha256": digest},
    )
    db.add(upload_event)
    db.flush()
    row = Attachment(
        event_id=upload_event.id,
        thread_id=thread_id,
        filename=filename,
        mime=mime,
        size=len(data),
        sha256=digest,
        object_key=key,
    )
    db.add(row)
    db.commit()
    try:
        object_store.put_content_addressed(service.BUCKET, bytes(data))
    except Exception:
        db.delete(row)
        db.commit()
        raise HTTPException(503, "附件存储失败，请重试") from None
    return service.metadata(row)


@router.get("/{aid}")
def metadata(aid: str, db: Session = Depends(get_db)):
    return service.metadata(service.require(db, aid, get_current_thread_id(db)))


@router.get("/{aid}/content")
def download(aid: str, db: Session = Depends(get_db)):
    row = service.require(db, aid, get_current_thread_id(db))
    data = object_store.get_verified(
        object_store.ObjectRef(service.BUCKET, row.object_key), row.sha256
    )
    return Response(
        data,
        media_type=row.mime,
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''"
            + quote(row.filename),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
            "Content-Security-Policy": "sandbox",
        },
    )


@router.delete("/{aid}")
def remove(aid: str, db: Session = Depends(get_db)):
    row = service.require(db, aid, get_current_thread_id(db))
    if row.turn_record_id:
        raise HTTPException(409, "已发送附件请通过 Forget 管理")
    key = row.object_key
    db.delete(row)
    db.flush()
    if not db.query(Attachment).filter(Attachment.object_key == key).first():
        object_store.delete(object_store.ObjectRef(service.BUCKET, key))
    db.commit()
    return {"ok": True}
