"""Domain tool handlers (implementation) split from builtin_tools."""

from aiive.context.run_context import RunContext
from aiive.tools.base import BaseTool, DatabaseTool

from pathlib import Path
from typing import override, Any


def _reader(ctx: RunContext | None, workspace: str):
    from aiive.selfdev.project_reader import ProjectReader
    from aiive.tools.workspace_files import workspace_path

    if ctx and ctx.task_id:
        if ctx.task_workspace:
            return ProjectReader(workspace_path(ctx, workspace or "."))
        if not ctx.canonical_source_root or workspace:
            raise ValueError("task_workspace_required")
        return ProjectReader(Path(ctx.canonical_source_root))
    return ProjectReader(Path(workspace) if workspace else None)


class CreateSelfdevPlanTool(BaseTool):
    @override
    def _run(self, goal: str = "", files: list[str] | None = None, workspace: str = "", ctx: Any = None):
        reader = _reader(ctx, workspace)
        context = reader.collect_context(goal, explicit_files=files)
        if ctx and ctx.task_id:
            return {"goal": goal, "workspace": str(reader.repo_root), "context": context.to_dict()}
        from aiive.core.llm_client import default_llm_client
        from aiive.selfdev.planner import SelfDevPlanner

        return SelfDevPlanner(default_llm_client()).plan(goal, context=context)


class ApplyPatchToInactiveSlotTool(BaseTool):
    @override
    def _run(self, operations: list[dict[str, Any]] | None = None, ctx: Any = None):
        """将补丁应用到非活跃槽位（不影响当前运行版本）。

        参数:
            operations: 操作列表，每项包含 operation、target_file、content 等字段。
                        来自 create_selfdev_plan 的 plan.operations。
        """
        from aiive.selfdev.patch_executor import PatchExecutor
        from aiive.selfdev.repair_guard import (
            CandidateSlotGuard,
            apply_candidate_patch,
            finalize_candidate_patch,
        )
        from aiive.selfdev.targeted_test_runner import TargetedTestRunner

        frozen = operations or []
        if ctx and ctx.task_id:
            from aiive.tools.workspace_files import workspace_path, write_workspace_file

            if not ctx.task_workspace:
                return {"ok": False, "error": "task_workspace_required"}

            applied = []
            for operation in frozen:
                relative = operation["target_file"]
                action = operation.get("operation", "add_file")
                if operation.get("not_allowed_yet"):
                    continue
                if action == "delete_file":
                    workspace_path(ctx, relative).unlink()
                elif action in {"add_file", "modify_file"}:
                    write_workspace_file(relative, operation["content"], ctx=ctx)
                else:
                    return {"ok": False, "error": "unsupported_file_operation", "operations_applied": applied}
                applied.append(relative)
            return {"operations_applied": applied, "changed": bool(applied),
                    "workspace": ctx.task_workspace}
        executor = PatchExecutor()
        # 与自主修复闭环共用同一把候选代码互斥锁与候选状态标记。
        result = apply_candidate_patch(executor, frozen)
        if result.get("error") == "concurrent_repair_in_progress":
            return result
        test_report = None
        if result.get("operations_applied"):
            changed = [str(item.get("target_file", "")) for item in frozen if item.get("target_file")]
            candidate_root = Path(str(result.get("candidate_root") or ""))
            test_report = TargetedTestRunner(
                repo_root=candidate_root,
            ).run_for_changed_files(changed)
            result = {**result, "targeted_test_report": test_report}
        result["candidate_state"] = finalize_candidate_patch(
            result,
            tests_ok=isinstance(test_report, dict) and test_report.get("ok") is True,
            guard=CandidateSlotGuard(manager=getattr(executor, "_manager", None)),
            reason="targeted_tests_failed" if result.get("operations_applied") else "apply_failed",
        )
        return result


def _promote_slot(task_id: str | None) -> dict[str, Any]:
    from aiive.supervisor.client import supervisor_client_from_settings
    from aiive.supervisor.release_manager import ReleaseManager

    return ReleaseManager(client=supervisor_client_from_settings()).promote(task_id=task_id)


class PromoteSlotTool(BaseTool):
    @override
    def _run(self, ctx: Any = None):
        """健康检查通过后将非活跃槽位提升为活跃版本。

        启用 Supervisor 时切换由守护进程执行，返回 status=switch_requested；
        此时"已晋升"必须由后续读取（/api/selfdev/supervisor 或新进程状态）确认。
        """
        return _promote_slot(ctx.task_id if ctx else None)


class RunSelfdevRepairTool(BaseTool):
    @override
    def _run(self,
        goal: str = "", max_rounds: int = 3, promote: bool = False,
        files: list[str] | None = None, workspace: str = "", ctx: Any = None,
    ):
        """For a Worker, validate and stage the workspace. Legacy callers retain the planner."""
        if ctx and ctx.task_id:
            from aiive.selfdev.workspace_delivery import deliver_workspace
            from aiive.tools.workspace_files import workspace_path

            root = workspace_path(ctx, workspace or ".")
            if root != workspace_path(ctx):
                return {"ok": False, "error": "workspace_must_be_task_root"}
            result = deliver_workspace(root, task_id=ctx.task_id, run_id=ctx.action_id, files=files)
            if result.get("ok") and promote:
                result["promotion"] = _promote_slot(ctx.task_id)
                result["ok"] = result["promotion"].get("ok", False)
            return result
        from aiive.selfdev.repair_loop import SelfDevRepairLoop
        return SelfDevRepairLoop(max_rounds=max_rounds, reader=_reader(ctx, workspace)).run(
            goal, promote=promote, files=files, trace_id=ctx.trace_id if ctx else None,
            run_id=(ctx.action_id or None) if ctx else None,
        )


class RollbackSlotTool(BaseTool):
    @override
    def _run(self):
        """回滚到上一个活跃槽位版本。

        自动检测当前活跃槽位（A/B），回退到另一个槽位。
        """
        from aiive.supervisor.client import supervisor_client_from_settings
        from aiive.supervisor.release_manager import ReleaseManager
        return ReleaseManager(client=supervisor_client_from_settings()).rollback()


class PromotionStatusTool(DatabaseTool):
    @override
    def _run(self, db: Any, ctx: Any, action_id: str):
        from aiive.db.models import AgentAction
        from aiive.supervisor.promotion_status import promotion_status
        from aiive.task_runtime.read_repository import TaskReadRepository

        action = db.get(AgentAction, action_id)
        if not ctx or not ctx.task_id or not action or action.task_id != ctx.task_id:
            raise ValueError("promotion_action_unavailable")
        if not TaskReadRepository(db).visible_child("agent_action", action):
            raise ValueError("promotion_action_unavailable")
        request_id = (action.result_summary or {}).get("promotion_request_id")
        if not isinstance(request_id, str):
            raise ValueError("promotion_has_no_supervisor_request")
        return {"request_id": request_id, "status": promotion_status(request_id)}


class RuntimeIdentityTool(BaseTool):
    @override
    def _run(self):
        from aiive.project_paths import runtime_identity

        return runtime_identity()
