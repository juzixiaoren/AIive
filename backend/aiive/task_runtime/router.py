"""规范化 TaskBrief 的发现策略，并选择任务执行器。"""
from __future__ import annotations

from typing import Any

SELFDEV_TASK_TYPES = frozenset({"self_improvement", "selfdev", "lifecycle"})
CODING_TASK_TYPES = SELFDEV_TASK_TYPES | {"coding", "code"}
CODING_CAPABILITIES = ("read_file", "edit_file", "write_file", "apply_patch", "shell_session")
SELFDEV_CAPABILITIES = (
    "create_selfdev_plan", "apply_patch_to_inactive_slot", "run_selfdev_repair",
    "promote_slot", "get_promotion_status", "rollback_slot", "get_runtime_identity",
)


def with_selfdev_scope(task_type: str, brief: dict[str, Any]) -> dict[str, Any]:
    if task_type not in CODING_TASK_TYPES:
        return brief
    from aiive.project_paths import source_root

    root = source_root()
    scope = dict(brief.get("scope") or {})
    if not scope.get("allowed_capabilities"):
        scope["capability_policy"] = "unified"
    initial = dict(brief.get("initial_input") or {})
    roots = scope.get("allowed_roots") or []
    if task_type in SELFDEV_TASK_TYPES:
        previous = initial.get("selfdev_project_root")
        roots = list(dict.fromkeys([str(root), *(value for value in roots if value != previous)]))
        initial["selfdev_project_root"] = str(root)
    scope["allowed_roots"] = roots or [str(root)]
    scope["isolation_mode"] = "git_worktree"
    return {**brief, "scope": scope, "initial_input": initial}


def normalize_task_brief(task_type: str, raw: dict[str, Any] | None) -> dict[str, Any]:
    """规范化任务 brief，并确定能力策略。

    统一 Agent 模型：不再用 task_type 预置能力白名单。只有调用方显式给出
    allowed_capabilities 时才按 restricted 执行授权校验；否则标记为 unified，
    Agent 可以发现全部可用能力，真实约束（路径根、域名、密钥、节点可用性、
    策略与审批）不受影响。
    """
    brief = dict(raw or {})
    scope = dict(brief.get("scope") or {})
    if scope.get("allowed_capabilities"):
        scope["capability_policy"] = "restricted"
    else:
        scope["allowed_capabilities"] = []
        scope["capability_policy"] = "unified"
    brief["scope"] = scope
    return with_selfdev_scope(task_type, brief)


def select_executor_type(task_brief: dict[str, Any]) -> str:
    initial = task_brief.get("initial_input")
    if isinstance(initial, dict) and isinstance(initial.get("deterministic_action"), dict):
        return "deterministic"
    return "agent"


def enqueue_selfdev_task(
    db, goal: str, *, files: list[str] | None = None, promote: bool = False,
    acceptance_criteria: list[str] | None = None, initial_input: dict[str, Any] | None = None,
):
    from aiive.runtime.conversation_state import get_current_thread_id
    from aiive.task_runtime.repository import TaskRepository

    return TaskRepository(db).create_task(
        thread_id=get_current_thread_id(db), task_type="selfdev", goal=goal,
        task_brief={
            "acceptance_criteria": acceptance_criteria or [],
            "initial_input": {**(initial_input or {}), "files": files or [], "promote_requested": promote},
        },
    )
