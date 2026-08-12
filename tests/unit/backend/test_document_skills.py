"""内置 Skill 与结构化文档处理回归测试。"""
from __future__ import annotations

import zipfile

import pytest
from pypdf import PdfWriter

from aiive.context.run_context import RunContext
from aiive.config import settings
from aiive.db.models import AgentTask, Chunk, Thread
from aiive.knowledge.access import allowed_knowledge_roots, resolve_knowledge_path
from aiive.knowledge.document_reader import DocumentReadError, extract_document
from aiive.knowledge.ingestor import KnowledgeIngestor
from aiive.skills import get_skill, list_skills
from aiive.task_runtime.context_assembler import TaskContextAssembler
from aiive.task_runtime.tools import build_main_agent_registry
from aiive.tools.registry import get_tool_registry


def _write_docx(path, paragraphs: list[str]) -> None:
    body = "".join(
        f'<w:p><w:r><w:t>{text}</w:t></w:r></w:p>' for text in paragraphs
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)


def test_builtin_skill_catalog_is_ready_and_backed_by_skill_files() -> None:
    skills = list_skills()
    assert {skill.skill_id for skill in skills} == {
        "document_processing", "web_research", "knowledge_base", "mcp_management",
    }
    web = get_skill("web_research")
    assert web is not None and web.requires_network is True
    assert "web_search" in web.capabilities
    assert "不可信" in web.instructions
    registry = get_tool_registry()
    assert all(registry.get(capability) is not None for skill in skills for capability in skill.capabilities)


def test_document_reader_extracts_html_without_scripts(tmp_path) -> None:
    source = tmp_path / "page.html"
    source.write_text(
        "<html><head><title>标题</title><script>steal()</script></head>"
        "<body><h1>公开内容</h1><p>第二段</p></body></html>",
        encoding="utf-8",
    )
    document = extract_document(source)
    assert document.doc_type == "html"
    assert "公开内容" in document.text and "第二段" in document.text
    assert "steal" not in document.text


def test_document_reader_and_ingestor_extract_docx_text(db_session, tmp_path) -> None:
    source = tmp_path / "report.docx"
    _write_docx(source, ["季度报告", "收入增长 20%"])

    extracted = extract_document(source)
    result = KnowledgeIngestor(db_session).ingest(str(source))
    chunks = db_session.query(Chunk).filter(Chunk.document_id == result["document_id"]).all()

    assert extracted.doc_type == "docx"
    assert "收入增长 20%" in extracted.text
    assert result["doc_type"] == "docx"
    assert "收入增长 20%" in "\n".join(chunk.content for chunk in chunks)


def test_document_reader_rejects_unknown_binary(tmp_path) -> None:
    source = tmp_path / "payload.bin"
    source.write_bytes(b"\x00\xff\x00\xff")
    try:
        extract_document(source)
    except DocumentReadError as error:
        assert "unsupported_document_type" in str(error)
    else:
        raise AssertionError("unknown binary must not be decoded as text")


def test_pdf_reader_parses_pdf_and_rejects_empty_text_layer(tmp_path) -> None:
    source = tmp_path / "scanned.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with source.open("wb") as stream:
        writer.write(stream)

    with pytest.raises(DocumentReadError, match="document_has_no_extractable_text"):
        extract_document(source)


def test_knowledge_roots_use_settings_and_reject_sibling_paths(
    monkeypatch, tmp_path,
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    sibling = tmp_path / "allowed-evil" / "secret.txt"
    sibling.parent.mkdir()
    sibling.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(settings, "aiive_knowledge_roots", str(allowed))

    assert allowed_knowledge_roots() == (allowed.resolve(),)
    assert resolve_knowledge_path("document.md") == (allowed / "document.md").resolve()
    try:
        resolve_knowledge_path(str(sibling))
    except ValueError as error:
        assert "outside_allowed_roots" in str(error)
    else:
        raise AssertionError("sibling path must not pass the knowledge root boundary")


def test_delegate_task_applies_skill_profile_and_worker_instructions(db_session) -> None:
    thread = Thread(title="skill delegation")
    db_session.add(thread)
    db_session.commit()
    registry = build_main_agent_registry()

    result = registry.execute(
        "delegate_task",
        {"goal": "研究公开资料", "skill_id": "web_research"},
        "trusted_user_command",
        run_context=RunContext(thread_id=thread.id, trace_id="trace"),
    )

    assert result["ok"] is True
    task = db_session.get(AgentTask, result["result"]["task_id"])
    assert task is not None
    scope = task.task_brief["scope"]
    assert scope["allow_network"] is True
    assert {"web_search", "fetch_web_page"}.issubset(scope["allowed_capabilities"])
    messages, snapshot, _refs = TaskContextAssembler(db_session).messages(
        task, [{"capability_id": "web_search"}],
    )
    assert snapshot["skill"]["skill_id"] == "web_research"
    assert "Active Skill: 联网研究" in messages[0]["content"]
