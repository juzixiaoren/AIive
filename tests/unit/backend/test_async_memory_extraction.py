"""Test async memory extraction via OutboxJob enqueue (via TurnExecutionService)."""
from langchain_core.messages import AIMessage

from aiive.db.models import OutboxJob


def _run(message, thread_id=None, llm_client=None):
    """走真实 TurnExecutionService（ContextAssembler + _execute_graph）。"""
    from aiive.core.llm_client import FakeLLMClient
    from aiive.runtime.turn_execution import TurnExecutionService

    return TurnExecutionService(llm_client=llm_client or FakeLLMClient()).execute_turn(
        message, thread_id=thread_id,
    )


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

        result = _run(message="Hi")

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

        result = _run(message="我叫小明")

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

        _run(message="Test message")

        job = db_session.query(OutboxJob).first()
        assert job.payload["user_message"] == "Test message"
        assert job.payload["reply"] == "OK"
