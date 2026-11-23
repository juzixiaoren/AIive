"""Task Action 终态必须与工具真实执行结果一致。"""
from __future__ import annotations

import uuid

from aiive.db.models import AgentAction, AgentRun, AgentTaskEvent, TaskEvidence, Thread
from aiive.task_runtime.agent_executor import AgentDecision
from aiive.task_runtime.repository import TaskRepository
from aiive.task_runtime.runtime import PersistentTaskRuntime
from aiive.task_runtime.schemas import ActionProposal, ActionStatus, TaskStatus
from aiive.tools.base import BaseTool
from aiive.tools.registry import (
    CapabilitySafetySchema,
    ToolDefinition,
    get_tool_registry,
)


class SequenceAgent:
    def __init__(self, *proposals: ActionProposal):
        self.proposals = list(proposals)

    def decide(self, task, capabilities):  # noqa: ANN001 - 测试替身
        proposal = self.proposals.pop(0)
        return AgentDecision(
            proposal=proposal,
            model="fake-task-model",
            token_usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            input_snapshot={"task_id": task.id, "capabilities": capabilities},
            prompt_refs=[],
        )


def _thread(db_session) -> Thread:
    thread = Thread(id=str(uuid.uuid4()), title="action contract test")
    db_session.add(thread)
    db_session.flush()
    return thread


def _task(db_session, thread: Thread, capability_ids: list[str], roots: list[str] | None = None):
    scope: dict[str, object] = {"allowed_capabilities": capability_ids}
    if roots is not None:
        scope["allowed_roots"] = roots
    return TaskRepository(db_session).create_task(
        thread_id=thread.id,
        goal="验证 Action 终态",
        title="Action 终态",
        task_type="general",
        task_brief={"scope": scope},
        enqueue=False,
    )


class _UnknownOutcomeTool(BaseTool):
    def _run(self):
        return {
            "ok": False,
            "error_type": "execution_unknown",
            "error": "external call dropped after submit",
        }


class _CancelledOutcomeTool(BaseTool):
    def _run(self):
        return {"ok": False, "error_type": "cancelled", "error": "Cancelled before execution"}


class _PartialFailureTool(BaseTool):
    def _run(self):
        return {
            "ok": False,
            "error_type": "execution_failed",
            "error": "写了一半文件",
            "outcome_known": True,
            "side_effect_started": True,
        }


def _register(tool_type, capability_id: str):
    registry = get_tool_registry()
    registry.register(
        tool_type(
            ToolDefinition(
                CapabilitySafetySchema(capability_id, "test", "trusted", "low"),
                description="contract probe",
                parameters={},
            )
        )
    )
    return registry


def test_failed_tool_action_is_not_recorded_as_succeeded(db_session, tmp_path) -> None:
    target = tmp_path / "missing.txt"
    task = _task(db_session, _thread(db_session), ["read_text_file"], [str(tmp_path)])
    agent = SequenceAgent(
        ActionProposal(
            decision="action", capability_id="read_text_file",
            arguments={"path": str(target)}, summary="读取不存在的文件",
        ),
        ActionProposal(decision="finish", summary="工具失败", report_status="partial"),
    )

    PersistentTaskRuntime(db_session, agent_executor=agent).run(task.id)

    db_session.expire_all()
    action = db_session.query(AgentAction).filter(AgentAction.task_id == task.id).one()
    stored = db_session.get(type(task), task.id)

    assert action.status == ActionStatus.FAILED.value
    assert action.error_message == "File not found"
    assert stored.status == TaskStatus.PARTIAL.value
    evidence_kinds = {
        row[0] for row in db_session.query(TaskEvidence.kind).filter(TaskEvidence.task_id == task.id)
    }
    assert "tool_error" in evidence_kinds


def test_unknown_tool_outcome_marks_action_unknown_not_succeeded(db_session, tmp_path) -> None:
    capability_id = f"contract_unknown_{uuid.uuid4().hex[:8]}"
    registry = _register(_UnknownOutcomeTool, capability_id)
    try:
        task = _task(db_session, _thread(db_session), [capability_id], [str(tmp_path)])
        agent = SequenceAgent(
            ActionProposal(decision="action", capability_id=capability_id, arguments={}),
            ActionProposal(decision="finish", summary="不应到达", report_status="succeeded"),
        )

        PersistentTaskRuntime(db_session, agent_executor=agent).run(task.id)

        db_session.expire_all()
        action = db_session.query(AgentAction).filter(AgentAction.task_id == task.id).one()
        stored = db_session.get(type(task), task.id)
        run = db_session.query(AgentRun).filter(AgentRun.task_id == task.id).one()

        assert action.status == ActionStatus.UNKNOWN.value
        assert stored.status == TaskStatus.RECONCILING.value
        assert run.status == "completed"
        # 未知结果不产生成功证据，也不把 Action 记成 succeeded。
        assert db_session.query(TaskEvidence).filter(
            TaskEvidence.action_id == action.id,
        ).count() == 0
    finally:
        registry.unregister(capability_id)


def test_cancelled_tool_outcome_marks_action_cancelled(db_session, tmp_path) -> None:
    capability_id = f"contract_cancelled_{uuid.uuid4().hex[:8]}"
    registry = _register(_CancelledOutcomeTool, capability_id)
    try:
        task = _task(db_session, _thread(db_session), [capability_id], [str(tmp_path)])
        agent = SequenceAgent(
            ActionProposal(decision="action", capability_id=capability_id, arguments={}),
            ActionProposal(decision="finish", summary="已取消", report_status="partial"),
        )

        PersistentTaskRuntime(db_session, agent_executor=agent).run(task.id)

        db_session.expire_all()
        action = db_session.query(AgentAction).filter(AgentAction.task_id == task.id).one()
        stored = db_session.get(type(task), task.id)

        assert action.status == ActionStatus.CANCELLED.value
        assert stored.status == TaskStatus.PARTIAL.value
    finally:
        registry.unregister(capability_id)


def test_successful_action_still_succeeds(db_session, tmp_path) -> None:
    task = _task(db_session, _thread(db_session), ["get_current_time"], [str(tmp_path)])
    agent = SequenceAgent(
        ActionProposal(decision="action", capability_id="get_current_time", arguments={}),
        ActionProposal(decision="finish", summary="完成", report_status="succeeded"),
    )

    PersistentTaskRuntime(db_session, agent_executor=agent).run(task.id)

    db_session.expire_all()
    action = db_session.query(AgentAction).filter(AgentAction.task_id == task.id).one()
    assert action.status == ActionStatus.SUCCEEDED.value
    assert db_session.get(type(task), task.id).status == TaskStatus.SUCCEEDED.value


def test_failed_action_event_keeps_side_effect_fact(db_session, tmp_path) -> None:
    """确定性失败也可能已经产生了部分外部副作用，事件必须保留这个事实。"""
    capability_id = f"contract_partial_{uuid.uuid4().hex[:8]}"
    registry = _register(_PartialFailureTool, capability_id)
    try:
        task = _task(db_session, _thread(db_session), [capability_id], [str(tmp_path)])
        agent = SequenceAgent(
            ActionProposal(decision="action", capability_id=capability_id, arguments={}),
            ActionProposal(decision="finish", summary="部分完成", report_status="partial"),
        )

        PersistentTaskRuntime(db_session, agent_executor=agent).run(task.id)

        db_session.expire_all()
        action = db_session.query(AgentAction).filter(AgentAction.task_id == task.id).one()
        assert action.status == ActionStatus.FAILED.value
        event = db_session.query(AgentTaskEvent).filter(
            AgentTaskEvent.task_id == task.id,
            AgentTaskEvent.event_type == "action_failed",
        ).one()
        assert event.payload["error_type"] == "execution_failed"
        assert event.payload["outcome_known"] is True
        assert event.payload["side_effect_started"] is True
    finally:
        registry.unregister(capability_id)
