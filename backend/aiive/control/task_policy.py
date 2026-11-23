"""Action 的确定性策略决策；LLM 不参与授权。"""
from __future__ import annotations

import ntpath
from dataclasses import dataclass
from typing import Any

from aiive.control.scope import TaskScope
from aiive.tools.base import BaseTool
from aiive.tools.shell_policy import (
    SHELL_CAPABILITIES,
    classify_shell_command,
    shell_path_violations,
)


@dataclass(frozen=True)
class ActionPolicyDecision:
    outcome: str  # allow | approval | block
    reason: str


ALWAYS_APPROVE = frozenset({
    "promote_slot", "rollback_slot",
})
FILE_MUTATIONS = frozenset({
    "safe_delete", "apply_patch_to_inactive_slot", "run_selfdev_repair",
})


def approval_reason(registration: BaseTool, capability_id: str, arguments: dict[str, Any]) -> str | None:
    if capability_id in ALWAYS_APPROVE or (
        capability_id == "run_selfdev_repair" and arguments.get("promote") is True
    ):
        return "release_change_requires_approval"
    if (
        capability_id in FILE_MUTATIONS or capability_id.startswith("desktop_fs_")
        or (registration.safety.definition_source == "remote_mcp"
            and capability_id.startswith(("mcp_server_filesystem_", "mcp_filesystem_")))
    ):
        return None
    if capability_id in SHELL_CAPABILITIES:
        verdict = classify_shell_command(
            str(arguments.get("command") or ""),
            windows_shell=capability_id == "desktop_exec" and bool(ntpath.splitdrive(str(arguments.get("cwd") or ""))[0]),
        )
        return None if verdict.read_only or verdict.file_only else f"shell_{verdict.reason}"
    safety = registration.safety
    if safety.requires_confirmation or safety.risk_level in {"high", "critical"}:
        return "high_risk_action_requires_approval"
    return None


class TaskPolicyEngine:
    def decide(
        self,
        registration: BaseTool | None,
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
        if capability_id in SHELL_CAPABILITIES:
            command = str(arguments.get("command") or "")
            cwd = str(arguments.get("cwd") or "")
            remote = capability_id == "desktop_exec"
            if not cwd or not scope.allowed_roots:
                return ActionPolicyDecision("block", "shell_requires_scoped_cwd")
            verdict = classify_shell_command(command, windows_shell=remote and bool(ntpath.splitdrive(cwd)[0]))
            if verdict.uses_network and not scope.allow_network:
                return ActionPolicyDecision("block", "network_access_not_in_task_scope")
            violations, dynamic_path = shell_path_violations(
                command,
                cwd=cwd,
                allowed_roots=scope.allowed_roots,
                remote=remote,
            )
            if violations:
                detail = ",".join(violations[:3])
                return ActionPolicyDecision("block", f"shell_path_outside_task_scope:{detail}")
            if dynamic_path:
                return ActionPolicyDecision("approval", "shell_dynamic_path_requires_approval")
        if reason := approval_reason(registration, capability_id, arguments):
            return ActionPolicyDecision("approval", reason)
        return ActionPolicyDecision("allow", "task_scope_and_policy_allow")
