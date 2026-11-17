"""PostgreSQL 专项：取消后恢复的事务/锁语义与 DatabaseTool 回滚。"""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from aiive.db.models import (
    AgentAction,
    AgentRun,
    AgentTask,
    AgentTaskEvent,
    OutboxJob,
    Thread,
)
from aiive.task_runtime.repository import TaskRepository, recover_stale_tasks, utcnow
from aiive.task_runtime.schemas import ActionStatus, RunStatus, TaskStatus
from aiive.tools.base import DatabaseTool, ToolError, ToolExecutionContext, ToolResult
from aiive.tools.executor import ToolExecutor
from aiive.tools.registry import CapabilitySafetySchema, ToolDefinition


def _cancelled_task(factory) -> tuple[str, str, str]:
    """提交一个"已取消 + Run 租约过期 + Action 仍 dispatched"的真实场景。"""
    with factory() as db:
        thread = Thread(id=str(uuid.uuid4()), title="pg cancelled recovery")
        db.add(thread)
        db.flush()
        task = TaskRepository(db).create_task(
            thread_id=thread.id,
            goal="取消后崩溃恢复",
            title="取消恢复",
            task_type="general",
            task_brief={"scope": {"allowed_capabilities": ["get_current_time"]}},
            enqueue=False,
        )
        repo = TaskRepository(db)
        task, run = repo.start_run(task.id, trigger="dispatch")
        action = repo.create_action(
            task=task, run=run, capability_id="get_current_time", arguments={},
            descriptor_hash="descriptor", risk_level="low", requires_approval=False,
        )
        repo.transition_action(action, ActionStatus.VALIDATED.value)
        repo.transition_action(action, ActionStatus.READY.value)
        action.execution_run_id = run.id
        action.execution_token = str(uuid.uuid4())
        repo.transition_action(action, ActionStatus.DISPATCHED.value)
        repo.cancel_task(task.id, reason="user_cancelled")
        run.lease_expires_at = utcnow() - timedelta(seconds=30)
        db.commit()
        return task.id, run.id, action.id


def test_cancelled_recovery_commits_terminal_states_on_postgres(pg_sessionmaker) -> None:
    task_id, run_id, action_id = _cancelled_task(pg_sessionmaker)

    with pg_sessionmaker() as db:
        assert recover_stale_tasks(db) == 1
        db.commit()

    with pg_sessionmaker() as db:
        task = db.get(AgentTask, task_id)
        run = db.get(AgentRun, run_id)
        action = db.get(AgentAction, action_id)
        assert task.status == TaskStatus.CANCELLED.value
        assert run.status == RunStatus.INTERRUPTED.value
        assert action.status == ActionStatus.UNKNOWN.value
        assert action.cancel_requested_at is not None
        assert db.query(AgentTaskEvent).filter(
            AgentTaskEvent.task_id == task_id,
            AgentTaskEvent.event_type == "cancelled_execution_recovered",
        ).count() == 1
        assert db.query(OutboxJob).filter(OutboxJob.job_type == "agent_task_run").count() == 0

    with pg_sessionmaker() as db:
        assert recover_stale_tasks(db) == 0
        db.commit()


def test_recovery_skips_row_locked_by_another_worker(pg_sessionmaker) -> None:
    task_id, _run_id, action_id = _cancelled_task(pg_sessionmaker)

    holder = pg_sessionmaker()
    try:
        # 模拟另一个 Worker 正持有该 Task 行锁。
        holder.execute(
            select(AgentTask).where(AgentTask.id == task_id).with_for_update()
        ).scalar_one()
        with pg_sessionmaker() as db:
            # SKIP LOCKED：被占用的行本轮不处理，绝不能阻塞或重复收敛。
            assert recover_stale_tasks(db) == 0
            db.commit()
        holder.rollback()
    finally:
        holder.close()

    with pg_sessionmaker() as db:
        assert recover_stale_tasks(db) == 1
        db.commit()

    with pg_sessionmaker() as db:
        assert db.get(AgentAction, action_id).status == ActionStatus.UNKNOWN.value
        assert db.get(AgentTask, task_id).status == TaskStatus.CANCELLED.value


class _RejectingWriteTool(DatabaseTool):
    def _run(self, db):
        db.add(Thread(id="pg-contract-rolled-back", title="must roll back"))
        db.flush()
        return {"ok": False, "error_type": "execution_failed", "error": "业务校验失败"}


class _ScriptedWriteTool(DatabaseTool):
    def __init__(self, definition, outcome):
        super().__init__(definition)
        self.outcome = outcome
        self.row_id = f"pg-contract-row-{uuid.uuid4()}"

    def _run(self, db):
        db.add(Thread(id=self.row_id, title="scripted write"))
        db.flush()
        return self.outcome


class _CommittingWriteTool(DatabaseTool):
    def _run(self, db):
        db.add(Thread(id="pg-contract-committed", title="must commit"))
        db.flush()
        return "written"


def _db_tool(tool_type, capability_id: str):
    return tool_type(
        ToolDefinition(
            CapabilitySafetySchema(capability_id, "test", "trusted", "low"),
            parameters={},
        )
    )


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param({"ok": False, "error_type": "execution_failed", "error": "失败"}, id="dict_ok_false"),
        pytest.param({"ok": False, "allowed": False, "reason": "范围外"}, id="dict_allowed_false"),
        pytest.param(ToolError("execution_failed", "失败"), id="tool_error"),
        pytest.param(
            ToolResult(error=ToolError("execution_unknown", "未知", outcome_known=False)),
            id="tool_result_error",
        ),
        pytest.param(
            ToolResult(value={"ok": False, "error_type": "execution_failed", "error": "失败"}),
            id="tool_result_value",
        ),
    ],
)
def test_database_tool_structured_failure_rolls_back_on_postgres(pg_sessionmaker, outcome) -> None:
    tool = _ScriptedWriteTool(
        ToolDefinition(
            CapabilitySafetySchema("pg_contract_scripted_write", "test", "trusted", "low"),
            parameters={},
        ),
        outcome,
    )

    payload = ToolExecutor.execute_direct(
        tool, ToolExecutionContext(tool.safety.capability_id), {}, 10,
    )

    assert payload["ok"] is False
    with pg_sessionmaker() as db:
        assert db.get(Thread, tool.row_id) is None


def test_database_tool_failure_rolls_back_on_postgres(pg_sessionmaker) -> None:
    tool = _db_tool(_RejectingWriteTool, "pg_contract_rejecting_write")

    payload = ToolExecutor.execute_direct(
        tool, ToolExecutionContext(tool.safety.capability_id), {}, 10,
    )

    assert payload["ok"] is False
    assert payload["execution_status"] == "failed"
    with pg_sessionmaker() as db:
        assert db.get(Thread, "pg-contract-rolled-back") is None


def test_database_tool_success_commits_on_postgres(pg_sessionmaker) -> None:
    tool = _db_tool(_CommittingWriteTool, "pg_contract_committing_write")

    payload = ToolExecutor.execute_direct(
        tool, ToolExecutionContext(tool.safety.capability_id), {}, 10,
    )

    assert payload["ok"] is True
    with pg_sessionmaker() as db:
        assert db.get(Thread, "pg-contract-committed") is not None
