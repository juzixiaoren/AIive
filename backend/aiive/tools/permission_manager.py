from aiive.tools.registry import ToolRegistry


class PermissionManager:
    def __init__(self, registry: ToolRegistry):
        self._registry = registry

    def can_trigger_from(self, capability_id: str, instruction_source: str) -> bool:
        reg = self._registry.get(capability_id)
        if not reg:
            return False
        return instruction_source in reg.safety.allowed_instruction_sources

    def check(
        self,
        capability_id: str,
        instruction_source: str,
    ) -> dict:
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
