"""Test async memory extraction via OutboxJob enqueue."""
from langchain_core.messages import AIMessage

from aiive.core.llm_client import FakeLLMClient
from aiive.db.models import OutboxJob
from aiive.runtime.agent_loop import AgentLoop


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
            "aiive.runtime.agent_loop.AgentLoop._build_langchain_llm",
            lambda self: DeterministicLLM(content="Hello!"),
        )

        llm = FakeLLMClient(fixed_content="Hello!")
        loop = AgentLoop(llm, db_session)
        result = loop.run(message="Hi")

        jobs = (
            db_session.query(OutboxJob)
            .filter(OutboxJob.trace_id == result["trace_id"])
            .all()
        )
        job_types = {j.job_type for j in jobs}
        assert "memory_extraction" in job_types
        assert "steward_extraction" in job_types
        # Status may be pending, completed, or deadletter (outbox processes immediately)
        for j in jobs:
            assert j.status in ("pending", "completed", "deadletter")

    def test_memory_and_steward_both_enqueued(self, db_session, monkeypatch):
        monkeypatch.setattr(
            "aiive.runtime.agent_loop.AgentLoop._build_langchain_llm",
            lambda self: DeterministicLLM(content="Reply"),
        )

        llm = FakeLLMClient(fixed_content="Reply")
        loop = AgentLoop(llm, db_session)
        result = loop.run(message="我叫小明")

        memory_job = (
            db_session.query(OutboxJob)
            .filter(
                OutboxJob.trace_id == result["trace_id"],
                OutboxJob.job_type == "memory_extraction",
            )
            .first()
        )
        steward_job = (
            db_session.query(OutboxJob)
            .filter(
                OutboxJob.trace_id == result["trace_id"],
                OutboxJob.job_type == "steward_extraction",
            )
            .first()
        )
        assert memory_job is not None
        assert steward_job is not None

    def test_jobs_have_correct_payload(self, db_session, monkeypatch):
        monkeypatch.setattr(
            "aiive.runtime.agent_loop.AgentLoop._build_langchain_llm",
            lambda self: DeterministicLLM(content="OK"),
        )

        llm = FakeLLMClient(fixed_content="OK")
        loop = AgentLoop(llm, db_session)
        loop.run(message="Test message")

        job = db_session.query(OutboxJob).first()
        assert job.payload["user_message"] == "Test message"
        assert job.payload["reply"] == "OK"
