"""
定向测试运行模块。

根据变更文件路径自动匹配相关的单元测试并执行。
维护 TEST_MAPPING 映射表，将源代码目录映射到对应的测试文件列表。
仅运行与变更相关的测试，而非全量测试。
"""

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 仓库根：本文件位于 backend/aiive/selfdev/targeted_test_runner.py
# parents[3] = selfdev -> aiive -> backend -> 仓库根
_DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[3]

# 源代码目录 → 测试文件映射表
# 当某目录下的文件发生变更时，自动运行对应的测试文件
TEST_MAPPING = {
    "backend/aiive/": ["tests/unit/backend/test_health.py"],
    "backend/aiive/core/": ["tests/unit/backend/test_agent_graph.py", "tests/unit/backend/test_identity_memory.py"],
    "backend/aiive/memory/": ["tests/unit/backend/test_memory_store.py", "tests/unit/backend/test_memory_gate.py", "tests/unit/backend/test_memory_extractor.py", "tests/unit/backend/test_memory_types.py", "tests/unit/backend/test_steward_signal_extractor.py"],
    "backend/aiive/tools/": ["tests/unit/backend/test_tool_registry.py", "tests/unit/backend/test_safe_delete.py"],
    "backend/aiive/mcp/": ["tests/unit/backend/test_mcp_discovery.py", "tests/unit/backend/test_mcp_installer.py", "tests/unit/backend/test_mcp_runtime_client.py"],
    "backend/aiive/supervisor/": ["tests/unit/backend/test_slot_manager.py", "tests/unit/backend/test_supervisor_health.py"],
    "backend/aiive/selfdev/": ["tests/unit/backend/test_selfdev_planner.py"],
    "backend/aiive/prompts/": ["tests/unit/backend/test_prompt_registry.py"],
    "backend/aiive/task_runtime/": ["tests/unit/backend/test_persistent_task_runtime.py"],
    "backend/aiive/control/": ["tests/unit/backend/test_persistent_task_runtime.py", "backend/tests/test_approval_security.py"],
    "backend/aiive/desktop/": ["tests/unit/backend/test_desktop_nodes.py", "tests/unit/backend/test_persistent_task_runtime.py"],
    "frontend/": [],
}


class TargetedTestRunner:
    """定向测试运行器，根据变更文件选择并执行相关测试。"""

    def __init__(self, repo_root: Path | None = None):
        """
        初始化测试运行器。

        参数:
            repo_root: 项目根目录路径，默认从当前文件向上推导。
        """
        self._repo_root: Path = repo_root or _DEFAULT_REPO_ROOT
        if repo_root is None and not (self._repo_root / "backend" / "aiive").is_dir():
            logger.warning(
                "推导的仓库根疑似不正确（缺少 backend/aiive）: %s", self._repo_root
            )

    def run_for_changed_files(
        self, changed_files: list[str], artifact_dir: Path | None = None
    ) -> dict[str, Any]:
        """
        根据变更文件列表，选择并运行相关的单元测试。

        参数:
            changed_files: 变更文件路径列表。
            artifact_dir: 测试报告输出目录，可选。

        返回:
            测试报告字典，包含 ok、total、passed、failed、results 等字段。
        """
        tests = self._select_tests(changed_files)
        frontend_changed = any(path.replace("\\", "/").startswith("frontend/") for path in changed_files)
        if not tests and not frontend_changed:
            if changed_files:
                return {
                    "ok": False,
                    "degraded": True,
                    "tests_run": [],
                    "reason": "No validation mapped for changed files",
                }
            return {"ok": True, "tests_run": [], "reason": "No tests mapped, skipped"}
        report: dict[str, Any] = self._run_tests(tests, artifact_dir) if tests else {
            "ok": True, "degraded": False, "total": 0, "passed": 0, "failed": 0, "results": [],
        }
        if frontend_changed:
            frontend_result = self._run_frontend_build()
            results = report.get("results")
            if not isinstance(results, list):
                results = []
                report["results"] = results
            results.append(frontend_result)
            report["total"] = int(report.get("total", 0)) + 1
            if frontend_result["result"] == "passed":
                report["passed"] = int(report.get("passed", 0)) + 1
            else:
                report["failed"] = int(report.get("failed", 0)) + 1
                report["ok"] = False
        report["degraded"] = int(report.get("passed", 0)) + int(report.get("failed", 0)) == 0
        return report

    def _run_frontend_build(self) -> dict[str, Any]:
        frontend = self._repo_root / "frontend"
        if not (frontend / "package.json").is_file():
            return {"file": "frontend", "result": "failed", "reason": "candidate frontend missing"}
        try:
            proc = subprocess.run(
                ["npm", "run", "build"],
                cwd=frontend,
                capture_output=True,
                text=True,
                timeout=180,
            )
            return {
                "file": "frontend",
                "result": "passed" if proc.returncode == 0 else "failed",
                "stdout": (proc.stdout + proc.stderr)[-2000:],
            }
        except Exception as error:
            return {"file": "frontend", "result": "error", "error": str(error)}

    def _select_tests(self, files: list[str]) -> list[str]:
        """
        根据变更文件匹配对应的测试文件。

        参数:
            files: 变更文件路径列表。

        返回:
            去重排序后的测试文件路径列表。
        """
        selected: set[str] = set()
        for f in files:
            for prefix, test_files in TEST_MAPPING.items():
                if f.startswith(prefix):
                    selected.update(test_files)
        return sorted(selected)

    def _run_tests(self, test_files: list[str], artifact_dir: Path | None) -> dict[str, Any]:
        """
        执行指定的测试文件列表，收集结果。

        每个测试文件独立运行，超时时间为 30 秒。
        测试报告可选地写入 artifact_dir。

        参数:
            test_files: 测试文件路径列表。
            artifact_dir: 测试报告输出目录。

        返回:
            测试报告字典。
        """
        results = []
        passed = 0
        failed = 0

        for tf in test_files:
            test_path = self._repo_root / tf
            if not test_path.exists():
                # 映射声明的验证缺失意味着 Candidate 不完整，必须 fail closed。
                failed += 1
                results.append({"file": tf, "result": "failed", "reason": "not found"})
                continue

            try:
                # 用当前解释器（sys.executable）而非硬编码 "python3"（Windows 不存在）
                proc = subprocess.run(
                    [sys.executable, "-m", "pytest", str(test_path), "-q", "--tb=no"],
                    cwd=str(self._repo_root),
                    capture_output=True,
                    text=True,
                    timeout=30,  # 单个测试文件超时 30 秒
                )
                ok = proc.returncode == 0
                if ok:
                    passed += 1
                else:
                    failed += 1
                results.append({
                    "file": tf,
                    "result": "passed" if ok else "failed",
                    "stdout": (proc.stdout + proc.stderr)[-2000:],
                })
            except subprocess.TimeoutExpired:
                logger.warning("测试超时: %s", tf)
                failed += 1
                results.append({"file": tf, "result": "timeout"})
            except Exception as e:
                logger.error("测试执行失败: %s", tf, exc_info=True)
                failed += 1
                results.append({"file": tf, "result": "error", "error": str(e)})

        executed = passed + failed
        # 全部 skipped（未真正执行任何测试）不能算成功：ok=False 并标记 degraded
        report = {
            "ok": failed == 0 and executed > 0,
            "degraded": executed == 0,
            "total": len(test_files),
            "passed": passed,
            "failed": failed,
            "results": results,
        }

        # 如果指定了输出目录，写入 JSON 测试报告
        if artifact_dir:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            import json
            (artifact_dir / "test_report.json").write_text(json.dumps(report, indent=2))

        return report
