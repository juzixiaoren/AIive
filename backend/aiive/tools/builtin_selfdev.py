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
    from aiive.selfdev.targeted_test_runner import TargetedTestRunner

    frozen = operations or []
    result = PatchExecutor().apply_to_inactive(frozen)
    if result.get("operations_applied"):
        changed = [str(item.get("target_file", "")) for item in frozen if item.get("target_file")]
        candidate_root = Path(str(result.get("candidate_root") or ""))
        result = {
            **result,
            "targeted_test_report": TargetedTestRunner(
                repo_root=candidate_root,
            ).run_for_changed_files(changed),
        }
    return result

def handle_promote_slot():
    """健康检查通过后将非活跃槽位提升为活跃版本。

    启用 Supervisor 时切换由守护进程执行，返回 status=switch_requested；
    此时"已晋升"必须由后续读取（/api/selfdev/supervisor 或新进程状态）确认。
    """
    from aiive.supervisor.client import supervisor_client_from_settings
    from aiive.supervisor.release_manager import ReleaseManager
    return ReleaseManager(client=supervisor_client_from_settings()).promote()

def handle_rollback_slot():
    """回滚到上一个活跃槽位版本。

    自动检测当前活跃槽位（A/B），回退到另一个槽位。
    """
    from aiive.supervisor.client import supervisor_client_from_settings
    from aiive.supervisor.release_manager import ReleaseManager
    return ReleaseManager(client=supervisor_client_from_settings()).rollback()
