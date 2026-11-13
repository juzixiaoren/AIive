import uuid
from contextlib import contextmanager

import pytest
from fastapi import HTTPException

from aiive.context.run_context import RunContext
from aiive.db.models import Attachment, Event, Thread, TurnRecord
from aiive.forget.phase_a_shield import execute_phase_a_shield
from aiive.storage import object_store
from aiive.tools import attachment_tools as tools


@pytest.fixture
def attachment(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(object_store, "ROOT", tmp_path / "objects")

    @contextmanager
    def session():
        yield db_session

    monkeypatch.setattr(tools, "SessionLocal", session)
    thread = Thread(id=str(uuid.uuid4()))
    db_session.add(thread)
    db_session.flush()
    turn = TurnRecord(
        thread_id=thread.id, turn_id=str(uuid.uuid4()), status="completed"
    )
    event = Event(
        thread_id=thread.id,
        trace_id=str(uuid.uuid4()),
        event_type="user_message",
        payload={"content": "source contract"},
        turn_id=turn.turn_id,
    )
    db_session.add_all([turn, event])
    db_session.flush()
    data = (
        "# Contract\n\n"
        + "\n\n".join(
            "Clause %s payment terms. " % i + "legal words " * 130 for i in range(10)
        )
    ).encode()
    ref, digest = object_store.put_content_addressed("chat-attachments", data, {})
    row = Attachment(
        thread_id=thread.id,
        turn_record_id=turn.id,
        event_id=event.id,
        filename="contract.md",
        mime="text/markdown",
        size=len(data),
        sha256=digest,
        object_key=ref.key,
    )
    db_session.add(row)
    db_session.flush()
    return row, RunContext(thread_id=thread.id, trace_id="test", turn_record_id=turn.id)


def test_read_directory_scope_integrity_forget(attachment, db_session, monkeypatch):
    row, ctx = attachment
    listing = tools.list_thread_attachments(query="source contract", ctx=ctx)
    assert listing["attachments"][0]["attachment_id"] == row.id
    result = tools.read_attachment(row.id, ctx=ctx)
    assert result["total_chunks"] > 2
    assert len(result["chunks"]) == 1 and result["chunks"][0]["token_count"] <= 450
    assert result["chunks"][0]["heading_path"] == ("Contract",)
    assert result["next_chunk_index"] == 1
    query = tools.read_attachment(row.id, query="payment", max_chunks=50, ctx=ctx)
    assert len(query["chunks"]) == 2
    with pytest.raises(HTTPException):
        tools.read_attachment(row.id, ctx=RunContext(thread_id="other", trace_id="x"))
    with pytest.raises(HTTPException):
        tools.read_attachment("invalid", ctx=ctx)
    row.sha256 = "0" * 64
    with pytest.raises(ValueError, match="bytes_unavailable"):
        tools.read_attachment(row.id, ctx=ctx)
    execute_phase_a_shield(db_session, mode="everywhere", turn_ids=[row.turn_record_id])
    db_session.flush()
    assert not tools.list_thread_attachments(ctx=ctx)["attachments"]
    with pytest.raises(HTTPException):
        tools.read_attachment(row.id, ctx=ctx)


def test_registration_boundaries():
    from aiive.task_runtime.tools import build_main_agent_registry
    from aiive.tools.registry import ToolRegistry

    main = build_main_agent_registry()
    assert main.definition("read_attachment")
    assert main.definition("record_attachment_progress") is None
    worker = ToolRegistry()
    tools.register_attachment_tools(worker, worker=True)
    assert worker.definition("record_attachment_progress")


def test_original_message_references_and_token_conversion(
    attachment, db_session, monkeypatch
):
    from aiive.runtime.thread_state import ThreadState
    from aiive.runtime.context_assembler import ContextAssembler

    row, ctx = attachment
    event = db_session.get(Event, row.event_id)
    other = Event(
        thread_id=ctx.thread_id,
        trace_id="x",
        event_type="user_message",
        payload={"content": "normal chat"},
    )
    db_session.add(other)
    db_session.flush()
    monkeypatch.setattr(
        object_store,
        "get_verified",
        lambda *a: pytest.fail("metadata must not read originals"),
    )
    messages = ThreadState._events_to_dicts([event, other])
    assert messages[0]["attachments"][0]["id"] == row.id
    assert not messages[1]["attachments"]
    for convert in (
        ThreadState._dicts_to_chat_messages,
        ContextAssembler._dicts_to_chat_messages,
    ):
        result = convert(messages)
        assert row.id in result[0]["content"]
        assert row.id not in result[1]["content"]
    assert tools.list_thread_attachments(ctx=ctx)["total"] == 1


def test_worker_progress_checkpoint_resume_and_forget(attachment, db_session):
    from aiive.task_runtime.repository import TaskRepository
    from aiive.task_runtime.context_assembler import TaskContextAssembler
    from aiive.task_runtime.read_repository import TaskReadRepository

    row, ctx = attachment
    repo = TaskRepository(db_session)
    task = repo.create_task(
        thread_id=ctx.thread_id,
        goal="process attachment " + row.id,
        task_brief={"initial_input": {"skill_id": "document_processing"}},
    )
    ctx.task_id = task.id
    first = tools.read_attachment(row.id, ctx=ctx)
    total = first["total_chunks"]
    with pytest.raises(ValueError, match="invalid_attachment_progress"):
        tools.record_attachment_progress(row.id, 2, total, "skip", ctx)
    saved = tools.record_attachment_progress(
        row.id, 1, total, "section Contract, lines 1 onwards", ctx
    )
    assert tools.record_attachment_progress(row.id, 1, total, "duplicate", ctx) == saved
    assert (
        repo.latest_checkpoint(task.id).state["task_state"]["attachment_processing"][
            row.id
        ]["next_chunk_index"]
        == 1
    )
    # Recreate context/reader: the durable state is the only resume input.
    db_session.expire_all()
    snapshot = TaskContextAssembler(db_session).snapshot(task, [])
    assert (
        snapshot["task"]["state"]["attachment_processing"][row.id]["next_chunk_index"]
        == 1
    )
    for i in range(1, total):
        page = tools.read_attachment(row.id, chunk_index=i, ctx=ctx)
        assert page["chunks"][0]["chunk_index"] == i
        tools.record_attachment_progress(
            row.id, i + 1, total, f"processed through {i}", ctx
        )
    assert (
        task.task_state["attachment_processing"][row.id]["processed_chunk_count"]
        == total
    )
    execute_phase_a_shield(db_session, mode="everywhere", turn_ids=[row.turn_record_id])
    db_session.flush()
    assert not TaskReadRepository(db_session).task_visible(task)
    with pytest.raises(ValueError, match="task_unavailable"):
        tools.read_attachment(row.id, ctx=ctx)


def test_image_calls_vision_without_persisting_base64(
    attachment, db_session, monkeypatch
):
    from types import SimpleNamespace
    from aiive.config import settings
    from aiive.core.llm_client import LLMClient

    row, ctx = attachment
    from PIL import Image
    import io

    output = io.BytesIO()
    Image.new("RGB", (10, 10), "red").save(output, format="PNG")
    data = output.getvalue()
    ref, digest = object_store.put_content_addressed("chat-attachments", data, {})
    row.object_key, row.sha256, row.size = ref.key, digest, len(data)
    row.filename, row.mime = "image.png", "image/png"
    monkeypatch.setattr(settings, "aiive_vision_model", "test-vision")
    seen = []

    def chat(self, messages, **kwargs):
        seen.extend(messages)
        return SimpleNamespace(content="red square", model="test-vision")

    monkeypatch.setattr(LLMClient, "chat", chat)
    result = tools.read_attachment(row.id, query="what color?", ctx=ctx)
    assert "base64," in str(seen) and "what color?" in str(seen)
    assert result["analysis"] == "red square"
    assert "base64" not in str(result)
    assert "base64" not in str([e.payload for e in db_session.query(Event)])
    monkeypatch.setattr(settings, "aiive_vision_model", "")
    monkeypatch.setattr(settings, "aiive_llm_vision_enabled", False)
    with pytest.raises(ValueError, match="vision_not_configured"):
        tools.read_attachment(row.id, ctx=ctx)


def test_chunk_evidence_remains_available_to_worker(attachment, db_session):
    from aiive.task_runtime.evidence import _summary
    from aiive.runtime.tool_output_guard import guard_tool_output
    import json

    row, ctx = attachment
    result = tools.read_attachment(row.id, query="legal", max_chunks=2, ctx=ctx)
    text = json.dumps(result)
    assert len(text) > 1200
    assert guard_tool_output("read_attachment", text).content == text
    assert json.loads(_summary("read_attachment", result)) == json.loads(text)


@pytest.mark.parametrize("finish_early", [False, True])
def test_runtime_full_processing_and_partial_guard(
    attachment, db_session, finish_early
):
    from aiive.task_runtime.repository import (
        TaskRepository,
        recover_stale_tasks,
        utcnow,
    )
    from aiive.task_runtime.runtime import PersistentTaskRuntime
    from aiive.task_runtime.schemas import ActionProposal
    from tests.unit.backend.test_persistent_task_runtime import SequenceAgent
    from datetime import timedelta

    row, ctx = attachment
    data = b"# First\n\nfirst clause payment.\n\n# Second\n\nsecond clause liability."
    ref, digest = object_store.put_content_addressed("chat-attachments", data, {})
    row.object_key, row.sha256, row.size = ref.key, digest, len(data)
    repo = TaskRepository(db_session)
    task = repo.create_task(
        thread_id=ctx.thread_id,
        goal="process attachment " + row.id,
        enqueue=False,
        task_brief={
            "initial_input": {"skill_id": "document_processing"},
            "scope": {"capability_policy": "unified"},
        },
    )
    ctx.task_id = task.id
    page = tools.read_attachment(row.id, ctx=ctx)
    assert page["total_chunks"] == 2
    tools.record_attachment_progress(row.id, 1, 2, "First: payment, lines 3", ctx)
    # Simulate a crashed worker and recover through the actual lease reconciler.
    _, run = repo.start_run(task.id, trigger="test")
    run.lease_expires_at = utcnow() - timedelta(minutes=5)
    db_session.flush()
    assert recover_stale_tasks(db_session) == 1
    proposals = (
        []
        if finish_early
        else [
            ActionProposal(
                decision="action",
                capability_id="read_attachment",
                arguments={"attachment_id": row.id, "chunk_index": 1},
            ),
            ActionProposal(
                decision="action",
                capability_id="record_attachment_progress",
                arguments={
                    "attachment_id": row.id,
                    "next_chunk_index": 2,
                    "total_chunks": 2,
                    "rolling_summary": "First payment lines 3; Second liability lines 7",
                },
            ),
        ]
    )
    proposals.append(
        ActionProposal(
            decision="finish", summary="Processed document", report_status="succeeded"
        )
    )
    result = PersistentTaskRuntime(
        db_session, agent_executor=SequenceAgent(*proposals)
    ).run(task.id)
    assert result["status"] == ("partial" if finish_early else "succeeded")
    if not finish_early:
        assert task.task_state["attachment_processing"][row.id]["next_chunk_index"] == 2


def test_packer_evicts_reference_with_turn_directory_survives(attachment, db_session):
    from aiive.runtime.context_overflow import (
        ContextCandidates,
        ContextOverflowPacker,
        HistoryTurn,
    )
    from aiive.runtime.thread_state import ThreadState
    from aiive.runtime.token_models import TokenCount
    from types import SimpleNamespace

    row, ctx = attachment
    original = ThreadState._events_to_dicts([db_session.get(Event, row.event_id)])
    candidates = ContextCandidates(history=[HistoryTurn("T1", original)])

    def render():
        return ThreadState._dicts_to_chat_messages(
            [e for t in candidates.history for e in t.events]
        ) + [{"role": "user", "content": "T2"}], []

    counter = SimpleNamespace(
        count_messages=lambda model, messages, tools: TokenCount(
            sum(len(m["content"]) for m in messages), 0
        )
    )
    messages, _, _, safe = ContextOverflowPacker().pack(
        candidates, render, counter, "test", 10, 1
    )
    assert safe and not candidates.history
    assert row.id not in str(messages)
    assert (
        tools.list_thread_attachments(ctx=ctx)["attachments"][0]["attachment_id"]
        == row.id
    )


def test_query_ranks_sections_and_preserves_pdf_pages(attachment, db_session):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject, DictionaryObject
    import io

    row, ctx = attachment
    writer = PdfWriter()
    for text in ("alpha payment", "beta liability"):
        page = writer.add_blank_page(width=200, height=200)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        stream = DecodedStreamObject()
        stream.set_data(
            f"BT /F1 12 Tf 10 100 Td ({(text + chr(32)) * 300}) Tj ET".encode()
        )
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    data = output.getvalue()
    ref, digest = object_store.put_content_addressed("chat-attachments", data, {})
    row.object_key, row.sha256, row.size = ref.key, digest, len(data)
    row.filename, row.mime = "contract.pdf", "application/pdf"
    result = tools.read_attachment(row.id, query="liability", ctx=ctx)
    assert result["chunks"][0]["page_start"] == 2
    assert "liability" in result["chunks"][0]["content"]


def test_large_task_state_keeps_recovery_cursor():
    from aiive.task_runtime.context_assembler import _compact_task_state

    progress = {
        "att_1": {
            "next_chunk_index": 5,
            "total_chunks": 10,
            "rolling_summary": "chapter summary",
        }
    }
    state = _compact_task_state(
        {"attachment_processing": progress, "large_other_field": "x" * 40000}
    )
    assert state["attachment_processing"] == progress


def test_worker_audit_snapshot_keeps_refs_without_page_body():
    from aiive.task_runtime.agent_executor import attachment_reference_snapshot

    snapshot = {
        "actions": [
            {
                "id": "a",
                "capability_id": "read_attachment",
                "result_summary": {"summary": "private page text"},
            }
        ],
        "evidence": [{"id": "e", "action_id": "a", "summary": "private page text"}],
    }
    audit = attachment_reference_snapshot(snapshot)
    assert "private page text" not in str(audit)
    assert audit["evidence"][0]["id"] == "e"
    assert snapshot["evidence"][0]["summary"] == "private page text"
