"""长期目标（Goal）生命周期与事件驱动推进的验收测试（全离线，Fake LLM）。"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest

from aiive.db.models import (
    AgentRun,
    AgentTask,
    AgentTaskCheckpoint,
    AgentTaskEvent,
    AgentTaskWatch,
    Event,
    OutboxJob,
    Thread,
)
from aiive.task_runtime.agent_executor import AgentDecision
from aiive.task_runtime.goal_plan import GoalStepStatus, parse_datetime
from aiive.task_runtime.goal_service import GoalError, GoalService
from aiive.task_runtime.repository import TaskRepository, utcnow
from aiive.task_runtime.runtime import PersistentTaskRuntime
from aiive.task_runtime.schemas import ActionProposal, TaskStatus
from aiive.task_runtime.watchers import WatcherService


class SequenceAgent:
    """按顺序返回预设决策的 Fake LLM；不访问任何外部服务。"""

    def __init__(self, *proposals: ActionProposal):
        self.proposals = list(proposals)

    def decide(self, task, capabilities):
        proposal = self.proposals.pop(0)
        return AgentDecision(
            proposal=proposal,
            model="fake-goal-model",
            token_usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            input_snapshot={"task_id": task.id, "capabilities": capabilities},
            prompt_refs=["task_runtime.worker_system@test"],
        )


def _thread(db) -> Thread:
    thread = Thread(id=str(uuid.uuid4()), title="long term goal test")
    db.add(thread)
    db.flush()
    return thread


def _plan(**overrides) -> dict:
    plan = {
        "summary": "三个月后开始求职，每周三模拟面试",
        "deadline": "2027-01-31",
        "acceptance": ["简历完成", "至少三次模拟面试"],
        "timezone": "Asia/Shanghai",
        "steps": [
            {"id": "resume", "title": "整理简历", "kind": "autonomous", "acceptance": ["简历文件存在"]},
            {"id": "portfolio", "title": "整理作品集", "kind": "autonomous"},
        ],
    }
    plan.update(overrides)
    return plan


def _propose(db, thread: Thread, plan: dict | None = None) -> AgentTask:
    task = TaskRepository(db).create_task(
        thread_id=thread.id,
        goal="三个月后开始求职",
        title="求职计划",
        task_type="long_term_goal",
        # 测试只需要一个确定可执行的能力；真实提案走 unified 能力面。
        task_brief={"scope": {"allowed_capabilities": ["get_current_time"]}},
        enqueue=False,
    )
    GoalService(db).attach_plan(task, plan or _plan())
    db.commit()
    return task


def _fresh(db, task: AgentTask) -> AgentTask:
    db.expire_all()
    stored = db.get(AgentTask, task.id)
    assert stored is not None
    return stored


def _jobs(db, task_id: str) -> list[OutboxJob]:
    return db.query(OutboxJob).filter(
        OutboxJob.job_type == "agent_task_run",
        OutboxJob.operation_id.like(f"agent_task_run:{task_id}:%"),
    ).all()


def _notifications(db, task_id: str, category: str | None = None) -> list[Event]:
    events = db.query(Event).filter(
        Event.trace_id == task_id,
        Event.event_type == "notification_created",
    ).all()
    if category is None:
        return events
    return [item for item in events if (item.payload or {}).get("category") == category]


def _active_watches(db, task_id: str) -> list[AgentTaskWatch]:
    return db.query(AgentTaskWatch).filter(
        AgentTaskWatch.task_id == task_id,
        AgentTaskWatch.status == "active",
    ).all()


def _make_due(db, watch: AgentTaskWatch, *, at: datetime | None = None) -> None:
    when = at or (utcnow() - timedelta(minutes=1))
    watch.config = {**(watch.config or {}), "at": when.isoformat()}
    watch.next_check_at = when
    db.flush()


def _poll(db) -> int:
    triggered = WatcherService(db).poll_due()
    db.commit()
    return triggered


def _run(db, task_id: str, *proposals: ActionProposal) -> dict:
    return PersistentTaskRuntime(db, agent_executor=SequenceAgent(*proposals)).run(task_id)


def _action() -> ActionProposal:
    return ActionProposal(decision="action", capability_id="get_current_time", summary="读取时间")


def _finish(status: str = "succeeded") -> ActionProposal:
    return ActionProposal(decision="finish", summary="步骤完成", report_status=status)


# ----------------------------------------------------------------------
# 1. 提案未确认不执行；确认只启动一次
# ----------------------------------------------------------------------
def test_proposal_does_not_run_before_confirmation_and_confirm_starts_once(db_session) -> None:
    task = _propose(db_session, _thread(db_session))
    stored = _fresh(db_session, task)

    assert stored.status == TaskStatus.BLOCKED_USER.value
    assert stored.wake_at is not None
    assert _jobs(db_session, task.id) == []
    assert GoalService(db_session).detail(stored)["plan"]["status"] == "proposed"

    GoalService(db_session).confirm(task.id)
    db_session.commit()
    jobs = _jobs(db_session, task.id)
    assert len(jobs) == 1
    assert jobs[0].payload["trigger"] == "goal_confirmed"

    with pytest.raises(GoalError):
        GoalService(db_session).confirm(task.id)
    assert len(_jobs(db_session, task.id)) == 1


# ----------------------------------------------------------------------
# 2. 单步完成后更新进度并推进下一步
# ----------------------------------------------------------------------
def test_step_completion_updates_progress_and_advances(db_session) -> None:
    task = _propose(db_session, _thread(db_session))
    GoalService(db_session).confirm(task.id)
    db_session.commit()

    _run(db_session, task.id, _action(), _finish())

    stored = _fresh(db_session, task)
    plan = GoalService(db_session).load_plan(stored)
    assert stored.status == TaskStatus.QUEUED.value
    assert plan.step("resume").status == GoalStepStatus.DONE.value
    assert plan.step("portfolio").status == GoalStepStatus.READY.value
    assert plan.current_step_id == "portfolio"
    assert plan.progress()["done"] == 1
    assert len(_jobs(db_session, task.id)) >= 2


def test_goal_completes_only_after_every_step_has_evidence(db_session) -> None:
    task = _propose(db_session, _thread(db_session))
    GoalService(db_session).confirm(task.id)
    db_session.commit()

    _run(db_session, task.id, _action(), _finish())
    result = _run(db_session, task.id, _action(), _finish())

    stored = _fresh(db_session, task)
    plan = GoalService(db_session).load_plan(stored)
    assert stored.status == TaskStatus.SUCCEEDED.value
    assert plan.status == "completed"
    assert plan.progress()["done"] == 2
    assert result["status"] == TaskStatus.SUCCEEDED.value


# ----------------------------------------------------------------------
# 3. 无证据不得标记成功
# ----------------------------------------------------------------------
def test_success_without_evidence_blocks_goal(db_session) -> None:
    task = _propose(db_session, _thread(db_session))
    GoalService(db_session).confirm(task.id)
    db_session.commit()

    _run(db_session, task.id, _finish("succeeded"))

    stored = _fresh(db_session, task)
    plan = GoalService(db_session).load_plan(stored)
    assert stored.status == TaskStatus.BLOCKED_USER.value
    assert plan.status == "blocked"
    assert plan.step("resume").status == GoalStepStatus.FAILED.value
    assert plan.step("resume").note == "missing_evidence"
    assert _notifications(db_session, task.id, "goal_missing_evidence")


# ----------------------------------------------------------------------
# 4. 等待用户输入只阻塞依赖步骤
# ----------------------------------------------------------------------
def test_waiting_step_blocks_only_dependents(db_session) -> None:
    plan = {
        "summary": "面试准备",
        "steps": [
            {"id": "slot", "title": "确认面试时间", "kind": "interactive"},
            {"id": "prepare", "title": "按确认时间准备", "kind": "autonomous", "depends_on": ["slot"]},
            {"id": "notes", "title": "整理笔记", "kind": "autonomous"},
        ],
    }
    task = _propose(db_session, _thread(db_session), plan)
    GoalService(db_session).confirm(task.id)
    db_session.commit()

    # 交互步骤由用户点击开始后才进入运行，运行中再把问题交回用户。
    GoalService(db_session).start_step(task.id, "slot")
    db_session.commit()

    _run(db_session, task.id, ActionProposal(decision="ask_user", question="你希望周几面试？"))

    stored = _fresh(db_session, task)
    current = GoalService(db_session).load_plan(stored)
    assert stored.status == TaskStatus.QUEUED.value
    assert current.step("slot").status == GoalStepStatus.WAITING_USER.value
    assert current.step("prepare").status == GoalStepStatus.PENDING.value
    assert current.current_step_id == "notes"

    _run(db_session, task.id, _action(), _finish())

    stored = _fresh(db_session, task)
    current = GoalService(db_session).load_plan(stored)
    assert current.status == "active"
    assert current.step("notes").status == GoalStepStatus.DONE.value
    assert current.step("slot").status == GoalStepStatus.WAITING_USER.value
    assert stored.status == TaskStatus.BLOCKED_USER.value


# ----------------------------------------------------------------------
# 5. 定时触发通过 Outbox 执行且不重复
# ----------------------------------------------------------------------
def test_time_trigger_enqueues_once(db_session) -> None:
    at = (utcnow() + timedelta(minutes=5)).isoformat()
    plan = {
        "summary": "到期自动整理",
        "steps": [
            {
                "id": "nightly",
                "title": "整理材料",
                "kind": "autonomous",
                "schedule": {"kind": "once", "at": at},
            }
        ],
    }
    task = _propose(db_session, _thread(db_session), plan)
    GoalService(db_session).confirm(task.id)
    db_session.commit()

    assert _jobs(db_session, task.id) == []
    watches = _active_watches(db_session, task.id)
    assert len(watches) == 1

    _make_due(db_session, watches[0])
    _poll(db_session)

    stored = _fresh(db_session, task)
    assert stored.status == TaskStatus.QUEUED.value
    assert len(_jobs(db_session, task.id)) == 1

    _poll(db_session)
    _poll(db_session)
    assert len(_jobs(db_session, task.id)) == 1


def test_interactive_due_only_notifies_without_starting(db_session) -> None:
    plan = {
        "summary": "每周三模拟面试",
        "steps": [
            {
                "id": "interview",
                "title": "模拟面试",
                "kind": "interactive",
                "schedule": {"kind": "weekly", "weekday": 2, "time": "20:00"},
            }
        ],
    }
    task = _propose(db_session, _thread(db_session), plan)
    GoalService(db_session).confirm(task.id)
    db_session.commit()

    watches = _active_watches(db_session, task.id)
    assert len(watches) == 1
    _make_due(db_session, watches[0])
    _poll(db_session)

    stored = _fresh(db_session, task)
    current = GoalService(db_session).load_plan(stored)
    assert stored.status == TaskStatus.BLOCKED_USER.value
    assert _jobs(db_session, task.id) == []
    assert db_session.query(AgentRun).filter(AgentRun.task_id == task.id).count() == 0
    assert current.step("interview").status == GoalStepStatus.WAITING_USER.value
    assert _notifications(db_session, task.id, "goal_step_due")
    # 下一次周期已经排好，不会重复启动同一场。
    assert len(_active_watches(db_session, task.id)) == 1


def test_user_start_runs_in_isolated_context(db_session) -> None:
    from aiive.task_runtime.context_assembler import TaskContextAssembler

    plan = {
        "summary": "每周三模拟面试",
        "steps": [{"id": "interview", "title": "模拟面试", "kind": "interactive"}],
    }
    task = _propose(db_session, _thread(db_session), plan)
    GoalService(db_session).confirm(task.id)
    db_session.commit()

    # 交互步骤确认后不会自动起跑，必须由用户点击开始。
    stored = _fresh(db_session, task)
    assert _jobs(db_session, task.id) == []
    assert (
        GoalService(db_session).load_plan(stored).step("interview").status
        == GoalStepStatus.WAITING_USER.value
    )

    GoalService(db_session).start_step(task.id, "interview")
    db_session.commit()

    stored = _fresh(db_session, task)
    assert stored.status == TaskStatus.QUEUED.value
    snapshot = TaskContextAssembler(db_session).snapshot(stored, [])
    assert snapshot["goal"]["current_step"]["id"] == "interview"
    # 目标任务上下文不含会话原文，只注入目标与步骤状态。
    assert "conversation" not in snapshot
    assert "history" not in snapshot

    _run(db_session, task.id, _action(), _finish())
    stored = _fresh(db_session, task)
    assert GoalService(db_session).load_plan(stored).step("interview").status == GoalStepStatus.DONE.value


# ----------------------------------------------------------------------
# 6. 重启恢复
# ----------------------------------------------------------------------
def test_plan_and_pending_trigger_survive_restart(db_session) -> None:
    at = (utcnow() + timedelta(minutes=5)).isoformat()
    plan = {
        "summary": "重启恢复",
        "steps": [
            {"id": "later", "title": "稍后执行", "kind": "autonomous", "schedule": {"kind": "once", "at": at}}
        ],
    }
    task = _propose(db_session, _thread(db_session), plan)
    GoalService(db_session).confirm(task.id)
    db_session.commit()

    # 模拟进程重启：丢弃全部 ORM 身份，从库里重新装配。
    db_session.expire_all()
    restored = db_session.get(AgentTask, task.id)
    assert restored is not None
    recovered = GoalService(db_session).load_plan(restored)
    assert recovered is not None and recovered.status == "active"
    checkpoint = db_session.query(AgentTaskCheckpoint).filter(
        AgentTaskCheckpoint.task_id == task.id,
    ).order_by(AgentTaskCheckpoint.version.desc()).first()
    assert checkpoint is not None and checkpoint.plan

    watches = _active_watches(db_session, task.id)
    assert len(watches) == 1
    _make_due(db_session, watches[0])
    _poll(db_session)
    assert len(_jobs(db_session, task.id)) == 1


# ----------------------------------------------------------------------
# 7. 暂停 / 恢复 / 修改 / 结束
# ----------------------------------------------------------------------
def test_pause_resume_modify_end_invalidate_old_plan(db_session) -> None:
    at = (utcnow() + timedelta(days=3)).isoformat()
    plan = {
        "summary": "生命周期",
        "steps": [
            {"id": "first", "title": "第一步", "kind": "autonomous"},
            {"id": "later", "title": "后续", "kind": "autonomous", "schedule": {"kind": "once", "at": at}},
        ],
    }
    task = _propose(db_session, _thread(db_session), plan)
    GoalService(db_session).confirm(task.id)
    db_session.commit()
    assert len(_jobs(db_session, task.id)) == 1

    GoalService(db_session).pause(task.id)
    db_session.commit()
    stored = _fresh(db_session, task)
    assert stored.status == TaskStatus.BLOCKED_USER.value
    assert GoalService(db_session).load_plan(stored).status == "paused"
    assert _active_watches(db_session, task.id) == []

    GoalService(db_session).resume(task.id)
    db_session.commit()
    stored = _fresh(db_session, task)
    assert stored.status == TaskStatus.QUEUED.value
    assert len(_jobs(db_session, task.id)) == 2

    before = GoalService(db_session).load_plan(stored)
    new_at = (utcnow() + timedelta(days=10)).isoformat()
    updated = GoalService(db_session).modify(task.id, {
        "steps": [
            {"id": "first", "title": "第一步", "kind": "autonomous"},
            {"id": "later", "title": "后续", "kind": "autonomous", "schedule": {"kind": "once", "at": new_at}},
        ]
    })
    db_session.commit()
    assert updated["plan"]["revision"] == before.revision + 1
    watches = _active_watches(db_session, task.id)
    assert len(watches) == 1
    assert parse_datetime(watches[0].config["occurrence"]) == parse_datetime(new_at)

    GoalService(db_session).end(task.id, "用户放弃")
    db_session.commit()
    stored = _fresh(db_session, task)
    assert stored.status == TaskStatus.CANCELLED.value
    assert GoalService(db_session).load_plan(stored).status == "abandoned"
    assert _active_watches(db_session, task.id) == []


# ----------------------------------------------------------------------
# 8. 预算耗尽不得自行扩大
# ----------------------------------------------------------------------
def test_budget_exhaustion_blocks_goal_without_new_trigger(db_session) -> None:
    task = _propose(db_session, _thread(db_session))
    task.budgets = {**(task.budgets or {}), "max_total_tokens": 1000}
    task.usage = {**(task.usage or {}), "prompt_tokens": 5000}
    db_session.flush()
    GoalService(db_session).confirm(task.id)
    db_session.commit()
    jobs_before = len(_jobs(db_session, task.id))

    _run(db_session, task.id, _action(), _finish())

    stored = _fresh(db_session, task)
    assert stored.status == TaskStatus.PARTIAL.value
    assert GoalService(db_session).load_plan(stored).status == "blocked"
    assert len(_jobs(db_session, task.id)) == jobs_before


# ----------------------------------------------------------------------
# 9. 连续错过周期任务
# ----------------------------------------------------------------------
def test_repeated_missed_occurrences_suggest_adjustment(db_session) -> None:
    plan = {
        "summary": "每周模拟面试",
        "steps": [
            {
                "id": "interview",
                "title": "模拟面试",
                "kind": "interactive",
                "schedule": {"kind": "weekly", "weekday": 2, "time": "20:00"},
            }
        ],
    }
    task = _propose(db_session, _thread(db_session), plan)
    GoalService(db_session).confirm(task.id)
    db_session.commit()

    for _ in range(4):
        watches = _active_watches(db_session, task.id)
        assert len(watches) == 1
        _make_due(db_session, watches[0])
        _poll(db_session)

    stored = _fresh(db_session, task)
    current = GoalService(db_session).load_plan(stored)
    assert current.step("interview").missed_count == 3
    assert current.status == "active"  # 不自动修改长期目标
    assert _notifications(db_session, task.id, "goal_missed_streak")
    events = db_session.query(AgentTaskEvent).filter(
        AgentTaskEvent.task_id == task.id,
        AgentTaskEvent.event_type == "goal_adjustment_suggested",
    ).all()
    assert len(events) == 1


# ----------------------------------------------------------------------
# 10. 沉默策略：提案提醒与休眠
# ----------------------------------------------------------------------
def test_unconfirmed_proposal_reminds_once_then_goes_dormant(db_session) -> None:
    task = _propose(db_session, _thread(db_session))
    stored = _fresh(db_session, task)
    created = parse_datetime(GoalService(db_session).load_plan(stored).created_at)
    assert created is not None

    assert GoalService(db_session).scan_due_reminders(now=created + timedelta(hours=25)) == 1
    db_session.commit()
    assert len(_notifications(db_session, task.id, "goal_proposal_pending")) == 1

    assert GoalService(db_session).scan_due_reminders(now=created + timedelta(hours=30)) == 0
    db_session.commit()
    assert len(_notifications(db_session, task.id, "goal_proposal_pending")) == 1

    assert GoalService(db_session).scan_due_reminders(now=created + timedelta(days=8)) == 1
    db_session.commit()
    stored = _fresh(db_session, task)
    assert stored.wake_at is None
    assert len(_notifications(db_session, task.id, "goal_proposal_pending")) == 1
    assert db_session.query(AgentTaskEvent).filter(
        AgentTaskEvent.task_id == task.id,
        AgentTaskEvent.event_type == "goal_proposal_dormant",
    ).count() == 1


# ----------------------------------------------------------------------
# 11. 主 Agent 通过 delegate_task 提案（工具面不变）
# ----------------------------------------------------------------------
def test_delegate_task_accepts_goal_plan_and_keeps_tool_surface(db_session) -> None:
    from aiive.context.run_context import RunContext
    from aiive.task_runtime.tools import build_main_agent_registry

    registry = build_main_agent_registry()
    assert {item["capability_id"] for item in registry.list_all()} == {
        "list_skills", "delegate_task", "get_task_status", "cancel_task", "send_task_input",
    }
    thread = _thread(db_session)
    handler = registry.get("delegate_task").handler
    result = handler(
        goal="三个月后开始求职，每周三模拟面试",
        goal_plan=_plan(),
        ctx=RunContext(thread_id=thread.id, trace_id="goal-trace", source="test"),
    )

    assert result["status"] == "proposed"
    assert result["plan"]["steps"]
    stored = db_session.get(AgentTask, result["task_id"])
    assert stored is not None
    assert stored.status == TaskStatus.BLOCKED_USER.value
    assert _jobs(db_session, stored.id) == []


# ----------------------------------------------------------------------
# 12. REST 控制面（任务中心）
# ----------------------------------------------------------------------
def _client(db_session):
    from fastapi.testclient import TestClient

    from aiive.db.base import get_db
    from aiive.main import create_app

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def test_goal_api_exposes_plan_and_guards_state(db_session) -> None:
    task = _propose(db_session, _thread(db_session))
    client = _client(db_session)

    detail = client.get(f"/api/agent-tasks/{task.id}/goal")
    assert detail.status_code == 200
    body = detail.json()
    assert body["plan"]["status"] == "proposed"
    assert body["summary"]["progress"]["total"] == 2

    confirmed = client.post(f"/api/agent-tasks/{task.id}/goal/confirm")
    assert confirmed.status_code == 200
    assert confirmed.json()["plan"]["status"] == "active"
    assert len(_jobs(db_session, task.id)) == 1

    again = client.post(f"/api/agent-tasks/{task.id}/goal/confirm")
    assert again.status_code == 409
    assert again.json()["detail"] == "goal_not_proposed"

    paused = client.post(f"/api/agent-tasks/{task.id}/goal/pause")
    assert paused.status_code == 200
    assert paused.json()["plan"]["status"] == "paused"

    resumed = client.post(f"/api/agent-tasks/{task.id}/goal/resume")
    assert resumed.status_code == 200
    assert resumed.json()["plan"]["status"] == "active"

    ended = client.post(f"/api/agent-tasks/{task.id}/goal/end", json={"reason": "不做了"})
    assert ended.status_code == 200
    assert ended.json()["plan"]["status"] == "abandoned"
    stored = _fresh(db_session, task)
    assert stored.status == TaskStatus.CANCELLED.value


def test_goal_api_rejects_unknown_task(db_session) -> None:
    client = _client(db_session)
    assert client.get("/api/agent-tasks/not-exist/goal").status_code == 404
    assert client.post("/api/agent-tasks/not-exist/goal/confirm").status_code == 404


# ----------------------------------------------------------------------
# 13. 真实调用链：watch 到期 → Outbox job → handler → runtime → 步骤结算
# ----------------------------------------------------------------------
def test_outbox_handler_drives_goal_step(db_session, monkeypatch) -> None:
    from aiive.task_runtime.agent_executor import AgentDecision, TaskAgentExecutor
    from aiive.worker.handlers_agent_tasks import handle_agent_task_run
    from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome

    at = (utcnow() + timedelta(minutes=3)).isoformat()
    plan = {
        "summary": "到期执行",
        "steps": [
            {"id": "job", "title": "定时整理", "kind": "autonomous", "schedule": {"kind": "once", "at": at}}
        ],
    }
    task = _propose(db_session, _thread(db_session), plan)
    GoalService(db_session).confirm(task.id)
    db_session.commit()

    watches = _active_watches(db_session, task.id)
    _make_due(db_session, watches[0])
    _poll(db_session)
    jobs = _jobs(db_session, task.id)
    assert len(jobs) == 1

    proposals = [_action(), _finish()]
    monkeypatch.setattr(
        TaskAgentExecutor,
        "decide",
        lambda self, task, capabilities: AgentDecision(
            proposal=proposals.pop(0),
            model="fake-goal-model",
            token_usage={"prompt_tokens": 10, "completion_tokens": 5},
            input_snapshot={"task_id": task.id},
            prompt_refs=["task_runtime.worker_system@test"],
        ),
    )
    job = jobs[0]
    result = handle_agent_task_run(ClaimedJob(
        id=job.id, job_type=job.job_type, payload=job.payload, trace_id=job.trace_id,
        retry_count=0, max_retries=job.max_retries, claim_token="claim-test",
        schema_version=1, worker_id="worker-test",
    ))
    assert result.outcome == HandlerOutcome.COMPLETED

    stored = _fresh(db_session, task)
    current = GoalService(db_session).load_plan(stored)
    assert stored.status == TaskStatus.SUCCEEDED.value
    assert current.status == "completed"
    assert current.step("job").status == GoalStepStatus.DONE.value

# ----------------------------------------------------------------------
# 14. approval_required：未审批不执行，审批后只启动一次
# ----------------------------------------------------------------------
def test_approval_required_step_runs_only_after_approval(db_session) -> None:
    plan = {
        "summary": "审批后执行",
        "steps": [
            {
                "id": "migrate", "title": "迁移数据",
                "kind": "approval_required", "acceptance": ["迁移完成"],
            },
        ],
    }
    task = _propose(db_session, _thread(db_session), plan)
    GoalService(db_session).confirm(task.id)
    db_session.commit()

    stored = _fresh(db_session, task)
    current = GoalService(db_session).load_plan(stored)
    assert stored.status == TaskStatus.BLOCKED_USER.value
    assert current.step("migrate").status == GoalStepStatus.WAITING_USER.value
    assert current.step("migrate").approved_at is None
    assert _jobs(db_session, task.id) == []
    assert len(_notifications(db_session, task.id, "goal_step_approval")) == 1

    # 未审批时连强制起跑都不允许：任务不在 queued，运行时拒绝启动。
    with pytest.raises(ValueError):
        _run(db_session, task.id, _action(), _finish())

    GoalService(db_session).approve_step(task.id, "migrate")
    db_session.commit()
    jobs = _jobs(db_session, task.id)
    assert len(jobs) == 1
    assert jobs[0].payload["trigger"] == "goal_step_approved"

    with pytest.raises(GoalError):
        GoalService(db_session).approve_step(task.id, "migrate")
    assert len(_jobs(db_session, task.id)) == 1

    _run(db_session, task.id, _action(), _finish())
    stored = _fresh(db_session, task)
    current = GoalService(db_session).load_plan(stored)
    assert stored.status == TaskStatus.SUCCEEDED.value
    assert current.status == "completed"
    assert current.step("migrate").status == GoalStepStatus.DONE.value


# ----------------------------------------------------------------------
# 15. 交互步骤：不自动起跑，只提示；等待 24h 提醒一次
# ----------------------------------------------------------------------
def test_interactive_step_waits_for_user_and_reminds_once(db_session) -> None:
    plan = {
        "summary": "每周复盘",
        "steps": [
            {"id": "review", "title": "一起复盘", "kind": "interactive"},
        ],
    }
    task = _propose(db_session, _thread(db_session), plan)
    GoalService(db_session).confirm(task.id)
    db_session.commit()

    stored = _fresh(db_session, task)
    current = GoalService(db_session).load_plan(stored)
    assert stored.status == TaskStatus.BLOCKED_USER.value
    assert current.step("review").status == GoalStepStatus.WAITING_USER.value
    assert _jobs(db_session, task.id) == []
    assert len(_notifications(db_session, task.id, "goal_step_ready")) == 1

    reminded_at = utcnow() + timedelta(hours=25)
    assert GoalService(db_session).scan_due_reminders(now=reminded_at) == 1
    db_session.commit()
    assert len(_notifications(db_session, task.id, "goal_step_waiting_user")) == 1
    # 只提醒一次，不反复打扰。
    assert GoalService(db_session).scan_due_reminders(now=reminded_at + timedelta(hours=5)) == 0

def test_goal_api_approves_approval_step(db_session) -> None:
    plan = {
        "summary": "审批后执行",
        "steps": [
            {"id": "migrate", "title": "迁移数据", "kind": "approval_required"},
            {"id": "verify", "title": "校验结果", "kind": "autonomous", "depends_on": ["migrate"]},
        ],
    }
    task = _propose(db_session, _thread(db_session), plan)
    client = _client(db_session)
    assert client.post(f"/api/agent-tasks/{task.id}/goal/confirm").status_code == 200

    approved = client.post(f"/api/agent-tasks/{task.id}/goal/steps/migrate/approve")
    assert approved.status_code == 200
    assert approved.json()["plan"]["steps"][0]["approved_at"]

    again = client.post(f"/api/agent-tasks/{task.id}/goal/steps/migrate/approve")
    assert again.status_code == 409
    assert again.json()["detail"] == "step_already_approved"

    wrong = client.post(f"/api/agent-tasks/{task.id}/goal/steps/verify/approve")
    assert wrong.status_code == 409
    assert wrong.json()["detail"] == "step_not_approvable"

# ----------------------------------------------------------------------
# 16. 修改计划：执行内容变化 vs 调度时间变化
# ----------------------------------------------------------------------
def _two_step_plan(**overrides) -> dict:
    plan = {
        "summary": "求职准备",
        "steps": [
            {"id": "resume", "title": "整理简历", "kind": "autonomous", "acceptance": ["简历文件存在"]},
            {"id": "portfolio", "title": "整理作品集", "kind": "autonomous"},
        ],
    }
    plan.update(overrides)
    return plan


def _modified_event(db, task_id: str) -> dict:
    event = db.query(AgentTaskEvent).filter(
        AgentTaskEvent.task_id == task_id,
        AgentTaskEvent.event_type == "goal_modified",
    ).order_by(AgentTaskEvent.sequence.desc()).first()
    assert event is not None
    return event.payload or {}


def test_modify_content_change_invalidates_done_step_and_evidence(db_session) -> None:
    task = _propose(db_session, _thread(db_session), _two_step_plan())
    GoalService(db_session).confirm(task.id)
    db_session.commit()
    _run(db_session, task.id, _action(), _finish())

    stored = _fresh(db_session, task)
    before = GoalService(db_session).load_plan(stored)
    assert before.step("resume").status == GoalStepStatus.DONE.value
    assert before.step("resume").evidence_refs

    # 只改验收标准（执行内容），调度时间完全没变
    GoalService(db_session).modify(task.id, {"steps": [
        {"id": "resume", "title": "整理简历", "kind": "autonomous", "acceptance": ["简历通过评审"]},
        {"id": "portfolio", "title": "整理作品集", "kind": "autonomous"},
    ]})
    db_session.commit()

    stored = _fresh(db_session, task)
    after = GoalService(db_session).load_plan(stored)
    # 内容变化后该步骤被重新排为待执行（_advance 会把它 arm 成 ready）。
    assert after.step("resume").status == GoalStepStatus.READY.value
    assert after.current_step_id == "resume"
    assert after.step("resume").evidence_refs == []
    assert after.step("resume").note == "content_changed"
    assert after.status == "active"
    payload = _modified_event(db_session, task.id)
    assert {"step_id": "resume", "reason": "content_changed"} in payload["invalidated"]
    assert payload["rescheduled"] == []


def test_modify_schedule_only_keeps_completed_step(db_session) -> None:
    at = (utcnow() + timedelta(days=2)).isoformat()
    plan = {
        "summary": "求职准备",
        "steps": [
            {"id": "resume", "title": "整理简历", "kind": "autonomous"},
            {"id": "review", "title": "周期复盘", "kind": "autonomous", "schedule": {"kind": "once", "at": at}},
        ],
    }
    task = _propose(db_session, _thread(db_session), plan)
    GoalService(db_session).confirm(task.id)
    db_session.commit()
    _run(db_session, task.id, _action(), _finish())

    stored = _fresh(db_session, task)
    before = GoalService(db_session).load_plan(stored)
    evidence_before = list(before.step("resume").evidence_refs)
    assert before.step("resume").status == GoalStepStatus.DONE.value
    assert evidence_before

    new_at = (utcnow() + timedelta(days=9)).isoformat()
    GoalService(db_session).modify(task.id, {"steps": [
        {"id": "resume", "title": "整理简历", "kind": "autonomous"},
        {"id": "review", "title": "周期复盘", "kind": "autonomous", "schedule": {"kind": "once", "at": new_at}},
    ]})
    db_session.commit()

    stored = _fresh(db_session, task)
    after = GoalService(db_session).load_plan(stored)
    # 仅改时间：已完成步骤的完成状态与证据必须保留
    assert after.step("resume").status == GoalStepStatus.DONE.value
    assert after.step("resume").evidence_refs == evidence_before
    assert after.step("review").status == GoalStepStatus.PENDING.value
    watches = _active_watches(db_session, task.id)
    assert len(watches) == 1
    assert parse_datetime(watches[0].config["occurrence"]) == parse_datetime(new_at)
    payload = _modified_event(db_session, task.id)
    assert payload["invalidated"] == []
    assert payload["rescheduled"] == ["review"]


def test_modify_dependency_change_invalidates_downstream(db_session) -> None:
    plan = {
        "summary": "链式目标",
        "steps": [
            {"id": "a", "title": "第一步", "kind": "autonomous"},
            {"id": "b", "title": "第二步", "kind": "autonomous", "depends_on": ["a"]},
            {"id": "c", "title": "独立步骤", "kind": "autonomous"},
        ],
    }
    task = _propose(db_session, _thread(db_session), plan)
    GoalService(db_session).confirm(task.id)
    db_session.commit()
    _run(db_session, task.id, _action(), _finish())
    _run(db_session, task.id, _action(), _finish())

    stored = _fresh(db_session, task)
    before = GoalService(db_session).load_plan(stored)
    assert before.step("a").status == GoalStepStatus.DONE.value
    assert before.step("b").status == GoalStepStatus.DONE.value

    # 改 a 的执行内容：依赖 a 的 b 也不能继续算完成
    GoalService(db_session).modify(task.id, {"steps": [
        {"id": "a", "title": "第一步（重做）", "kind": "autonomous"},
        {"id": "b", "title": "第二步", "kind": "autonomous", "depends_on": ["a"]},
        {"id": "c", "title": "独立步骤", "kind": "autonomous"},
    ]})
    db_session.commit()

    stored = _fresh(db_session, task)
    after = GoalService(db_session).load_plan(stored)
    assert after.step("a").status == GoalStepStatus.READY.value
    assert after.current_step_id == "a"
    assert after.step("b").status == GoalStepStatus.PENDING.value
    assert after.step("b").note == "dependency_changed:a"
    assert after.step("b").evidence_refs == []
    # 与 a 无关的步骤不受影响
    assert after.step("c").status == GoalStepStatus.READY.value


def test_modify_content_change_invalidates_approval(db_session) -> None:
    plan = {
        "summary": "数据迁移",
        "steps": [
            {"id": "migrate", "title": "迁移数据", "kind": "approval_required", "acceptance": ["迁移完成"]},
            {"id": "verify", "title": "校验数据", "kind": "autonomous", "depends_on": ["migrate"]},
        ],
    }
    task = _propose(db_session, _thread(db_session), plan)
    GoalService(db_session).confirm(task.id)
    db_session.commit()
    GoalService(db_session).approve_step(task.id, "migrate")
    db_session.commit()
    _run(db_session, task.id, _action(), _finish())

    stored = _fresh(db_session, task)
    before = GoalService(db_session).load_plan(stored)
    assert before.step("migrate").status == GoalStepStatus.DONE.value
    assert before.step("migrate").approved_at
    jobs_before = len(_jobs(db_session, task.id))

    # 执行内容变了：旧审批不得继续授权新操作
    GoalService(db_session).modify(task.id, {"steps": [
        {
            "id": "migrate", "title": "迁移数据到新库",
            "kind": "approval_required", "acceptance": ["新旧库一致"],
        },
        {"id": "verify", "title": "校验数据", "kind": "autonomous", "depends_on": ["migrate"]},
    ]})
    db_session.commit()

    stored = _fresh(db_session, task)
    after = GoalService(db_session).load_plan(stored)
    assert after.step("migrate").approved_at is None
    assert after.step("migrate").status == GoalStepStatus.WAITING_USER.value
    assert after.step("migrate").evidence_refs == []
    assert after.step("verify").status == GoalStepStatus.PENDING.value
    assert after.step("verify").note == "dependency_changed:migrate"
    assert len(_jobs(db_session, task.id)) == jobs_before
    payload = _modified_event(db_session, task.id)
    assert payload["approval_invalidated"] == ["migrate"]

    # 排队中的 run 也不能越权执行未重新审批的步骤
    result = _run(db_session, task.id, _action(), _finish())
    assert result["status"] == TaskStatus.BLOCKED_USER.value
    stored = _fresh(db_session, task)
    after = GoalService(db_session).load_plan(stored)
    assert after.step("migrate").status == GoalStepStatus.WAITING_USER.value
    assert after.step("migrate").evidence_refs == []


def test_repeated_approval_keeps_single_run(db_session) -> None:
    plan = {
        "summary": "审批一次",
        "steps": [{"id": "migrate", "title": "迁移数据", "kind": "approval_required"}],
    }
    task = _propose(db_session, _thread(db_session), plan)
    GoalService(db_session).confirm(task.id)
    db_session.commit()
    assert _jobs(db_session, task.id) == []

    GoalService(db_session).approve_step(task.id, "migrate")
    db_session.commit()
    stored = _fresh(db_session, task)
    approved_at = GoalService(db_session).load_plan(stored).step("migrate").approved_at
    assert approved_at
    assert len(_jobs(db_session, task.id)) == 1

    with pytest.raises(GoalError):
        GoalService(db_session).approve_step(task.id, "migrate")
    stored = _fresh(db_session, task)
    assert GoalService(db_session).load_plan(stored).step("migrate").approved_at == approved_at
    assert len(_jobs(db_session, task.id)) == 1


def test_approve_requires_dependencies_met(db_session) -> None:
    plan = {
        "summary": "顺序执行",
        "steps": [
            {"id": "a", "title": "第一步", "kind": "autonomous"},
            {"id": "b", "title": "第二步", "kind": "approval_required", "depends_on": ["a"]},
        ],
    }
    task = _propose(db_session, _thread(db_session), plan)
    GoalService(db_session).confirm(task.id)
    db_session.commit()
    assert len(_jobs(db_session, task.id)) == 1

    with pytest.raises(GoalError) as error:
        GoalService(db_session).approve_step(task.id, "b")
    assert str(error.value) == "step_dependencies_unmet"
    stored = _fresh(db_session, task)
    plan_after = GoalService(db_session).load_plan(stored)
    assert plan_after.step("b").approved_at is None
    assert len(_jobs(db_session, task.id)) == 1

def test_modify_rejected_while_step_is_running(db_session) -> None:
    task = _propose(db_session, _thread(db_session), _two_step_plan())
    GoalService(db_session).confirm(task.id)
    db_session.commit()

    stored = _fresh(db_session, task)
    repo = TaskRepository(db_session)
    repo.transition_task(stored, TaskStatus.DISPATCHING.value)
    repo.transition_task(stored, TaskStatus.RUNNING.value)
    db_session.flush()
    before_revision = GoalService(db_session).load_plan(stored).revision

    with pytest.raises(GoalError) as error:
        GoalService(db_session).modify(task.id, {"summary": "运行中改计划"})
    assert str(error.value) == "goal_running"

    stored = _fresh(db_session, task)
    plan_after = GoalService(db_session).load_plan(stored)
    assert plan_after.revision == before_revision
    assert plan_after.step("resume").status == GoalStepStatus.READY.value
