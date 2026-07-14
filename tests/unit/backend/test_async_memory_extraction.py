"""Test async memory extraction via OutboxJob enqueue."""
from langchain_core.messages import AIMessage

from aiive.core.llm_client import FakeLLMClient
from aiive.db.models import OutboxJob
from aiive.runtime.agent_graph import AgentGraph


class DeterministicLLM:
    """Mock ChatOpenAI for testing."""

    def __init__(self, content: str = "Hello!", **kwargs):
        self._content = content

    def bind_tools(self, tools):
        return self

    def invoke(self, messages, **kwargs):
        return AIMessage(content=self._content)


class TestAsyncMemoryExtraction:
    def test_chat_enqueues_memory_jobs(self, db_session, monkeypatch):
        monkeypatch.setattr(
            "aiive.runtime.agent_graph.AgentGraph._build_langchain_llm",
            lambda self: DeterministicLLM(content="Hello!"),
        )

        llm = FakeLLMClient(fixed_content="Hello!")
        graph = AgentGraph(llm, db_session)
        result = graph.run(message="Hi")

        jobs = (
            db_session.query(OutboxJob)
            .filter(OutboxJob.trace_id == result["trace_id"])
            .all()
        )
        job_types = {j.job_type for j in jobs}
        assert "memory_extraction" in job_types
        # steward_extraction 不再独立入队（已合并或移除），仅校验 memory_extraction
        # Status may be pending, completed, or deadletter (outbox processes immediately)
        for j in jobs:
            assert j.status in ("pending", "completed", "deadletter")

    def test_memory_extraction_enqueued(self, db_session, monkeypatch):
        monkeypatch.setattr(
            "aiive.runtime.agent_graph.AgentGraph._build_langchain_llm",
            lambda self: DeterministicLLM(content="Reply"),
        )

        llm = FakeLLMClient(fixed_content="Reply")
        graph = AgentGraph(llm, db_session)
        result = graph.run(message="我叫小明")

        memory_job = (
            db_session.query(OutboxJob)
            .filter(
                OutboxJob.trace_id == result["trace_id"],
                OutboxJob.job_type == "memory_extraction",
            )
            .first()
        )
        assert memory_job is not None
        # steward_extraction 不再独立入队，此处仅校验 memory_extraction 正确排入
        assert memory_job.payload["user_message"] == "我叫小明"
        assert memory_job.payload["reply"] == "Reply"

    def test_jobs_have_correct_payload(self, db_session, monkeypatch):
        monkeypatch.setattr(
            "aiive.runtime.agent_graph.AgentGraph._build_langchain_llm",
            lambda self: DeterministicLLM(content="OK"),
        )

        llm = FakeLLMClient(fixed_content="OK")
        graph = AgentGraph(llm, db_session)
        graph.run(message="Test message")

        job = db_session.query(OutboxJob).first()
        assert job.payload["user_message"] == "Test message"
        assert job.payload["reply"] == "OK"
