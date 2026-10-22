"""身份类记忆的回归测试：约束进入工具 schema，且 Runtime Identity 正确拆分。

核心防回归点：
- remember_or_update 工具的 content/memory_key 字段描述必须告诉模型身份键用纯值、
  关系风格走 agent.persona.relationship（避免再次把整句塞进 user_display_name）。
- Runtime Identity 只渲染 Core Memory 投影不了的身份字段（agent_runtime_id /
  agent_display_name / relationship_style）；称呼 user_display_name 由 Core Memory
  投影提供，不得在此重复渲染（见 context_inspector.get_runtime_identity_placeholder）。
"""
import pytest

from aiive.runtime.agent_graph import _build_runtime_identity
from aiive.tools.builtin_tools import register_builtin_tools
from aiive.tools.langchain_adapter import build_langchain_tools
from aiive.tools.registry import ToolRegistry


def _remember_tool():
    reg = ToolRegistry()
    register_builtin_tools(reg)
    tools = build_langchain_tools(reg)
    return next(t for t in tools if t.name == "remember_or_update")


def test_remember_tool_schema_carries_identity_guidance():
    """content / memory_key 的字段描述必须包含身份键纯值与关系键的约束。"""
    rem = _remember_tool()
    schema = rem.args_schema.model_json_schema()
    props = schema["properties"]

    assert "纯值" in props["content"]["description"]
    assert "agent.persona." in props["memory_key"]["description"]
    # 工具级描述也应兜底包含同样约束
    assert "纯值" in rem.description
    assert "agent.persona." in rem.description


def test_runtime_identity_renders_agent_fields_and_relationship_only():
    """Runtime Identity 只渲染 agent 侧字段与关系风格，称呼交给 Core Memory 投影。"""
    text = _build_runtime_identity({
        "agent_runtime_id": "agent-1",
        "agent_display_name": "小忆",
        "user_display_name": "博士",
        "relationship_style": "user addresses agent as 主人",
    })
    assert "agent_runtime_id: agent-1" in text
    assert "agent_display_name: 小忆" in text
    assert "relationship_style: user addresses agent as 主人" in text
    # user_display_name 由 Core Memory 投影提供，此处重复渲染会让同一份数据出现两次
    assert "user_display_name" not in text
    assert "博士" not in text


def test_runtime_identity_omits_empty_relationship():
    """没有可渲染的身份字段时整块为空，不输出空的 relationship_style 行。"""
    text = _build_runtime_identity({"user_display_name": "博士"})
    assert text == ""
    assert "relationship_style" not in text
