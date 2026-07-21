"""测试聊天 API 和 AgentGraph 的请求-响应流程。"""
import uuid

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock

from langchain_core.messages import AIMessage

from aiive.db.base import get_db
from aiive.db.models import Thread
from aiive.main import create_app
from aiive.runtime.agent_graph import AgentGraph


class FailingLLM:
    """模拟 LangChain 调用阶段发生模型超时。"""

    def bind_tools(self, tools):
        return self

    def invoke(self, messages, **kwargs):
        raise TimeoutError("upstream secret timeout")


class DeterministicLLM:
    """返回预设 AIMessage 的 Mock ChatOpenAI。"""

    def __init__(self, content: str = "Hello, user!", **kwargs):
        self._content = content
        self._call_count = 0

    def bind_tools(self, tools):
        """bind_tools 返回 self 以保证链式调用正常工作。"""
        return self

    def invoke(self, messages, **kwargs):
        self._call_count += 1
        return AIMessage(content=self._content)


def _mock_ensure_committed_thread(db_session, thread_id=None):
    """Mock ThreadBootstrapService.ensure_committed_thread，使用测试 session。"""
    if thread_id:
        existing = db_session.get(Thread, thread_id)
        if existing:
            return thread_id
        thread = Thread(id=thread_id)
        db_session.add(thread)
        db_session.flush()
        return thread_id
    new_id = str(uuid.uuid4())
    thread = Thread(id=new_id)
    db_session.add(thread)
    db_session.flush()
    return new_id


class TestAgentGraph:
    """通过真实 TurnExecutionService（含 ContextAssembler + _execute_graph）验证请求-响应。"""

    @staticmethod
    def _run(message, thread_id=None):
        from aiive.core.llm_client import FakeLLMClient
        from aiive.runtime.turn_execution import TurnExecutionService

        return TurnExecutionService(llm_client=FakeLLMClient()).execute_turn(
            message, thread_id=thread_id,
        )

    def test_returns_reply_and_thread_id(self, db_session, monkeypatch):
        """execute_turn 应返回 reply、thread_id 和 trace_id。"""
        mock_llm = DeterministicLLM(content="Hello, user!")

        monkeypatch.setattr(
            "aiive.runtime.agent_graph.AgentGraph._build_langchain_llm",
            lambda self: mock_llm,
        )
        monkeypatch.setattr(
            "aiive.runtime.thread_bootstrap.ThreadBootstrapService.ensure_committed_thread",
            staticmethod(lambda tid=None: _mock_ensure_committed_thread(db_session, tid)),
        )

        result = self._run(message="Hi")
        assert result["reply"] == "Hello, user!"
        assert result["thread_id"]
        assert result["trace_id"]

    def test_accepts_custom_thread_id(self, db_session, monkeypatch):
        """应接受并使用自定义 thread_id。"""
        thread = Thread(id="thread-42")
        db_session.add(thread)
        db_session.flush()

        mock_llm = DeterministicLLM(content="OK")
        monkeypatch.setattr(
            "aiive.runtime.agent_graph.AgentGraph._build_langchain_llm",
            lambda self: mock_llm,
        )
        monkeypatch.setattr(
            "aiive.runtime.thread_bootstrap.ThreadBootstrapService.ensure_committed_thread",
            staticmethod(lambda tid=None: _mock_ensure_committed_thread(db_session, tid)),
        )

        result = self._run(message="Hi", thread_id="thread-42")
        assert result["thread_id"] == "thread-42"

    def test_generates_thread_id_when_not_provided(self, db_session, monkeypatch):
        """未提供 thread_id 时，每次应生成新的。"""
        mock_llm = DeterministicLLM(content="OK")
        monkeypatch.setattr(
            "aiive.runtime.agent_graph.AgentGraph._build_langchain_llm",
            lambda self: mock_llm,
        )
        monkeypatch.setattr(
            "aiive.runtime.thread_bootstrap.ThreadBootstrapService.ensure_committed_thread",
            staticmethod(lambda tid=None: _mock_ensure_committed_thread(db_session, tid)),
        )

        r1 = self._run(message="A")
        r2 = self._run(message="B")
        assert r1["thread_id"] != r2["thread_id"]

    def test_continues_existing_thread(self, db_session, monkeypatch):
        """提供 thread_id 时应继续同一线程。"""
        mock_llm = DeterministicLLM(content="Reply")
        monkeypatch.setattr(
            "aiive.runtime.agent_graph.AgentGraph._build_langchain_llm",
            lambda self: mock_llm,
        )
        monkeypatch.setattr(
            "aiive.runtime.thread_bootstrap.ThreadBootstrapService.ensure_committed_thread",
            staticmethod(lambda tid=None: _mock_ensure_committed_thread(db_session, tid)),
        )

        r1 = self._run(message="First")
        r2 = self._run(message="Second", thread_id=r1["thread_id"])
        assert r2["thread_id"] == r1["thread_id"]
        assert r2["reply"] == "Reply"


class TestChatAPI:
    """测试 /api/chat 端点的请求处理和校验。"""

    @pytest.fixture
    def client(self, db_session, monkeypatch):
        """创建测试用的 HTTP 客户端，注入 mock LLM 和 DB。"""
        from sqlalchemy.orm import sessionmaker as _sm
        from aiive.db import base

        mock_llm = DeterministicLLM(content="API reply")
        monkeypatch.setattr(
            "aiive.runtime.agent_graph.AgentGraph._build_langchain_llm",
            lambda self: mock_llm,
        )
        monkeypatch.setattr(
            "aiive.runtime.thread_bootstrap.ThreadBootstrapService.ensure_committed_thread",
            staticmethod(lambda tid=None: _mock_ensure_committed_thread(db_session, tid)),
        )
        # invoke_chat 内用 from aiive.db.base import SessionLocal 创建独立会话；
        # 替换为 SQLite 工厂，避免连 PostgreSQL。
        test_factory = _sm(bind=db_session.get_bind())
        monkeypatch.setattr(base, "SessionLocal", test_factory)

        app = create_app()
        app.dependency_overrides[get_db] = lambda: db_session
        return TestClient(app)

    def test_post_chat_returns_reply(self, client):
        """POST /api/chat 应返回 reply、thread_id 和 trace_id。"""
        response = client.post("/api/chat", json={"message": "Hello"})
        assert response.status_code == 200
        data = response.json()
        assert data["reply"] == "API reply"
        assert data["thread_id"]
        assert data["trace_id"]
        assert data["action_cards"] == []
        assert data["pending_operations"] == []

    def test_post_chat_accepts_custom_thread_id(self, client):
        """POST /api/chat 应接受自定义 thread_id。"""
        response = client.post(
            "/api/chat",
            json={"message": "Hello", "thread_id": "my-thread"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["thread_id"]

    def test_post_chat_empty_message_rejected(self, client):
        """空消息应被拒绝（422）。"""
        response = client.post("/api/chat", json={"message": ""})
        assert response.status_code == 422

    def test_post_chat_missing_message_rejected(self, client):
        """缺少 message 字段应被拒绝（422）。"""
        response = client.post("/api/chat", json={})
        assert response.status_code == 422

    def test_post_chat_returns_structured_llm_timeout(self, client, monkeypatch):
        """LangChain 超时应返回可重试且带 trace_id 的结构化错误。"""
        monkeypatch.setattr(
            "aiive.runtime.agent_graph.AgentGraph._build_langchain_llm",
            lambda self: FailingLLM(),
        )

        response = client.post("/api/chat", json={"message": "Hello"})

        assert response.status_code == 504
        detail = response.json()["detail"]
        assert detail["code"] == "llm_timeout"
        assert detail["retryable"] is True
        assert detail["trace_id"]
        assert "secret" not in detail["message"]
