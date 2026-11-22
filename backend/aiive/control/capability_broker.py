"""Task Action 到本地或 Desktop Node 的唯一执行边界。"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from aiive.context.run_context import RunContext
from aiive.control.scope import TASK_READ_CAPABILITIES, TaskScope, action_resource_keys
from aiive.control.task_policy import ActionPolicyDecision, TaskPolicyEngine
from aiive.db.models import AgentAction, AgentRun, AgentTask, TaskEvidence
from aiive.desktop.registry_overlay import build_registry_for_thread
from aiive.mcp.bootstrap import registered_tool_ids
from aiive.task_runtime.resource_locks import ResourceBusyError, ResourceLockService
from aiive.tools.base import BaseTool, outcome_is_unknown
from aiive.tools.registry import ToolRegistry
from aiive.tool_discovery import DEFAULT_LIMIT
from aiive.tool_discovery.service import get_discovery_service


@dataclass(frozen=True)
class PreparedCapability:
    registration: BaseTool | None
    decision: ActionPolicyDecision


class CapabilityBroker:
    """Broker 同时强制 capability allowlist、路径 scope、policy、锁和定义指纹。"""

    def __init__(
        self,
        db: Session,
        *,
        policy: TaskPolicyEngine | None = None,
        locks: ResourceLockService | None = None,
    ) -> None:
        self.db: Session = db
        self.policy: TaskPolicyEngine = policy or TaskPolicyEngine()
        self.locks: ResourceLockService = locks or ResourceLockService()

    def registry_for(self, task: AgentTask, *, available: ToolRegistry | None = None) -> ToolRegistry:
        """执行视图：restricted 时裁剪到授权白名单，unified 时返回全部可用能力。"""
        scope = TaskScope.from_brief(task.task_brief or {})
        registry = available if available is not None else self.available_registry(task)
        if scope.capability_policy == "unified":
            return registry
        return registry.scoped(scope.allowed_capabilities | TASK_READ_CAPABILITIES)

    def available_registry(self, task: AgentTask) -> ToolRegistry:
        """发现视图：内置 + 在线桌面 + MCP 的完整可用能力，不按任务收窄。

        发现只回答"系统里有什么"，不代表获得执行授权；实际调用仍由
        registry_for 的授权视图与 TaskPolicyEngine 判定。
        """
        return build_registry_for_thread(self.db, task.thread_id, task.target_node_id)

    def search_capabilities(
        self, task: AgentTask, query: str, *, namespace_ids: list[str] | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """检索同一快照中的目录，为 Worker 选择预算内的执行定义引用。"""
        from aiive.task_runtime.tool_injection import select_search_definitions

        available = self.available_registry(task).snapshot()
        execution = self.registry_for(task, available=available)
        result = get_discovery_service().search(available, query, namespace_ids=namespace_ids, limit=limit)
        for entry in result["tools"]:
            entry["in_scope"] = execution.get(entry["capability_id"]) is not None
        result["loaded"] = select_search_definitions(execution, result["tools"])
        return result

    def describe_capability(self, task: AgentTask, capability_id: str) -> dict[str, Any] | None:
        """从执行视图读取定义，授权范围外仅返回目录摘要。"""
        available = self.available_registry(task).snapshot()
        definition = self.registry_for(task, available=available).definition(capability_id)
        if definition is not None:
            return {**definition, "in_scope": True}
        summary = available.summary(capability_id)
        if summary is not None:
            return {**summary, "description": summary["description"][:500], "in_scope": False}
        return None

    def list_capabilities(self, task: AgentTask) -> list[dict[str, Any]]:
        return self.registry_for(task).list_all()

    def prepare(self, task: AgentTask, capability_id: str, arguments: dict[str, Any]) -> PreparedCapability:
        scope = TaskScope.from_brief(task.task_brief or {})
        registration = self.registry_for(task).get(capability_id)
        lifecycle_block = self._lifecycle_block(task, capability_id, arguments)
        if lifecycle_block:
            return PreparedCapability(
                registration=registration,
                decision=ActionPolicyDecision("block", lifecycle_block),
            )
        return PreparedCapability(
            registration=registration,
            decision=self.policy.decide(registration, scope, capability_id, arguments),
        )

    def _lifecycle_block(self, task: AgentTask, capability_id: str, arguments: dict[str, Any]) -> str | None:
        if task.task_type not in {"self_improvement", "selfdev", "lifecycle"}:
            return None
        if capability_id == "apply_patch_to_inactive_slot":
            from aiive.selfdev.trusted_core import validate_operations

            operations = arguments.get("operations")
            issues = validate_operations(operations if isinstance(operations, list) else [])
            return "trusted_core_policy_denied" if issues else None
        if capability_id == "promote_slot":
            import json

            from aiive.task_runtime.evidence import EvidenceStore

            from aiive.task_runtime.read_repository import TaskReadRepository
            evidence = list(reversed(TaskReadRepository(self.db).evidence(task.id)[-20:]))

            def passed(value: Any) -> bool:
                if isinstance(value, dict):
                    report = value.get("targeted_test_report")
                    if isinstance(report, dict) and report.get("ok") is True:
                        return True
                    return any(passed(item) for item in value.values())
                if isinstance(value, list):
                    return any(passed(item) for item in value)
                return False

            def payload(item: TaskEvidence) -> Any:
                try:
                    raw, _ = EvidenceStore.read(self.db, item, 0, min(item.content_size, 1_048_576))
                    return json.loads(raw)
                except Exception:
                    return None

            if not any(passed(payload(item)) for item in evidence):
                return "promotion_requires_passing_isolated_evaluation"
        return None

    def _is_disabled(self, *, capability_id: str) -> bool:
        """该工具是否属于某个已被显式停用的能力。"""
        from aiive.db.models import Capability

        rows = (
            self.db.query(Capability)
            .filter(Capability.state == "disabled")
            .limit(50)
            .all()
        )
        return any(capability_id in registered_tool_ids(cap) for cap in rows)

    @staticmethod
    def validate_preconditions(action: AgentAction) -> list[str]:
        """对服务端可观察的文件 preconditions 做 TOCTOU 前置校验。

        Desktop 文件仍会在 Node 紧邻副作用前重验同一 preconditions。
        """
        issues: list[str] = []
        pre = action.preconditions or {}
        if set(pre) - {"exists", "sha256", "size", "mtime_ns", "file_id"}:
            return ["precondition_unsupported_field"]
        path_value = action.arguments.get("path") or action.arguments.get("source") or action.arguments.get("file_path")
        if not isinstance(path_value, str):
            return ["precondition_path_required"] if pre else []
        if action.target_node_id:
            return issues
        path = Path(path_value).expanduser()
        if pre.get("exists") is True and not path.exists():
            issues.append("precondition_missing_path")
        if pre.get("exists") is False and path.exists():
            issues.append("precondition_path_already_exists")
        expected = pre.get("sha256") or action.arguments.get("expected_sha256")
        if expected and not path.is_file():
            issues.append("precondition_sha256_requires_file")
        elif expected:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual.casefold() != str(expected).casefold():
                issues.append("precondition_sha256_changed")
        if not path.exists() and any(pre.get(key) is not None for key in ("size", "mtime_ns", "file_id")):
            issues.append("precondition_metadata_requires_path")
        if path.exists():
            stat = path.stat()
            expected_size = pre.get("size")
            if expected_size is not None:
                try:
                    if stat.st_size != int(expected_size):
                        issues.append("precondition_size_changed")
                except (TypeError, ValueError):
                    issues.append("precondition_size_invalid")
            expected_mtime = pre.get("mtime_ns")
            if expected_mtime is not None:
                try:
                    if stat.st_mtime_ns != int(expected_mtime):
                        issues.append("precondition_mtime_changed")
                except (TypeError, ValueError):
                    issues.append("precondition_mtime_invalid")
            expected_file_id = pre.get("file_id")
            actual_file_id = f"{stat.st_dev}:{stat.st_ino}"
            if expected_file_id is not None and str(expected_file_id) != actual_file_id:
                issues.append("precondition_file_id_changed")
        return issues

    def dispatch(self, task: AgentTask, run: AgentRun, action: AgentAction) -> dict[str, Any]:
        from aiive.worker.outbox_execution_auth import require_execution_authorization
        from aiive.worker.job_source_guard import task_sources_visible

        require_execution_authorization()
        from aiive.task_runtime.read_repository import TaskReadRepository
        if not task_sources_visible(self.db, task.id) or not TaskReadRepository(self.db).visible_child("agent_action", action):
            return {"ok": False, "error": "forget_source_blocked", "error_type": "source_blocked"}
        scope = TaskScope.from_brief(task.task_brief or {})
        registry = self.registry_for(task)
        registration = registry.get(action.capability_id)
        if registration is None:
            # 停用与"从未存在"必须区分：停用是用户显式动作，任务应按可恢复错误处理，
            # 不能当成工具名写错（unknown_tool）。
            if self._is_disabled(capability_id=action.capability_id):
                return {
                    "ok": False,
                    "error": "capability_disabled",
                    "error_type": "capability_disabled",
                }
            return {"ok": False, "error": "capability_unavailable", "error_type": "unknown_tool"}
        if registration.safety.descriptor_hash != action.descriptor_hash:
            return {"ok": False, "error": "descriptor_changed", "error_type": "descriptor_changed"}
        issues = scope.validate_arguments(action.capability_id, action.arguments or {})
        issues.extend(self.validate_preconditions(action))
        if issues:
            return {"ok": False, "error": ";".join(issues), "error_type": "precondition_failed"}

        resources = action_resource_keys(
            action.capability_id,
            action.arguments or {},
            action.target_node_id,
            mutates_resources=registration.safety.writes_external_world,
        )
        try:
            grant = self.locks.acquire(
                self.db,
                task_id=task.id,
                action_id=action.id,
                resource_keys=resources,
            )
            # 锁必须在外部副作用前可被其他 Worker 看到。
            self.db.commit()
        except ResourceBusyError as error:
            self.db.rollback()
            return {"ok": False, "error": str(error), "error_type": "resource_busy"}
        except Exception:
            self.db.rollback()
            raise

        release_grant = True
        try:
            from aiive.tools.executor import ToolExecutor
            result = ToolExecutor(registry).execute(
                action.capability_id, action.arguments or {}, "task_runtime",
                RunContext(thread_id=task.thread_id, trace_id=task.id, source="task_runtime",
                           task_id=task.id, agent_run_id=run.id, action_id=action.id,
                           user_timezone=(task.task_brief.get("initial_input") or {}).get("user_timezone")),
                expected_descriptor_hash=action.descriptor_hash, channel="task_action",
                authorization={
                    "node_id": registration.executor_node_id, "task_id": task.id, "run_id": run.id,
                    "action_id": action.id, "idempotency_key": action.idempotency_key,
                    "arguments_hash": action.arguments_hash, "capability_id": action.capability_id,
                    "params": action.arguments or {}, "preconditions": action.preconditions or {},
                    "scope": scope.as_envelope(), "fencing_token": action.execution_token or str(uuid.uuid4()),
                    "timeout_seconds": registration.safety.timeout_seconds or 30,
                },
            )
            if outcome_is_unknown(result):
                # 结果未知的 Action 保留资源锁，避免后续 Action 与未确认副作用并发。
                release_grant = False
            return result
        except Exception:
            release_grant = False
            raise
        finally:
            if release_grant:
                try:
                    self.locks.release(self.db, grant)
                    self.db.commit()
                except Exception:
                    self.db.rollback()
