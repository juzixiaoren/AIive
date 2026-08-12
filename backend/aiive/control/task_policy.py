"""Action 的确定性策略决策；LLM 不参与授权。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiive.control.scope import TaskScope
from aiive.tools.registry import ToolRegistration


@dataclass(frozen=True)
class ActionPolicyDecision:
    outcome: str  # allow | approval | block
    reason: str


ALWAYS_APPROVE = frozenset({
    "desktop_fs_delete", "desktop_exec", "apply_patch_to_inactive_slot",
    "promote_slot", "rollback_slot",
})


class TaskPolicyEngine:
    def decide(
        self,
        registration: ToolRegistration | None,
        scope: TaskScope,
        capability_id: str,
        arguments: dict[str, Any],
    ) -> ActionPolicyDecision:
        if registration is None:
            return ActionPolicyDecision("block", "unknown_capability")
        issues = scope.validate_arguments(capability_id, arguments)
        if issues:
            return ActionPolicyDecision("block", ";".join(issues))
        safety = registration.safety
        if safety.can_access_secret and not scope.allow_secrets:
            return ActionPolicyDecision("block", "secret_access_not_in_task_scope")
        if capability_id == "install_mcp_sandbox" and arguments.get("env_keys") and not scope.allow_secrets:
            return ActionPolicyDecision("block", "mcp_environment_access_not_in_task_scope")
        if safety.uses_network and not scope.allow_network:
            return ActionPolicyDecision("block", "network_access_not_in_task_scope")
        if (
            capability_id in ALWAYS_APPROVE
            or safety.requires_confirmation
            or safety.can_delete
            or safety.risk_level in {"high", "critical"}
        ):
            return ActionPolicyDecision("approval", "high_risk_action_requires_approval")
        return ActionPolicyDecision("allow", "task_scope_and_policy_allow")
