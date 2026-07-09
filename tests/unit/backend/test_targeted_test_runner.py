from aiive.selfdev.targeted_test_runner import TargetedTestRunner


class TestTargetedTestRunner:
    def test_selects_tests_for_memory_changes(self):
        runner = TargetedTestRunner()
        tests = runner._select_tests(["backend/aiive/memory/memory_store.py"])
        assert len(tests) > 0
        assert any("memory_store" in t for t in tests)

    def test_selects_tests_for_tools_changes(self):
        runner = TargetedTestRunner()
        tests = runner._select_tests(["backend/aiive/tools/registry.py"])
        assert len(tests) > 0
        assert any("tool_registry" in t for t in tests)

    def test_no_tests_for_frontend_changes(self):
        runner = TargetedTestRunner()
        tests = runner._select_tests(["frontend/src/App.tsx"])
        assert tests == []

    def test_no_tests_for_unknown_path(self):
        runner = TargetedTestRunner()
        tests = runner._select_tests(["some/unknown/file.py"])
        assert tests == []

    def test_deduplicates_duplicate_mappings(self, tmp_path):
        runner = TargetedTestRunner()
        # Same prefix should not duplicate test files
        tests = runner._select_tests([
            "backend/aiive/memory/store.py",
            "backend/aiive/memory/gate.py",
        ])
        # Check no duplicates
        assert len(tests) == len(set(tests))

    def test_run_for_empty_changed_files(self):
        runner = TargetedTestRunner()
        result = runner.run_for_changed_files([])
        assert result["ok"] is True
        assert result["tests_run"] == []
