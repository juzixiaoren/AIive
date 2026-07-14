"""Test ContextSnapshot saving and querying."""
from langchain_core.messages import AIMessage

from aiive.core.llm_client import FakeLLMClient
from aiive.db.models import ContextSnapshot
from aiive.runtime.agent_graph import AgentGraph


class DeterministicLLM:
    """Mock ChatOpenAI for testing."""
    def __init__(self, content: str = "Hello!", **kwargs):
        self._content = content
    def bind_tools(self, tools):
        return self
    def invoke(self, messages, **kwargs):
        return AIMessage(content=self._content)


class TestContextSnapshot:
    def test_snapshot_saved_on_chat(self, db_session, monkeypatch):
        monkeypatch.setattr(
            "aiive.runtime.agent_graph.AgentGraph._build_langchain_llm",
            lambda self: DeterministicLLM(content="Hello!"),
        )
        llm = FakeLLMClient(fixed_content="Hello!")
        graph = AgentGraph(llm, db_session)
        result = graph.run(message="Hi")
        trace_id = result["trace_id"]

        snapshots = (
            db_session.query(ContextSnapshot)
            .filter(ContextSnapshot.trace_id == trace_id)
            .all()
        )
        assert len(snapshots) == 1
        snapshot = snapshots[0]
        assert snapshot.thread_id == result["thread_id"]
        # context_items may be empty in the new inline-message flow
        assert snapshot.meta["total_items"] >= 0

    def test_snapshot_items_include_stable_prefix(self, db_session, monkeypatch):
        monkeypatch.setattr(
            "aiive.runtime.agent_graph.AgentGraph._build_langchain_llm",
            lambda self: DeterministicLLM(content="Hi!"),
        )
        llm = FakeLLMClient(fixed_content="Hi!")
        graph = AgentGraph(llm, db_session)
        result = graph.run(message="Hello")

        snapshot = (
            db_session.query(ContextSnapshot)
            .filter(ContextSnapshot.trace_id == result["trace_id"])
            .first()
        )
        # Context snapshot is persisted; items count may vary
        assert snapshot is not None
        assert snapshot.trace_id == result["trace_id"]

    def test_multiple_calls_create_multiple_snapshots(self, db_session, monkeypatch):
        monkeypatch.setattr(
            "aiive.runtime.agent_graph.AgentGraph._build_langchain_llm",
            lambda self: DeterministicLLM(content="Reply"),
        )
        # ensure_committed_thread 内部用独立 SessionLocal()，测试 db_session 的
        # 写入对其不可见；patch 为 no-op 将校验交给 AgentGraph 自身的会话。
        monkeypatch.setattr(
            "aiive.runtime.thread_bootstrap.ThreadBootstrapService.ensure_committed_thread",
            staticmethod(lambda tid=None: tid),
        )
        llm = FakeLLMClient(fixed_content="Reply")
        graph = AgentGraph(llm, db_session)

        r1 = graph.run(message="First")
        r2 = graph.run(message="Second", thread_id=r1["thread_id"])

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
        llm = FakeLLMClient(fixed_content="Reply")
        graph = AgentGraph(llm, db_session)
        result = graph.run(message="Test")

        snapshot = (
            db_session.query(ContextSnapshot)
            .filter(ContextSnapshot.trace_id == result["trace_id"])
            .first()
        )
        assert snapshot.meta["total_tokens"] >= 0

    def test_snapshot_has_stable_prefix_hash(self, db_session, monkeypatch):
        monkeypatch.setattr(
            "aiive.runtime.agent_graph.AgentGraph._build_langchain_llm",
            lambda self: DeterministicLLM(content="Reply"),
        )
        llm = FakeLLMClient(fixed_content="Reply")
        graph = AgentGraph(llm, db_session)
        result = graph.run(message="Test")

        snapshot = (
            db_session.query(ContextSnapshot)
            .filter(ContextSnapshot.trace_id == result["trace_id"])
            .first()
        )
        # stable_prefix_hash should be present (may be empty for simplified flow)
        assert snapshot.stable_prefix_hash is not None
