"""身份类记忆的回归测试：约束进入工具 schema，且 Runtime Identity 正确拆分。

核心防回归点：
- remember_or_update 工具的 content/memory_key 字段描述必须告诉模型身份键用纯值、
  关系风格走 agent.persona.relationship（避免再次把整句塞进 user_display_name）。
- Runtime Identity 渲染把称呼（user_display_name）与关系风格（relationship_style）分开。
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


def test_runtime_identity_separates_display_name_and_relationship():
    """称呼与关系风格必须分两行，且关系表述不得进入 display name。"""
    text = _build_runtime_identity({
        "user_display_name": "博士",
        "relationship_style": "user addresses agent as 主人",
    })
    assert "user_display_name: 博士" in text
    assert "relationship_style:" in text
    # 关系表述不应出现在 user_display_name 行
    assert "我的主人" not in text.split("relationship_style:")[0]


def test_runtime_identity_omits_empty_relationship():
    """没有关系风格时不渲染 relationship_style 行。"""
    text = _build_runtime_identity({"user_display_name": "博士"})
    assert "user_display_name: 博士" in text
    assert "relationship_style" not in text
