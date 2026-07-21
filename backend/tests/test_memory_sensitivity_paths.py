"""记忆敏感度在投影、导出和检查器读取链的回归测试。"""
from datetime import datetime, timezone

from aiive.api.routes_inspector import get_context_item_detail, get_context_run
from aiive.db.models import ContextSnapshot, MemoryRecord, Thread
from aiive.memory.core_memory_projection import build_blocks
from aiive.memory.projection import MemoryProjection


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


def test_inspector_redacts_context_snapshot_on_server(db):
    """普通 Inspector 返回前必须在服务端遮蔽上下文正文。"""
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

    assert run["snapshots"][0]["context_items"][0]["content"] == "[已脱敏]"
    assert detail["full_content"] == "[已脱敏]"
