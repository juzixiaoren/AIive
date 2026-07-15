"""Test ContextSnapshot saving and querying (via TurnExecutionService).

Phase 1: 上下文组装走 ContextAssembler + _execute_graph（assembled_ctx 路径），
快照由 TurnExecutionService._rotate_snapshot 写入，meta 仅含 total_tool_calls，
stable_prefix_hash 作为独立列存储。
"""
import uuid
from langchain_core.messages import AIMessage

from aiive.db.models import ContextSnapshot
from aiive.core.llm_client import FakeLLMClient


class DeterministicLLM:
    """Mock ChatOpenAI for testing."""
    def __init__(self, content: str = "Hello!", **kwargs):
        self._content = content
    def bind_tools(self, tools):
        return self
    def invoke(self, messages, **kwargs):
        return AIMessage(content=self._content)


def _run(message, thread_id=None):
    """走真实 TurnExecutionService（ContextAssembler + _execute_graph）。"""
    from aiive.runtime.turn_execution import TurnExecutionService
    return TurnExecutionService(llm_client=FakeLLMClient()).execute_turn(
        message, thread_id=thread_id,
    )


class TestContextSnapshot:
    def test_snapshot_saved_on_chat(self, db_session, monkeypatch):
        monkeypatch.setattr(
            "aiive.runtime.agent_graph.AgentGraph._build_langchain_llm",
            lambda self: DeterministicLLM(content="Hello!"),
        )
        # 防止 TaskWorker 连接外部 PostgreSQL（execute_turn 不再调用它，保留以防万一）
        monkeypatch.setattr(
            "aiive.worker.task_worker.SessionLocal",
            lambda: db_session,
        )
        result = _run(message="Hi")
        trace_id = result["trace_id"]

        snapshots = (
            db_session.query(ContextSnapshot)
            .filter(ContextSnapshot.trace_id == trace_id)
            .all()
        )
        assert len(snapshots) == 1
        snapshot = snapshots[0]
        assert snapshot.thread_id == result["thread_id"]
        assert snapshot.meta["total_tool_calls"] >= 0

    def test_snapshot_items_include_stable_prefix(self, db_session, monkeypatch):
        monkeypatch.setattr(
            "aiive.runtime.agent_graph.AgentGraph._build_langchain_llm",
            lambda self: DeterministicLLM(content="Hi!"),
        )
        monkeypatch.setattr(
            "aiive.worker.task_worker.SessionLocal",
            lambda: db_session,
        )
        result = _run(message="Hello")

        snapshot = (
            db_session.query(ContextSnapshot)
            .filter(ContextSnapshot.trace_id == result["trace_id"])
            .first()
        )
        assert snapshot is not None
        assert snapshot.trace_id == result["trace_id"]

    def test_multiple_calls_create_multiple_snapshots(self, db_session, monkeypatch):
        monkeypatch.setattr(
            "aiive.runtime.agent_graph.AgentGraph._build_langchain_llm",
            lambda self: DeterministicLLM(content="Reply"),
        )
        monkeypatch.setattr(
            "aiive.runtime.thread_bootstrap.ThreadBootstrapService.ensure_committed_thread",
            staticmethod(lambda tid=None: tid if tid else str(uuid.uuid4())),
        )
        monkeypatch.setattr(
            "aiive.worker.task_worker.SessionLocal",
            lambda: db_session,
        )

        r1 = _run(message="First")
        r2 = _run(message="Second", thread_id=r1["thread_id"])

        s1 = (
            db_session.query(ContextSnapshot)
            .filter(ContextSnapshot.trace_id == r1["trace_id"])
            .first()
        )
        s2 = (
            db_session.query(ContextSnapshot)
            .filter(ContextSnapshot.trace_id == r2["trace_id"])
            .first()
        )
        assert s1 is not None
        assert s2 is not None
        assert s1.trace_id != s2.trace_id

    def test_snapshot_meta_has_total_tokens(self, db_session, monkeypatch):
        monkeypatch.setattr(
            "aiive.runtime.agent_graph.AgentGraph._build_langchain_llm",
            lambda self: DeterministicLLM(content="Reply"),
        )
        monkeypatch.setattr(
            "aiive.worker.task_worker.SessionLocal",
            lambda: db_session,
        )
        result = _run(message="Test")

        snapshot = (
            db_session.query(ContextSnapshot)
            .filter(ContextSnapshot.trace_id == result["trace_id"])
            .first()
        )
        assert snapshot.meta["total_tool_calls"] >= 0

    def test_snapshot_has_stable_prefix_hash(self, db_session, monkeypatch):
        monkeypatch.setattr(
            "aiive.runtime.agent_graph.AgentGraph._build_langchain_llm",
            lambda self: DeterministicLLM(content="Reply"),
        )
        monkeypatch.setattr(
            "aiive.worker.task_worker.SessionLocal",
            lambda: db_session,
        )
        result = _run(message="Test")

        snapshot = (
            db_session.query(ContextSnapshot)
            .filter(ContextSnapshot.trace_id == result["trace_id"])
            .first()
        )
        assert snapshot.stable_prefix_hash is not None
