"""流式工具调用标识关联回归测试。"""

from aiive.runtime.agent_graph import _StreamToolCallMatcher


def test_stream_tool_call_matcher_reuses_model_call_id_for_start_and_end():
    """开始与结束事件必须使用同一个模型生成的工具调用标识。"""
    matcher = _StreamToolCallMatcher()
    matcher.register_batch([
        {
            "id": "call-memory-1",
            "name": "remember_or_update",
            "args": {"content": "称呼用户为博士"},
        },
    ])

    assert matcher.start(
        "run-1",
        "remember_or_update",
        {"content": "称呼用户为博士"},
    ) == "call-memory-1"
    assert matcher.finish("run-1") == "call-memory-1"


def test_stream_tool_call_matcher_keeps_same_name_calls_in_order():
    """同名工具连续调用时必须按模型调用顺序逐一关联。"""
    matcher = _StreamToolCallMatcher()
    matcher.register_batch([
        {"id": "call-memory-1", "name": "remember_or_update", "args": {"content": "偏好一"}},
        {"id": "call-memory-2", "name": "remember_or_update", "args": {"content": "偏好二"}},
    ])

    assert matcher.start("run-1", "remember_or_update", {"content": "偏好一"}) == "call-memory-1"
    assert matcher.start("run-2", "remember_or_update", {"content": "偏好二"}) == "call-memory-2"
    assert matcher.finish("run-2") == "call-memory-2"
    assert matcher.finish("run-1") == "call-memory-1"


def test_stream_tool_call_matcher_prefers_event_id_for_identical_parallel_calls():
    """同名同参调用乱序开始时必须优先使用事件携带的真实标识。"""
    matcher = _StreamToolCallMatcher()
    matcher.register_batch([
        {"id": "call-1", "name": "echo", "args": {"text": "相同参数"}},
        {"id": "call-2", "name": "echo", "args": {"text": "相同参数"}},
    ])

    assert matcher.start("run-2", "echo", {"text": "相同参数"}, "call-2") == "call-2"
    assert matcher.start("run-1", "echo", {"text": "相同参数"}, "call-1") == "call-1"
    assert matcher.finish("run-2") == "call-2"
    assert matcher.finish("run-1") == "call-1"


def test_stream_tool_call_matcher_preserves_tool_message_fallback_id():
    """开始事件无法匹配时，结束事件仍可回退到 ToolMessage 标识。"""
    matcher = _StreamToolCallMatcher()

    assert matcher.start("run-unknown", "remember_or_update", {}) == ""
    assert matcher.finish("run-unknown", "call-fallback") == "call-fallback"
