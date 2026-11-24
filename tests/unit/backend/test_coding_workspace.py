"""Scripted model decisions through real Worker functions and local processes."""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from aiive.config import settings
from aiive.db.models import AgentAction, ApprovalRequest, Thread
from aiive.task_runtime.agent_executor import TaskAgentExecutor
from aiive.task_runtime.evidence import EvidenceStore
from aiive.task_runtime.function_decisions import proposal_function_name
from aiive.task_runtime.repository import TaskRepository
from aiive.task_runtime.runtime import PersistentTaskRuntime
from aiive.tools.local_shell import resolve_bash


class ScriptedCodingModel:
    def __init__(self, steps):
        self.steps = iter(steps)

    def bind_tools(self, tools, **kwargs):
        self.functions = {item["function"]["name"]: item["function"] for item in tools}
        return self

    def invoke(self, messages, **kwargs):
        if kwargs["config"].get("run_name") == "task_report":
            return AIMessage(content="Coding checks complete.")
        tool, args = next(self.steps)
        name = proposal_function_name(tool) if tool != "task_finish" else tool
        definition = self.functions[name]
        if tool == "read_file":
            assert "preconditions" not in definition["parameters"]["properties"]
        return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": name}],
                         usage_metadata={"input_tokens": 30, "output_tokens": 10, "total_tokens": 40})


def _call(tool, **arguments):
    return tool, {"arguments": arguments}


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _result(db, action):
    from aiive.db.models import TaskEvidence

    item = db.query(TaskEvidence).filter_by(action_id=action.id).one()
    data, more = EvidenceStore.read(db, item, 0, item.content_size)
    assert not more
    result = json.loads(data)
    return result.get("result", result.get("details", result))


@pytest.mark.parametrize("git", [True, False])
def test_workspace_edit_patch_and_failed_test_recovery(db_session, tmp_path, monkeypatch, git):
    if not resolve_bash() or not shutil.which("git"):
        pytest.skip("Bash and Git required")
    monkeypatch.setattr(settings, "aiive_data_dir", str(tmp_path / "data"))
    source = tmp_path / "source"
    source.mkdir()
    (source / "test_probe.py").write_text("def test_probe():\n    assert 1 == 2\n", encoding="utf-8")
    (source / "second.txt").write_bytes(b"before\r\n")
    if git:
        _git(source, "init", "-q")
        _git(source, "add", ".")
        _git(source, "-c", "user.name=AIive Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")
    thread = Thread(title="Coding Worker")
    db_session.add(thread)
    db_session.flush()
    task = TaskRepository(db_session).create_task(
        thread_id=thread.id, task_type="coding", goal="Fix probe and edit second file.",
        task_brief={"scope": {"allowed_roots": [str(source)], "env": {"AIIVE_TEST_WORKSPACE": "active"}}},
        budgets={"max_actions_per_run": 16}, enqueue=False,
    )
    steps = [
        _call("run_shell", command='test "$AIIVE_TEST_WORKSPACE" = active && python -m pytest -q test_probe.py'),
        _call("read_file", path="test_probe.py"),
        _call("edit_file", path="test_probe.py", old_text="assert 1 == 2", new_text="assert 2 == 2"),
        _call("edit_file", path="second.txt", old_text="before", new_text="after", expected_sha256="0" * 64),
        _call("apply_patch", patch="--- /dev/null\n+++ third.txt\n@@ -0,0 +1 @@\n+created\n"),
        _call("run_shell", command="python -m pytest -q test_probe.py" + (" && git diff" if git else "")),
        ("task_finish", {"summary": "Tests passed."}),
    ]
    worker = TaskAgentExecutor(db_session, chat_model=ScriptedCodingModel(steps))
    result = PersistentTaskRuntime(db_session, agent_executor=worker).run(task.id)
    assert result["status"] == "succeeded", result
    actions = db_session.query(AgentAction).filter_by(task_id=task.id).order_by(AgentAction.sequence).all()
    assert [row.status for row in actions] == ["failed", *["succeeded"] * 5], [
        (row.capability_id, row.status, row.error_message) for row in actions
    ]
    workspace = Path(task.task_state["worktree_path"])
    assert (workspace / "second.txt").read_bytes() == b"after\r\n"
    assert _result(db_session, actions[3])["rebased"]
    assert (workspace / "third.txt").read_text(encoding="utf-8") == "created\n"
    assert "assert 2 == 2" in (workspace / "test_probe.py").read_text(encoding="utf-8")
    assert (source / "second.txt").read_bytes() == b"before\r\n"
    assert db_session.query(ApprovalRequest).filter_by(task_id=task.id).count() == 0


@pytest.mark.skipif(not shutil.which("rg") or not shutil.which("npm"), reason="rg and Node required")
def test_selfdev_candidate_with_real_gate_and_supervisor(db_session, tmp_path, monkeypatch):
    from aiive.eval import gate
    from aiive.prompts import get_prompt_registry
    from aiive.supervisor.client import SupervisorClient
    from aiive.supervisor.daemon import Supervisor, SupervisorConfig
    from aiive.supervisor.release_manager import ReleaseManager
    from aiive.supervisor.slot_manager import SlotManager
    from aiive.tools.shell_sessions import read_session

    if not resolve_bash() or not shutil.which("git"):
        pytest.skip("Bash and Git required")
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
        "import os, time\nfrom aiive.coding_probe import VALUE\n"
        "\ndef test_repair():\n    if os.environ.get('AIIVE_TEST_SLOW'):\n        time.sleep(31)\n"
        "    assert VALUE == 2\n",
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
    deploy.init_slots()
    shutil.copytree(source, deploy.active_app_root(), dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git"))
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
        _call("run_shell", command="rg -l 'def build_stable_contract' backend/aiive/runtime"),
        _call("read_file", path="backend/aiive/runtime/system_contract.py"),
        _call("edit_file", path=contract, old_text=heading, new_text=heading + "\n\nCoding acceptance probe."),
        _call("edit_file", path=desktop, old_text=first_line, new_text="// Coding acceptance probe\n" + first_line),
        _call("edit_file", path=target, old_text="VALUE = 0", new_text="VALUE = 1"),
        _call("run_shell", command="git diff --stat && git diff --check"),
        _call("run_selfdev_repair", goal="Validate candidate while probe still fails", files=[target], workspace="."),
        _call("read_file", path=target),
        _call("edit_file", path=target, old_text="VALUE = 1", new_text="VALUE = 2"),
        _call("run_shell", command="AIIVE_TEST_SLOW=1 python -m pytest tests/unit/backend/test_health.py -q",
              timeout_seconds=60),
        _call("run_shell", command=build,
              cwd="frontend", timeout_seconds=180),
        _call("run_shell", command="printf begin; sleep 2; printf end; exit 7", background=True),
        _call("run_selfdev_repair", goal="Validate all current workspace changes"),
        ("task_finish", {"summary": "Candidate tested."}),
    ]

    class WaitForBackground(ScriptedCodingModel):
        def invoke(self, messages, **kwargs):
            installed = Path(task.task_state["worktree_path"]) / "frontend" / "node_modules"
            if deps.is_dir() and not installed.is_dir():
                subprocess.run(["node", "-e", "require('fs').symlinkSync(process.argv[1], process.argv[2], 'junction')",
                                str(deps), str(installed)], check=True, capture_output=True)
            actions = db_session.query(AgentAction).filter_by(task_id=task.id).order_by(AgentAction.sequence).all()
            if actions and actions[-1].arguments.get("background"):
                payload = _result(db_session, actions[-1])
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    session = read_session(task.id, payload["session_id"])
                    if session["status"] == "finished":
                        assert session["exit_code"] == 7 and session["stdout"] == "beginend"
                        name = proposal_function_name("shell_session")
                        assert name in self.functions
                        return AIMessage(content="", tool_calls=[{
                            "name": name, "args": {"arguments": {"session_id": payload["session_id"], "stdout_offset": 5}},
                            "id": name,
                        }], usage_metadata={"input_tokens": 30, "output_tokens": 10, "total_tokens": 40})
                    time.sleep(0.1)
                else:
                    pytest.fail("Bash session did not finish")
            return super().invoke(messages, **kwargs)

    worker = TaskAgentExecutor(db_session, chat_model=WaitForBackground(steps))
    result = PersistentTaskRuntime(db_session, agent_executor=worker).run(task.id)
    actions = db_session.query(AgentAction).filter_by(task_id=task.id).order_by(AgentAction.sequence).all()
    assert result["status"] == "succeeded", (result, [
        (row.capability_id, row.status, row.error_message) for row in actions
    ][-8:])
    [session_action] = [row for row in actions if row.capability_id == "shell_session"]
    assert session_action.status == "succeeded"
    assert _result(db_session, session_action)["stdout"] == "end"
    actions = [row for row in actions if row.id != session_action.id]
    assert [row.status for row in actions] == ["failed" if i == 6 else "succeeded" for i in range(13)], [
        (row.capability_id, row.status, row.error_message, _result(db_session, row))
        for row in actions if row.status != "succeeded"
    ]
    assert "backend/aiive/runtime/system_contract.py" in _result(db_session, actions[0])["stdout"].replace("\\", "/")
    assert _result(db_session, actions[1])["complete"]
    assert _result(db_session, actions[7])["content"].splitlines() == ["VALUE = 1"]
    assert _result(db_session, actions[9])["duration_ms"] > 30_000
    staged = _result(db_session, actions[-1])
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
        release = ReleaseManager(client=SupervisorClient(supervisor.paths)).promote()
        assert release["status"] == "switch_requested", release
        supervisor.tick()
        outcome = SupervisorClient(supervisor.paths).result(release["request_id"])
        assert outcome["ok"], outcome
        assert deploy.get_active_slot() == "B"
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health?runtime=true", timeout=5) as response:
            runtime = json.load(response)["runtime"]
        assert runtime["loaded_slot"] == "B" and runtime["pid"] != old_pid
        assert Path(runtime["system_contract_module"]).is_relative_to(deploy.active_app_root())
        expected = deploy._read_manifest(deploy.base_dir / "B")["stable_contract_audit_ref"]
        assert runtime["stable_contract_audit_ref"] == expected
        assert expected != get_prompt_registry().render("agent.stable_contract").audit_ref
    finally:
        supervisor.stop_managed_process()
