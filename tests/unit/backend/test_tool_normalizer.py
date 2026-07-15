"""Phase 1: ToolResultNormalizer 单元测试。

覆盖：
- 内联（小结果，不引用化、不落库）
- 有界引用化（大结果 → Artifact 持久化，content 替换为引用）
- 唯一约束 (turn_record_id, tool_call_id) 冲突时复用既有 ref（幂等）
- 持久化失败时仅返回有界错误预览，绝不传完整大结果
"""
import uuid

import pytest

from aiive.db.models import Artifact
from aiive.runtime.token_models import TokenCount
from aiive.runtime.tool_normalizer import ToolResultNormalizer


class _FakeTokenCounter:
    """可控制估算 token 数的假计数器。"""

    def __init__(self, tokens: int):
        self.tokens = tokens

    def count_messages(self, model, messages, tools=None):
        return TokenCount(
            estimated_tokens=self.tokens,
            safety_margin_tokens=0,
            source="fake",
            confidence="high",
            model=model,
        )


def _normalizer(db_session, tokens: int = 5000) -> ToolResultNormalizer:
    return ToolResultNormalizer(_FakeTokenCounter(tokens), "fake-model")


def test_inline_small_result_not_referenced(db_session):
    """小结果：is_reference=False，内容原样返回，不创建 Artifact。"""
    norm = _normalizer(db_session, tokens=10)
    view = norm.normalize("短结果", "t1", "trace-1", turn_record_id="tr1", tool_call_id="c1")
    assert view.is_reference is False
    assert view.inline == "短结果"
    assert view.to_tool_message_content() == "短结果"
    assert db_session.query(Artifact).count() == 0


def test_large_result_creates_artifact(db_session):
    """大结果：引用化并持久化 Artifact，ToolMessage 可见引用且含稳定 ref。"""
    norm = _normalizer(db_session, tokens=5000)
    big = "X" * 100000
    view = norm.normalize(big, "t1", "trace-1", turn_record_id="tr1", tool_call_id="c1")
    assert view.is_reference is True
    content = view.to_tool_message_content()
    assert "artifact_ref" in content

    arts = db_session.query(Artifact).filter(Artifact.thread_id == "t1").all()
    assert len(arts) == 1
    assert arts[0].tool_call_id == "c1"
    assert arts[0].turn_record_id == "tr1"
    assert arts[0].ref in content
    assert arts[0].content == big  # 原始大结果已落库


def test_idempotent_reuse_on_duplicate(db_session, monkeypatch):
    """唯一约束冲突时复用既有 Artifact.ref，不重复创建。

    生产里每次 normalize 都开独立新会话；此处用同一引擎上的独立会话模拟，
    避免 conftest 共享单会话在 commit/rollback 交错后查询不到已提交行。
    """
    import aiive.runtime.tool_normalizer as tn
    from sqlalchemy.orm import Session as _SA_Session

    engine = db_session.get_bind().engine

    def _fresh_session():
        return _SA_Session(bind=engine)

    monkeypatch.setattr(tn, "SessionLocal", _fresh_session)
    norm = _normalizer(db_session, tokens=5000)
    v1 = norm.normalize("内容A", "t1", "trace", turn_record_id="tr1", tool_call_id="dup")
    v2 = norm.normalize("内容B-不同", "t1", "trace", turn_record_id="tr1", tool_call_id="dup")
    assert v1.reference["artifact_ref"] == v2.reference["artifact_ref"]
    # 仍只持久化一条 Artifact
    assert db_session.query(Artifact).filter(Artifact.thread_id == "t1").count() == 1


def test_persistence_failure_bounded_preview(db_session, monkeypatch):
    """Artifact 持久化失败时，仅返回有界错误预览，绝不传完整大结果。"""
    import aiive.runtime.tool_normalizer as tn

    class _BoomSession:
        def add(self, obj):
            pass

        def commit(self):
            raise RuntimeError("boom")

        def query(self, *args, **kwargs):
            raise RuntimeError("boom")

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(tn, "SessionLocal", lambda: _BoomSession())
    norm = _normalizer(db_session, tokens=5000)
    view = norm.normalize("big", "t1", "trace", turn_record_id="tr1", tool_call_id="c1")
    assert view.is_reference is True
    assert view.reference.get("error") == "persistence_failure"
    assert "artifact_ref" in view.reference
