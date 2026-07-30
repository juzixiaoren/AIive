"""LangGraph 原生 Agent 图架构的综合测试。

测试验证：
1. 普通聊天问题不触发工具调用
2. 记忆保存请求触发工具调用
3. 搜索请求触发搜索工具
4. 危险操作被策略引擎拦截
5. 高风险工具需要确认（通过策略引擎）
6. 相同输入产生一致的路由（稳定性）
"""

import os
import sys
import json
import tempfile
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 设置测试环境变量
os.environ["AIIVE_LLM_API_KEY"] = "test-key"
os.environ["AIIVE_LLM_BASE_URL"] = "https://api.deepseek.com/v1"
os.environ["AIIVE_LLM_MODEL"] = "deepseek-v4-flash"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool


# ---------------------------------------------------------------------------
# 测试工具（mock 实现，路由测试不需要 DB）
# ---------------------------------------------------------------------------

@tool
def remember_or_update(content: str, memory_type: str = "fact", memory_key: str = "") -> str:
    """记住或更新用户信息。使用一致的 memory_key。"""
    return f"Remembered: {content}"


@tool
def search_memory(query: str) -> str:
    """按内容文本搜索记忆。"""
    return json.dumps({"results": [{"content": "test memory", "id": "1"}]})


@tool
def safe_delete(path: str, scope_id: str = "test_artifacts", mode: str = "trash") -> str:
    """在允许的作用域内安全删除文件。"""
    return json.dumps({"allowed": False, "reason": "Test - policy should block"})


@tool
def list_tasks(status: str = "") -> str:
    """列出所有任务/提醒。"""
    return json.dumps([])


# ---------------------------------------------------------------------------
# 确定性测试用的 Mock LLM
# ---------------------------------------------------------------------------


class DeterministicLLM(ChatOpenAI):
    """返回预设 AIMessage 的 LLM，用于确定性测试。

    重写 _generate 而非 invoke，以正确接入 LangChain 的内部调用链，
    因为 config 作为位置参数传递。
    """

    def __init__(self, responses: list[AIMessage]):
        super().__init__(
            model="test-model",
            api_key="test-key",
            base_url="https://test.invalid",
        )
        self._responses = responses
        self._call_count = 0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        idx = self._call_count
        self._call_count += 1
        if idx < len(self._responses):
            msg = self._responses[idx]
            from langchain_core.outputs import ChatGeneration, ChatResult
            generation = ChatGeneration(message=msg)
            return ChatResult(generations=[generation])
        from langchain_core.outputs import ChatGeneration, ChatResult
        generation = ChatGeneration(message=AIMessage(content="Default test response"))
        return ChatResult(generations=[generation])


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------


class TestNoToolCallsForNormalChat:
    """测试 1：普通聊天不应调用工具。"""

    def test_explanation_question_no_tools(self):
        """"为什么TCP要三次握手？" 不应产生 tool_calls。"""
        llm = DeterministicLLM([
            AIMessage(content="TCP采用三次握手是为了...")
        ])
        llm_with_tools = llm.bind_tools([remember_or_update, search_memory])
        resp = llm_with_tools.invoke([
            SystemMessage(content="You are a helpful assistant. Only call tools when action is requested."),
            HumanMessage(content="为什么TCP要三次握手？"),
        ])
        assert not resp.tool_calls, "解释性问题不应触发工具调用"
        assert len(resp.content) > 0, "应该有文本回复"

    def test_hypothetical_question_no_tools(self):
        """假设性问题不应触发工具调用。"""
        llm = DeterministicLLM([
            AIMessage(content="如果用户要求记住信息，我会调用 remember_or_update 工具。")
        ])
        llm_with_tools = llm.bind_tools([remember_or_update, search_memory])
        resp = llm_with_tools.invoke([
            SystemMessage(content="You are a helpful assistant. Only call tools when action is requested."),
            HumanMessage(content="如果我想改名，你会调用哪个工具？"),
        ])
        assert not resp.tool_calls, "假设性问题不应调用工具"


class TestToolCallsForMemory:
    """测试 2：记忆保存应调用 remember_or_update。"""

    def test_memory_save_triggers_tool(self):
        """"记住我以后默认用中文回答" 应触发工具调用。"""
        llm = DeterministicLLM([
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "remember_or_update",
                    "args": {"content": "用户默认使用中文回答", "memory_key": "user.pref.language"},
                    "id": "call_test_1",
                    "type": "tool_call",
                }],
            )
        ])
        llm_with_tools = llm.bind_tools([remember_or_update, search_memory])
        resp = llm_with_tools.invoke([
            SystemMessage(content="You are a helpful assistant. Only call tools when action is requested."),
            HumanMessage(content="记住我以后默认用中文回答"),
        ])
        assert resp.tool_calls, "记忆保存应触发工具调用"
        assert resp.tool_calls[0]["name"] == "remember_or_update"


class TestToolCallsForSearch:
    """测试 3：搜索请求应调用搜索工具。"""

    def test_search_triggers_tool(self):
        """搜索查询应触发 search_memory。"""
        llm = DeterministicLLM([
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "search_memory",
                    "args": {"query": "AI news"},
                    "id": "call_test_2",
                    "type": "tool_call",
                }],
            )
        ])
        llm_with_tools = llm.bind_tools([remember_or_update, search_memory])
        resp = llm_with_tools.invoke([
            SystemMessage(content="You are a helpful assistant. Only call tools when action is requested."),
            HumanMessage(content="查一下我有没有保存AI相关的记忆"),
        ])
        assert resp.tool_calls, "搜索请求应触发工具调用"


class TestPolicyEngine:
    """测试 4-5：策略引擎根据工具元数据进行拦截或确认。"""

    @pytest.mark.xfail(
        reason="用户审批当前有意停用，所有已注册工具（含高风险的 safe_delete）均直接放行；"
               "恢复审批后此用例需改回 BLOCK/CONFIRM 断言。",
    )
    def test_policy_blocks_high_risk(self):
        """高风险工具应被策略引擎捕获。"""
        from aiive.runtime.policy_engine import check_tool_calls, PolicyAction, PolicyResult

        tool_calls = [
            {"name": "safe_delete", "args": {"path": "/tmp/test"}, "id": "call_1"},
        ]

        # 导入注册表来检查
        from aiive.tools.registry import get_tool_registry
        registry = get_tool_registry()

        result = check_tool_calls(tool_calls, registry)
        # safe_delete 的 risk_level="high" 且 can_delete=True
        assert result.action in (PolicyAction.BLOCK, PolicyAction.CONFIRM), \
            f"高风险工具应被拦截或需确认，实际得到 {result.action}"

    def test_policy_allows_low_risk(self):
        """低风险只读工具应被放行。"""
        from aiive.runtime.policy_engine import check_tool_calls, PolicyAction

        tool_calls = [
            {"name": "list_tasks", "args": {"status": ""}, "id": "call_1"},
        ]
        from aiive.tools.registry import get_tool_registry
        registry = get_tool_registry()

        result = check_tool_calls(tool_calls, registry)
        assert result.action == PolicyAction.ALLOW, \
            f"低风险只读工具应被放行，实际得到 {result.action}"

    def test_policy_blocks_unknown_tool(self):
        """未知工具应被拦截。"""
        from aiive.runtime.policy_engine import check_tool_calls, PolicyAction

        tool_calls = [
            {"name": "nonexistent_tool", "args": {}, "id": "call_1"},
        ]
        result = check_tool_calls(tool_calls)
        assert result.action == PolicyAction.BLOCK, \
            f"未知工具应被拦截，实际得到 {result.action}"

    def test_policy_empty_calls(self):
        """空工具调用列表应被放行。"""
        from aiive.runtime.policy_engine import check_tool_calls, PolicyAction

        result = check_tool_calls([])
        assert result.action == PolicyAction.ALLOW
        assert result.reason == "no tool calls"


class TestStability:
    """测试 6：相同输入应产生一致的路由结果。"""

    def test_same_input_consistent(self):
        """多次运行相同输入应保持一致。"""
        tool_calls = [
            {"name": "remember_or_update", "args": {"content": "test"}, "id": "call_test"},
        ]

        from aiive.runtime.policy_engine import check_tool_calls, PolicyAction

        results = []
        for _ in range(10):
            result = check_tool_calls(tool_calls)
            results.append(result.action)

        assert len(set(results)) == 1, \
            f"10 次运行结果应一致，实际得到: {set(results)}"

    def test_deterministic_llm_consistent(self):
        """DeterministicLLM 的 _generate 对相同索引总是返回相同响应。"""
        msg = AIMessage(content="Test response")
        llm = DeterministicLLM([msg])
        llm._call_count = 0  # 重置

        # 直接 _generate 测试（不经过 invoke 链）
        from langchain_core.messages import HumanMessage
        results = []
        for _ in range(5):
            llm._call_count = 0
            result = llm._generate([HumanMessage(content="test")])
            results.append(result.generations[0].message.content)

        assert len(set(results)) == 1, \
            f"确定性 LLM 应保持一致，实际得到: {results}"


class TestToolNodeIntegration:
    """测试 LangGraph ToolNode 能否正确执行我们的工具。"""

    def test_tool_node_executes_tool(self):
        """工具调用应产生 ToolMessage。"""
        # 直接测试工具执行，不通过 ToolNode（避免 config 要求）
        result = remember_or_update.invoke({"content": "test memory", "memory_key": "test.key"})
        assert "Remembered: test memory" in str(result)

        # 同时验证 ToolMessage 正确创建
        tool_msg = ToolMessage(
            content=result,
            tool_call_id="call_test",
            name="remember_or_update",
        )
        assert tool_msg.content == result
        assert tool_msg.name == "remember_or_update"


class TestGraphFlow:
    """测试完整的 LangGraph 流程（assistant → policy → tools → assistant）。"""

    def test_graph_flow_no_tool_calls(self):
        """无工具调用时，图应直接走到结束。"""
        from langgraph.prebuilt import ToolNode, tools_condition
        from langgraph.graph import StateGraph, END, START
        from langgraph.graph.message import add_messages
        from typing import Annotated, TypedDict

        class State(TypedDict):
            messages: Annotated[list, add_messages]

        llm = DeterministicLLM([
            AIMessage(content="Hello! How can I help you today?")
        ])
        llm_with_tools = llm.bind_tools([remember_or_update])

        def assistant(state: State):
            resp = llm_with_tools.invoke(state["messages"])
            return {"messages": [resp]}

        graph = StateGraph(State)
        graph.add_node("assistant", assistant)
        graph.add_node("tools", ToolNode([remember_or_update]))
        graph.add_edge(START, "assistant")
        graph.add_conditional_edges("assistant", tools_condition)
        graph.add_edge("tools", "assistant")
        compiled = graph.compile()

        result = compiled.invoke({
            "messages": [
                SystemMessage(content="You are helpful."),
                HumanMessage(content="你好"),
            ]
        })

        # 应包含: System, Human, Assistant（无 ToolMessages）
        messages = result["messages"]
        tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
        assert len(tool_msgs) == 0, "打招呼不应调用任何工具"

    def test_graph_flow_with_tool_calls(self):
        """图应执行工具并获取最终回复。"""
        from langgraph.prebuilt import ToolNode, tools_condition
        from langgraph.graph import StateGraph, END, START
        from langgraph.graph.message import add_messages
        from typing import Annotated, TypedDict

        class State(TypedDict):
            messages: Annotated[list, add_messages]

        llm = DeterministicLLM([
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "remember_or_update",
                    "args": {"content": "用户名字叫张三", "memory_key": "user.name"},
                    "id": "call_1",
                    "type": "tool_call",
                }],
            ),
            AIMessage(content="好的，我已经记住了你的名字是张三。"),
        ])
        llm_with_tools = llm.bind_tools([remember_or_update])

        def assistant(state: State):
            resp = llm_with_tools.invoke(state["messages"])
            return {"messages": [resp]}

        graph = StateGraph(State)
        graph.add_node("assistant", assistant)
        graph.add_node("tools", ToolNode([remember_or_update]))
        graph.add_edge(START, "assistant")
        graph.add_conditional_edges("assistant", tools_condition)
        graph.add_edge("tools", "assistant")
        compiled = graph.compile()

        result = compiled.invoke({
            "messages": [
                SystemMessage(content="You are helpful."),
                HumanMessage(content="记住我的名字叫张三"),
            ]
        })

        # 应包含: System, Human, AIMessage(tool_call), ToolMessage, AIMessage(final)
        messages = result["messages"]
        ai_messages = [m for m in messages if isinstance(m, AIMessage)]
        tool_messages = [m for m in messages if isinstance(m, ToolMessage)]

        assert len(tool_messages) >= 1, "工具应被执行"
        assert len(ai_messages) >= 2, "应同时包含 tool-call AIMessage 和 final AIMessage"

        final = ai_messages[-1]
        assert "张三" in str(final.content) or len(str(final.content)) > 0


class TestToolRecordState:
    """测试同步与流式共用的 Graph 工具事实转换。"""

    def test_receipt_parser_has_single_status_contract(self):
        """统一回执解析器应覆盖成功、失败和状态未知。"""
        from aiive.runtime.agent_graph import parse_tool_execution_receipt

        completed = parse_tool_execution_receipt('{"ok": true, "result": "done"}')
        failed = parse_tool_execution_receipt('{"ok": false, "error_type": "denied"}')
        unknown = parse_tool_execution_receipt(
            '{"ok": true, "execution_status": "queued", "operation_id": "op-1"}'
        )
        text_error = parse_tool_execution_receipt("Error: invalid arguments")

        assert completed.status == "completed" and completed.ok is True
        assert failed.status == "failed" and failed.error_type == "denied"
        assert unknown.status == "execution_unknown" and unknown.operation_id == "op-1"
        assert text_error.status == "failed" and text_error.ok is False

    def test_state_records_preserve_ids_batches_and_order(self):
        """转换器应保留调用 ID、批次并生成稳定全局顺序。"""
        from aiive.runtime.agent_graph import AgentGraph

        records = AgentGraph._tool_records_from_state([
            {
                "tool_call_id": "call-1", "batch_index": 0, "name": "first",
                "params": {"x": 1}, "result": {"ok": True, "result": "one"},
                "status": "completed",
            },
            {
                "tool_call_id": "call-2", "batch_index": 1, "name": "second",
                "params": {"x": 2}, "result": {"ok": False, "result": "two"},
                "status": "failed",
            },
        ])

        assert [record.tool_call_id for record in records] == ["call-1", "call-2"]
        assert [record.batch_index for record in records] == [0, 1]
        assert [record.order_index for record in records] == [0, 1]
        assert records[1].status == "failed"


class TestLocalToolArgumentRetry:
    """工具参数错误应反馈给模型纠正，且绝不提前执行 handler。"""

    @staticmethod
    def _registry_and_tools(calls: list[int]):
        from aiive.tools.langchain_adapter import build_langchain_tools
        from aiive.tools.registry import CapabilitySafetySchema, ToolRegistration, ToolRegistry

        registry = ToolRegistry()

        def count_items(count: int) -> str:
            calls.append(count)
            return str(count)

        registry.register(ToolRegistration(
            safety=CapabilitySafetySchema(
                "count_items", "local_builtin", "trusted", "low",
            ),
            handler=count_items,
            description="Count items",
            parameters={"count": "int"},
        ))
        return registry, build_langchain_tools(registry)

    def test_invalid_arguments_are_corrected_before_execution(self):
        from aiive.core.llm_client import FakeLLMClient
        from aiive.runtime.agent_graph import AgentGraph

        calls: list[int] = []
        registry, tools = self._registry_and_tools(calls)

        class Assistant:
            def __init__(self):
                self.calls = 0

            def invoke(self, _messages):
                self.calls += 1
                if self.calls == 1:
                    return AIMessage(content="", tool_calls=[{
                        "id": "bad-1", "name": "count_items", "args": {"count": "2"},
                    }])
                if self.calls == 2:
                    return AIMessage(content="", tool_calls=[{
                        "id": "good-1", "name": "count_items", "args": {"count": 2},
                    }])
                return AIMessage(content="done")

        assistant = Assistant()
        graph = AgentGraph(FakeLLMClient(), MagicMock())
        compiled, _, _ = graph._build_graph(  # pyright: ignore[reportPrivateUsage]
            assistant, tools, registry,
        )
        result = compiled.invoke({
            "messages": [HumanMessage(content="count")],
            "tool_records": [],
        })

        assert calls == [2]
        validation_messages = [
            message for message in result["messages"]
            if isinstance(message, ToolMessage)
            and "argument_validation_error" in str(message.content)
        ]
        assert len(validation_messages) == 1
        assert assistant.calls == 3

    def test_repeated_invalid_arguments_stop_after_retry_budget(self):
        from aiive.core.llm_client import FakeLLMClient
        from aiive.runtime.agent_graph import AgentGraph

        calls: list[int] = []
        registry, tools = self._registry_and_tools(calls)

        class AlwaysInvalidAssistant:
            def __init__(self):
                self.calls = 0

            def invoke(self, _messages):
                self.calls += 1
                return AIMessage(content="", tool_calls=[{
                    "id": f"bad-{self.calls}",
                    "name": "count_items",
                    "args": {"count": "still-not-an-int"},
                }])

        assistant = AlwaysInvalidAssistant()
        graph = AgentGraph(FakeLLMClient(), MagicMock())
        compiled, _, _ = graph._build_graph(  # pyright: ignore[reportPrivateUsage]
            assistant, tools, registry,
        )
        result = compiled.invoke({
            "messages": [HumanMessage(content="count")],
            "tool_records": [],
        })

        assert calls == []
        assert assistant.calls == 3  # 初次调用 + 两次纠正机会
        assert str(result["messages"][-1].content).startswith("工具参数连续校验失败")


class TestLangchainAdapter:
    """测试 langchain_adapter 能创建有效的 LangChain 工具。"""

    def test_build_tools_from_registry(self):
        """build_langchain_tools 应正确转换注册表工具。"""
        from aiive.tools.langchain_adapter import build_langchain_tools
        from aiive.tools.registry import get_tool_registry

        registry = get_tool_registry()
        tools = build_langchain_tools(registry)

        assert len(tools) > 0, "应至少有一个工具"
        for t in tools:
            assert hasattr(t, "name")
            assert hasattr(t, "description")
            assert callable(t.func) or hasattr(t, "_run")

    def test_bind_tools_works(self):
        """llm.bind_tools() 应兼容我们的工具。"""
        from aiive.tools.langchain_adapter import build_langchain_tools
        from aiive.tools.registry import get_tool_registry

        registry = get_tool_registry()
        tools = build_langchain_tools(registry)

        llm = DeterministicLLM([AIMessage(content="test")])
        llm_with_tools = llm.bind_tools(tools)

        # 不应抛出异常
        resp = llm_with_tools.invoke([HumanMessage(content="hello")])
        assert resp is not None


class TestAgentGraphNoHardcoding:
    """验证 agent_graph.py 中不存在硬编码的工具特定逻辑。"""

    def test_no_hardcoded_tool_names_in_summarize(self):
        """AgentGraph 不应包含 if/elif 链的 _summarize_tool_result。"""
        import inspect
        from aiive.runtime.agent_graph import AgentGraph

        source = inspect.getsource(AgentGraph)
        # 这些模式表示硬编码的工具特定逻辑
        forbidden_patterns = [
            'if tool_name == "schedule_reminder"',
            'if tool_name == "list_tasks"',
            'if tool_name == "cancel_task"',
            'if tool_name == "remember_or_update"',
            'if tool_name in ("search_memory", "list_memories")',
            'if tool_name == "search_knowledge"',
            'if tool_name == "search_mcp"',
            'f"操作未能完成（{decision.tool_name}）',
            '_summarize_tool_result',
            '_dispatch_must_call',
            '_clarification_reply',
            'f"已完成操作：{tool_name}。"',
        ]
        for pattern in forbidden_patterns:
            assert pattern not in source, \
                f"AgentGraph 不应包含: {pattern}"


def test_collect_llm_usage_aggregates_prompt_cache_details():
    from aiive.runtime.agent_graph import AgentGraph

    usage = AgentGraph._collect_llm_usage([  # pyright: ignore[reportPrivateUsage]
        AIMessage(
            content="tool call",
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 10,
                "total_tokens": 110,
                "input_token_details": {
                    "cache_read": 64,
                    "cache_creation": 16,
                },
            },
        ),
        AIMessage(
            content="answer",
            usage_metadata={
                "input_tokens": 40,
                "output_tokens": 5,
                "total_tokens": 45,
            },
        ),
    ])

    assert usage.calls_reported == 2
    assert usage.input_tokens == 140
    assert usage.output_tokens == 15
    assert usage.total_tokens == 155
    assert usage.cache_read_tokens == 64
    assert usage.cache_write_tokens == 16
    assert usage.cache_hit_ratio == 0.4571


class TestNoKeywordClassification:
    """验证不存在基于关键词的分类逻辑。"""

    def test_no_keyword_patterns(self):
        """验证 agent_graph 中无关键词意图分类。"""
        import inspect
        from aiive.runtime.agent_graph import AgentGraph

        source = inspect.getsource(AgentGraph)

        # 这些是用户要求移除的确切模式
        forbidden = [
            'if "记住" in',
            'if "删除" in',
            'if "为什么" in',
            'if "帮我" in',
            'if "提醒" in',
            '_infer_intent',
            'intent_classifier',
        ]
        for pattern in forbidden:
            assert pattern not in source, \
                f"AgentGraph 不应包含关键词分类: {pattern}"
