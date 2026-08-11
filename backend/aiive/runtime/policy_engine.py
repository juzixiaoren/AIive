"""
运行时层 - 策略引擎。

策略按注册表安全元数据执行：未知工具阻止；删除、高危或显式标记的工具进入
服务端冻结参数的审批路径；其余已注册工具直接执行。
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


EFFECT_TYPE_RULES = {
    "destructive_write": PolicyAction.CONFIRM,
    "external_communication": PolicyAction.CONFIRM,
    "shell_execution": PolicyAction.CONFIRM,
}


def check_tool_calls(
    tool_calls: list[dict[str, Any]],
    registry: ToolRegistry | None = None,
) -> PolicyResult:
    """批量校验 AIMessage.tool_calls 的注册状态与审批元数据。

    批次中只要包含未注册工具就阻止整批；否则只要存在一个需确认工具，整批
    冻结等待审批，避免低风险调用先执行后高风险调用被拒造成半批副作用。

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
    confirm: list[str] = []

    for tc in tool_calls:
        tool_name = tc.get("name", "")
        registration = registry.get(tool_name)
        if registration is None:
            blocked.append(tool_name)
        else:
            allowed.append(tool_name)
            safety = registration.safety
            if (
                safety.requires_confirmation
                or safety.can_delete
                or safety.risk_level in ("high", "critical")
            ):
                confirm.append(tool_name)

    if blocked:
        return PolicyResult(
            action=PolicyAction.BLOCK,
            reason="; ".join(f"Unknown tool: {name}" for name in blocked),
            blocked_tools=blocked,
            allowed_tools=allowed,
            confirm_tools=confirm,
        )

    if confirm:
        return PolicyResult(
            action=PolicyAction.CONFIRM,
            reason="需要用户确认: " + ", ".join(confirm),
            confirm_tools=confirm,
            allowed_tools=allowed,
        )

    return PolicyResult(
        action=PolicyAction.ALLOW,
        reason="all registered tools allowed",
        allowed_tools=allowed,
    )
