import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class CapabilitySafetySchema:
    capability_id: str
    definition_source: str  # local_builtin | generated_by_agent | remote_mcp | user_installed
    definition_trust_level: str  # trusted | semi_trusted | untrusted
    risk_level: str  # low | medium | high | critical
    requires_confirmation: bool = False
    writes_external_world: bool = False
    can_access_secret: bool = False
    can_delete: bool = False
    allowed_instruction_sources: list[str] = field(default_factory=lambda: ["trusted_user_command"])
    descriptor_hash: str = ""
    tool_description_is_instruction: bool = False


def compute_descriptor_hash(schema: dict) -> str:
    raw = json.dumps(schema, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class ToolRegistration:
    safety: CapabilitySafetySchema
    handler: Callable[..., Any]
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolRegistration] = {}

    def register(self, reg: ToolRegistration) -> None:
        self._tools[reg.safety.capability_id] = reg

    def get(self, capability_id: str) -> ToolRegistration | None:
        return self._tools.get(capability_id)

    def list_all(self) -> list[dict[str, Any]]:
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

    def execute(
        self,
        capability_id: str,
        params: dict[str, Any],
        instruction_source: str,
    ) -> dict[str, Any]:
        reg = self.get(capability_id)
        if not reg:
            return {"ok": False, "error": f"Unknown tool: {capability_id}"}

        # Mechanical guard: instruction source check
        if instruction_source not in reg.safety.allowed_instruction_sources:
            return {
                "ok": False,
                "error": "Instruction source not authorized for this tool",
                "source": instruction_source,
                "allowed": reg.safety.allowed_instruction_sources,
            }

        # Mechanical guard: confirmation required
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
            return {"ok": False, "error": str(e)}


# Global singleton
_registry: ToolRegistry | None = None


def get_tool_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
        _register_builtins(_registry)
    return _registry


def _register_builtins(registry: ToolRegistry) -> None:
    from aiive.tools.builtin_tools import register_builtin_tools

    register_builtin_tools(registry)
