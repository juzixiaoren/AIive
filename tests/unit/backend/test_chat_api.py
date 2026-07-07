import pytest
from fastapi.testclient import TestClient

from aiive.core.llm_client import FakeLLMClient
from aiive.db.base import get_db
from aiive.main import create_app
from aiive.runtime.agent_loop import AgentLoop


class TestAgentLoop:
    def test_returns_reply_and_thread_id(self, db_session):
        llm = FakeLLMClient(fixed_content="Hello, user!")
        loop = AgentLoop(llm, db_session)
        result = loop.run(message="Hi")
        assert result["reply"] == "Hello, user!"
        assert result["thread_id"]
        assert result["trace_id"]

    def test_accepts_custom_thread_id(self, db_session):
        from aiive.db.models import Thread

        thread = Thread(id="thread-42")
        db_session.add(thread)
        db_session.flush()

        llm = FakeLLMClient()
        loop = AgentLoop(llm, db_session)
        result = loop.run(message="Hi", thread_id="thread-42")
        assert result["thread_id"] == "thread-42"

    def test_generates_thread_id_when_not_provided(self, db_session):
        llm = FakeLLMClient()
        loop = AgentLoop(llm, db_session)
        r1 = loop.run(message="A")
        r2 = loop.run(message="B")
        assert r1["thread_id"] != r2["thread_id"]

    def test_continues_existing_thread(self, db_session):
        llm = FakeLLMClient(fixed_content="Reply")
        loop = AgentLoop(llm, db_session)
        r1 = loop.run(message="First")
        r2 = loop.run(message="Second", thread_id=r1["thread_id"])
        assert r2["thread_id"] == r1["thread_id"]
        assert r2["reply"] == "Reply"


class TestChatAPI:
    @pytest.fixture
    def client(self, db_session, monkeypatch):
        from aiive.api import routes_chat

        fake_llm = FakeLLMClient(fixed_content="API reply", fixed_model="test-model")
        monkeypatch.setattr(routes_chat, "_build_client", lambda: fake_llm)

        app = create_app()
        app.dependency_overrides[get_db] = lambda: db_session
        return TestClient(app)

    def test_post_chat_returns_reply(self, client):
        response = client.post("/api/chat", json={"message": "Hello"})
        assert response.status_code == 200
        data = response.json()
        assert data["reply"] == "API reply"
        assert data["thread_id"]
        assert data["trace_id"]

    def test_post_chat_accepts_custom_thread_id(self, client):
        # Create thread first, then API should continue it
        client.post("/api/chat", json={"message": "first"})
        response = client.post(
            "/api/chat",
            json={"message": "Hello", "thread_id": "my-thread"},
        )
        assert response.status_code == 200
        data = response.json()
        # my-thread doesn't exist, so a new thread is created
        assert data["thread_id"]

    def test_post_chat_empty_message_rejected(self, client):
        response = client.post("/api/chat", json={"message": ""})
        assert response.status_code == 422

    def test_post_chat_missing_message_rejected(self, client):
        response = client.post("/api/chat", json={})
        assert response.status_code == 422
