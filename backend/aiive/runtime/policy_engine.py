"""
运行时层 - 策略引擎。

当前策略对所有已注册工具直接放行，仅阻止未注册工具。
风险等级、外部写入、删除能力和确认标记仍保留在工具元数据中，
但暂不参与运行时审批判定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from aiive.tools.registry import ToolRegistry, get_tool_registry


class PolicyAction(str, Enum):
    """策略动作枚举。"""
    ALLOW = "allow"       # 允许执行，传递给 ToolNode
    BLOCK = "block"       # 完全拒绝执行
    CONFIRM = "confirm"   # 需要用户确认


@dataclass
class PolicyResult:
    """策略校验结果数据类。

    Attributes:
        action: 策略动作（允许/阻止/确认）
        reason: 决策原因
        blocked_tools: 被阻止的工具名称列表
        confirm_tools: 需要确认的工具名称列表
        allowed_tools: 被允许的工具名称列表
    """
    action: PolicyAction
    reason: str = ""
    blocked_tools: list[str] = field(default_factory=list)
    confirm_tools: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)


# ---- 策略规则（基于工具元数据，非关键词） ----

# TODO: 用户审批当前有意停用。恢复时必须同步启用策略判定、审批节点、前端 UI、测试和文档，禁止仅接通单层逻辑。
# 以下规则仅保留为未来审批策略的元数据定义，当前不得参与 check_tool_calls 判定。
EFFECT_TYPE_RULES = {
    "destructive_write": PolicyAction.CONFIRM,  # can_delete = True
    "external_communication": PolicyAction.CONFIRM,  # writes_external_world = True + 高风险
    "shell_execution": PolicyAction.CONFIRM,
}


def check_tool_calls(
    tool_calls: list[dict[str, Any]],
    registry: ToolRegistry | None = None,
) -> PolicyResult:
    """批量校验 AIMessage.tool_calls 是否已注册。

    当前关闭用户审批：所有已注册工具均直接允许执行，工具风险元数据不参与
    策略判定。批次中只要包含未注册工具，仍阻止整个批次，避免部分执行。

    Args:
        tool_calls: AIMessage.tool_calls 中的工具调用字典列表，
            每个字典包含 'name'、'args'、'id' 字段。
        registry: ToolRegistry 实例（可选，默认使用单例）。

    Returns:
        PolicyResult 对象，包含策略动作和分类后的工具列表。
    """
    if not tool_calls:
        return PolicyResult(action=PolicyAction.ALLOW, reason="no tool calls")

    if registry is None:
        registry = get_tool_registry()

    allowed: list[str] = []
    blocked: list[str] = []

    for tc in tool_calls:
        tool_name = tc.get("name", "")
        if registry.get(tool_name) is None:
            blocked.append(tool_name)
        else:
            allowed.append(tool_name)

    if blocked:
        return PolicyResult(
            action=PolicyAction.BLOCK,
            reason="; ".join(f"Unknown tool: {name}" for name in blocked),
            blocked_tools=blocked,
            allowed_tools=allowed,
        )

    return PolicyResult(
        action=PolicyAction.ALLOW,
        reason="all registered tools allowed",
        allowed_tools=allowed,
    )
