"""Action 的确定性策略决策；LLM 不参与授权。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiive.control.scope import TaskScope
from aiive.tools.registry import ToolRegistration
from aiive.tools.shell_policy import (
    SHELL_CAPABILITY_ID,
    classify_shell_command,
    shell_path_violations,
)


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
        if capability_id == SHELL_CAPABILITY_ID:
            # 本机 Shell 按命令内容分级：只读命令放行，其余进入审批；
            # 涉及网络的命令还必须显式允许联网；命令里出现的路径必须落在
            # allowed_roots 内（否则只读白名单会变成任意路径读取的通道）。
            command = str(arguments.get("command") or "")
            verdict = classify_shell_command(command)
            if verdict.uses_network and not scope.allow_network:
                return ActionPolicyDecision("block", "network_access_not_in_task_scope")
            violations, dynamic_path = shell_path_violations(
                command,
                cwd=str(arguments.get("cwd") or ""),
                allowed_roots=scope.allowed_roots,
            )
            if violations:
                detail = ",".join(violations[:3])
                return ActionPolicyDecision("block", f"shell_path_outside_task_scope:{detail}")
            if not verdict.read_only:
                return ActionPolicyDecision("approval", f"shell_{verdict.reason}")
            if dynamic_path:
                # 含 $VAR/反引号等无法静态判定的路径，不能按只读直接放行。
                return ActionPolicyDecision("approval", "shell_dynamic_path_requires_approval")
        if (
            capability_id in ALWAYS_APPROVE
            or safety.requires_confirmation
            or safety.can_delete
            or safety.risk_level in {"high", "critical"}
        ):
            return ActionPolicyDecision("approval", "high_risk_action_requires_approval")
        return ActionPolicyDecision("allow", "task_scope_and_policy_allow")
