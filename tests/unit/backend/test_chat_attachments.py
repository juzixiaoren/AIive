"""Attachment security, persistence and real HTTP SSE flow; only model IO is stubbed."""

import io
import json
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from PIL import Image

from aiive.config import settings
from aiive.db.base import get_db
from aiive.db.models import Attachment, ContextSnapshot, Event, TurnRecord, OutboxJob
from aiive.forget.phase_a_shield import execute_phase_a_shield
from aiive.main import create_app
from aiive.runtime import attachments as service
from aiive.runtime.conversation_state import get_current_thread_id
from aiive.runtime.turn_execution import TurnExecutionService
from aiive.core.llm_client import FakeLLMClient
from aiive.storage import object_store


@pytest.fixture
def db_session(_db_template, tmp_path, monkeypatch):
    # SSE and heartbeat threads must use separate real transactions/sessions.
    import shutil
    import sys
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session, sessionmaker
    import aiive.db.base as base

    path = tmp_path / "attachments.db"
    shutil.copyfile(_db_template, path)
    engine = create_engine(f"sqlite:///{path}")
    session = Session(engine)
    factory = sessionmaker(engine)
    previous = base.SessionLocal
    for name, module in list(sys.modules.items()):
        if (
            name.startswith("aiive.")
            and module is not None
            and getattr(module, "SessionLocal", None) is previous
        ):
            monkeypatch.setattr(module, "SessionLocal", factory)
    try:
        yield session
    finally:
        for name, module in list(sys.modules.items()):
            if (
                name.startswith("aiive.")
                and module is not None
                and getattr(module, "SessionLocal", None) is factory
            ):
                setattr(module, "SessionLocal", previous)
        session.close()
        engine.dispose()


@pytest.fixture
def client(db_session, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "aiive_auth_enabled", False)
    monkeypatch.setattr(object_store, "ROOT", tmp_path / "objects")
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def upload(
    client, data=b"attachment secret evidence", name="report.txt", mime="text/plain"
):
    return client.post(
        "/api/attachments",
        content=data,
        headers={"X-Filename": quote(name), "Content-Type": mime},
    )


@pytest.mark.parametrize(
    "name,data,mime",
    [
        ("a.txt", b"hello", "text/plain"),
        ("a.md", b"# hello", "text/markdown"),
        ("a.html", b"<h1>hello</h1><script>bad()</script>", "text/html"),
        ("a.json", b'{"hello": 1}', "application/json"),
        ("a.csv", b"a,b\n1,2", "text/csv"),
        ("a.tsv", b"a\tb\n1\t2", "text/tab-separated-values"),
    ],
)
def test_formats(client, name, data, mime):
    result = upload(client, data, name, mime)
    assert result.status_code == 200, result.text
    aid = result.json()["id"]
    assert client.get(f"/api/attachments/{aid}/content").content == data
    assert (
        client.get(f"/api/attachments/{aid}").json()["sha256"]
        == result.json()["sha256"]
    )


def test_docx_pdf(client):
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>document</w:t></w:r></w:p></w:document>',
        )
    assert (
        upload(client, package.getvalue(), "a.docx", service.MIMES[".docx"]).status_code
        == 200
    )
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject, DictionaryObject

    writer = PdfWriter()
    page = writer.add_blank_page(300, 300)
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 10 10 Td (hello) Tj ET")
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): writer._add_object(font)}
            )
        }
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    assert (
        upload(client, output.getvalue(), "a.pdf", "application/pdf").status_code == 200
    )


@pytest.mark.parametrize(
    "name,data,mime",
    [
        ("../a.txt", b"hello", "text/plain"),
        ("a.png", b"bad", "image/png"),
        ("a.txt", b"MZexec", "text/plain"),
        ("a.pdf", b"fake", "application/pdf"),
        ("a.txt", b"hello", "image/png"),
        ("a.exe", b"exec", "application/octet-stream"),
    ],
)
def test_reject_unsafe(client, name, data, mime):
    assert upload(client, data, name, mime).status_code == 422


def test_limit_zip_bomb_and_no_vision(client, monkeypatch):
    monkeypatch.setattr(settings, "aiive_attachment_max_bytes", 4)
    assert upload(client, b"12345").status_code == 413
    monkeypatch.setattr(settings, "aiive_attachment_max_bytes", 20 * 1024 * 1024)
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"x" * (41 * 1024 * 1024))
    assert (
        upload(client, package.getvalue(), "a.docx", service.MIMES[".docx"]).status_code
        == 422
    )
    monkeypatch.setattr(settings, "aiive_llm_vision_enabled", False)
    monkeypatch.setattr(settings, "aiive_vision_model", "")
    result = upload(client, b"png", "a.png", "image/png")
    assert result.status_code == 422 and "视觉模型" in result.text


def test_access_and_cleanup(client, db_session, monkeypatch):
    aid = upload(client).json()["id"]
    with pytest.raises(HTTPException):
        service.require(db_session, aid, "foreign-thread")
    assert client.get("/api/attachments/unknown/content").status_code == 404
    row = db_session.get(Attachment, aid)
    row.created_at = datetime.now(timezone.utc) - timedelta(days=2)
    db_session.flush()
    monkeypatch.setattr(object_store, "delete", lambda ref: None)
    assert service.cleanup(db_session) == 1
    assert db_session.get(Attachment, aid) is None


def test_failed_write_and_auth(client, db_session, monkeypatch):
    monkeypatch.setattr(
        object_store,
        "put_content_addressed",
        lambda *args: (_ for _ in ()).throw(OSError("disk")),
    )
    assert upload(client).status_code == 503
    assert db_session.query(Attachment).count() == 0
    monkeypatch.setattr(settings, "aiive_auth_enabled", True)
    assert upload(client).status_code == 401
    assert client.get("/api/attachments/unknown").status_code == 401


class Model:
    def __init__(self, calls):
        self.calls = calls

    def bind_tools(self, tools):
        def reply(messages):
            self.calls.append(messages)
            return AIMessage(content="understood")

        return RunnableLambda(reply)


def test_real_sse_history_followup_idempotency_forget(client, db_session, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "aiive.runtime.agent_graph.AgentGraph._build_langchain_llm",
        lambda self: Model(calls),
    )
    monkeypatch.setattr(
        "aiive.api.routes_chat.default_llm_client", lambda: FakeLLMClient()
    )
    aid = upload(client).json()["id"]
    request = {
        "message": "summarize",
        "turn_id": str(uuid.uuid4()),
        "attachment_ids": [aid],
    }
    result = client.post("/api/chat/stream", json=request)
    assert "event: done" in result.text, result.text
    assert "attachment secret evidence" in str(calls[0])
    assert all(
        not job.payload.get("reply")
        for job in db_session.query(OutboxJob).filter(
            OutboxJob.job_type == "memory_extraction"
        )
    )
    count = db_session.query(TurnRecord).count()
    assert "event: done" in client.post("/api/chat/stream", json=request).text
    assert db_session.query(TurnRecord).count() == count
    tid = get_current_thread_id(db_session)
    history = client.get(f"/api/threads/{tid}/messages").json()["messages"]
    assert history[0]["attachments"][0]["id"] == aid
    assert history[0]["content"] == "summarize"
    assert client.post("/api/chat", json={"message": "follow up"}).status_code == 200
    assert "attachment secret evidence" in str(calls[-1])
    assert (
        client.post("/api/chat", json={**request, "message": "changed"}).status_code
        == 409
    )
    assert "attachment secret evidence" not in str(
        [e.payload for e in db_session.query(Event)]
    )
    assert "attachment secret evidence" not in str(
        [s.meta for s in db_session.query(ContextSnapshot)]
    )
    row = db_session.get(Attachment, aid)
    execute_phase_a_shield(db_session, mode="everywhere", turn_ids=[row.turn_record_id])
    db_session.commit()
    assert client.get(f"/api/attachments/{aid}/content").status_code == 404
    assert not service.prompt_parts(db_session, tid)
    history = client.get(f"/api/threads/{tid}/messages").json()["messages"]
    assert not history


def test_images_and_budget(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "aiive_llm_vision_enabled", True)
    calls = []
    monkeypatch.setattr(
        "aiive.runtime.agent_graph.AgentGraph._build_langchain_llm",
        lambda self: Model(calls),
    )
    monkeypatch.setattr(
        "aiive.api.routes_chat.default_llm_client", lambda: FakeLLMClient()
    )
    for fmt, ext, mime in [
        ("PNG", "png", "image/png"),
        ("JPEG", "jpg", "image/jpeg"),
        ("WEBP", "webp", "image/webp"),
    ]:
        output = io.BytesIO()
        Image.new("RGB", (20, 20), "red").save(output, format=fmt)
        result = upload(client, output.getvalue(), f"a.{ext}", mime)
        assert result.status_code == 200, result.text
        response = client.post(
            "/api/chat",
            json={"message": "image?", "attachment_ids": [result.json()["id"]]},
        )
        assert response.status_code == 200, response.text
    assert "image_url" in str(calls[-1]) and "base64," in str(calls[-1])
    from aiive.runtime.context_budget import ContextBudget
    from aiive.runtime.context_assembler import ContextBudgetExceededError

    service_instance = TurnExecutionService(FakeLLMClient())
    service_instance._budget = ContextBudget(100, 10)
    result = service_instance.execute_turn("too small")
    assert result["error"] == "context_budget_exceeded"


def test_cancel_and_binding(client, db_session, monkeypatch):
    aid = upload(client).json()["id"]
    instance = TurnExecutionService(FakeLLMClient(), attachment_ids=[aid])
    turn, execution, _ = instance._resolve_and_preempt(
        "cancel me", None, str(uuid.uuid4())
    )
    assert db_session.get(Attachment, aid).turn_record_id == turn.id
    instance._finalize_cancelled_turn(turn, execution, "trace", [])
    assert client.get(f"/api/attachments/{aid}/content").status_code == 404
    monkeypatch.setattr(object_store, "delete", lambda ref: None)
    service.cleanup(db_session)
    assert db_session.get(Attachment, aid) is None


def test_binding_rejects_reuse_duplicates_and_foreign_thread(
    client, db_session, monkeypatch
):
    from aiive.db.models import Thread

    calls = []
    monkeypatch.setattr(
        "aiive.runtime.agent_graph.AgentGraph._build_langchain_llm",
        lambda self: Model(calls),
    )
    monkeypatch.setattr(
        "aiive.api.routes_chat.default_llm_client", lambda: FakeLLMClient()
    )
    aid = upload(client).json()["id"]
    assert (
        client.post(
            "/api/chat", json={"message": "first", "attachment_ids": [aid]}
        ).status_code
        == 200
    )
    count = db_session.query(TurnRecord).count()
    assert (
        client.post(
            "/api/chat", json={"message": "reuse", "attachment_ids": [aid]}
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/chat", json={"message": "duplicate", "attachment_ids": [aid, aid]}
        ).status_code
        == 400
    )
    assert db_session.query(TurnRecord).count() == count
    second = upload(client).json()["id"]
    foreign = Thread(id=str(uuid.uuid4()))
    db_session.add(foreign)
    db_session.flush()
    db_session.get(Attachment, second).thread_id = foreign.id
    db_session.commit()
    assert (
        client.post(
            "/api/chat", json={"message": "foreign", "attachment_ids": [second]}
        ).status_code
        == 404
    )
    assert client.get(f"/api/attachments/{second}/content").status_code == 404
    assert db_session.query(TurnRecord).count() == count


def test_gc_preserves_shared_live_object(client, db_session, monkeypatch):
    first = upload(client).json()["id"]
    second = upload(client).json()["id"]
    a, b = db_session.get(Attachment, first), db_session.get(Attachment, second)
    assert a.object_key == b.object_key
    a.created_at = datetime.now(timezone.utc) - timedelta(days=2)
    db_session.flush()

    def forbidden(ref):
        raise AssertionError("live object deleted")

    monkeypatch.setattr(object_store, "delete", forbidden)
    service.cleanup(db_session)
    assert client.get(f"/api/attachments/{second}/content").status_code == 200


def test_forget_pending_upload_cannot_resurrect(client, db_session):
    aid = upload(client).json()["id"]
    row = db_session.get(Attachment, aid)
    source = row.event_id
    execute_phase_a_shield(db_session, mode="everywhere", event_ids=[source])
    db_session.commit()
    assert client.get(f"/api/attachments/{aid}/content").status_code == 404
    with pytest.raises(HTTPException):
        service.require(db_session, aid, row.thread_id)


def test_pending_upload_does_not_taint_other_device_turn(
    client, db_session, monkeypatch
):
    from aiive.db.forget_models import ContentProvenanceRef

    calls = []
    monkeypatch.setattr(
        "aiive.runtime.agent_graph.AgentGraph._build_langchain_llm",
        lambda self: Model(calls),
    )
    monkeypatch.setattr(
        "aiive.api.routes_chat.default_llm_client", lambda: FakeLLMClient()
    )
    aid = upload(client).json()["id"]
    source = db_session.get(Attachment, aid).event_id
    response = client.post(
        "/api/chat", json={"message": "remember my ordinary preference"}
    )
    assert response.status_code == 200
    assert "attachment secret evidence" not in str(calls[-1])
    assert (
        db_session.query(ContentProvenanceRef)
        .filter_by(owner_type="attachment_context")
        .count()
        == 0
    )
    assert any(
        job.payload.get("reply")
        for job in db_session.query(OutboxJob).filter_by(job_type="memory_extraction")
    )
    execute_phase_a_shield(db_session, mode="everywhere", event_ids=[source])
    db_session.commit()
    tid = get_current_thread_id(db_session)
    assert len(client.get(f"/api/threads/{tid}/messages").json()["messages"]) == 2


def test_quotas_reject_before_object_io(client, db_session, monkeypatch):
    aid = upload(client).json()["id"]
    monkeypatch.setattr(settings, "aiive_attachment_pending_max_count", 1)
    assert upload(client, b"second").status_code == 413
    monkeypatch.setattr(settings, "aiive_attachment_pending_max_count", 10)
    monkeypatch.setattr(settings, "aiive_attachment_pending_max_bytes", 25)
    assert upload(client, b"second").status_code == 413
    monkeypatch.setattr(
        settings, "aiive_attachment_pending_max_bytes", 40 * 1024 * 1024
    )
    monkeypatch.setattr(settings, "aiive_attachment_thread_max_bytes", 25)
    assert upload(client, b"second").status_code == 413
    row = db_session.get(Attachment, aid)
    instance = TurnExecutionService(FakeLLMClient(), attachment_ids=[aid])
    turn, _, _ = instance._resolve_and_preempt("inspect", None, str(uuid.uuid4()))
    monkeypatch.setattr(settings, "aiive_attachment_thread_max_bytes", 1)

    def forbidden(*args):
        raise AssertionError("read before quota")

    monkeypatch.setattr(object_store, "get_verified", forbidden)
    with pytest.raises(HTTPException) as error:
        service.prompt_parts(db_session, row.thread_id)
    assert error.value.status_code == 413


def test_context_rejection_can_reupload_draft(client, db_session, monkeypatch):
    from aiive.runtime.context_budget import ContextBudget

    monkeypatch.setattr(
        "aiive.api.routes_chat.default_llm_client", lambda: FakeLLMClient()
    )
    aid = upload(client).json()["id"]
    original = TurnExecutionService._load_context

    def too_small(self, *args, **kwargs):
        self._budget = ContextBudget(100, 10)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(TurnExecutionService, "_load_context", too_small)
    response = client.post(
        "/api/chat/stream", json={"message": "draft text", "attachment_ids": [aid]}
    )
    assert '"draft_recoverable": true' in response.text
    assert "context_budget_exceeded" in response.text
    # The original File is retained by the shared draft model; explicit upload issues a fresh ID.
    fresh = upload(client).json()["id"]
    assert fresh != aid
    monkeypatch.setattr(TurnExecutionService, "_load_context", original)
    calls = []
    monkeypatch.setattr(
        "aiive.runtime.agent_graph.AgentGraph._build_langchain_llm",
        lambda self: Model(calls),
    )
    assert (
        client.post(
            "/api/chat", json={"message": "draft text", "attachment_ids": [fresh]}
        ).status_code
        == 200
    )


def test_gc_commits_before_retryable_object_delete(client, db_session, monkeypatch):
    from aiive.db.forget_models import ForgetAction

    aid = upload(client).json()["id"]
    row = db_session.get(Attachment, aid)
    source, key = row.event_id, row.object_key
    from aiive.db.models import Event

    row.created_at = datetime.now(timezone.utc) - timedelta(days=2)
    db_session.commit()
    assert service.cleanup(db_session) == 1
    assert object_store.exists(object_store.ObjectRef(service.BUCKET, key))
    db_session.rollback()
    assert db_session.get(Attachment, aid) is not None
    service.cleanup(db_session)
    db_session.commit()
    assert db_session.get(Event, source).payload == {}

    def failure(ref):
        raise OSError("retry")

    monkeypatch.setattr(object_store, "delete", failure)
    service.process_cleanup_actions(db_session)
    assert db_session.query(ForgetAction).filter_by(status="failed").count() == 1
    assert object_store.exists(object_store.ObjectRef(service.BUCKET, key))


def test_history_queries_only_page_attachment_sources(client, db_session, monkeypatch):
    from aiive.api.routes_threads import ThreadState

    tid = get_current_thread_id(db_session)
    aid = upload(client).json()["id"]
    row = db_session.get(Attachment, aid)
    monkeypatch.setattr(
        ThreadState,
        "list_thread_messages_page",
        lambda *args, **kwargs: {
            "messages": [{"event_id": "other-event"}],
            "has_more": False,
            "next_cursor": None,
        },
    )
    import aiive.runtime.attachments as attachment_service

    original = attachment_service.visible_rows
    seen = []

    def selected_only(db, rows):
        seen.extend(rows)
        return original(db, rows)

    monkeypatch.setattr(attachment_service, "visible_rows", selected_only)
    assert client.get(f"/api/threads/{tid}/messages").status_code == 200
    assert not seen
