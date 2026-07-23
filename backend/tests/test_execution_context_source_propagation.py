"""方案 C 回归测试：真实消息来源必须从 TurnExecutionContext 贯穿到工具 RunContext 与记忆策略。

历史缺陷：TurnExecutionService 已保存真实来源（user / system_command /
runtime_event），但进入 AgentGraph 时未传递 message_source，导致图内工具
RunContext.source 与记忆信号分类静默退化为 user。引入 TurnExecutionContext
作为单一事实源后，任何调用方都必须整体提供上下文，来源不再可能漏传。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage

from aiive.context.run_context import RunContext
from aiive.core.action_planner import MemorySignalDecision
from aiive.memory.extraction_policy import (
    MessageSource,
    MemoryExtractionPolicy,
    MemorySignalAction,
)
from aiive.runtime import agent_graph as ag_module
from aiive.runtime.agent_graph import AgentGraph
from aiive.runtime.context_assembler import ContextSnapshotData
from aiive.runtime.execution_context import TurnExecutionContext


class _FakeCompiled:
    """替代 LangGraph 编译图，直接返回固定终态，避免真实 LLM 调用。"""

    def invoke(self, state: dict) -> dict:
        return {"messages": [AIMessage(content="ok reply")], "tool_records": []}


class _FakeAssembledCtx:
    """替代 ContextAssembler 输出，仅提供图执行所需最小字段。"""

    messages: list = []
    tools_schema = None
    snapshot = ContextSnapshotData(
        items=[], full_contents={}, stable_prefix_hash="", injected_memory_ids=[],
    )


def _fake_ctx_bundle() -> object:
    bundle = MagicMock()
    bundle.assembled_ctx = _FakeAssembledCtx()
    return bundle


def _build_graph_for_test(
    message_source: MessageSource, monkeypatch: pytest.MonkeyPatch,
) -> tuple[AgentGraph, RunContext]:
    """构造最小 AgentGraph 并捕获图执行时注入工具的 RunContext。"""
    captured: dict[str, RunContext] = {}

    fake_llm = MagicMock()
    fake_llm.default_model = "deepseek-chat"
    fake_llm.api_key = "sk-test"
    fake_llm.base_url = "http://localhost"
    fake_llm.timeout_seconds = 30

    graph = AgentGraph.__new__(AgentGraph)
    graph._llm_client = fake_llm
    graph._db = MagicMock()
    graph._logger = MagicMock()
    graph._thread_state = MagicMock()
    graph._thread_state.get_or_create_thread.return_value = MagicMock(id="thread-1")
    graph._memory_store = MagicMock()
    graph._action_planner = MagicMock()
    graph._action_planner.classify_memory_signal.return_value = MemorySignalDecision(
        action=MemorySignalAction.EXTRACT_ASYNC.value, confidence=0.5, reason="test",
    )

    def _fake_build_langchain_tools(registry, run_context: RunContext):
        captured["run_context"] = run_context
        return []

    def _fake_build_graph(*_args, **_kwargs):
        return _FakeCompiled(), [], []

    monkeypatch.setattr(ag_module, "build_langchain_tools", _fake_build_langchain_tools)
    graph._build_graph = _fake_build_graph  # pyright: ignore[reportAttributeAccessIssue]

    exec_ctx = TurnExecutionContext(
        message_source=message_source,
        thread_id="thread-1",
        turn_id="turn-1",
        turn_record_id="rec-1",
        execution_id="exec-1",
        trace_id="trace-1",
    )
    result = graph._execute_graph(  # pyright: ignore[reportPrivateUsage]
        message="hi", exec_ctx=exec_ctx, ctx_bundle=_fake_ctx_bundle(),
        normalizer=MagicMock(),
    )
    return result, captured["run_context"]


def test_system_command_source_propagates_to_run_context_and_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """system_command 必须进入工具 RunContext.source，且记忆策略按非用户跳过。"""
    result, run_ctx = _build_graph_for_test(MessageSource.SYSTEM_COMMAND, monkeypatch)
    assert run_ctx.source == "system_command"
    assert result.memory_signal.action == MemorySignalAction.SKIP.value


def test_runtime_event_source_propagates_to_run_context_and_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """runtime_event 不得退化为 user，记忆策略同样按非用户跳过。"""
    result, run_ctx = _build_graph_for_test(MessageSource.RUNTIME_EVENT, monkeypatch)
    assert run_ctx.source == "runtime_event"
    assert result.memory_signal.action == MemorySignalAction.SKIP.value


def test_user_source_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """普通 user 来源保持透传，且记忆策略走模型分类（非强制跳过）。"""
    result, run_ctx = _build_graph_for_test(MessageSource.USER, monkeypatch)
    assert run_ctx.source == "user"
    assert result.memory_signal.action == MemorySignalAction.EXTRACT_ASYNC.value


def test_resolve_action_honors_structured_source() -> None:
    """结构化来源守卫：非 user 来源忽略模型分类结果，强制 SKIP。"""
    decision = MemorySignalDecision(
        action=MemorySignalAction.EXTRACT_SYNC.value, confidence=0.9, reason="x",
    )
    action = MemoryExtractionPolicy.resolve_action(
        decision.action, "正文", MessageSource.SYSTEM_COMMAND,
    )
    assert action == MemorySignalAction.SKIP
