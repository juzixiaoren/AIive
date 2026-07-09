import os
import subprocess
from pathlib import Path

TEST_MAPPING = {
    "backend/aiive/core/": ["tests/unit/backend/test_llm_client.py", "tests/unit/backend/test_context_builder.py"],
    "backend/aiive/memory/": ["tests/unit/backend/test_memory_store.py", "tests/unit/backend/test_memory_gate.py", "tests/unit/backend/test_memory_extractor.py", "tests/unit/backend/test_memory_types.py", "tests/unit/backend/test_steward_signal_extractor.py"],
    "backend/aiive/tools/": ["tests/unit/backend/test_tool_registry.py", "tests/unit/backend/test_permission_manager.py", "tests/unit/backend/test_safe_delete.py"],
    "backend/aiive/mcp/": ["tests/unit/backend/test_mcp_discovery.py", "tests/unit/backend/test_mcp_installer.py", "tests/unit/backend/test_mcp_runtime_client.py"],
    "backend/aiive/supervisor/": ["tests/unit/backend/test_slot_manager.py", "tests/unit/backend/test_supervisor_health.py"],
    "backend/aiive/selfdev/": ["tests/unit/backend/test_selfdev_planner.py"],
    "frontend/": [],
}


class TargetedTestRunner:
    def __init__(self, repo_root: Path | None = None):
        self._repo_root = repo_root or Path(__file__).resolve().parent.parent.parent.parent.parent

    def run_for_changed_files(
        self, changed_files: list[str], artifact_dir: Path | None = None
    ) -> dict:
        tests = self._select_tests(changed_files)
        if not tests:
            return {"ok": True, "tests_run": [], "reason": "No tests mapped, skipped"}

        return self._run_tests(tests, artifact_dir)

    def _select_tests(self, files: list[str]) -> list[str]:
        selected: set[str] = set()
        for f in files:
            for prefix, test_files in TEST_MAPPING.items():
                if f.startswith(prefix):
                    selected.update(test_files)
        return sorted(selected)

    def _run_tests(self, test_files: list[str], artifact_dir: Path | None) -> dict:
        results = []
        passed = 0
        failed = 0

        for tf in test_files:
            test_path = self._repo_root / tf
            if not test_path.exists():
                results.append({"file": tf, "result": "skipped", "reason": "not found"})
                continue

            try:
                proc = subprocess.run(
                    ["python3", "-m", "pytest", str(test_path), "-q", "--tb=no"],
                    cwd=str(self._repo_root),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                ok = proc.returncode == 0
                if ok:
                    passed += 1
                else:
                    failed += 1
                results.append({
                    "file": tf,
                    "result": "passed" if ok else "failed",
                    "stdout": proc.stdout[:500],
                })
            except subprocess.TimeoutExpired:
                failed += 1
                results.append({"file": tf, "result": "timeout"})
            except Exception as e:
                failed += 1
                results.append({"file": tf, "result": "error", "error": str(e)})

        report = {
            "ok": failed == 0,
            "total": len(test_files),
            "passed": passed,
            "failed": failed,
            "results": results,
        }

        if artifact_dir:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            import json
            (artifact_dir / "test_report.json").write_text(json.dumps(report, indent=2))

        return report
