"""
权限管理器：检查工具调用权限。
负责验证指令来源是否被授权调用指定工具，以及是否需要用户确认。
"""

from aiive.tools.registry import ToolRegistry


class PermissionManager:
    """工具权限管理器，基于指令来源和安全等级进行访问控制。

    职责：
    - 检查指令来源是否在工具允许的来源列表中
    - 检查工具是否需要用户确认
    - 判断工具是否可安全用于不信任内容
    """

    def __init__(self, registry: ToolRegistry):
        """初始化权限管理器。

        参数:
            registry: 工具注册表实例，用于查询工具的安全配置
        """
        self._registry = registry

    def can_trigger_from(self, capability_id: str, instruction_source: str) -> bool:
        """检查指定指令来源是否有权限触发该工具。

        参数:
            capability_id: 工具的能力标识符
            instruction_source: 指令来源（如 trusted_user_command）

        返回:
            是否允许触发
        """
        reg = self._registry.get(capability_id)
        if not reg:
            return False
        return instruction_source in reg.safety.allowed_instruction_sources

    def check(
        self,
        capability_id: str,
        instruction_source: str,
    ) -> dict:
        """执行权限检查，返回详细的检查结果。

        参数:
            capability_id: 工具的能力标识符
            instruction_source: 指令来源

        返回:
            包含 allowed、reason 等字段的字典。
            如果 requires_confirmation 为 True，表示需要用户确认。
        """
        reg = self._registry.get(capability_id)
        if not reg:
            return {"allowed": False, "reason": f"Unknown tool: {capability_id}"}

        if instruction_source not in reg.safety.allowed_instruction_sources:
            return {
                "allowed": False,
                "reason": "Instruction source not authorized",
                "source": instruction_source,
                "allowed_sources": reg.safety.allowed_instruction_sources,
            }

        if reg.safety.requires_confirmation:
            return {
                "allowed": False,
                "requires_confirmation": True,
                "tool": capability_id,
            }

        return {"allowed": True}

    def is_safe_for_untrusted_content(self, capability_id: str) -> bool:
        """判断工具是否可安全用于不信任的内容。

        安全检查条件：
        - 风险等级为 low
        - 不会写入外部世界
        - 不可删除
        - 不可访问密钥

        参数:
            capability_id: 工具的能力标识符

        返回:
            是否安全
        """
        reg = self._registry.get(capability_id)
        if not reg:
            return False
        safety = reg.safety
        return (
            safety.risk_level == "low"
            and not safety.writes_external_world
            and not safety.can_delete
            and not safety.can_access_secret
        )
