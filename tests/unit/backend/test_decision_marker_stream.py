"""内部决策标记的全流式防泄漏（离线，无 DB）。

覆盖：标记位于开头/中间/结尾、任意分片边界、多标记、普通文本、非法与未完成
标记、缓冲有界，以及 SSE token 流与最终持久化助手消息逐字一致。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast, final

import pytest
from aiive.core.llm_client import FakeLLMClient
from aiive.memory.extraction_policy import MessageSource
from aiive.runtime.agent_graph import AgentGraph
from aiive.runtime.execution_context import TurnExecutionContext
from aiive.runtime.turn_events import (
    DECISION_MARKER_MAX_LEN,
    INTERNAL_CONTEXT_HEADER,
    DecisionMarkerStreamFilter,
    decision_from_response,
    remove_decision_markers,
    scan_decision_marker,
)
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

# 含合法标记：清洗后不得残留任何标记片段。
LEAK_FREE_CASES = [
    "[[EVENT_DECISION: continue]]完成",
    "[[EVENT_DECISION: defer]]\n\n稍后处理",
    "[[ EVENT_DECISION : continue ]]完成",
    "先说明一下。[[EVENT_DECISION: no_action]]然后继续。",
    "结论如下。\n[[EVENT_DECISION: continue]]",
    "[[EVENT_DECISION: continue]][[EVENT_DECISION: defer]]完成",
    "  \n[[EVENT_DECISION: continue]]你好",
    "   ",
]

# 非法或未完成：正则不匹配，因此必须原样保留（确定性，不吞字）。
LITERAL_CASES = [
    "普通回答，没有任何标记。",
    "代码片段 arr[0] 与 arr[[1]] 都不是标记",
    "[[EVENT_DECISION: maybe]]这不是合法决策",
    "[[EVENT_DECISION: cont",
    "[[EVENT_DECISION: defer",
    "结尾一个左括号 [",
    "[[",
]


def _stream(parts: list[str]) -> str:
    stream_filter = DecisionMarkerStreamFilter()
    chunks = [stream_filter.feed(part) for part in parts]
    chunks.append(stream_filter.flush())
    return "".join(chunks)


def _all_splits(text: str) -> list[list[str]]:
    """整段、逐字符、以及所有二分切点——覆盖任意分片边界。"""
    splits: list[list[str]] = [[text]]
    splits.extend([text[:index], text[index:]] for index in range(1, len(text)))
    splits.append(list(text))
    return splits


# ---------------------------------------------------------------------------
# 1. 过滤器与最终清洗一致，且不泄漏
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", LEAK_FREE_CASES)
def test_filter_matches_final_cleaning_and_never_leaks(text: str) -> None:
    expected = remove_decision_markers(text)
    assert "EVENT_DECISION" not in expected
    for parts in _all_splits(text):
        assert _stream(parts) == expected, parts


@pytest.mark.parametrize("text", LITERAL_CASES)
def test_invalid_or_incomplete_marker_is_preserved(text: str) -> None:
    expected = remove_decision_markers(text)
    for parts in _all_splits(text):
        assert _stream(parts) == expected, parts


def test_marker_at_start_middle_end() -> None:
    assert _stream(["[[EVENT_DECISION: continue]]", "开始"]) == "开始"
    assert _stream(["前文", "[[EVENT_DECISION: defer]]", "后文"]) == "前文后文"
    assert _stream(["结论", "[[EVENT_DECISION: no_action]]"]) == "结论"


def test_multiple_markers_are_all_stripped() -> None:
    text = (
        "[[EVENT_DECISION: continue]]A"
        "[[EVENT_DECISION: defer]]B"
        "[[EVENT_DECISION: no_action]]C"
    )
    assert _stream(list(text)) == "ABC"


def test_plain_text_is_not_altered() -> None:
    for text in ["普通回答", "arr[0] = 1", "a [[b]] c", "结尾左括号 ["]:
        assert _stream(list(text)) == text


def test_safe_text_is_forwarded_immediately() -> None:
    stream_filter = DecisionMarkerStreamFilter()
    assert stream_filter.feed("你好") == "你好"
    assert stream_filter.feed("，世界") == "，世界"
    assert stream_filter.flush() == ""


# ---------------------------------------------------------------------------
# 2. 有界缓冲与扫描状态
# ---------------------------------------------------------------------------


def test_buffer_is_bounded_for_unfinished_marker() -> None:
    stream_filter = DecisionMarkerStreamFilter()
    emitted = [stream_filter.feed("[") for _ in range(50)]
    assert stream_filter.pending_chars <= DECISION_MARKER_MAX_LEN
    emitted.append(stream_filter.feed("EVENT_DECISION: cont"))
    assert stream_filter.pending_chars <= DECISION_MARKER_MAX_LEN
    emitted.append(stream_filter.flush())
    assert "".join(emitted) == "[" * 50 + "EVENT_DECISION: cont"


def test_overlong_marker_like_text_is_flushed_not_buffered() -> None:
    text = "[[EVENT_DECISION" + " " * 500 + "continue]]"
    stream_filter = DecisionMarkerStreamFilter()
    out = stream_filter.feed(text) + stream_filter.flush()
    assert out == text
    assert stream_filter.pending_chars == 0


def test_scan_decision_marker_states() -> None:
    assert scan_decision_marker("[[") == ("partial", 0)
    assert scan_decision_marker("[[event_decision: def") == ("partial", 0)
    assert scan_decision_marker("[[EVENT_DECISION: defer]]") == ("match", 25)
    assert scan_decision_marker("[[EVENT_DECISION: maybe]]")[0] == "none"
    assert scan_decision_marker("[[EVENT_DECISION" + " " * 9)[0] == "none"
    assert scan_decision_marker("不是标记")[0] == "none"


# ---------------------------------------------------------------------------
# 3. 图执行流的 SSE token 与最终回复一致
# ---------------------------------------------------------------------------


@final
class _FakeCompiled:
    """模拟 astream_events 的 chat model 事件序列（含注入上下文）。"""

    chunks: list[str]
    final_reply: str
    injected: bool
    nested: bool

    def __init__(
        self, *, chunks: list[str], final_reply: str, injected: bool, nested: bool = True,
    ) -> None:
        self.chunks = chunks
        self.final_reply = final_reply
        self.injected = injected
        self.nested = nested

    async def astream_events(self, input_state: Any, version: str = "v2"):
        del input_state, version
        started: list[Any] = [HumanMessage(content="原任务")]
        if self.injected:
            started.insert(0, SystemMessage(content=INTERNAL_CONTEXT_HEADER + "\n事件：goal_trigger_fired"))
        yield {
            "event": "on_chat_model_start",
            "name": "ChatOpenAI",
            "run_id": "run-model",
            "metadata": {"langgraph_node": "assistant"},
            "data": {"input": {"messages": [started]} if self.nested else started},
        }
        for chunk in self.chunks:
            yield {
                "event": "on_chat_model_stream",
                "name": "ChatOpenAI",
                "run_id": "run-model",
                "metadata": {"langgraph_node": "assistant"},
                "data": {"chunk": AIMessage(content=chunk)},
            }
        yield {
            "event": "on_chat_model_end",
            "name": "ChatOpenAI",
            "run_id": "run-model",
            "metadata": {"langgraph_node": "assistant"},
            "data": {"output": AIMessage(content="".join(self.chunks))},
        }
        yield {
            "event": "on_chain_end",
            "name": "LangGraph",
            "run_id": "run-root",
            "parent_ids": [],
            "metadata": {},
            "data": {
                "output": {
                    "messages": [
                        HumanMessage(content="原任务"),
                        AIMessage(content=self.final_reply),
                    ],
                    "tool_records": [],
                    "event_decisions": [],
                }
            },
        }


def _run_stream(
    monkeypatch: pytest.MonkeyPatch,
    *,
    chunks: list[str],
    final_reply: str,
    injected: bool,
    nested: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    graph = AgentGraph(FakeLLMClient(), cast(Any, None))

    def _get_thread(thread_id: str) -> SimpleNamespace:
        return SimpleNamespace(id=thread_id)

    def _prepare(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return {
            "compiled": _FakeCompiled(
                chunks=chunks, final_reply=final_reply, injected=injected, nested=nested,
            ),
            "context_snapshot": None,
            "_records_raw": [],
            "pending_approvals": [],
            "initial_messages": [HumanMessage(content="原任务")],
        }

    def _signal(*args: Any, **kwargs: Any) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(action="skip")

    def _usage(messages: Any) -> None:
        del messages

    monkeypatch.setattr(graph, "_thread_state", SimpleNamespace(get_or_create_thread=_get_thread))
    monkeypatch.setattr(graph, "_prepare_execution", _prepare)
    monkeypatch.setattr(graph, "_classify_memory_signal", _signal)
    monkeypatch.setattr(graph, "_collect_llm_usage", _usage)
    captured: dict[str, Any] = {}

    def _assemble(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(reply=kwargs["reply"], event_decisions=kwargs.get("event_decisions", []))

    monkeypatch.setattr(graph, "_assemble_agent_result", _assemble)
    context = TurnExecutionContext(
        message_source=MessageSource.USER,
        thread_id="thread-1",
        turn_id="turn-1",
        turn_record_id="1",
        execution_id="exec-1",
        trace_id="trace-1",
    )

    async def _collect() -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        async for event in graph._execute_graph_stream("原任务", context):  # pyright: ignore[reportPrivateUsage]
            events.append(event)
        return events

    return asyncio.run(_collect()), captured


def test_sse_tokens_and_final_reply_stay_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    full = "先说明情况。[[EVENT_DECISION: defer]]\n然后继续原任务。[[EVENT_DECISION: continue]]"
    final_reply = decision_from_response(full, [])[0]
    chunks = [full[:3], full[3:9], full[9:17], full[17:26], full[26:]]

    events, captured = _run_stream(
        monkeypatch, chunks=chunks, final_reply=final_reply, injected=True,
    )
    tokens = "".join(event["text"] for event in events if event["type"] == "token")

    assert "EVENT_DECISION" not in tokens
    assert "EVENT_DECISION" not in str(captured["reply"])
    assert tokens == captured["reply"] == final_reply


def test_sse_filter_detects_flat_chat_model_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实 ChatOpenAI 的 on_chat_model_start 输入是扁平消息列表，不是 {"messages": [[...]]}。"""
    full = "[[EVENT_DECISION: continue]]完成"
    events, captured = _run_stream(
        monkeypatch,
        chunks=[full[:8], full[8:]],
        final_reply=remove_decision_markers(full),
        injected=True,
        nested=False,
    )
    tokens = "".join(event["text"] for event in events if event["type"] == "token")
    assert "EVENT_DECISION" not in tokens
    assert tokens == captured["reply"] == "完成"


def test_sse_stream_is_unfiltered_without_injection(monkeypatch: pytest.MonkeyPatch) -> None:
    full = "[[EVENT_DECISION: continue]]普通聊天不受影响"
    events, captured = _run_stream(
        monkeypatch, chunks=[full[:10], full[10:]], final_reply=full, injected=False,
    )
    tokens = "".join(event["text"] for event in events if event["type"] == "token")
    assert tokens == full
    assert captured["reply"] == full
