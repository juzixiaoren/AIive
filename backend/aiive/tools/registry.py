"""
工具注册表：管理所有工具（能力）的注册、查询和执行。
提供全局单例，所有内置工具通过 register_builtin_tools 注册。

核心组件:
- CapabilitySafetySchema: 工具的安全元数据（风险等级、来源、权限等）
- ToolRegistration: 工具注册条目，包含安全配置和处理函数
- ToolRegistry: 工具注册表，管理注册、查询、schema 渲染和执行
"""

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CapabilitySafetySchema:
    """工具能力的安全配置 schema（不可变）。

    属性:
        capability_id: 工具唯一标识符，如 "echo"、"schedule_reminder"
        definition_source: 工具来源（local_builtin / generated_by_agent / remote_mcp / user_installed）
        definition_trust_level: 信任等级（trusted / semi_trusted / untrusted）
        risk_level: 风险等级（low / medium / high / critical）
        requires_confirmation: 是否需要用户确认
        writes_external_world: 是否会写入外部世界（文件系统、数据库等）
        can_access_secret: 是否可访问密钥
        can_delete: 是否可删除数据
        allowed_instruction_sources: 允许的指令来源列表
        descriptor_hash: 安全描述的 SHA256 哈希（用于变更检测）
        tool_description_is_instruction: 工具描述是否可作为指令
    """
    capability_id: str
    definition_source: str  # 来源：local_builtin | generated_by_agent | remote_mcp | user_installed
    definition_trust_level: str  # 信任等级：trusted | semi_trusted | untrusted
    risk_level: str  # 风险等级：low | medium | high | critical
    requires_confirmation: bool = False
    writes_external_world: bool = False
    can_access_secret: bool = False
    can_delete: bool = False
    allowed_instruction_sources: list[str] = field(default_factory=lambda: ["trusted_user_command"])
    descriptor_hash: str = ""
    tool_description_is_instruction: bool = False


def compute_descriptor_hash(schema: dict) -> str:
    """计算安全 schema 的 SHA256 哈希值（取前 16 位）。

    用于检测工具定义是否发生变化。

    参数:
        schema: 安全配置字典

    返回:
        16 位十六进制哈希字符串
    """
    raw = json.dumps(schema, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class ToolRegistration:
    """工具注册条目，将安全配置与处理函数绑定。

    属性:
        safety: 安全配置 schema
        handler: 工具处理函数（可调用对象）
        description: 工具功能描述
        parameters: 参数定义，格式为 {参数名: 类型字符串}
    """
    safety: CapabilitySafetySchema
    handler: Callable[..., Any]
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)


class ToolRegistry:
    """工具注册表：管理所有工具能力的注册、查询和执行。

    功能：
    - register: 注册新工具
    - get: 按 capability_id 查询工具
    - list_all: 列出所有已注册工具
    - render_tool_schemas: 生成注入 system prompt 的工具描述文本
    - execute: 执行工具调用（含安全守卫）
    """

    def __init__(self):
        """初始化空的工具注册表。"""
        self._tools: dict[str, ToolRegistration] = {}

    def register(self, reg: ToolRegistration) -> None:
        """注册一个工具。

        参数:
            reg: 工具注册条目
        """
        self._tools[reg.safety.capability_id] = reg

    def get(self, capability_id: str) -> ToolRegistration | None:
        """按 ID 查询已注册工具。

        参数:
            capability_id: 工具能力标识符

        返回:
            ToolRegistration 或 None
        """
        return self._tools.get(capability_id)

    def list_all(self) -> list[dict[str, Any]]:
        """列出所有已注册工具的摘要信息。

        返回:
            工具信息字典列表，每项包含 capability_id、description、风险等级等
        """
        return [
            {
                "capability_id": r.safety.capability_id,
                "description": r.description,
                "definition_source": r.safety.definition_source,
                "definition_trust_level": r.safety.definition_trust_level,
                "risk_level": r.safety.risk_level,
                "requires_confirmation": r.safety.requires_confirmation,
                "writes_external_world": r.safety.writes_external_world,
                "can_access_secret": r.safety.can_access_secret,
                "can_delete": r.safety.can_delete,
                "descriptor_hash": r.safety.descriptor_hash,
            }
            for r in self._tools.values()
        ]

    def render_tool_schemas(self) -> str:
        """生成工具 schema 文本，用于注入 system prompt。

        每个工具渲染为：名称(参数)、描述、风险标签、副作用标记、确认要求。

        返回:
            格式化的工具 schema 文本
        """
        lines = ["## Tools"]
        for i, reg in enumerate(self._tools.values(), 1):
            safety = reg.safety
            params = reg.parameters
            param_str = ", ".join(f"{k}: {v}" for k, v in params.items()) if params else ""
            lines.append(f"{i}. {safety.capability_id}({param_str})")
            lines.append(f"   {reg.description}")
            side_effect = safety.writes_external_world or safety.can_delete
            flags = []
            flags.append(f"risk: {safety.risk_level}")
            if side_effect:
                flags.append("side_effect: true")
            else:
                flags.append("side_effect: false")
            if safety.requires_confirmation:
                flags.append("requires_confirmation: true")
            lines.append(f"   [{', '.join(flags)}]")
            lines.append("")
        return "\n".join(lines)

    def execute(
        self,
        capability_id: str,
        params: dict[str, Any],
        instruction_source: str,
    ) -> dict[str, Any]:
        """执行工具调用，包含两道安全守卫。

        守卫 1: 检查指令来源是否被授权
        守卫 2: 检查是否需要用户确认（需要时直接拒绝，等待确认后再调用）

        参数:
            capability_id: 工具能力标识符
            params: 调用参数
            instruction_source: 指令来源（如 trusted_user_command）

        返回:
            包含 ok、result（或 error/approval_required）的字典
        """
        reg = self.get(capability_id)
        if not reg:
            return {"ok": False, "error": f"Unknown tool: {capability_id}"}

        # 机械守卫 1：指令来源检查
        if instruction_source not in reg.safety.allowed_instruction_sources:
            return {
                "ok": False,
                "error": "Instruction source not authorized for this tool",
                "source": instruction_source,
                "allowed": reg.safety.allowed_instruction_sources,
            }

        # 机械守卫 2：确认要求检查
        if reg.safety.requires_confirmation:
            return {
                "ok": False,
                "approval_required": True,
                "tool": capability_id,
                "params": params,
            }

        try:
            result = reg.handler(**params)
            return {"ok": True, "result": result}
        except Exception as e:
            logger.exception("工具执行失败: capability_id=%s params=%s", capability_id, params)
            return {"ok": False, "error": str(e)}


# 全局单例
_registry: ToolRegistry | None = None


def get_tool_registry() -> ToolRegistry:
    """获取全局工具注册表单例。

    首次调用时自动初始化并注册所有内置工具。

    返回:
        ToolRegistry 全局单例
    """
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
        _register_builtins(_registry)
    return _registry


def _register_builtins(registry: ToolRegistry) -> None:
    """向注册表中注册所有内置工具。

    参数:
        registry: 目标注册表实例
    """
    from aiive.tools.builtin_tools import register_builtin_tools

    register_builtin_tools(registry)
