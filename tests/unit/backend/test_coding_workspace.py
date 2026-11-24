"""Scripted model decisions through real Worker functions and local processes."""
from __future__ import annotations

import json
import shutil
import subprocess
import time
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
    (source / ".env").write_text("TEST_SECRET=fixture\n", encoding="utf-8")
    (source / ".env.example").write_text("TEST_SECRET=\n", encoding="utf-8")
    for folder in (".data", "slots", "runtime", "pgdata", "postgres", "qdrant", "object_store", "logs",
                   "backend/aiive/runtime"):
        path = source / folder
        path.mkdir(parents=True, exist_ok=True)
        (path / "fixture.py").write_text("VALUE = 1\n", encoding="utf-8")
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
    assert not (workspace / ".env").exists()
    assert (workspace / ".env.example").is_file()
    assert (workspace / "backend/aiive/runtime/fixture.py").is_file()
    assert not any((workspace / name).exists() for name in
                   (".data", "slots", "runtime", "pgdata", "postgres", "qdrant", "object_store", "logs"))
    assert _result(db_session, actions[3])["rebased"]
    assert (workspace / "third.txt").read_text(encoding="utf-8") == "created\n"
    assert "assert 2 == 2" in (workspace / "test_probe.py").read_text(encoding="utf-8")
    assert (source / "second.txt").read_bytes() == b"before\r\n"
    assert db_session.query(ApprovalRequest).filter_by(task_id=task.id).count() == 0



@pytest.mark.parametrize("invalid", ["zero", "many", "malformed"])
@pytest.mark.parametrize("recovers", [True, False])
def test_worker_corrects_invalid_tool_calls_without_dispatch(db_session, tmp_path, monkeypatch, invalid, recovers):
    from langchain_core.messages import ToolMessage, convert_to_openai_messages

    monkeypatch.setattr(settings, "aiive_data_dir", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "tool_argument_max_retries", 2)
    source = tmp_path / "source"
    source.mkdir()
    (source / "probe.txt").write_text("hello", encoding="utf-8")
    thread = Thread(title="Coding correction")
    db_session.add(thread)
    db_session.flush()
    task = TaskRepository(db_session).create_task(
        thread_id=thread.id, task_type="coding", goal="Read the probe",
        task_brief={"scope": {"allowed_roots": [str(source)]}}, enqueue=False,
    )
    read = proposal_function_name("read_file")
    call = {"name": read, "args": {"arguments": {"path": "probe.txt"}}, "id": "original_read"}
    bad = {
        "zero": AIMessage(content="I will read the file."),
        "many": AIMessage(content="", tool_calls=[call, {**call, "id": "second_read"}]),
        "malformed": AIMessage(content="", invalid_tool_calls=[
            {"name": None, "args": "{broken", "id": "bad_call", "error": "invalid JSON"},
        ]),
    }[invalid]

    class CorrectingModel(ScriptedCodingModel):
        decisions = 0
        reports = 0

        def invoke(self, messages, **kwargs):
            if kwargs["config"].get("run_name") == "task_report":
                self.reports += 1
                return AIMessage(content="Reported outcome.")
            self.decisions += 1
            if self.decisions == 1:
                return bad
            if self.decisions == 2 or not recovers:
                assert db_session.query(AgentAction).filter_by(task_id=task.id).count() == 0
                wire = convert_to_openai_messages(messages)
                if invalid == "zero":
                    assert json.loads(wire[-1]["content"])["issues"][0]["code"] == "tool_call_count"
                    assert wire[-2]["role"] != "assistant"
                else:
                    feedback = [m for m in messages if isinstance(m, ToolMessage)]
                    assert [m.tool_call_id for m in feedback] == (
                        ["original_read", "second_read"] if invalid == "many" else ["bad_call"]
                    )
                    assert json.loads(feedback[0].content)["issues"][0]["code"] == (
                        "tool_call_count" if invalid == "many" else "invalid_function_name"
                    )
                    if invalid == "many":
                        assert messages[-3].tool_calls[0]["args"] == call["args"]
            return super().invoke(messages, **kwargs) if recovers else bad

    model = CorrectingModel([_call("read_file", path="probe.txt"), ("task_finish", {"summary": "Read probe."})])
    worker = TaskAgentExecutor(db_session, chat_model=model)
    result = PersistentTaskRuntime(db_session, agent_executor=worker).run(task.id)
    actions = db_session.query(AgentAction).filter_by(task_id=task.id).all()
    assert model.decisions == 3 and model.reports == 1
    assert result["status"] == ("succeeded" if recovers else "partial"), result
    if recovers:
        assert [action.capability_id for action in actions] == ["read_file"]
        assert _result(db_session, actions[0])["content"] == "hello"
    else:
        assert not actions


@pytest.mark.parametrize("cancel", [False, True])
def test_worker_waits_for_background_streams_and_exit(db_session, tmp_path, monkeypatch, cancel):
    if not resolve_bash():
        pytest.skip("Bash required")
    monkeypatch.setattr(settings, "aiive_data_dir", str(tmp_path / "data"))
    source = tmp_path / "source"
    source.mkdir()
    thread = Thread(title="Background shell")
    db_session.add(thread)
    db_session.flush()
    task = TaskRepository(db_session).create_task(
        thread_id=thread.id, task_type="coding", goal="Wait for shell output",
        task_brief={"scope": {"allowed_roots": [str(source)]}},
        budgets={"max_actions_per_run": 24}, enqueue=False,
    )
    command = "printf begin; sleep 2; printf end; printf err >&2; sleep 1; exit 7"
    if cancel:
        command = "printf begin; sleep 60"
    stdout, stderr = [], []

    class WaitingModel(ScriptedCodingModel):
        decisions = 0

        def invoke(self, messages, **kwargs):
            if kwargs["config"].get("run_name") == "task_report":
                return AIMessage(content="Background shell observed.")
            self.decisions += 1
            actions = db_session.query(AgentAction).filter_by(task_id=task.id).order_by(AgentAction.sequence).all()
            if not actions:
                return super().invoke(messages, **kwargs)
            current = _result(db_session, actions[-1])
            if actions[-1].capability_id == "shell_session":
                stdout.append(current["stdout"])
                stderr.append(current["stderr"])
                if current["status"] == "finished":
                    if cancel:
                        assert current["cancelled"]
                    else:
                        assert current["exit_code"] == 7 and not current["command_ok"]
                    return super().invoke(messages, **kwargs)
            name = proposal_function_name("shell_session")
            args = {"session_id": current["session_id"], "wait_seconds": 20,
                    "stdout_offset": sum(map(len, stdout)), "stderr_offset": sum(map(len, stderr))}
            if cancel and stdout and not current["cancel_requested"]:
                args["cancel"] = True
            return AIMessage(content="", tool_calls=[{"name": name, "id": f"wait_{self.decisions}",
                                                    "args": {"arguments": args}}])

    model = WaitingModel([_call("run_shell", command=command, background=True),
                          ("task_finish", {"summary": "Observed exit."})])
    before = time.monotonic()
    worker = TaskAgentExecutor(db_session, chat_model=model)
    result = PersistentTaskRuntime(db_session, agent_executor=worker).run(task.id)
    assert result["status"] == "succeeded", result
    assert "".join(stdout) == ("begin" if cancel else "beginend")
    assert "".join(stderr) == ("" if cancel else "err")
    assert stdout[0] == "begin"
    assert model.decisions <= 6 and time.monotonic() - before < 30
