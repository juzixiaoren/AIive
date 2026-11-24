"""
定向测试运行模块。

根据变更文件路径自动匹配相关的单元测试并执行。
维护 TEST_MAPPING 映射表，将源代码目录映射到对应的测试文件列表。
仅运行与变更相关的测试，而非全量测试。
"""

import logging
import os
import subprocess
import shutil
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from aiive.project_paths import source_root

TEST_MAPPING = {
    "backend/aiive/": ["tests/unit/backend/test_health.py"],
    "backend/aiive/core/": ["tests/unit/backend/test_agent_graph.py", "tests/unit/backend/test_identity_memory.py"],
    "backend/aiive/memory/": ["tests/unit/backend/test_memory_store.py", "tests/unit/backend/test_memory_gate.py", "tests/unit/backend/test_memory_extractor.py", "tests/unit/backend/test_memory_types.py", "tests/unit/backend/test_steward_signal_extractor.py"],
    "backend/aiive/tools/": [
        "tests/unit/backend/test_tool_registry.py", "tests/unit/backend/test_safe_delete.py",
        "tests/unit/backend/test_local_shell.py", "tests/unit/backend/test_shell_output_capture.py",
        "tests/unit/backend/test_tool_execution_cancellation.py", "tests/unit/backend/test_tool_result_contract.py",
    ],
    "backend/aiive/mcp/": ["tests/unit/backend/test_mcp_discovery.py", "tests/unit/backend/test_mcp_installer.py", "tests/unit/backend/test_mcp_runtime_client.py"],
    "backend/aiive/supervisor/": ["tests/unit/backend/test_slot_manager.py", "tests/unit/backend/test_supervisor_health.py"],
    "backend/aiive/selfdev/": [
        "tests/unit/backend/test_selfdev_planner.py", "tests/unit/backend/test_selfdev_repair.py",
        "tests/unit/backend/test_patch_executor.py", "tests/unit/backend/test_selfdev_repair_guard.py",
    ],
    "backend/aiive/eval/": [
        "tests/unit/backend/test_eval_gate.py", "tests/unit/backend/test_eval_contracts.py",
    ],
    "backend/aiive/runtime/": [
        "tests/unit/backend/test_context_assembler.py", "tests/unit/backend/test_tool_calling_runtime.py",
        "tests/unit/backend/test_runtime_module_boundaries.py", "tests/unit/backend/test_prompt_registry.py",
    ],
    "backend/aiive/prompts/": ["tests/unit/backend/test_prompt_registry.py", "tests/unit/backend/test_context_assembler.py"],
    "backend/aiive/config.py": ["tests/unit/backend/test_health.py", "tests/unit/backend/test_data_paths.py"],
    "backend/aiive/project_paths.py": ["tests/unit/backend/test_slot_manager.py", "tests/unit/backend/test_supervisor_lifecycle.py"],
    "pyproject.toml": ["tests/unit/backend/test_health.py", "tests/unit/backend/test_prompt_registry.py"],
    "backend/aiive/task_runtime/": [
        "tests/unit/backend/test_persistent_task_runtime.py", "tests/unit/backend/test_worker_tool_injection.py",
        "tests/unit/backend/test_tool_discovery.py",
    ],
    "backend/aiive/task_runtime/evidence.py": ["tests/unit/backend/test_worker_tool_result_backfill.py"],
    "backend/aiive/tool_discovery/": [
        "tests/unit/backend/test_tool_discovery.py", "tests/unit/backend/test_worker_tool_injection.py",
    ],
    "backend/aiive/control/": ["tests/unit/backend/test_persistent_task_runtime.py", "tests/integration/backend/test_approval_security.py"],
    "backend/aiive/desktop/": ["tests/unit/backend/test_desktop_nodes.py", "tests/unit/backend/test_persistent_task_runtime.py"],
    "backend/aiive/db/": ["tests/unit/backend/test_schema_migrations.py"],
    "backend/alembic/": ["tests/unit/backend/test_schema_migrations.py"],
    "alembic.ini": ["tests/unit/backend/test_schema_migrations.py"],
    ".github/": ["tests/unit/backend/test_health.py", "tests/unit/backend/test_runtime_module_boundaries.py"],
    "frontend/": [],
    "apps/desktop/": [],
    "deploy/": ["tests/unit/backend/test_supervisor_lifecycle.py"],
    "scripts/": ["tests/unit/backend/test_supervisor_lifecycle.py"],
}


class TargetedTestRunner:
    """定向测试运行器，根据变更文件选择并执行相关测试。"""

    def __init__(self, repo_root: Path | None = None):
        """
        初始化测试运行器。

        参数:
            repo_root: 项目根目录路径，默认使用配置的 canonical source root。
        """
        self._repo_root: Path = repo_root or source_root()
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
        paths = [path.replace("\\", "/").removeprefix("./") for path in changed_files]
        frontend = [path for path in paths if path.startswith(("frontend/", "shared/"))]
        desktop = any(path.startswith("apps/desktop/") for path in paths)
        shell_scripts = sorted(path for path in paths if path.endswith(".sh") and (self._repo_root / path).is_file())
        yaml_files = sorted(path for path in paths if path.endswith((".yaml", ".yml")) and
                            (self._repo_root / path).is_file())
        compose = any(Path(path).name.startswith(("compose.", "docker-compose.")) for path in paths)
        dockerfiles = [path for path in paths if Path(path).name.startswith("Dockerfile")]
        checks = []
        if frontend:
            scripts = {
                "test:chat": ("chat", "message", "composer"),
                "test:work": ("work", "task"),
                "test:inspector": ("inspector", "context"),
            }
            matching = [script for script, names in scripts.items()
                        if any(name in path.casefold() for path in frontend for name in names)]
            checks.extend(self._npm_checks("frontend", [*(matching or scripts), "build"], install=True))
        if desktop:
            checks.extend(self._npm_checks("apps/desktop", ["test:node-host"]))
        if shell_scripts:
            from aiive.tools.local_shell import resolve_bash

            bash = resolve_bash()
            checks.extend(self._command(path, [bash, "-n", path]) if bash else
                          {"file": path, "result": "error", "error": "bash not found"} for path in shell_scripts)
        if yaml_files:
            checks.append(self._command("YAML", [
                sys.executable, "-c",
                "import pathlib,sys,yaml; [list(yaml.safe_load_all(pathlib.Path(p).read_text(encoding='utf-8'))) for p in sys.argv[1:]]",
                *yaml_files,
            ]))
        docker = shutil.which("docker")
        if compose:
            checks.append(self._command("docker compose config", [docker or "docker", "compose", "config", "--quiet",
                                                                  "--no-interpolate", "--no-env-resolution"]))
        for path in dockerfiles:
            if (self._repo_root / path).is_file():
                checks.append(self._command(path, [docker or "docker", "buildx", "build", "--check", "-f", path, "."]))
        if not tests and not checks:
            if changed_files:
                return {"ok": False, "degraded": True, "tests_run": [],
                        "reason": "No validation mapped for changed files"}
            return {"ok": True, "tests_run": [], "reason": "No tests mapped, skipped"}
        report = self._run_tests(tests, None) if tests else {"results": []}
        results = report["results"] + checks
        passed = sum(item["result"] == "passed" for item in results)
        report = {"ok": passed == len(results), "degraded": not results, "total": len(results),
                  "passed": passed, "failed": len(results) - passed, "results": results}
        if artifact_dir:
            import json

            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "test_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report

    def _command(self, name: str, argv: list[str], *, cwd: Path | None = None) -> dict[str, Any]:
        try:
            proc = subprocess.run(argv, cwd=cwd or self._repo_root, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=240, check=False)
            return {"file": name, "result": "passed" if proc.returncode == 0 else "failed",
                    "stdout": (proc.stdout + proc.stderr)[-5000:]}
        except (OSError, subprocess.SubprocessError) as error:
            return {"file": name, "result": "error", "error": str(error)}

    def _npm_checks(self, directory: str, scripts: list[str], *, install: bool = False) -> list[dict[str, Any]]:
        root = self._repo_root / directory
        npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
        if not npm or not (root / "package.json").is_file():
            return [{"file": directory, "result": "error", "error": "npm or package.json missing"}]
        if install and not (root / "node_modules").is_dir():
            installed = self._command(f"{directory}: npm ci", [npm, "ci", "--no-audit", "--no-fund"], cwd=root)
            if installed["result"] != "passed":
                return [installed]
        return [self._command(f"{directory}: npm run {script}", [npm, "run", script], cwd=root)
                for script in scripts]

    def _select_tests(self, files: list[str]) -> list[str]:
        """
        根据变更文件匹配对应的测试文件。

        参数:
            files: 变更文件路径列表。

        返回:
            去重排序后的测试文件路径列表。
        """
        selected: set[str] = set()
        for raw in files:
            f = raw.replace("\\", "/").removeprefix("./")
            if f.startswith("tests/") and Path(f).name.startswith("test_") and f.endswith(".py"):
                if (self._repo_root / f).is_file():
                    selected.add(f)
            for prefix, test_files in TEST_MAPPING.items():
                if f.startswith(prefix):
                    selected.update(test_files)
        return sorted(selected)

    def _run_tests(self, test_files: list[str], artifact_dir: Path | None) -> dict[str, Any]:
        """
        执行指定的测试文件列表，收集结果。

        每个测试文件在候选代码环境中独立运行。
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
        env = {**os.environ, "PYTHONPATH": str((self._repo_root / "backend").resolve()),
               "PYTHONIOENCODING": "utf-8"}

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
                    [sys.executable, "-m", "pytest", str(test_path), "-q", "--tb=short"],
                    cwd=str(self._repo_root),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                    timeout=120,
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
