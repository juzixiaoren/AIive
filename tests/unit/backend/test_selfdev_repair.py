"""自主修复闭环测试：代码理解、补丁生成、有限修复、安全门与受控晋升。

全部用例离线运行：LLM 用脚本替身，测试运行器用假报告，不访问网络、不需要 API Key。
"""

import json
from pathlib import Path

from aiive.core.llm_client import FakeLLMClient
from aiive.selfdev.planner import SelfDevPlanner
from aiive.selfdev.project_reader import (
    ContextFile,
    ProjectContext,
    ProjectReader,
    format_context_for_prompt,
)
from aiive.selfdev.repair_loop import (
    SelfDevRepairLoop,
    patch_fingerprint,
    persist_repair_run,
)

# ── 测试替身 ──────────────────────────────────────────────────────


class ScriptedPlanner:
    """按脚本返回补丁计划，并记录每次调用的入参。"""

    def __init__(self, plans):
        self._plans = list(plans)
        self.calls = []

    def plan(
        self, goal, trace_id=None, *, context=None,
        previous_plan=None, failure_feedback=None,
    ):
        self.calls.append({
            "goal": goal,
            "trace_id": trace_id,
            "context": context,
            "previous_plan": previous_plan,
            "failure_feedback": failure_feedback,
        })
        if not self._plans:
            return {"operations": []}
        return self._plans.pop(0)


class FakeReader:
    """返回固定上下文，并记录调用。"""

    def __init__(self, context=None):
        self.context = context or ProjectContext()
        self.calls = []

    def collect_context(self, goal, *, explicit_files=None):
        self.calls.append({"goal": goal, "files": explicit_files})
        return self.context


class FakeExecutor:
    """按脚本返回应用结果，并记录每次应用的补丁。"""

    def __init__(self, results=None):
        self._results = list(results or [])
        self.calls = []

    def apply_to_inactive(self, operations):
        self.calls.append(operations)
        if not self._results:
            return {
                "ok": True,
                "candidate_root": "/tmp/candidate/app",
                "operations_applied": list(operations),
                "operations_failed": [],
            }
        return self._results.pop(0)


class _Runner:
    def __init__(self, reports):
        self._reports = reports

    def run_for_changed_files(self, changed_files):
        if not self._reports:
            return _passed_report()
        return self._reports.pop(0)


class RunnerFactory:
    """共享同一份报告队列，模拟多轮测试结果。"""

    def __init__(self, reports):
        self._reports = list(reports)
        self.roots = []

    def __call__(self, candidate_root):
        self.roots.append(candidate_root)
        return _Runner(self._reports)


class PromoteRecorder:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.result


def _plan(content="v1", target="backend/aiive/foo.py", blocked=False):
    return {
        "goal_summary": "fix foo",
        "operations": [{
            "operation": "modify_file",
            "target_file": target,
            "content": content,
            "reason": "fix",
            "not_allowed_yet": blocked,
        }],
        "test_plan": "pytest",
        "requires_schema_change": False,
    }


def _failed_report(message="AssertionError: boom"):
    return {
        "ok": False,
        "total": 1,
        "passed": 0,
        "failed": 1,
        "results": [{
            "file": "tests/unit/backend/test_x.py",
            "result": "failed",
            "stdout": message,
        }],
    }


def _passed_report():
    return {
        "ok": True,
        "total": 1,
        "passed": 1,
        "failed": 0,
        "results": [{
            "file": "tests/unit/backend/test_x.py",
            "result": "passed",
        }],
    }


def _loop(planner, *, reports, max_rounds=3, gate=None, promote=None, executor=None):
    return SelfDevRepairLoop(
        planner=planner,
        reader=FakeReader(),
        executor=executor or FakeExecutor(),
        test_runner_factory=RunnerFactory(reports),
        gate_runner=gate or (lambda _root: {"ok": True}),
        promote_runner=promote or PromoteRecorder({"ok": True}),
        max_rounds=max_rounds,
    )


# ── 补丁指纹 ──────────────────────────────────────────────────────


class TestPatchFingerprint:
    def test_same_operations_produce_same_fingerprint(self):
        assert patch_fingerprint(_plan("v1")["operations"]) == patch_fingerprint(
            _plan("v1")["operations"]
        )

    def test_content_change_changes_fingerprint(self):
        assert patch_fingerprint(_plan("v1")["operations"]) != patch_fingerprint(
            _plan("v2")["operations"]
        )

    def test_target_change_changes_fingerprint(self):
        assert patch_fingerprint(_plan("v1")["operations"]) != patch_fingerprint(
            _plan("v1", target="backend/aiive/bar.py")["operations"]
        )


# ── 修复循环 ──────────────────────────────────────────────────────


class TestRepairLoop:
    def test_succeeds_after_one_repair(self):
        planner = ScriptedPlanner([_plan("v1"), _plan("v2")])
        loop = _loop(planner, reports=[_failed_report(), _passed_report()])

        record = loop.run("fix foo")

        assert record["ok"] is True
        assert record["status"] == "tests_passed"
        assert len(record["rounds"]) == 2
        assert record["rounds"][0]["test_ok"] is False
        assert record["rounds"][1]["test_ok"] is True
        assert record["rounds"][0]["patch_fingerprint"] != record["rounds"][1]["patch_fingerprint"]
        # 问题分析与最终补丁指纹必须可追溯
        assert record["analysis"]["goal_summary"] == "fix foo"
        assert record["final_patch_fingerprint"] == record["rounds"][1]["patch_fingerprint"]
        # 第二轮必须拿到真实错误与上一轮计划
        assert "AssertionError: boom" in (planner.calls[1]["failure_feedback"] or "")
        assert planner.calls[1]["previous_plan"] is not None

    def test_max_rounds_exhausted_never_reports_success(self):
        planner = ScriptedPlanner([_plan("v1"), _plan("v2")])
        executor = FakeExecutor()
        promote = PromoteRecorder({"ok": True})
        loop = _loop(
            planner,
            reports=[_failed_report("err-1"), _failed_report("err-2")],
            max_rounds=1,
            promote=promote,
            executor=executor,
        )

        record = loop.run("fix foo", promote=True)

        assert record["ok"] is False
        assert record["status"] == "tests_failed"
        assert record["failure_reason"] == "max_rounds_exhausted"
        assert len(record["rounds"]) == 2
        assert executor.calls and len(executor.calls) == 2
        assert promote.calls == 0

    def test_repeated_patch_stops_before_second_apply(self):
        planner = ScriptedPlanner([_plan("same"), _plan("same")])
        executor = FakeExecutor()
        loop = _loop(
            planner,
            reports=[_failed_report()],
            max_rounds=3,
            executor=executor,
        )

        record = loop.run("fix foo")

        assert record["ok"] is False
        assert record["status"] == "tests_failed"
        assert record["failure_reason"] == "repeated_patch_fingerprint"
        assert len(record["rounds"]) == 1
        assert len(executor.calls) == 1

    def test_blocked_plan_never_applies(self):
        executor = FakeExecutor()
        loop = _loop(
            ScriptedPlanner([_plan("v1", blocked=True)]),
            reports=[],
            executor=executor,
        )

        record = loop.run("fix foo")

        assert record["status"] == "blocked"
        assert record["failure_reason"] == "planner_produced_no_executable_operations"
        assert executor.calls == []

    def test_apply_failure_stops_with_reason(self):
        executor = FakeExecutor([{
            "ok": False,
            "candidate_root": "/tmp/candidate/app",
            "operations_applied": [],
            "operations_failed": [{"reason": "path_escape_denied"}],
        }])
        loop = _loop(ScriptedPlanner([_plan("v1")]), reports=[], executor=executor)

        record = loop.run("fix foo")

        assert record["status"] == "apply_failed"
        assert "path_escape_denied" in (record["failure_reason"] or "")
        assert record["rounds"][0]["failed"] == 1

    def test_planner_exception_is_reported_not_faked(self):
        class BrokenPlanner:
            def plan(self, *_args, **_kwargs):
                raise RuntimeError("llm down")

        loop = _loop(BrokenPlanner(), reports=[])
        record = loop.run("fix foo")

        assert record["status"] == "planner_failed"
        assert record["ok"] is False

    def test_gate_failure_blocks_promotion(self):
        promote = PromoteRecorder({"ok": True})
        loop = _loop(
            ScriptedPlanner([_plan("v1")]),
            reports=[_passed_report()],
            gate=lambda _root: {"ok": False, "error": "kernel contract check(s) failed"},
            promote=promote,
        )

        record = loop.run("fix foo", promote=True)

        assert record["status"] == "gate_failed"
        assert record["ok"] is False
        assert "kernel contract" in (record["failure_reason"] or "")
        assert promote.calls == 0

    def test_promotion_requested_after_gate(self):
        promote = PromoteRecorder({"ok": True, "status": "switch_requested"})
        loop = _loop(
            ScriptedPlanner([_plan("v1")]),
            reports=[_passed_report()],
            promote=promote,
        )

        record = loop.run("fix foo", promote=True)

        assert record["status"] == "promote_requested"
        assert record["ok"] is True
        assert record["promotion"]["status"] == "switch_requested"
        assert promote.calls == 1

    def test_promotion_failure_is_reported(self):
        promote = PromoteRecorder({"ok": False, "error": "Health check failed"})
        loop = _loop(
            ScriptedPlanner([_plan("v1")]),
            reports=[_passed_report()],
            promote=promote,
        )

        record = loop.run("fix foo", promote=True)

        assert record["status"] == "promote_failed"
        assert record["ok"] is False
        assert "Health check failed" in (record["failure_reason"] or "")

    def test_promotion_not_called_when_disabled(self):
        promote = PromoteRecorder({"ok": True})
        loop = _loop(
            ScriptedPlanner([_plan("v1")]),
            reports=[_passed_report()],
            promote=promote,
        )

        record = loop.run("fix foo", promote=False)

        assert record["status"] == "tests_passed"
        assert promote.calls == 0

    def test_gate_exception_is_fail_closed(self):
        def broken_gate(_root):
            raise RuntimeError("gate crashed")

        promote = PromoteRecorder({"ok": True})
        loop = _loop(
            ScriptedPlanner([_plan("v1")]),
            reports=[_passed_report()],
            gate=broken_gate,
            promote=promote,
        )

        record = loop.run("fix foo", promote=True)

        assert record["status"] == "gate_failed"
        assert record["failure_reason"] == "gate_raised"
        assert promote.calls == 0

    def test_default_gate_runner_targets_slot_root(self, monkeypatch):
        """默认闸门接收槽位根（candidate_root 的父目录），而不是 app 目录。"""
        captured = {}

        def fake_gate(slot_root, **kwargs):
            captured["slot_root"] = slot_root
            return {"ok": True}

        monkeypatch.setattr("aiive.eval.gate.evaluate_promotion_gate", fake_gate)
        loop = SelfDevRepairLoop(
            planner=ScriptedPlanner([_plan("v1")]),
            reader=FakeReader(),
            executor=FakeExecutor(),
            test_runner_factory=RunnerFactory([_passed_report()]),
            promote_runner=PromoteRecorder({"ok": True}),
        )

        record = loop.run("fix foo")

        assert record["status"] == "tests_passed"
        assert str(captured["slot_root"]).replace("\\", "/").endswith("candidate")

    def test_context_is_collected_and_recorded(self):
        context = ProjectContext(
            files=[ContextFile(
                path="backend/aiive/foo.py",
                content="def foo():\n    return 1\n",
                truncated=False,
                bytes=24,
                reason="search",
            )],
            matches=[{"path": "backend/aiive/foo.py", "line": 1, "text": "def foo():"}],
            keywords=["foo"],
        )
        planner = ScriptedPlanner([_plan("v1")])
        loop = SelfDevRepairLoop(
            planner=planner,
            reader=FakeReader(context),
            executor=FakeExecutor(),
            test_runner_factory=RunnerFactory([_passed_report()]),
            gate_runner=lambda _root: {"ok": True},
            promote_runner=PromoteRecorder({"ok": True}),
        )

        record = loop.run("fix foo")

        assert record["context"]["files"] == ["backend/aiive/foo.py"]
        assert record["context"]["keywords"] == ["foo"]
        assert record["context"]["matches"] == 1


# ── 真实 PatchExecutor：只改非活跃槽位 ────────────────────────────


class TestInactiveSlotIsolation:
    def test_patch_only_touches_inactive_slot(self, tmp_path):
        from aiive.selfdev.patch_executor import PatchExecutor
        from aiive.supervisor.slot_manager import SlotManager

        manager = SlotManager(base_dir=tmp_path / "slots")
        manager.init_slots()
        active_app = tmp_path / "slots" / "A" / "app"
        active_app.mkdir(parents=True, exist_ok=True)
        (active_app / "existing.py").write_text("original", encoding="utf-8")

        loop = SelfDevRepairLoop(
            planner=ScriptedPlanner([_plan("patched", target="existing.py")]),
            reader=FakeReader(),
            executor=PatchExecutor(manager=manager),
            test_runner_factory=RunnerFactory([_passed_report()]),
            gate_runner=lambda _root: {"ok": True},
            promote_runner=PromoteRecorder({"ok": True}),
        )

        record = loop.run("fix existing.py")

        assert record["status"] == "tests_passed"
        assert (active_app / "existing.py").read_text(encoding="utf-8") == "original"
        inactive_app = tmp_path / "slots" / "B" / "app"
        assert (inactive_app / "existing.py").read_text(encoding="utf-8") == "patched"


# ── Project Reader ────────────────────────────────────────────────


def _build_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "backend" / "aiive").mkdir(parents=True)
    (root / "tests" / "unit" / "backend").mkdir(parents=True)
    (root / "backend" / "aiive" / "foo.py").write_text(
        "def target_function():\n    return 'ok'\n", encoding="utf-8"
    )
    (root / "tests" / "unit" / "backend" / "test_foo.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    (root / ".env").write_text("SECRET=topsecret\n", encoding="utf-8")
    (root / "data").mkdir()
    (root / "data" / "app.db").write_text("binary", encoding="utf-8")
    return root


class TestProjectReader:
    def test_search_finds_code(self, tmp_path):
        reader = ProjectReader(_build_repo(tmp_path))
        results = reader.search(["target_function"])

        assert any(item["path"] == "backend/aiive/foo.py" for item in results)

    def test_denies_secrets_and_runtime_data(self, tmp_path):
        reader = ProjectReader(_build_repo(tmp_path))

        assert reader.read(".env") is None
        assert reader.read("data/app.db") is None

    def test_denies_path_traversal_and_absolute(self, tmp_path):
        root = _build_repo(tmp_path)
        (tmp_path / "outside.py").write_text("secret", encoding="utf-8")
        reader = ProjectReader(root)

        assert reader.read("../outside.py") is None
        assert reader.read(str(tmp_path / "outside.py")) is None

    def test_collect_context_includes_related_test(self, tmp_path):
        reader = ProjectReader(_build_repo(tmp_path))
        context = reader.collect_context("fix target_function in foo")

        paths = [item.path for item in context.files]
        assert "backend/aiive/foo.py" in paths
        assert "tests/unit/backend/test_foo.py" in paths

    def test_format_context_contains_code_and_matches(self):
        context = ProjectContext(
            files=[ContextFile(
                path="backend/aiive/foo.py",
                content="def foo(): pass",
                truncated=False,
                bytes=13,
                reason="search",
            )],
            matches=[{"path": "backend/aiive/foo.py", "line": 1, "text": "def foo():"}],
            keywords=["foo"],
        )
        text = format_context_for_prompt(context)

        assert "def foo(): pass" in text
        assert "backend/aiive/foo.py:1" in text

    def test_format_context_without_files_is_explicit(self):
        text = format_context_for_prompt(ProjectContext())
        assert "not_allowed_yet" in text


# ── Planner 上下文注入 ────────────────────────────────────────────


class TestPlannerContextInjection:
    def test_context_and_feedback_are_in_prompt(self):
        fake = FakeLLMClient(fixed_content=json.dumps({
            "goal_summary": "fix",
            "operations": [],
            "test_plan": "",
            "requires_schema_change": False,
        }))
        planner = SelfDevPlanner(fake)
        context = ProjectContext(
            files=[ContextFile(
                path="backend/aiive/foo.py",
                content="VALUE = '$5 {x}'\n",
                truncated=False,
                bytes=16,
                reason="search",
            )],
            keywords=["foo"],
        )

        planner.plan(
            "fix foo",
            context=context,
            previous_plan={"operations": [{
                "operation": "modify_file",
                "target_file": "backend/aiive/foo.py",
            }]},
            failure_feedback="AssertionError: boom",
        )

        prompt = fake._call_history[0]["messages"][0]["content"]
        assert "backend/aiive/foo.py" in prompt
        assert "VALUE = '$5 {x}'" in prompt
        assert "AssertionError: boom" in prompt
        assert "上一轮补丁" in prompt

    def test_plan_without_context_keeps_backward_compatibility(self):
        fake = FakeLLMClient(fixed_content=json.dumps({
            "goal_summary": "plain",
            "operations": [],
            "test_plan": "",
            "requires_schema_change": False,
        }))
        plan = SelfDevPlanner(fake).plan("plain goal")
        assert plan["goal_summary"] == "plain"


# ── 持久化与接线 ──────────────────────────────────────────────────


class TestPersistenceAndWiring:
    def test_persist_repair_run_writes_request_and_operations(self, db_session):
        from aiive.db.models import PatchOperation, SelfDevRequest

        record = {
            "goal": "fix foo",
            "trace_id": "trace-1",
            "status": "tests_passed",
            "started_at": "2026-01-01T00:00:00+00:00",
            "finished_at": "2026-01-01T00:01:00+00:00",
            "rounds": [{"round": 0, "test_ok": True}],
            "final_operations": [{
                "operation": "modify_file",
                "target_file": "backend/aiive/foo.py",
                "reason": "fix",
                "risk_notes": "",
                "requires_schema_change": False,
                "safe_delete_scope": None,
                "not_allowed_yet": False,
            }],
        }

        request = persist_repair_run(db_session, record)

        assert record["request_id"] == request.id
        stored = db_session.get(SelfDevRequest, request.id)
        assert stored is not None
        assert stored.status == "tests_passed"
        assert stored.plan["repair"]["goal"] == "fix foo"
        operations = db_session.query(PatchOperation).filter(
            PatchOperation.request_id == request.id
        ).all()
        assert len(operations) == 1
        assert operations[0].target_file == "backend/aiive/foo.py"

    def test_repair_route_is_registered(self):
        from aiive.api import routes_selfdev
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(routes_selfdev.router)
        paths = app.openapi()["paths"]

        assert "post" in paths["/api/selfdev/repair"]

    def test_repair_tool_is_registered(self):
        from aiive.tools.builtin_tools import register_builtin_tools
        from aiive.tools.registry import ToolRegistry

        registry = ToolRegistry()
        register_builtin_tools(registry)

        assert registry.get("run_selfdev_repair") is not None
