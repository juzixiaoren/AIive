"""本机 Bash 执行契约测试（离线；真实 bash，不访问网络）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from aiive.config import settings
from aiive.control.scope import TaskScope
from aiive.control.task_policy import TaskPolicyEngine
from aiive.db.models import AgentAction, Thread
from aiive.task_runtime.agent_executor import AgentDecision
from aiive.task_runtime.repository import TaskRepository
from aiive.task_runtime.runtime import PersistentTaskRuntime
from aiive.task_runtime.schemas import ActionProposal, TaskStatus
from aiive.task_runtime.tool_injection import WORKER_RESIDENT_CAPABILITIES
from aiive.tools import local_shell
from aiive.tools.local_shell import bash_version, resolve_bash, run_bash
from aiive.tools.registry import get_tool_registry
from aiive.tools.shell_policy import SHELL_CAPABILITY_ID, classify_shell_command


class _ScriptedAgent:
    def __init__(self, *proposals: ActionProposal) -> None:
        self.proposals = list(proposals)

    def decide(self, task, capabilities):
        return AgentDecision(
            proposal=self.proposals.pop(0),
            model="fake-task-model",
            token_usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            input_snapshot={"task_id": task.id},
            prompt_refs=["task_runtime.worker_system@test"],
        )


def _task(db_session, scope: dict, goal: str = "执行本机命令"):
    thread = Thread(title="local shell")
    db_session.add(thread)
    db_session.flush()
    return TaskRepository(db_session).create_task(
        thread_id=thread.id,
        goal=goal,
        task_brief={"goal": goal, "scope": scope},
        enqueue=False,
    )


def _require_bash() -> str:
    bash = resolve_bash()
    if bash is None:
        pytest.skip("本机未安装 bash，跳过真实执行用例")
    return bash


def test_resolve_bash_points_to_real_bash() -> None:
    bash = _require_bash()
    assert Path(bash).is_file()
    assert "bash" in bash_version(bash).casefold()


def test_run_shell_executes_real_bash(tmp_path) -> None:
    _require_bash()
    result = run_bash("echo hello", cwd=str(tmp_path), timeout_seconds=20)
    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert result["stdout"] == "hello\n"
    assert result["timed_out"] is False
    assert result["shell"]["kind"] == "bash"
    assert "bash" in result["shell"]["version"].casefold()
    assert result["cwd"] == str(tmp_path)


def test_run_shell_reports_nonzero_exit(tmp_path) -> None:
    _require_bash()
    result = run_bash("echo bad 1>&2; exit 7", cwd=str(tmp_path), timeout_seconds=20)
    assert result["ok"] is False
    assert result["exit_code"] == 7
    assert "bad" in result["stderr"]


def test_run_shell_respects_cwd(tmp_path) -> None:
    _require_bash()
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    result = run_bash("basename \"$PWD\"", cwd=str(workdir), timeout_seconds=20)
    assert result["ok"] is True
    assert result["stdout"].strip() == "workspace"


def test_run_shell_timeout_terminates_process_tree(tmp_path) -> None:
    _require_bash()
    result = run_bash("sleep 30", cwd=str(tmp_path), timeout_seconds=1)
    assert result["timed_out"] is True
    assert result["ok"] is False
    assert result["exit_code"] is None
    assert result["error_type"] == "timeout"
    assert result["duration_ms"] < 15_000


def test_run_shell_truncates_large_output(tmp_path, monkeypatch) -> None:
    _require_bash()
    monkeypatch.setattr(settings, "shell_max_output_bytes", 1000)
    result = run_bash("printf 'a%.0s' {1..5000}", cwd=str(tmp_path), timeout_seconds=20)
    assert result["ok"] is True
    assert len(result["stdout"]) == 1000
    assert result["truncated"]["stdout"] is True


def test_run_shell_isolates_secret_environment(tmp_path, monkeypatch) -> None:
    _require_bash()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("AIIVE_TEST_SECRET", "leak")
    monkeypatch.setenv("AIIVE_VISIBLE_FLAG", "hidden")
    result = run_bash(
        'printf "%s|%s|%s" "$OPENAI_API_KEY" "$AIIVE_TEST_SECRET" "$AIIVE_VISIBLE_FLAG"',
        cwd=str(tmp_path),
        timeout_seconds=20,
    )
    assert result["ok"] is True
    assert result["stdout"] == "||"


def test_run_shell_missing_cwd_returns_structured_error(tmp_path) -> None:
    _require_bash()
    missing = tmp_path / "not-here"
    result = run_bash("echo x", cwd=str(missing), timeout_seconds=5)
    assert result["ok"] is False
    assert result["error_type"] == "cwd_not_found"
    assert result["exit_code"] is None


def test_run_shell_never_falls_back_when_bash_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "shell_bash_path", "")
    monkeypatch.setattr(local_shell.shutil, "which", lambda _name: None)
    monkeypatch.setattr(local_shell, "_WINDOWS_BASH_CANDIDATES", ())
    result = run_bash("echo x", cwd=str(tmp_path), timeout_seconds=5)
    assert result["ok"] is False
    assert result["error_type"] == "shell_unavailable"
    assert result["stdout"] == ""


@pytest.mark.parametrize(
    ("command", "read_only", "uses_network"),
    [
        ("ls -la", True, False),
        ("git status", True, False),
        ("git log --oneline -3", True, False),
        ("cat README.md", True, False),
        ("rm -rf build", False, False),
        ("git push origin main", False, False),
        ("cat a; rm -rf b", False, False),
        ("ls | grep x", False, False),
        ("curl https://example.com", False, True),
        ("wget https://example.com/a", False, True),
        ("", False, False),
    ],
)
def test_shell_command_classification(command: str, read_only: bool, uses_network: bool) -> None:
    verdict = classify_shell_command(command)
    assert verdict.read_only is read_only
    assert verdict.uses_network is uses_network


def test_task_policy_allows_read_only_and_requires_approval_for_write() -> None:
    registry = get_tool_registry()
    registration = registry.get(SHELL_CAPABILITY_ID)
    assert registration is not None
    scope = TaskScope.from_brief({"scope": {"capability_policy": "unified", "allowed_roots": ["/tmp"]}})
    policy = TaskPolicyEngine()
    read_only = policy.decide(registration, scope, SHELL_CAPABILITY_ID, {"command": "ls", "cwd": "/tmp"})
    assert read_only.outcome == "allow"
    write = policy.decide(registration, scope, SHELL_CAPABILITY_ID, {"command": "rm -rf x", "cwd": "/tmp"})
    assert write.outcome == "approval"
    network = policy.decide(
        registration, scope, SHELL_CAPABILITY_ID, {"command": "curl https://example.com", "cwd": "/tmp"},
    )
    assert network.outcome == "block"
    assert network.reason == "network_access_not_in_task_scope"


def test_run_shell_is_resident_core_capability() -> None:
    assert SHELL_CAPABILITY_ID in WORKER_RESIDENT_CAPABILITIES
    registration = get_tool_registry().get(SHELL_CAPABILITY_ID)
    assert registration is not None
    assert registration.executor_kind == "local"
    assert registration.safety.definition_source == "local_builtin"


def test_task_executes_read_only_shell_without_desktop_node(db_session, tmp_path) -> None:
    _require_bash()
    task = _task(db_session, {"capability_policy": "unified", "allowed_roots": [str(tmp_path)]})
    agent = _ScriptedAgent(
        ActionProposal(
            decision="action",
            capability_id="run_shell",
            arguments={"command": "echo from-bash", "cwd": str(tmp_path)},
        ),
        ActionProposal(decision="finish", summary="done"),
    )
    result = PersistentTaskRuntime(db_session, agent_executor=agent).run(task.id)

    assert result["status"] == TaskStatus.SUCCEEDED.value
    action = db_session.query(AgentAction).filter(AgentAction.task_id == task.id).one()
    assert action.capability_id == "run_shell"
    assert action.status == "succeeded"


def test_destructive_shell_requires_approval(db_session, tmp_path) -> None:
    _require_bash()
    task = _task(db_session, {"capability_policy": "unified", "allowed_roots": [str(tmp_path)]})
    agent = _ScriptedAgent(
        ActionProposal(
            decision="action",
            capability_id="run_shell",
            arguments={"command": "rm -rf build", "cwd": str(tmp_path)},
        ),
    )
    result = PersistentTaskRuntime(db_session, agent_executor=agent).run(task.id)

    assert result["status"] == TaskStatus.BLOCKED_APPROVAL.value
    action = db_session.query(AgentAction).filter(AgentAction.task_id == task.id).one()
    assert action.status == "awaiting_approval"
    assert action.requires_approval is True


def test_shell_cwd_outside_allowed_roots_is_blocked(db_session, tmp_path) -> None:
    _require_bash()
    outside = "C:/" if sys.platform.startswith("win") else "/"
    task = _task(db_session, {"capability_policy": "unified", "allowed_roots": [str(tmp_path)]})
    agent = _ScriptedAgent(
        ActionProposal(
            decision="action",
            capability_id="run_shell",
            arguments={"command": "ls", "cwd": outside},
        ),
        ActionProposal(decision="finish", summary="blocked"),
    )
    PersistentTaskRuntime(db_session, agent_executor=agent).run(task.id)

    action = db_session.query(AgentAction).filter(AgentAction.task_id == task.id).one()
    assert action.status == "failed"
    assert "path_outside_task_scope:cwd" in (action.error_message or "")
