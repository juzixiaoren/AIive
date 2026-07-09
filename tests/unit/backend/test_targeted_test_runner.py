"""测试 TargetedTestRunner（定向测试运行器）模块。

验证根据变更文件自动选择相关测试的能力，包括映射匹配、去重和空输入处理。
"""

from aiive.selfdev.targeted_test_runner import TargetedTestRunner


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
