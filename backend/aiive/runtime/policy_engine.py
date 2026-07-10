"""
运行时层 - 策略引擎。

基于工具元数据的机械化安全校验引擎。不依赖自然语言关键词匹配，
而是直接检查 AIMessage.tool_calls 与 ToolRegistry 元数据
（风险等级、是否外部写入、是否可删除等），以决定工具调用应：
- allowed（允许）：直接传递给 ToolNode 执行
- blocked（阻止）：拒绝执行
- confirm（确认）：需要用户确认后方可执行
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

# 工具效果类型与策略动作的映射（基于 CapabilitySafetySchema 字段）
EFFECT_TYPE_RULES = {
    "destructive_write": PolicyAction.CONFIRM,  # can_delete = True
    "external_communication": PolicyAction.CONFIRM,  # writes_external_world = True + 高风险
    "shell_execution": PolicyAction.CONFIRM,
}


def check_tool_calls(
    tool_calls: list[dict[str, Any]],
    registry: ToolRegistry | None = None,
) -> PolicyResult:
    """批量校验 AIMessage.tool_calls 是否符合安全策略。

    对每个工具调用依次检查：未知工具则阻止；高风险、破坏性操作、
    外部写入和显式要求确认的工具则要求用户确认；其余默认允许。

    如果同一批次中同时存在允许和需要确认的工具，为安全起见阻止整批次，
    避免部分执行。

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
    block_reasons: list[str] = []
    confirm_reasons: list[str] = []

    for tc in tool_calls:
        tool_name = tc.get("name", "")
        reg = registry.get(tool_name)

        # 未知工具：阻止
        if reg is None:
            blocked.append(tool_name)
            block_reasons.append(f"Any tool: {tool_name}")
            continue

        safety = reg.safety

        # 规则 1：高风险工具需要确认
        if safety.risk_level in ("high", "critical"):
            confirm.append(tool_name)
            confirm_reasons.append(f"High-risk tool ({safety.risk_level}): {tool_name}")
            continue

        # 规则 2：破坏性操作（can_delete）需要确认
        if safety.can_delete:
            confirm.append(tool_name)
            confirm_reasons.append(f"Destructive operation: {tool_name}")
            continue

        # 规则 3：中风险等级的外部世界写入需要确认
        if safety.writes_external_world and safety.risk_level == "medium":
            confirm.append(tool_name)
            confirm_reasons.append(f"External write (medium risk): {tool_name}")
            continue

        # 规则 4：显式要求确认的工具
        if safety.requires_confirmation:
            confirm.append(tool_name)
            confirm_reasons.append(f"Requires confirmation: {tool_name}")
            continue

        # 默认：允许
        allowed.append(tool_name)

    # 确定整体策略动作
    if blocked and not allowed and not confirm:
        return PolicyResult(
            action=PolicyAction.BLOCK,
            reason="; ".join(block_reasons),
            blocked_tools=blocked,
        )

    if confirm:
        # 如果既有允许又有需要确认的工具，为安全起见阻止整批次，避免部分执行
        reason_parts = confirm_reasons[:]
        if allowed:
            reason_parts.append(f"Also blocking {len(allowed)} other tools in same batch")
        return PolicyResult(
            action=PolicyAction.CONFIRM,
            reason="; ".join(reason_parts),
            confirm_tools=confirm,
            allowed_tools=allowed,
        )

    return PolicyResult(
        action=PolicyAction.ALLOW,
        reason="all tools allowed",
        allowed_tools=allowed,
    )
