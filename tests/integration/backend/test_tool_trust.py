from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from aiive.config import settings
from aiive.control.approval_service import TaskApprovalService
from aiive.control.tool_trust import ToolTrustPolicy
from aiive.core.llm_client import FakeLLMClient
from aiive.db.base import get_db
from aiive.db.models import AgentAction, ApprovalRequest, Base, DesktopNode
from aiive.desktop.connection_manager import desktop_connection_manager
from aiive.main import create_app
from aiive.runtime.agent_graph import AgentGraph
from aiive.runtime.turn_tool_surface import build_turn_tool_registry
from aiive.task_runtime.repository import TaskRepository
from aiive.task_runtime.runtime import PersistentTaskRuntime
from aiive.task_runtime.schemas import ActionProposal, ActionStatus, TaskStatus
from aiive.tools.registry import get_tool_registry
from aiive.tools.langchain_adapter import build_langchain_tools
from tests.unit.backend.test_persistent_task_runtime import SequenceAgent, _task, _thread


def test_setting_is_shared_by_clients_and_survives_database_reopen(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "aiive_auth_enabled", False)
    url = f"sqlite:///{tmp_path / 'settings.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    app = create_app()

    def session():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = session
    endpoint = "/api/settings/tool-trust"
    web, android = TestClient(app), TestClient(app)
    with factory.begin() as db:
        db.add(DesktopNode(id="settings-node", name="Settings desktop", platform="win32", arch="x64", status="online",
                           lease_expires_at=datetime.now(UTC) + timedelta(minutes=5), capabilities=[
                               {"name": "desktop_fs_write_text", "description": "Write"},
                               {"name": "desktop_fs_read_text", "description": "Read"},
                           ]))
    monkeypatch.setattr(desktop_connection_manager, "is_connected", lambda node_id: node_id == "settings-node")
    initial = web.get(endpoint).json()
    assert initial["mode"] == "standard" and initial["approval_exempt_capabilities"] == []
    tools = {item["identity"]["capability_id"]: item for item in initial["tools"]}
    assert tools["run_shell"]["standard_approval_condition"] == "non_read_only_command"
    assert tools["desktop_fs_write_text"]["standard_requires_approval"]
    assert not tools["desktop_fs_read_text"]["standard_requires_approval"]
    assert tools["delegate_task"]["identity"]["definition_source"] == "task_runtime"
    grant = tools["desktop_fs_write_text"]["identity"]
    selection = {"mode": "partial_trust", "approval_exempt_capabilities": [grant]}
    assert web.put(endpoint, json={**selection, "approval_exempt_capabilities": [{**grant, "descriptor_hash": "stale"}]}).status_code == 409
    assert web.put(endpoint, json=selection).status_code == 200
    assert android.get(endpoint).json()["approval_exempt_capabilities"] == [grant]
    assert android.get(endpoint).json()["mode"] == "partial_trust"
    assert android.put(endpoint, json={"mode": "full_trust"}).json()["approval_exempt_capabilities"] == [grant]
    engine.dispose()
    reopened = create_engine(url)
    try:
        factory.configure(bind=reopened)
        restored = TestClient(app).get(endpoint).json()
        assert (restored["mode"], restored["approval_exempt_capabilities"]) == ("full_trust", [grant])
    finally:
        reopened.dispose()


@pytest.mark.parametrize(("mode", "approval"), [("standard", True), ("full_trust", False)])
def test_main_graph_uses_persisted_tool_trust(db_session, mode, approval):
    ToolTrustPolicy.save(db_session, mode, None)
    registry = build_turn_tool_registry()
    calls = []

    def assistant(_messages):
        if calls:
            return AIMessage(content="Done")
        calls.append("forget")
        return AIMessage(content="", tool_calls=[{
            "id": "forget-call", "name": "forget", "args": {"memory_ids": ["fixture"]},
        }])

    # Full trust reaches the tool boundary, where the missing durable Turn identity still blocks side effects.
    graph, records, pending = AgentGraph(FakeLLMClient(), db_session)._build_graph(
        RunnableLambda(assistant), build_langchain_tools(registry), registry,
    )
    graph.invoke({"messages": [HumanMessage(content="Forget fixture")]})
    assert [item["name"] for item in pending] == (["forget"] if approval else [])
    if not approval:
        assert records[0]["name"] == "forget"
        assert records[0]["result"]["error_type"] == "missing_execution_identity"


@pytest.mark.parametrize("decision", ["approve", "deny"])
def test_pending_workspace_action_requires_explicit_decision_after_switch(db_session, tmp_path, monkeypatch, decision):
    monkeypatch.setattr(settings, "aiive_data_dir", str(tmp_path / "data"))
    source = tmp_path / "source"
    source.mkdir()
    task = _task(db_session, _thread(db_session), task_type="coding", task_brief={
        "scope": {"allowed_roots": [str(source)]},
    })
    agent = SequenceAgent(
        ActionProposal(decision="action", capability_id="write_file", arguments={"path": "new.txt", "content": "approved"}),
        ActionProposal(decision="finish", summary="done"),
    )
    runtime = PersistentTaskRuntime(db_session, agent_executor=agent)
    pending = runtime.run(task.id)
    assert pending["status"] == "blocked_approval"
    output = Path(task.task_state["worktree_path"]) / "new.txt"
    ToolTrustPolicy.save(db_session, "full_trust", None)
    db_session.commit()
    assert db_session.get(ApprovalRequest, pending["approval_id"]).status == "pending"
    assert db_session.get(AgentAction, pending["action_id"]).status == "awaiting_approval"
    assert task.status == "blocked_approval" and not output.exists()

    decided = TaskApprovalService(db_session).decide(pending["approval_id"], decision)
    db_session.commit()
    assert decided["action"] == ("succeeded" if decision == "approve" else "denied")
    assert not output.exists()
    if decision == "approve":
        assert runtime.run(task.id, trigger="approval_granted")["status"] == "succeeded"
        assert output.read_text(encoding="utf-8") == "approved"
    else:
        assert task.status == "failed"


@pytest.mark.parametrize(("mode", "expected"), [("standard", "blocked_approval"), ("full_trust", "succeeded")])
def test_ready_action_uses_mode_at_dispatch(db_session, tmp_path, mode, expected):
    target = tmp_path / "ready.txt"
    target.write_text("old", encoding="utf-8")
    task = _task(db_session, _thread(db_session), task_brief={"scope": {
        "allowed_roots": [str(tmp_path)], "allowed_capabilities": ["safe_delete"],
    }})
    repo = TaskRepository(db_session)
    task, run = repo.start_run(task.id, trigger="dispatch")
    tool = get_tool_registry().get("safe_delete")
    action = repo.create_action(
        task=task, run=run, capability_id="safe_delete", arguments={"path": str(target)},
        descriptor_hash=tool.safety.descriptor_hash, risk_level=tool.safety.risk_level, requires_approval=False,
    )
    repo.transition_action(action, ActionStatus.VALIDATED.value)
    repo.transition_action(action, ActionStatus.READY.value)
    repo.finish_run(run)
    repo.transition_task(task, TaskStatus.QUEUED.value)
    ToolTrustPolicy.save(db_session, mode, None)
    db_session.commit()

    agent = SequenceAgent(ActionProposal(decision="finish", summary="done"))
    result = PersistentTaskRuntime(db_session, agent_executor=agent).run(task.id)
    assert result["status"] == expected
    if mode == "standard":
        assert target.exists() and action.status == "awaiting_approval"
        assert action.execution_token is None
        assert db_session.get(ApprovalRequest, result["approval_id"]).status == "pending"
    else:
        assert not target.exists() and action.status == "succeeded"
