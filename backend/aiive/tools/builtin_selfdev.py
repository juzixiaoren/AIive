"""Domain tool handlers (implementation) split from builtin_tools."""

from aiive.tools.base import BaseTool, DatabaseTool

from pathlib import Path
from typing import override, Any


class CreateSelfdevPlanTool(BaseTool):
    @override
    def _run(self, goal: str = ""):
        """生成自进化补丁计划。

        参数:
            goal: 改进目标描述

        返回:
            SelfDevPlanner 的计划结果
        """
        from aiive.core.llm_client import default_llm_client
        from aiive.selfdev.planner import SelfDevPlanner
        llm = default_llm_client()
        return SelfDevPlanner(llm).plan(goal)


class ApplyPatchToInactiveSlotTool(BaseTool):
    @override
    def _run(self, operations: list[dict[str, Any]] | None = None):
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


class PromoteSlotTool(BaseTool):
    @override
    def _run(self):
        """健康检查通过后将非活跃槽位提升为活跃版本。

        启用 Supervisor 时切换由守护进程执行，返回 status=switch_requested；
        此时"已晋升"必须由后续读取（/api/selfdev/supervisor 或新进程状态）确认。
        """
        from aiive.supervisor.client import supervisor_client_from_settings
        from aiive.supervisor.release_manager import ReleaseManager
        return ReleaseManager(client=supervisor_client_from_settings()).promote()


class RunSelfdevRepairTool(BaseTool):
    @override
    def _run(self,
        goal: str = "", max_rounds: int = 3, promote: bool = False
    ):
        """自主修复闭环：读码 → 规划 → 补丁 → 定向测试 → 有限修复 → 安全门。

        所有修改只进入非活跃槽位；promote=True 时仍需通过现有安全门与
        ReleaseManager（健康检查、Supervisor 切换、观察窗回滚）。
        """
        from aiive.selfdev.repair_loop import SelfDevRepairLoop
        return SelfDevRepairLoop(max_rounds=max_rounds).run(
            goal, promote=promote,
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
