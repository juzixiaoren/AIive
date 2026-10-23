"""能力生命周期状态机：状态集合、合法转换表与转换守卫。

状态语义（刻意区分四个不同事实，不合并为一个"可用"布尔）：

- `candidate`    目录候选，未安装（不落库，仅 API 合成视图）
- `sandbox`      安装成功：沙箱产物 + 安装记录齐备，尚未验证
- `needs_review` 验证失败：定义/可用性校验未通过，保留安装产物待修复
- `verified`     验证通过：tools/list 成功、工具列表与指纹已固化，尚未注册启用
- `active`       已启用：已注册进 ToolRegistry，可被 Agent 发现与执行
- `disabled`     已停用：安装产物与用户配置保留，已从发现索引移除

"安装成功 / 验证通过 / 注册成功 / 正式启用"四个事实分别记录在
`definition["lifecycle"]` 的 installed_at / verified_at / registered_at /
enabled_at 上，任一环节失败都不会被前一个环节的成功掩盖。
"""
from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum


class CapabilityState(StrEnum):
    """能力生命周期状态。"""

    CANDIDATE = "candidate"
    SANDBOX = "sandbox"
    NEEDS_REVIEW = "needs_review"
    VERIFIED = "verified"
    ACTIVE = "active"
    DISABLED = "disabled"


class InvalidCapabilityTransition(ValueError):
    """非法状态转换：调用方必须按状态机顺序推进，不能跳步。"""


# 转换表是唯一裁决处。同状态转换视为幂等空操作（不报错、不改状态）。
LIFECYCLE_TRANSITIONS: Mapping[str, frozenset[str]] = {
    CapabilityState.CANDIDATE: frozenset(),
    # 安装成功 → 验证通过 / 验证失败；兼容既有"安装-冒烟直达 active"路径。
    CapabilityState.SANDBOX: frozenset({
        CapabilityState.VERIFIED,
        CapabilityState.NEEDS_REVIEW,
        CapabilityState.ACTIVE,
    }),
    # 验证失败可重试（修复单向死路），也可重新安装回到 sandbox。
    CapabilityState.NEEDS_REVIEW: frozenset({
        CapabilityState.VERIFIED,
        CapabilityState.SANDBOX,
        CapabilityState.ACTIVE,
    }),
    # 验证通过后才能真正启用；重装/重验可回退。
    CapabilityState.VERIFIED: frozenset({
        CapabilityState.ACTIVE,
        CapabilityState.NEEDS_REVIEW,
        CapabilityState.SANDBOX,
    }),
    # 已启用可被停用；产物丢失/定义损坏时进入 needs_review 走修复流程。
    CapabilityState.ACTIVE: frozenset({
        CapabilityState.DISABLED,
        CapabilityState.NEEDS_REVIEW,
    }),
    # 停用后可重新启用，或重新验证（升级/修复场景）。
    CapabilityState.DISABLED: frozenset({
        CapabilityState.ACTIVE,
        CapabilityState.VERIFIED,
    }),
}


def known_states() -> frozenset[str]:
    """返回全部合法状态字符串。"""
    return frozenset(state.value for state in CapabilityState)


def parse_state(raw: str) -> CapabilityState:
    """把数据库中的状态字符串解析为枚举；未知状态直接报错。

    未知状态意味着数据被外部写入或版本不兼容，必须暴露而不是猜测。
    """
    try:
        return CapabilityState(raw)
    except ValueError as error:
        raise InvalidCapabilityTransition(f"unknown capability state: {raw}") from error


def allowed_transitions(current: str) -> frozenset[str]:
    """返回当前状态允许迁移到的目标状态集合。"""
    return LIFECYCLE_TRANSITIONS[parse_state(current)]


def can_transition(current: str, target: str) -> bool:
    """判断转换是否合法（同状态视为合法空操作）。"""
    if current == target:
        return current in known_states()
    return target in allowed_transitions(current)


def ensure_transition(current: str, target: str) -> None:
    """非法转换时抛出 InvalidCapabilityTransition。"""
    if current == target:
        if current not in known_states():
            raise InvalidCapabilityTransition(f"unknown capability state: {current}")
        return
    allowed = allowed_transitions(current)
    if target not in allowed:
        raise InvalidCapabilityTransition(
            f"illegal capability transition: {current} -> {target} "
            + f"(allowed: {sorted(allowed)})"
        )
