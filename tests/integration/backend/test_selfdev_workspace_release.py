"""Real SelfDev candidate validation, release and rollback through Supervisor."""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import urllib.request
from pathlib import Path

from aiive.config import settings
from aiive.db.models import AgentAction, ApprovalRequest, Thread
from aiive.task_runtime.agent_executor import TaskAgentExecutor
from aiive.task_runtime.repository import TaskRepository
from aiive.task_runtime.runtime import PersistentTaskRuntime
from aiive.tools.local_shell import resolve_bash
from tests.unit.backend.test_coding_workspace import ScriptedCodingModel, _call, _git, _result


def test_selfdev_candidate_with_real_gate_and_supervisor(db_session, tmp_path, monkeypatch):
    from aiive.eval import gate
    from aiive.prompts import get_prompt_registry
    from aiive.supervisor.client import SupervisorClient
    from aiive.supervisor.daemon import Supervisor, SupervisorConfig
    from aiive.supervisor.release_manager import ReleaseManager
    from aiive.supervisor.slot_manager import SlotManager

    assert resolve_bash() and shutil.which("git") and shutil.which("npm"), "Bash, Git and npm required"
    repo = Path.cwd()
    source = tmp_path / "source"
    source.mkdir()
    ignore = shutil.ignore_patterns("__pycache__", "node_modules", "dist", "*.pyc", "*.egg-info")
    for folder in ("backend", "frontend", "shared", "apps/desktop"):
        shutil.copytree(repo / folder, source / folder, ignore=ignore)
    for file in ("pyproject.toml", "alembic.ini"):
        shutil.copy2(repo / file, source / file)
    pytest_target = source / "tests/unit/backend/test_health.py"
    pytest_target.parent.mkdir(parents=True, exist_ok=True)
    pytest_target.write_text(
        "from aiive.coding_probe import VALUE\n\ndef test_repair():\n    assert VALUE == 2\n",
        encoding="utf-8",
    )
    for filename in ("test_context_assembler.py", "test_prompt_registry.py"):
        (pytest_target.parent / filename).write_text(
            "from aiive.runtime.system_contract import build_stable_contract\n"
            "\ndef test_prompt_assembly():\n    assert build_stable_contract()\n",
            encoding="utf-8",
        )
    (source / ".gitignore").write_text("node_modules/\ndist/\n__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    target = "backend/aiive/coding_probe.py"
    (source / target).write_text("VALUE = 0\n", encoding="utf-8")
    (source / ".env").write_text("FIXTURE_SECRET=source-only\n", encoding="utf-8")
    (source / ".env.example").write_text("FIXTURE_SECRET=\n", encoding="utf-8")
    (source / "logs").mkdir()
    (source / "logs/state.txt").write_text("runtime log", encoding="utf-8")
    _git(source, "init", "-q")
    _git(source, "add", ".")
    _git(source, "-c", "user.name=AIive Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")
    monkeypatch.setenv("AIIVE_SOURCE_ROOT", str(source))
    monkeypatch.setenv("AIIVE_SLOTS_ROOT", str(tmp_path / "slots"))
    monkeypatch.setenv("AIIVE_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("AIIVE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'runtime.db').as_posix()}")
    monkeypatch.setenv("AIIVE_MEMORY_VECTOR_ENABLED", "false")
    monkeypatch.setenv("AIIVE_TOOL_DISCOVERY_VECTOR_ENABLED", "false")
    monkeypatch.setattr(settings, "aiive_data_dir", str(tmp_path / "data"))
    deploy = SlotManager()
    assert not deploy.base_dir.exists()
    deploy.ensure_initialized(source)
    assert deploy.get_active_slot() == "A"
    assert (deploy.active_app_root() / target).read_text(encoding="utf-8") == "VALUE = 0\n"
    assert not (deploy.active_app_root() / ".env").exists()
    assert (deploy.active_app_root() / ".env.example").is_file()
    assert deploy.read_manifest(deploy.base_dir / "B")["slot"] == "B"
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    evaluate = gate.evaluate_promotion_gate
    monkeypatch.setattr(gate, "evaluate_promotion_gate", lambda candidate: evaluate(candidate, persist=False))

    contract = "backend/aiive/prompts/agent/stable_contract.md"
    heading = (source / contract).read_text(encoding="utf-8").splitlines()[0]
    desktop = "apps/desktop/src/node-host/locks.mjs"
    desktop_text = (source / desktop).read_text(encoding="utf-8")
    first_line = next(line for line in desktop_text.splitlines() if line.strip() and desktop_text.count(line) == 1)
    thread = Thread(title="SelfDev Worker")
    db_session.add(thread)
    db_session.flush()
    task = TaskRepository(db_session).create_task(
        thread_id=thread.id, task_type="selfdev", goal="Test cumulative workspace repair and candidate release.",
        task_brief={"acceptance_criteria": ["Preserve prompt and desktop edits after fixing test failures."]},
        budgets={"max_actions_per_run": 24}, enqueue=False,
    )
    deps = repo / "frontend" / "node_modules"
    build = "npm run build" if deps.is_dir() else "npm ci --no-audit --no-fund && npm run build"
    steps = [
        _call("run_shell", command="git grep -l 'def build_stable_contract' -- backend/aiive/runtime"),
        _call("read_file", path="backend/aiive/runtime/system_contract.py"),
        _call("edit_file", path=contract, old_text=heading, new_text=heading + "\n\nCoding acceptance probe."),
        _call("edit_file", path=desktop, old_text=first_line, new_text="// Coding acceptance probe\n" + first_line),
        _call("edit_file", path=target, old_text="VALUE = 0", new_text="VALUE = 1"),
        _call("run_shell", command="git diff --stat && git diff --check"),
        _call("run_selfdev_repair", goal="Validate candidate while probe still fails", files=[target], workspace="."),
        _call("read_file", path=target),
        _call("edit_file", path=target, old_text="VALUE = 1", new_text="VALUE = 2"),
        _call("run_shell", command="python -m pytest tests/unit/backend/test_health.py -q",
              timeout_seconds=60),
        _call("run_shell", command=build,
              cwd="frontend", timeout_seconds=180),
        _call("run_selfdev_repair", goal="Validate all current workspace changes"),
        ("task_finish", {"summary": "Candidate tested."}),
    ]

    class UseInstalledDependencies(ScriptedCodingModel):
        def invoke(self, messages, **kwargs):
            installed = Path(task.task_state["worktree_path"]) / "frontend" / "node_modules"
            if deps.is_dir() and not installed.is_dir():
                subprocess.run(["node", "-e", "require('fs').symlinkSync(process.argv[1], process.argv[2], 'junction')",
                                str(deps), str(installed)], check=True, capture_output=True)
            return super().invoke(messages, **kwargs)

    worker = TaskAgentExecutor(db_session, chat_model=UseInstalledDependencies(steps))
    result = PersistentTaskRuntime(db_session, agent_executor=worker).run(task.id)
    actions = db_session.query(AgentAction).filter_by(task_id=task.id).order_by(AgentAction.sequence).all()
    assert result["status"] == "succeeded", (result, [
        (row.capability_id, row.status, row.error_message) for row in actions
    ][-8:])
    assert [row.status for row in actions] == ["failed" if i == 6 else "succeeded" for i in range(12)], [
        (row.capability_id, row.status, row.error_message, _result(db_session, row))
        for row in actions if row.status != "succeeded"
    ]
    assert "backend/aiive/runtime/system_contract.py" in _result(db_session, actions[0])["stdout"].replace("\\", "/")
    assert _result(db_session, actions[1])["complete"]
    assert _result(db_session, actions[7])["content"].splitlines() == ["VALUE = 1"]
    assert _result(db_session, actions[6])["targeted_test_report"]["failed"] > 0
    staged = _result(db_session, actions[-1])
    workspace = Path(task.task_state["worktree_path"])
    candidate = Path(staged["candidate_root"])
    for root in (workspace, candidate):
        assert not (root / ".env").exists()
        assert not (root / "logs").exists()
        assert (root / ".env.example").is_file()

    assert staged["gate"]["ok"]
    assert any("test:node-host" in check["file"] for check in staged["targeted_test_report"]["results"])
    assert db_session.query(ApprovalRequest).filter_by(task_id=task.id).count() == 0
    assert deploy.get_active_slot() == "A"

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    supervisor = Supervisor(SupervisorConfig(port=port, start_timeout=40, monitor_seconds=0,
                                             state_dir=str(tmp_path / "control")))
    try:
        assert supervisor.recover()["ok"]
        old_pid = supervisor.state()["process"]["pid"]
        from aiive.context.run_context import RunContext
        from aiive.supervisor import client as supervisor_client
        from aiive.tools.builtin_selfdev import PromoteSlotTool, RunSelfdevRepairTool
        from tests.tool_helpers import invoke_test_tool
        from aiive.supervisor.slot_manager import read_candidate_state

        client = SupervisorClient(supervisor.paths)
        publisher = ReleaseManager(client=client)
        monkeypatch.setattr(supervisor_client, "supervisor_client_from_settings", lambda: client)
        context = RunContext(thread_id=thread.id, task_id=task.id, task_workspace=str(workspace),
                             action_id="redelivery", trace_id=task.id, source="task_runtime")
        artifact = workspace / target
        original = artifact.read_text(encoding="utf-8")
        assert read_candidate_state(candidate.parent)["workspace_hash"] == staged["workspace_hash"]
        assert "another_task" in publisher.promote(task_id="other")["error"]

        # Changes after delivery and after an accepted control request both block release.
        artifact.write_text(original + "# newer\n", encoding="utf-8")
        denied = invoke_test_tool(PromoteSlotTool, ctx=context)
        assert denied["error"].startswith("workspace_changed_since_validation"), denied
        artifact.write_text(original, encoding="utf-8")
        pending = publisher.promote(task_id=task.id)
        assert pending["status"] == "switch_requested", pending
        artifact.write_text(original + "# publish this revision\n", encoding="utf-8")
        supervisor.tick()
        rejected = client.result(pending["request_id"])
        assert rejected["error"].startswith("workspace_changed_since_validation"), rejected
        assert deploy.get_active_slot() == "A"

        updated = invoke_test_tool(RunSelfdevRepairTool, promote=True, ctx=context)
        assert updated["ok"] and updated["workspace_hash"] != staged["workspace_hash"], updated
        assert (candidate / target).read_text(encoding="utf-8") == original + "# publish this revision\n"
        release = updated["promotion"]
        assert release["status"] == "switch_requested", release
        supervisor.tick()
        outcome = SupervisorClient(supervisor.paths).result(release["request_id"])
        assert outcome["ok"], outcome
        assert deploy.get_active_slot() == "B"
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health?runtime=true", timeout=5) as response:
            runtime = json.load(response)["runtime"]
        assert runtime["loaded_slot"] == "B" and runtime["pid"] != old_pid
        assert Path(runtime["system_contract_module"]).is_relative_to(deploy.active_app_root())
        expected = deploy.read_manifest(deploy.base_dir / "B")["stable_contract_audit_ref"]
        assert runtime["stable_contract_audit_ref"] == expected
        assert expected != get_prompt_registry().render("agent.stable_contract").audit_ref
        deploy.ensure_initialized(source)
        assert deploy.get_active_slot() == "B"
        rollback = publisher.rollback()
        assert rollback["status"] == "switch_requested", rollback
        supervisor.tick()
        assert client.result(rollback["request_id"])["ok"]
        assert deploy.get_active_slot() == "A"
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health?runtime=true", timeout=5) as response:
            assert json.load(response)["runtime"]["loaded_slot"] == "A"
    finally:
        supervisor.stop_managed_process()
