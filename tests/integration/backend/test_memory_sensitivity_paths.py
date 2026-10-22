"""记忆敏感度在投影、导出和检查器读取链的回归测试。"""
import hashlib
import json
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from aiive.config import settings

from aiive.api.routes_inspector import get_context_item_detail, get_context_run
from aiive.db.models import ContextSnapshot, MemoryRecord, Thread
from aiive.memory.core_memory_projection import build_blocks
from aiive.memory.projection import MemoryProjection
from aiive.memory.memory_mutation import MemoryMutationExecutor
from aiive.worker.handler_registry import HandlerRegistry
from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome
from aiive.worker.outbox_handlers import handle_memory_markdown_project, register_all


def _memory(db, *, record_id: str, key: str, content: str, sensitivity: str) -> MemoryRecord:
    """创建参与 Core Memory 和导出的 active 记忆。"""
    now = datetime.now(timezone.utc)
    record = MemoryRecord(
        id=record_id,
        memory_type="user_profile",
        canonical_key=key,
        scope_type="global",
        content=content,
        lifecycle_state="active",
        validity_state="valid",
        sensitivity=sensitivity,
        confidence=0.9,
        importance=0.8,
        created_at=now,
        updated_at=now,
    )
    db.add(record)
    db.flush()
    return record


def test_secret_memory_is_excluded_from_core_memory(db):
    """secret 记忆不得进入长期稳定的 LLM Core Memory。"""
    _memory(
        db,
        record_id="secret-core",
        key="user.display_name",
        content="不应进入模型的姓名",
        sensitivity="secret",
    )

    blocks = build_blocks(db)

    assert all("不应进入模型的姓名" not in block.content for block in blocks)
    assert all("secret-core" not in block.source_memory_ids for block in blocks)


def test_secret_memory_is_redacted_in_exports(db):
    """Markdown 和 JSON 导出均不得暴露 secret 原文。"""
    _memory(
        db,
        record_id="secret-export",
        key="user.preference.private",
        content="不应导出的秘密",
        sensitivity="secret",
    )

    projection = MemoryProjection(db)
    markdown = projection.to_markdown()
    payload = projection.to_json()

    assert "不应导出的秘密" not in markdown
    assert "[记忆内容已脱敏]" in markdown
    assert payload[0]["content"] == "[记忆内容已脱敏]"


def test_invalid_memory_is_excluded_from_exports(db):
    """非 valid 的 active 记忆不得进入 Markdown 或 JSON 投影。"""
    record = _memory(
        db,
        record_id="invalid-export",
        key="user.preference.superseded",
        content="已失效的旧偏好",
        sensitivity="normal",
    )
    record.validity_state = "superseded"
    db.flush()

    projection = MemoryProjection(db)

    assert "已失效的旧偏好" not in projection.to_markdown()
    assert all(item["id"] != "invalid-export" for item in projection.to_json())


def test_file_projection_writes_consistent_atomic_snapshot(db, tmp_path, monkeypatch):
    """Handler 从同一数据库快照生成可校验的 Markdown 和 JSON 文件。"""
    _memory(
        db,
        record_id="file-export",
        key="user.preference.language",
        content="偏好中文",
        sensitivity="normal",
    )
    db.commit()
    monkeypatch.setattr(settings, "aiive_memory_file_projection_enabled", True)
    monkeypatch.setattr(settings, "aiive_memory_file_projection_dir", str(tmp_path))

    claimed = ClaimedJob(
        id="projection-job",
        job_type="memory_markdown_project",
        payload={"schema_version": 1, "memory_id": "file-export", "record_version": 1},
        trace_id="file-export",
        retry_count=0,
        max_retries=3,
        claim_token="projection-token",
        schema_version=1,
        worker_id="test-worker",
    )
    result = handle_memory_markdown_project(claimed)

    assert result.outcome == HandlerOutcome.COMPLETED
    markdown = (tmp_path / "memories.md").read_text(encoding="utf-8")
    document = json.loads((tmp_path / "memories.json").read_text(encoding="utf-8"))
    assert "偏好中文" in markdown
    assert document["record_count"] == 1
    assert document["records"][0]["id"] == "file-export"
    assert document["markdown_sha256"] == hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    assert list(tmp_path.glob("*.tmp")) == []


def test_file_projection_enqueue_is_idempotent_and_registered(db, monkeypatch):
    """同一记录版本只入队一个可由 Worker 分派的文件投影任务。"""
    record = _memory(
        db,
        record_id="projection-idempotent",
        key="user.preference.style",
        content="简洁",
        sensitivity="normal",
    )
    monkeypatch.setattr(settings, "aiive_memory_file_projection_enabled", True)
    executor = MemoryMutationExecutor(db)

    executor.enqueue_projection(record, "memory.created")
    executor.enqueue_projection(record, "memory.created")
    db.flush()
    from aiive.db.models import OutboxJob

    jobs = db.query(OutboxJob).filter(
        OutboxJob.job_type == "memory_markdown_project",
    ).all()
    assert len(jobs) == 1
    assert jobs[0].operation_id == "memory_file_projection:projection-idempotent:1"
    registry = HandlerRegistry()
    register_all(registry)
    assert registry.get("memory_markdown_project") is handle_memory_markdown_project
    assert registry.is_schema_supported("memory_markdown_project", 1) is True


def test_inspector_returns_trace_context_without_developer_redaction(db):
    """普通 Inspector 保留真实 Trace 上下文，不复用 Developer 脱敏规则。"""
    db.add(Thread(id="thread-sensitive"))
    db.flush()
    db.add(ContextSnapshot(
        id="snapshot-sensitive",
        trace_id="trace-sensitive",
        thread_id="thread-sensitive",
        stable_prefix_hash="hash",
        context_items=[{"item_id": "user:1", "content": "用户正文"}],
        meta={"full_contents": {"user:1": "完整用户正文"}},
        token_total=10,
    ))
    db.flush()

    run = get_context_run("trace-sensitive", db)
    detail = get_context_item_detail("trace-sensitive", "user:1", db)

    assert run["snapshots"][0]["context_items"][0]["content"] == "用户正文"
    assert detail["full_content"] == "完整用户正文"


def test_inspector_returns_404_for_missing_snapshot_and_item(db):
    """主接口和详情接口的未找到场景使用统一 HTTP 404 契约。"""
    with pytest.raises(HTTPException) as missing_snapshot:
        get_context_run("missing-trace", db)
    assert missing_snapshot.value.status_code == 404
    assert missing_snapshot.value.detail["code"] == "context_snapshot_not_found"

    db.add(Thread(id="thread-context-missing-item"))
    db.add(ContextSnapshot(
        id="snapshot-context-missing-item",
        trace_id="trace-context-missing-item",
        thread_id="thread-context-missing-item",
        stable_prefix_hash="hash",
        context_items=[],
        meta={"full_contents": {}},
        token_total=0,
    ))
    db.flush()

    with pytest.raises(HTTPException) as missing_item:
        get_context_item_detail("trace-context-missing-item", "missing-item", db)
    assert missing_item.value.status_code == 404
    assert missing_item.value.detail["code"] == "context_item_not_found"
