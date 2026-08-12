"""Task Action 到本地或 Desktop Node 的唯一执行边界。"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from aiive.context.run_context import RunContext
from aiive.control.scope import TaskScope, action_resource_keys
from aiive.control.task_policy import ActionPolicyDecision, TaskPolicyEngine
from aiive.db.models import AgentAction, AgentRun, AgentTask, TaskEvidence
from aiive.desktop.registry_overlay import build_registry_for_thread
from aiive.task_runtime.resource_locks import ResourceBusyError, ResourceLockService
from aiive.tools.registry import ToolRegistration, ToolRegistry


@dataclass(frozen=True)
class PreparedCapability:
    registration: ToolRegistration | None
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

    def registry_for(self, task: AgentTask) -> ToolRegistry:
        scope = TaskScope.from_brief(task.task_brief or {})
        registry = build_registry_for_thread(self.db, task.thread_id, task.target_node_id)
        return registry.scoped(scope.allowed_capabilities)

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

            evidence = self.db.query(TaskEvidence).filter(
                TaskEvidence.task_id == task.id,
            ).order_by(TaskEvidence.created_at.desc()).limit(20).all()

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
                if item.inline_payload is not None:
                    return item.inline_payload
                try:
                    raw, _ = EvidenceStore.read(self.db, item, 0, min(item.content_size, 1_048_576))
                    return json.loads(raw)
                except Exception:
                    return None

            if not any(passed(payload(item)) for item in evidence):
                return "promotion_requires_passing_isolated_evaluation"
        return None

    @staticmethod
    def validate_preconditions(action: AgentAction) -> list[str]:
        """对服务端可观察的文件 preconditions 做 TOCTOU 前置校验。

        Desktop 文件仍会在 Node 紧邻副作用前重验同一 preconditions。
        """
        issues: list[str] = []
        pre = action.preconditions or {}
        path_value = action.arguments.get("path") or action.arguments.get("source")
        if not isinstance(path_value, str) or action.target_node_id:
            return issues
        path = Path(path_value).expanduser()
        if pre.get("exists") is True and not path.exists():
            issues.append("precondition_missing_path")
        if pre.get("exists") is False and path.exists():
            issues.append("precondition_path_already_exists")
        expected = pre.get("sha256") or action.arguments.get("expected_sha256")
        if expected and path.is_file():
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual.casefold() != str(expected).casefold():
                issues.append("precondition_sha256_changed")
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
        scope = TaskScope.from_brief(task.task_brief or {})
        registry = self.registry_for(task)
        registration = registry.get(action.capability_id)
        if registration is None:
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
            if registration.executor_kind == "desktop_node":
                from aiive.desktop.connection_manager import DesktopDispatchError, desktop_connection_manager

                try:
                    return desktop_connection_manager.dispatch_action_sync(
                        node_id=registration.executor_node_id,
                        task_id=task.id,
                        run_id=run.id,
                        action_id=action.id,
                        idempotency_key=action.idempotency_key,
                        arguments_hash=action.arguments_hash,
                        capability_id=action.capability_id,
                        params=action.arguments or {},
                        preconditions=action.preconditions or {},
                        scope=scope.as_envelope(),
                        fencing_token=action.execution_token or str(uuid.uuid4()),
                        timeout_seconds=registration.safety.timeout_seconds or 30,
                    )
                except DesktopDispatchError as error:
                    # ACK/终态可能仍在路上。保留数据库锁直到 Journal 对账或租约
                    # 到期，不能让另一 Task 立即并发触碰同一资源。
                    release_grant = False
                    return {
                        "ok": False,
                        "error": str(error),
                        "error_type": "execution_unknown",
                        "execution_status": "execution_unknown",
                    }
            result = registry.execute_brokered(
                action.capability_id,
                action.arguments or {},
                action.descriptor_hash,
                RunContext(
                    thread_id=task.thread_id,
                    trace_id=task.id,
                    source="task_runtime",
                    task_id=task.id,
                    agent_run_id=run.id,
                    action_id=action.id,
                ),
            )
            if result.get("error_type") == "execution_unknown":
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
