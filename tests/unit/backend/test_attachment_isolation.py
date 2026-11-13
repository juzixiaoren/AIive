"""Explicit Worker grants, cache correctness and the real Vision transaction boundary."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException

from aiive.context.run_context import RunContext
from aiive.db.models import AgentTask, Attachment, Event, Thread, TurnRecord
from aiive.db.forget_models import ContentProvenanceRef
from aiive.forget.phase_a_shield import execute_phase_a_shield
from aiive.runtime import attachment_cache as cache
from aiive.storage import object_store
from aiive.task_runtime.repository import TaskRepository
from aiive.tools import attachment_tools as tools


@pytest.fixture
def files(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(object_store, "ROOT", tmp_path / "objects")
    monkeypatch.setattr(cache, "DOCUMENT_CACHE", cache.DocumentParseCache())

    @contextmanager
    def session():
        yield db_session

    monkeypatch.setattr(tools, "SessionLocal", session)
    thread = Thread(id=str(uuid.uuid4()))
    db_session.add(thread)
    db_session.flush()
    rows = []
    for name in ["A", "B"]:
        turn = TurnRecord(
            thread_id=thread.id, turn_id=str(uuid.uuid4()), status="completed"
        )
        event = Event(
            thread_id=thread.id,
            trace_id=str(uuid.uuid4()),
            event_type="user_message",
            turn_id=turn.turn_id,
            payload={"content": "process " + name},
        )
        db_session.add_all([turn, event])
        db_session.flush()
        data = (
            "# First\n\n"
            + "payment clause. " * 500
            + "\n\n# Second\n\n"
            + "liability clause. " * 500
        ).encode()
        ref, sha = object_store.put_content_addressed("chat-attachments", data, {})
        row = Attachment(
            thread_id=thread.id,
            event_id=event.id,
            turn_record_id=turn.id,
            filename=name + ".md",
            mime="text/markdown",
            size=len(data),
            sha256=sha,
            object_key=ref.key,
        )
        db_session.add(row)
        db_session.flush()
        rows.append(row)
    return rows, RunContext(thread_id=thread.id, trace_id="attachment-test")


def task_for(db, ctx, ids=None, skill="document_processing", goal="全文处理"):
    initial = {"skill_id": skill}
    if ids is not None:
        initial["attachment_ids"] = ids
    task = TaskRepository(db).create_task(
        thread_id=ctx.thread_id,
        goal=goal,
        task_brief={"initial_input": initial},
        enqueue=False,
    )
    return task, RunContext(thread_id=ctx.thread_id, trace_id="worker", task_id=task.id)


def test_explicit_allowlist_list_read_progress_and_main(files, db_session):
    (a, b), main = files
    task, worker = task_for(db_session, main, [a.id])
    assert task.task_state["required_attachment_ids"] == [a.id]
    assert task.task_brief["initial_input"]["attachment_ids"] == [a.id]
    assert [
        r["attachment_id"]
        for r in tools.list_thread_attachments(ctx=worker)["attachments"]
    ] == [a.id]
    with pytest.raises(ValueError, match="attachment_not_authorized"):
        tools.read_attachment(b.id, ctx=worker)
    with pytest.raises(ValueError, match="attachment_not_authorized"):
        tools.record_attachment_progress(b.id, 1, 1, "not allowed", ctx=worker)
    page = tools.read_attachment(a.id, ctx=worker)
    tools.record_attachment_progress(
        a.id, 1, page["total_chunks"], "First payment lines 3", ctx=worker
    )
    assert {
        r["attachment_id"]
        for r in tools.list_thread_attachments(ctx=main)["attachments"]
    } == {a.id, b.id}
    assert tools.read_attachment(b.id, ctx=main)["chunks"]
    refs = (
        db_session.query(ContentProvenanceRef)
        .filter_by(owner_type="agent_task", owner_id=task.id)
        .all()
    )
    assert a.event_id in {r.source_id for r in refs}
    assert b.event_id not in {r.source_id for r in refs}


@pytest.mark.parametrize("skill", ["document_processing", "web_research", ""])
def test_no_grant_or_wrong_skill_cannot_read(files, db_session, skill):
    (a, _), main = files
    task, worker = task_for(db_session, main, skill=skill, goal="attachment " + a.id)
    assert task.task_state["required_attachment_ids"] == []  # Goal text grants nothing.
    if skill == "document_processing":
        assert tools.list_thread_attachments(ctx=worker)["attachments"] == []
        error = "attachment_not_authorized"
    else:
        with pytest.raises(ValueError, match="attachment_skill_required"):
            tools.list_thread_attachments(ctx=worker)
        error = "attachment_skill_required"
    with pytest.raises(ValueError, match=error):
        tools.read_attachment(a.id, ctx=worker)
    with pytest.raises(ValueError, match=error):
        tools.record_attachment_progress(a.id, 1, 1, "x", ctx=worker)


def test_multiple_grants_and_forget_before_cache_access(files, db_session, monkeypatch):
    (a, b), main = files
    task, worker = task_for(db_session, main, [a.id, b.id])
    for row in [a, b]:
        page = tools.read_attachment(row.id, ctx=worker)
        tools.record_attachment_progress(
            row.id, 1, page["total_chunks"], row.filename, ctx=worker
        )
    event_id, turn_id, aid, bid = a.event_id, a.turn_record_id, a.id, b.id
    # Sources are recorded at creation, even if no tool ever read this second task.
    unstarted, other = task_for(db_session, main, [aid])
    refs = (
        db_session.query(ContentProvenanceRef)
        .filter_by(owner_type="agent_task", owner_id=unstarted.id)
        .all()
    )
    assert event_id in {r.source_id for r in refs}
    execute_phase_a_shield(db_session, mode="everywhere", turn_ids=[turn_id])
    db_session.commit()
    monkeypatch.setattr(
        cache.DOCUMENT_CACHE,
        "get",
        lambda *args: pytest.fail("permissions must precede cache lookup"),
    )
    with pytest.raises(HTTPException):
        tools.read_attachment(aid, ctx=main)
    for context in [worker, other]:
        with pytest.raises(ValueError, match="task_unavailable"):
            tools.read_attachment(bid, ctx=context)


@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "cross_thread",
        "upload_event",
        "system",
        "forgotten",
        "duplicate",
        "wrong_skill",
    ],
)
def test_creation_rejects_invalid_grants_atomically(files, db_session, failure):
    (a, _), main = files
    ids, skill = [a.id], "document_processing"
    if failure == "missing":
        ids = ["not-found"]
    elif failure == "cross_thread":
        t = Thread(id=str(uuid.uuid4()))
        db_session.add(t)
        db_session.flush()
        main.thread_id = t.id
    elif failure == "upload_event":
        db_session.get(Event, a.event_id).event_type = "attachment_uploaded"
    elif failure == "system":
        db_session.get(TurnRecord, a.turn_record_id).source = "system_command"
    elif failure == "forgotten":
        execute_phase_a_shield(
            db_session, mode="everywhere", turn_ids=[a.turn_record_id]
        )
    elif failure == "duplicate":
        ids = [a.id, a.id]
    else:
        skill = "web_research"
    db_session.flush()
    count = db_session.query(AgentTask).count()
    with pytest.raises((ValueError, HTTPException)):
        task_for(db_session, main, ids, skill=skill)
    assert db_session.query(AgentTask).count() == count


def test_delegate_persists_structured_grant(files, db_session, monkeypatch):
    from aiive.task_runtime import tools as meta

    (a, _), main = files
    # Preserve the real test session for inspection after the service's finally.
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(meta, "SessionLocal", lambda: db_session)
    result = meta._delegate_task(
        goal="summarize this file",
        skill_id="document_processing",
        attachment_ids=[a.id],
        ctx=main,
    )
    task = db_session.get(AgentTask, result["task_id"])
    assert task.task_state["required_attachment_ids"] == [a.id]
    assert task.task_brief["initial_input"]["attachment_ids"] == [a.id]
    assert (
        "attachment_ids"
        in meta.build_main_agent_registry().definition("delegate_task")["parameters"]
    )


def test_query_zero_match_whitespace_and_eof(files):
    (a, _), main = files
    result = tools.read_attachment(a.id, query="zzzz_does_not_exist", ctx=main)
    assert result["chunks"] == [] and result["matched"] is False
    first = tools.read_attachment(a.id, query=" \n\t ", ctx=main)
    assert first["chunks"][0]["chunk_index"] == 0
    with pytest.raises(ValueError, match="invalid_chunk_index"):
        tools.read_attachment(a.id, chunk_index=first["total_chunks"], ctx=main)


def test_cache_hit_sha_and_versions_invalidate(files, monkeypatch):
    (a, _), main = files
    reads = []
    parses = []
    original_read, original_parse = (
        object_store.get_verified,
        cache.blocks.parse_document_blocks,
    )

    def read(*args):
        reads.append(1)
        return original_read(*args)

    def parse(*args):
        parses.append(1)
        return original_parse(*args)

    monkeypatch.setattr(object_store, "get_verified", read)
    monkeypatch.setattr(cache.blocks, "parse_document_blocks", parse)
    tools.read_attachment(a.id, ctx=main)
    tools.read_attachment(a.id, chunk_index=1, ctx=main)
    assert len(reads) == len(parses) == 1
    monkeypatch.setattr(cache.blocks, "PARSER_VERSION", "next-parser")
    tools.read_attachment(a.id, ctx=main)
    monkeypatch.setattr(cache.chunker, "CHUNKER_VERSION", "next-chunker")
    tools.read_attachment(a.id, ctx=main)
    assert len(reads) == len(parses) == 3
    data = b"new document payment terms"
    ref, sha = object_store.put_content_addressed("chat-attachments", data, {})
    a.object_key, a.sha256 = ref.key, sha
    tools.read_attachment(a.id, ctx=main)
    assert len(reads) == len(parses) == 4
    a.sha256 = "0" * 64
    with pytest.raises(ValueError, match="attachment_bytes_unavailable"):
        tools.read_attachment(a.id, ctx=main)


def test_cache_memory_eviction_oversized_and_singleflight():
    lru = cache.DocumentParseCache(max_bytes=1800, max_entry_bytes=1800, max_entries=2)
    calls = []

    def load():
        calls.append(1)
        return ()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: lru.get(("same",), load), range(50)))
    assert len(calls) == 1
    for i in range(20):
        lru.get((str(i),), load)
        assert lru.bytes_used <= lru.max_bytes
        assert len(lru._entries) <= 2
    lru.get(("same",), load)
    assert len(calls) == 22
    lru.get(("x" * 10000,), load)
    assert len(lru._entries) <= 2 and lru.bytes_used <= 1800
    lru.clear()
    assert lru.bytes_used == 0


@pytest.mark.parametrize("change", ["none", "forget", "revision"])
def test_vision_has_no_transaction_and_discards_changed_sources(
    files, db_session, monkeypatch, change
):
    from aiive.config import settings
    from aiive.core.llm_client import LLMClient
    from PIL import Image
    import io

    (a, _), main = files
    image = io.BytesIO()
    Image.new("RGB", (5, 5), "red").save(image, format="PNG")
    ref, sha = object_store.put_content_addressed(
        "chat-attachments", image.getvalue(), {}
    )
    a.object_key, a.sha256, a.mime, a.filename = ref.key, sha, "image/png", "red.png"
    aid, turn_id = a.id, a.turn_record_id
    monkeypatch.setattr(settings, "aiive_vision_model", "vision-test")

    def chat(self, messages, **kwargs):
        assert not db_session.in_transaction(), (
            "network must not hold a DB transaction/Forget transaction lock"
        )
        assert "base64," in str(messages)
        if change == "forget":
            execute_phase_a_shield(db_session, mode="everywhere", turn_ids=[turn_id])
            db_session.commit()
        elif change == "revision":
            monkeypatch.setattr(tools, "forget_revision", lambda db: 9999)
        return SimpleNamespace(content="red image", model="vision-test")

    monkeypatch.setattr(LLMClient, "chat", chat)
    if change == "none":
        result = tools.read_attachment(aid, ctx=main)
        assert result["analysis"] == "red image" and "base64" not in str(result)
    else:
        with pytest.raises(ValueError, match="attachment_source_changed"):
            tools.read_attachment(aid, ctx=main)


@pytest.mark.parametrize("finish_all", [False, True])
def test_structured_required_ids_drive_full_completion(files, db_session, finish_all):
    from aiive.task_runtime.runtime import PersistentTaskRuntime
    from aiive.task_runtime.schemas import ActionProposal
    from tests.unit.backend.test_persistent_task_runtime import SequenceAgent

    (a, b), main = files
    task, worker = task_for(
        db_session, main, [a.id, b.id], goal="完整分析所有已授权文档"
    )
    for row in [a, b] if finish_all else [a]:
        total = tools.read_attachment(row.id, ctx=worker)["total_chunks"]
        for i in range(total):
            tools.read_attachment(row.id, chunk_index=i, ctx=worker)
            tools.record_attachment_progress(
                row.id,
                i + 1,
                total,
                f"processed {row.filename} through {i}",
                ctx=worker,
            )
    result = PersistentTaskRuntime(
        db_session,
        agent_executor=SequenceAgent(
            ActionProposal(
                decision="finish", report_status="succeeded", summary="完整分析"
            )
        ),
    ).run(task.id)
    assert result["status"] == ("succeeded" if finish_all else "partial")
