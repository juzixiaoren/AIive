from aiive.core.context_builder import ContextBuilder
from aiive.core.llm_client import FakeLLMClient
from aiive.db.models import ContextSnapshot
from aiive.runtime.agent_loop import AgentLoop


class TestContextSnapshot:
    def test_snapshot_saved_on_chat(self, db_session):
        llm = FakeLLMClient(fixed_content="Hello!")
        loop = AgentLoop(llm, db_session)
        result = loop.run(message="Hi")
        trace_id = result["trace_id"]

        snapshots = (
            db_session.query(ContextSnapshot)
            .filter(ContextSnapshot.trace_id == trace_id)
            .all()
        )
        assert len(snapshots) == 1
        snapshot = snapshots[0]
        assert snapshot.thread_id == result["thread_id"]
        assert snapshot.stable_prefix_hash
        assert len(snapshot.context_items) >= 2
        assert snapshot.meta["total_items"] >= 2

    def test_snapshot_items_include_stable_prefix(self, db_session):
        llm = FakeLLMClient(fixed_content="Hi!")
        loop = AgentLoop(llm, db_session)
        result = loop.run(message="Hello")

        snapshot = (
            db_session.query(ContextSnapshot)
            .filter(ContextSnapshot.trace_id == result["trace_id"])
            .first()
        )
        items = snapshot.context_items
        assert items[0]["kind"] == "stable_prefix"
        assert items[0]["trust_level"] == "trusted"

    def test_multiple_calls_create_multiple_snapshots(self, db_session):
        llm = FakeLLMClient(fixed_content="Reply")
        loop = AgentLoop(llm, db_session)

        r1 = loop.run(message="First")
        r2 = loop.run(message="Second", thread_id=r1["thread_id"])

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

    def test_snapshot_meta_has_total_tokens(self, db_session):
        llm = FakeLLMClient(fixed_content="Reply")
        loop = AgentLoop(llm, db_session)
        result = loop.run(message="Test")

        snapshot = (
            db_session.query(ContextSnapshot)
            .filter(ContextSnapshot.trace_id == result["trace_id"])
            .first()
        )
        assert snapshot.meta["total_tokens"] > 0

    def test_snapshot_has_stable_prefix_hash(self, db_session):
        from aiive.core.context_builder import _compute_stable_prefix_hash

        llm = FakeLLMClient(fixed_content="Reply")
        loop = AgentLoop(llm, db_session)
        result = loop.run(message="Test")

        snapshot = (
            db_session.query(ContextSnapshot)
            .filter(ContextSnapshot.trace_id == result["trace_id"])
            .first()
        )
        assert snapshot.stable_prefix_hash == _compute_stable_prefix_hash()
