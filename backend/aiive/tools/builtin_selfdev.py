"""Domain tool handlers (implementation) split from builtin_tools."""

from pathlib import Path
from typing import Any


def handle_create_selfdev_plan(goal: str = ""):
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

def handle_apply_patch_to_inactive_slot(operations: list[dict[str, Any]] | None = None):
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

def handle_promote_slot():
    """健康检查通过后将非活跃槽位提升为活跃版本。

    启用 Supervisor 时切换由守护进程执行，返回 status=switch_requested；
    此时"已晋升"必须由后续读取（/api/selfdev/supervisor 或新进程状态）确认。
    """
    from aiive.supervisor.client import supervisor_client_from_settings
    from aiive.supervisor.release_manager import ReleaseManager
    return ReleaseManager(client=supervisor_client_from_settings()).promote()

def handle_run_selfdev_repair(
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

def handle_rollback_slot():
    """回滚到上一个活跃槽位版本。

    自动检测当前活跃槽位（A/B），回退到另一个槽位。
    """
    from aiive.supervisor.client import supervisor_client_from_settings
    from aiive.supervisor.release_manager import ReleaseManager
    return ReleaseManager(client=supervisor_client_from_settings()).rollback()
