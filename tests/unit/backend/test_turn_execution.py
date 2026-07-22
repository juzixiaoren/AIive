"""Phase 1: TurnExecutionService 端到端回归测试。

验证 P0 修复（turn_sequence 在 Thread 行锁下严格递增）与快照保留约束
(current<=1 / previous<=1 / audit<=5 / temporary 不持久 / 每线程总数<=7)。
"""
import uuid

import pytest
from langchain_core.messages import AIMessage

from aiive.core.llm_client import FakeLLMClient
from aiive.db.models import ContextSnapshot, Epoch, Event, Segment, Thread, TurnRecord
from aiive.runtime.turn_execution import TurnExecutionService


class _FakeChat:
    """支持 bind_tools/invoke 的假 ChatModel，返回无工具调用的简单回复。"""

    def bind_tools(self, tools):
        return self

    def invoke(self, messages, **kwargs):
        return AIMessage(content="ok")


def _mock_ensure_committed_thread(db_session, thread_id=None):
    if thread_id:
        existing = db_session.get(Thread, thread_id)
        if existing:
            return thread_id
        db_session.add(Thread(id=thread_id))
        db_session.flush()
        return thread_id
    tid = str(uuid.uuid4())
    db_session.add(Thread(id=tid))
    db_session.flush()
    return tid


@pytest.fixture
def patched(monkeypatch, db_session):
    monkeypatch.setattr(
        "aiive.runtime.agent_graph.AgentGraph._build_langchain_llm",
        lambda self: _FakeChat(),
    )
    monkeypatch.setattr(
        "aiive.runtime.turn_execution.ThreadBootstrapService.ensure_committed_thread",
        staticmethod(lambda tid=None: _mock_ensure_committed_thread(db_session, tid)),
    )
    yield db_session


def _run_turn(patched, message, thread_id=None):
    svc = TurnExecutionService(llm_client=FakeLLMClient(), source="user_chat")
    return svc.execute_turn(message, thread_id=thread_id)


def test_turn_sequence_assigned_and_increments(patched):
    """P0: 新 Turn 必须分配严格递增的 turn_sequence，并归属 epoch/segment。"""
    tid = str(uuid.uuid4())
    r1 = _run_turn(patched, "第一条", thread_id=tid)
    r2 = _run_turn(patched, "第二条", thread_id=tid)

    # 使用 patched 夹具（= db_session，与 TurnExecutionService 写入的是同一 SQLite 会话）
    db = patched
    turns = db.query(TurnRecord).filter(TurnRecord.thread_id == tid).order_by(TurnRecord.turn_sequence).all()
    assert len(turns) == 2
    assert turns[0].turn_sequence == 1
    assert turns[1].turn_sequence == 2
    assert turns[0].turn_sequence != turns[1].turn_sequence

    # epoch/segment 在应用层强制非空
    for t in turns:
        assert t.epoch_id is not None
        assert t.segment_id is not None
    epochs = db.query(Epoch).filter(Epoch.thread_id == tid).all()
    segments = db.query(Segment).filter(Segment.thread_id == tid).all()
    assert len(epochs) >= 1
    assert len(segments) >= 1


def test_response_event_id_matches_persisted_llm_response(patched):
    """最终响应的 event_id 必须等于持久化 llm_response 事件的真实 ID。"""
    thread_id = str(uuid.uuid4())
    response = _run_turn(patched, "检查事件标识", thread_id=thread_id)

    event = patched.query(Event).filter(
        Event.thread_id == thread_id,
        Event.event_type == "llm_response",
    ).one()
    assert response["event_id"] == event.id


def test_snapshot_retention_constraints(patched):
    """快照轮换满足: current<=1 / previous<=1 / audit<=5 / temporary 不持久 / 总数<=7。"""
    tid = str(uuid.uuid4())
    for i in range(6):
        _run_turn(patched, f"消息{i}", thread_id=tid)

    db = patched
    snaps = db.query(ContextSnapshot).filter(ContextSnapshot.thread_id == tid).all()
    current = [s for s in snaps if s.retention == "current"]
    previous = [s for s in snaps if s.retention == "previous"]
    audit = [s for s in snaps if s.retention == "audit"]
    temporary = [s for s in snaps if s.retention == "temporary"]

    assert len(current) <= 1
    assert len(previous) <= 1
    assert len(audit) <= 5
    assert len(temporary) == 0  # temporary 不持久化
    assert len(snaps) <= 7
    # 每次 Turn 至少产生一个 current 或 audit 快照
    assert len(snaps) >= 1


def test_tools_node_normalizes_and_maintains_working_state(patched, monkeypatch):
    """缺口 A+B 接线：大型工具结果被规范化引用化，WorkingState 生命周期正确维护。"""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langchain_core.tools import StructuredTool

    from aiive.db.models import Artifact, WorkingState
    from aiive.runtime.agent_graph import AgentGraph
    from aiive.runtime.token_models import TokenCount
    from aiive.runtime.tool_normalizer import ToolResultNormalizer
    from aiive.runtime.working_state import WorkingStateService

    class _FakeTokenCounter:
        def __init__(self, tokens: int):
            self.tokens = tokens

        def count_messages(self, model, messages, tools=None):
            return TokenCount(estimated_tokens=self.tokens, safety_margin_tokens=0, model=model)

    def big_tool(x: str) -> str:
        return "Z" * 50000

    tool = StructuredTool.from_function(
        func=big_tool, name="big_tool", description="返回超大字符串的测试工具",
    )

    class _FakeAssistant:
        def __init__(self):
            self.calls = 0

        def invoke(self, msgs):
            self.calls += 1
            if self.calls == 1:
                return AIMessage(content="", tool_calls=[
                    {"id": "call_1", "name": "big_tool", "args": {"x": "hi"}},
                ])
            return AIMessage(content="done")

    # 绕过 policy_check 的 BLOCK 判定（确保走到 tools 节点）
    class _FakePolicy:
        action = "continue"

    monkeypatch.setattr(
        "aiive.runtime.agent_graph.check_tool_calls",
        lambda *a, **k: _FakePolicy(),
    )

    db = patched
    tid = str(uuid.uuid4())
    turn_id = str(uuid.uuid4())
    ws = WorkingStateService()

    graph = AgentGraph(FakeLLMClient(), db)
    normalizer = ToolResultNormalizer(_FakeTokenCounter(tokens=5000), "fake-model")
    compiled, _records, pending_approvals = graph._build_graph(
        _FakeAssistant(), [tool], None,
        normalizer=normalizer, ws_service=ws,
        thread_id=tid, turn_record_id=turn_id, execution_id="exec-1",
    )
    assert pending_approvals == []
    result = compiled.invoke({"messages": [HumanMessage(content="go")]})

    # 1) ToolMessage 内容被规范化为 artifact 引用
    tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert tool_msgs, "应产生 ToolMessage"
    content = str(tool_msgs[0].content)
    assert "artifact_ref" in content

    # 2) Artifact 已持久化，且 tool_call_id 与 ToolMessage 一致
    artifacts = db.query(Artifact).filter(Artifact.thread_id == tid).all()
    assert len(artifacts) == 1
    assert artifacts[0].tool_call_id == "call_1"
    assert artifacts[0].ref in content

    # 3) WorkingState 生命周期：running 已清空，verified/artifact_refs 已写入
    ws_row = db.query(WorkingState).filter(WorkingState.thread_id == tid).first()
    assert ws_row is not None
    assert ws_row.running_tool_state == []  # 调用后已移除
    assert any(s.get("tool_name") == "big_tool" for s in (ws_row.verified_tool_states or []))
    assert any(a.get("ref") == artifacts[0].ref for a in (ws_row.artifact_refs or []))
