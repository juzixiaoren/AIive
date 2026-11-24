"""测试 TargetedTestRunner（定向测试运行器）模块。

验证根据变更文件自动选择相关测试的能力，包括映射匹配、去重和空输入处理。
"""

import shutil

from aiive.selfdev.targeted_test_runner import CODING_TEST, SELFDEV_FLOW_TEST, TargetedTestRunner


class TestTargetedTestRunner:
    """测试 TargetedTestRunner 的测试选择和运行功能。"""

    def test_selects_tests_for_memory_changes(self):
        """memory 模块变更应自动选择相关测试。"""
        runner = TargetedTestRunner()
        tests = runner._select_tests(["backend/aiive/memory/memory_store.py"])
        assert len(tests) > 0
        assert any("memory_store" in t for t in tests)

    def test_selects_tests_for_tools_changes(self):
        """tools 模块变更应自动选择相关测试。"""
        runner = TargetedTestRunner()
        tests = runner._select_tests(["backend/aiive/tools/registry.py"])
        assert len(tests) > 0
        assert any("tool_registry" in t for t in tests)

    def test_no_tests_for_frontend_changes(self):
        """前端变更不应对应后端测试。"""
        runner = TargetedTestRunner()
        tests = runner._select_tests(["frontend/src/App.tsx"])
        assert tests == []

    def test_no_tests_for_unknown_path(self):
        """未知路径不应对应任何测试。"""
        runner = TargetedTestRunner()
        tests = runner._select_tests(["some/unknown/file.py"])
        assert tests == []

    def test_changed_file_without_validation_cannot_pass_release_gate(self):
        runner = TargetedTestRunner()
        result = runner.run_for_changed_files(["some/unknown/file.py"])
        assert result["ok"] is False
        assert result["degraded"] is True

    def test_deduplicates_duplicate_mappings(self, tmp_path):
        """相同前缀的多个文件不应导致测试重复。"""
        runner = TargetedTestRunner()
        # 相同前缀不应重复测试文件
        tests = runner._select_tests([
            "backend/aiive/memory/store.py",
            "backend/aiive/memory/gate.py",
        ])
        # 检查无重复
        assert len(tests) == len(set(tests))

    def test_run_for_empty_changed_files(self):
        """空变更列表应返回成功且无测试运行。"""
        runner = TargetedTestRunner()
        result = runner.run_for_changed_files([])
        assert result["ok"] is True
        assert result["tests_run"] == []

    def test_missing_mapped_test_fails_closed_even_when_another_test_passes(self, tmp_path, monkeypatch):
        runner = TargetedTestRunner(repo_root=tmp_path)
        monkeypatch.setattr(
            runner,
            "_select_tests",
            lambda _files: ["tests/present.py", "tests/missing.py"],
        )
        present = tmp_path / "tests" / "present.py"
        present.parent.mkdir(parents=True)
        present.write_text("def test_ok():\n    assert True\n", encoding="utf-8")

        result = runner.run_for_changed_files(["backend/aiive/example.py"])

        assert result["ok"] is False
        assert result["failed"] == 1
        assert any(item["file"].endswith("missing.py") for item in result["results"])

    def test_coding_and_release_workflows_are_mapped_selectively(self):
        runner = TargetedTestRunner()
        assert CODING_TEST in runner._select_tests(["backend/aiive/task_runtime/runtime.py"])
        assert SELFDEV_FLOW_TEST in runner._select_tests(["backend/aiive/selfdev/workspace_delivery.py"])
        assert SELFDEV_FLOW_TEST in runner._select_tests(["backend/aiive/supervisor/standalone.py"])
        assert SELFDEV_FLOW_TEST not in runner._select_tests(["backend/aiive/memory/memory_store.py"])

    def test_docker_unavailable_defers_candidate_validation(self, tmp_path, monkeypatch):
        (tmp_path / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
        monkeypatch.setattr(shutil, "which", lambda name: None)
        result = TargetedTestRunner(tmp_path).run_for_changed_files(["Dockerfile", "compose.yaml"])
        assert result["ok"] is False
        assert result["status"] == "deferred" and result["failed"] == 0 and result["deferred"] == 2
        assert result["missing_capabilities"] == ["docker_cli"]

    def test_docker_validation_failure_is_distinct_from_missing_buildx(self, tmp_path, monkeypatch):
        runner = TargetedTestRunner(tmp_path)
        monkeypatch.setattr(shutil, "which", lambda name: "docker")
        commands = []

        def command(name, argv, **kwargs):
            commands.append(argv)
            if argv[-1] == "--help" and "compose" in argv:
                return {"result": "passed", "stdout": "--no-env-resolution"}
            return {"file": name, "result": "failed", "stdout": "validation failed"}

        monkeypatch.setattr(runner, "_command", command)
        result = runner.run_for_changed_files(["Dockerfile", "infra/compose.custom.yml"])
        assert result["ok"] is False and result["status"] == "failed"
        assert result["failed"] == 1 and result["deferred"] == 1
        assert result["missing_capabilities"] == ["docker_buildx"]
        assert any(argv[1:5] == ["compose", "-f", "infra/compose.custom.yml", "config"] for argv in commands)
