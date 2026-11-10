"""V59 待回答问题的状态一致性验收测试（全离线，无外部 API）。

覆盖：
1. 目标暂停后普通聊天不再注入旧问题，直接回答只回报失效状态、不改目标；
2. 旧 question_id 的直接服务端调用同样被拒绝（不依赖聊天工具）；
3. 步骤被修改/重置后旧问题失效，同线程其它步骤的有效问题不受影响；
4. 同一步骤被重新提问后，旧问题被新问题取代；
5. 目标恢复后，仍有效的问题可继续回答，已变化的步骤问题仍然失效；
6. 审批步骤在恢复后仍受原审批限制（本次修复不绕过审批）；
7. 失效/过期与重复回复都不修改目标。

注意：生产代码会自开 SessionLocal，测试分发器把它映射到同一会话，close 会让
已加载的 ORM 实例 detach，因此只用 id 字符串传递身份，断言时重新查询。
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session

from aiive.context.run_context import RunContext
from aiive.db.models import AgentTask, AgentTaskEvent, Event, Thread
from aiive.runtime.question_tool import answer_goal_question
from aiive.task_runtime.goal_decision import GoalDecision, GoalDecisionEngine
from aiive.task_runtime.goal_plan import GoalStatus, GoalStepStatus
from aiive.task_runtime.goal_questions import (
    QUESTION_RESOLVED_EVENT,
    list_open_questions,
    load_question,
)
from aiive.task_runtime.goal_service import GoalError, GoalService
from aiive.task_runtime.repository import TaskRepository, utcnow


# ----------------------------------------------------------------------
# 夹具（与 V58 用例同构，保持独立文件便于单独运行）
# ----------------------------------------------------------------------
def _thread(db: Session) -> str:
    thread_id = str(uuid.uuid4())
    db.add(Thread(id=thread_id, title="v59"))
    db.commit()
    return thread_id


def _goal(db: Session, thread_id: str, steps: list[dict[str, Any]]) -> str:
    plan = {
        "summary": "状态一致性测试",
        "timezone": "Asia/Shanghai",
        "acceptance": ["完成"],
        "steps": steps,
    }
    task = TaskRepository(db).create_task(
        thread_id=thread_id,
        goal="状态一致性测试",
        title="状态一致性测试",
        task_type="long_term_goal",
        task_brief={"scope": {"allowed_capabilities": ["get_current_time"]}},
        enqueue=False,
    )
    GoalService(db).attach_plan(task, plan)
    GoalService(db).confirm(task.id)
    db.commit()
    return str(task.id)


def _step(step_id: str, title: str = "步骤", kind: str = "autonomous") -> dict[str, Any]:
    return {"id": step_id, "title": title, "kind": kind}


def _ask(db: Session, task_id: str, step_id: str, question: str,
         trigger_key: str = "once_at:2026-10-28T00:00:00+00:00") -> dict[str, Any]:
    """模拟一次 ask_user 决策应用（真实路径：GoalDecisionEngine.apply）。"""
    engine = GoalDecisionEngine(db)
    outcome = engine.apply(
        task_id=task_id,
        step_id=step_id,
        trigger_key=trigger_key,
        decision=GoalDecision(action="ask_user", step_id=step_id, message=question),
        message_text=question,
        trace_id=f"trace-{task_id}",
    )
    db.commit()
    return outcome


def _user_event(db: Session, thread_id: str, turn_id: str, content: str) -> str:
    event = Event(
        id=str(uuid.uuid4()),
        trace_id="trace-v59",
        thread_id=thread_id,
        turn_id=turn_id,
        turn_event_index=0,
        event_type="user_message",
        payload={"content": content, "message_source": "user"},
    )
    db.add(event)
    db.commit()
    return str(event.id)


def _ctx(thread_id: str, turn_id: str, user_event_id: str) -> RunContext:
    return RunContext(
        thread_id=thread_id,
        trace_id="trace-v59",
        turn_id=turn_id,
        turn_record_id="turn-record-v59",
        source_event_ids=[user_event_id],
    )


def _plan(db: Session, task_id: str):
    db.expire_all()
    task = db.get(AgentTask, task_id)
    assert task is not None
    plan = GoalService(db).load_plan(task)
    assert plan is not None
    return plan


def _events(db: Session, task_id: str, event_type: str) -> list[AgentTaskEvent]:
    db.expire_all()
    return db.query(AgentTaskEvent).filter(
        AgentTaskEvent.task_id == task_id,
        AgentTaskEvent.event_type == event_type,
    ).all()


def _open(db: Session, thread_id: str):
    db.expire_all()
    return list_open_questions(db, thread_id)


def _reply(db: Session, thread_id: str, question_id: str, text: str, turn_id: str) -> dict[str, Any]:
    user_event_id = _user_event(db, thread_id, turn_id, text)
    return answer_goal_question(
        answer=text,
        question_id=question_id,
        intent="answer",
        ctx=_ctx(thread_id, turn_id, user_event_id),
    )


def _retitle(db: Session, task_id: str, step_id: str, title: str) -> None:
    """通过真实 modify 入口改步骤执行内容（会让该步骤状态与证据失效）。"""
    plan = _plan(db, task_id)
    steps = [step.model_dump(mode="json") for step in plan.steps]
    for item in steps:
        if item["id"] == step_id:
            item["title"] = title
    GoalService(db).modify(task_id, {"steps": steps})
    db.commit()


class _FakeTokenCounter:
    def count_messages(self, model, messages, tools=None):
        from aiive.runtime.token_models import TokenCount

        return TokenCount(estimated_tokens=100, safety_margin_tokens=0, safe_tokens=100)


class _StubWorkingState:
    def context_fields(self, *args, **kwargs):
        return {}

    def render_context_fields(self, fields) -> str:
        return ""


def _stub_assembler(monkeypatch):
    import aiive.runtime.context_assembler as ca_mod
    from aiive.runtime.context_assembler import ContextAssembler
    from aiive.runtime.context_budget import ContextBudget
    from aiive.runtime.token_models import ModelProfile


    assembler = ContextAssembler(
        token_counter=_FakeTokenCounter(),
        budget=ContextBudget(model_context_window=200_000, reserved_output_tokens=2_000),
        profile=ModelProfile(
            provider="deepseek", model_id="deepseek-chat",
            full_name="deepseek/deepseek-chat",
            context_window=200_000, max_output_tokens=2_000,
        ),
    )
    assembler._load_agent_context = lambda db, message, thread, trace_id=None: {
        "system_content": "系统前缀",
        "recall_messages": [],
        "recall_pack": None,
        "history_summary_text": "",
        "stable_contract_text": "系统前缀",
        "core_memory_text": "",
        "recall_text": "",
    }
    assembler._load_history_bounded = lambda *a, **k: []
    assembler._dicts_to_chat_messages = lambda events: list(events)
    assembler._load_epoch_checkpoint = lambda *a, **k: ""
    assembler._load_segment_summaries = lambda *a, **k: ""
    assembler._load_sealing_bridge = lambda *a, **k: ""
    assembler._build_tools_schema_list = lambda *a, **k: []
    assembler._touch_injected_memory = lambda *a, **k: None
    monkeypatch.setattr(ca_mod, "WorkingStateService", _StubWorkingState)
    return assembler


def _assemble(db: Session, monkeypatch, thread_id: str, message: str = "周三吧"):
    thread = db.get(Thread, thread_id)
    assert thread is not None
    return _stub_assembler(monkeypatch).assemble(db=db, message=message, thread=thread)


# ----------------------------------------------------------------------
# 1. 目标暂停：不再注入，也不再能被回答
# ----------------------------------------------------------------------
def test_paused_goal_hides_question_and_rejects_answer(db_session, monkeypatch) -> None:
    thread_id = _thread(db_session)
    task_id = _goal(db_session, thread_id, [_step("interview", "模拟面试")])
    question_id = _ask(db_session, task_id, "interview", "你希望周几面试？")["question_id"]
    assert [item.question_id for item in _open(db_session, thread_id)] == [question_id]

    GoalService(db_session).pause(task_id)
    db_session.commit()

    # 暂停后：上下文不再注入该问题，本轮也不会绑定回答工具。
    assert _open(db_session, thread_id) == []
    assembled = _assemble(db_session, monkeypatch, thread_id)
    assert not any("待回答的问题" in message["content"] for message in assembled.messages)
    assert assembled.agent_ctx["open_questions"] == []

    # 直接按旧 question_id 回答：只回报失效状态，目标与步骤都不变。
    result = _reply(db_session, thread_id, question_id, "周三下午", "turn-p1")
    assert result["status"] == "question_stale"
    assert result["reason"] == "goal_paused"
    plan = _plan(db_session, task_id)
    assert plan.status == GoalStatus.PAUSED
    assert plan.step("interview").status == GoalStepStatus.WAITING_USER
    assert _events(db_session, task_id, "goal_step_answered") == []
    assert _events(db_session, task_id, QUESTION_RESOLVED_EVENT) == []


def test_paused_goal_rejects_direct_service_call(db_session) -> None:
    """旧 question_id 的直接服务端调用同样被校验，不依赖聊天工具。"""
    thread_id = _thread(db_session)
    task_id = _goal(db_session, thread_id, [_step("interview", "模拟面试")])
    question_id = _ask(db_session, task_id, "interview", "你希望周几面试？")["question_id"]
    GoalService(db_session).pause(task_id)
    db_session.commit()
    revision = _plan(db_session, task_id).revision

    with pytest.raises(GoalError) as excinfo:
        GoalService(db_session).answer_step_question(
            task_id, "interview",
            answer="周三下午", question_id=question_id, intent="answer",
        )
    # 目标级状态先于问题校验：暂停的目标一律不接受回答。
    assert str(excinfo.value) == "goal_not_active"
    plan = _plan(db_session, task_id)
    assert plan.revision == revision
    assert plan.step("interview").status == GoalStepStatus.WAITING_USER


# ----------------------------------------------------------------------
# 2. 步骤被修改 / 重置
# ----------------------------------------------------------------------
def test_step_content_change_invalidates_only_that_question(db_session) -> None:
    thread_id = _thread(db_session)
    task_id = _goal(db_session, thread_id, [
        _step("interview", "模拟面试"), _step("review", "复盘"),
    ])
    stale_id = _ask(db_session, task_id, "interview", "你希望周几面试？", trigger_key="k-a")["question_id"]
    valid_id = _ask(db_session, task_id, "review", "复盘定在什么时候？", trigger_key="k-b")["question_id"]
    assert {item.question_id for item in _open(db_session, thread_id)} == {stale_id, valid_id}

    _retitle(db_session, task_id, "interview", "模拟面试（改为线上）")

    # 被改的步骤不再是 waiting_user：旧问题失效；未改的步骤问题仍然有效。
    assert [item.question_id for item in _open(db_session, thread_id)] == [valid_id]
    closed = load_question(db_session, stale_id)
    assert closed is not None and closed.status == "stale"
    assert closed.invalid_reason == "step_not_waiting_user"

    revision = _plan(db_session, task_id).revision
    result = _reply(db_session, thread_id, stale_id, "周三下午", "turn-m1")
    assert result["status"] == "question_stale"
    assert result["reason"] == "step_not_waiting_user"
    plan = _plan(db_session, task_id)
    assert plan.revision == revision
    assert plan.step("interview").status != GoalStepStatus.WAITING_USER
    assert _events(db_session, task_id, "goal_step_answered") == []

    # 直接调用服务端（不经聊天工具）同样被旧 question_id 校验拦下。
    with pytest.raises(GoalError) as excinfo:
        GoalService(db_session).answer_step_question(
            task_id, "interview",
            answer="周三下午", question_id=stale_id, intent="answer",
        )
    assert str(excinfo.value).startswith("question_stale:step_not_waiting_user")

    # 有效问题不受影响：仍可回答并解除等待。
    applied = _reply(db_session, thread_id, valid_id, "周五晚上", "turn-m2")
    assert applied["status"] == "applied"
    assert _plan(db_session, task_id).step("review").status == GoalStepStatus.READY


def test_new_question_supersedes_previous_one_for_same_step(db_session) -> None:
    thread_id = _thread(db_session)
    task_id = _goal(db_session, thread_id, [_step("interview", "模拟面试")])
    first = _ask(db_session, task_id, "interview", "你希望周几面试？", trigger_key="k-1")["question_id"]
    second = _ask(db_session, task_id, "interview", "具体几点方便？", trigger_key="k-2")["question_id"]
    assert first != second
    assert [item.question_id for item in _open(db_session, thread_id)] == [second]

    closed = load_question(db_session, first)
    assert closed is not None and closed.invalid_reason == "superseded"
    revision = _plan(db_session, task_id).revision
    stale = _reply(db_session, thread_id, first, "周三下午", "turn-s1")
    assert stale["status"] == "question_stale"
    assert stale["reason"] == "superseded"
    assert _plan(db_session, task_id).revision == revision

    assert _reply(db_session, thread_id, second, "周三 15:00", "turn-s2")["status"] == "applied"


# ----------------------------------------------------------------------
# 3. 恢复目标：只有仍有效的问题可以继续关联
# ----------------------------------------------------------------------
def test_resume_restores_still_valid_question(db_session, monkeypatch) -> None:
    thread_id = _thread(db_session)
    task_id = _goal(db_session, thread_id, [_step("interview", "模拟面试")])
    question_id = _ask(db_session, task_id, "interview", "你希望周几面试？")["question_id"]

    GoalService(db_session).pause(task_id)
    db_session.commit()
    assert _open(db_session, thread_id) == []

    GoalService(db_session).resume(task_id)
    db_session.commit()
    # 步骤仍在 waiting_user、目标回到 active：问题自动重新有效。
    assert [item.question_id for item in _open(db_session, thread_id)] == [question_id]
    assembled = _assemble(db_session, monkeypatch, thread_id)
    assert any("待回答的问题" in message["content"] for message in assembled.messages)
    assert [item.question_id for item in assembled.agent_ctx["open_questions"]] == [question_id]

    result = _reply(db_session, thread_id, question_id, "周三下午", "turn-r1")
    assert result["status"] == "applied"
    assert _plan(db_session, task_id).step("interview").status == GoalStepStatus.READY


def test_resume_does_not_revive_question_for_changed_step(db_session) -> None:
    thread_id = _thread(db_session)
    task_id = _goal(db_session, thread_id, [_step("interview", "模拟面试")])
    question_id = _ask(db_session, task_id, "interview", "你希望周几面试？")["question_id"]

    GoalService(db_session).pause(task_id)
    db_session.commit()
    _retitle(db_session, task_id, "interview", "模拟面试（改为线上）")
    GoalService(db_session).resume(task_id)
    db_session.commit()

    assert _open(db_session, thread_id) == []
    result = _reply(db_session, thread_id, question_id, "周三下午", "turn-r2")
    assert result["status"] == "question_stale"
    assert result["reason"] == "step_not_waiting_user"
    assert _plan(db_session, task_id).step("interview").status != GoalStepStatus.WAITING_USER


# ----------------------------------------------------------------------
# 4. 审批边界不因本次修复被绕过
# ----------------------------------------------------------------------
def test_approval_step_keeps_original_approval_after_resume(db_session) -> None:
    thread_id = _thread(db_session)
    task_id = _goal(db_session, thread_id, [
        _step("approve", "确认发布", kind="approval_required"),
    ])
    question_id = _ask(db_session, task_id, "approve", "确认现在发布吗？")["question_id"]

    GoalService(db_session).pause(task_id)
    db_session.commit()
    assert _open(db_session, thread_id) == []
    GoalService(db_session).resume(task_id)
    db_session.commit()
    assert [item.question_id for item in _open(db_session, thread_id)] == [question_id]

    revision = _plan(db_session, task_id).revision
    result = _reply(db_session, thread_id, question_id, "确认", "turn-a1")
    assert result["status"] == "rejected"
    assert result["reason"] == "step_requires_approval"
    assert "审批" in result["hint"]
    plan = _plan(db_session, task_id)
    assert plan.revision == revision
    assert plan.step("approve").status == GoalStepStatus.WAITING_USER
    # 审批问题不因回答而关闭：仍需人工审批。
    assert len(_open(db_session, thread_id)) == 1


# ----------------------------------------------------------------------
# 5. 失效 / 过期 / 重复回复都不改目标
# ----------------------------------------------------------------------
def test_repeated_reply_to_stale_question_never_modifies_goal(db_session) -> None:
    thread_id = _thread(db_session)
    task_id = _goal(db_session, thread_id, [_step("interview", "模拟面试")])
    question_id = _ask(db_session, task_id, "interview", "你希望周几面试？")["question_id"]
    GoalService(db_session).pause(task_id)
    db_session.commit()
    revision = _plan(db_session, task_id).revision

    for turn in ("turn-d1", "turn-d2"):
        result = _reply(db_session, thread_id, question_id, "周三下午", turn)
        assert result["status"] == "question_stale"
        assert result["reason"] == "goal_paused"

    plan = _plan(db_session, task_id)
    assert plan.revision == revision
    assert plan.step("interview").status == GoalStepStatus.WAITING_USER
    assert _events(db_session, task_id, "goal_step_answered") == []
    assert _events(db_session, task_id, QUESTION_RESOLVED_EVENT) == []


def test_expired_question_stays_closed_across_repeats(db_session) -> None:
    thread_id = _thread(db_session)
    task_id = _goal(db_session, thread_id, [_step("interview", "模拟面试")])
    question_id = _ask(db_session, task_id, "interview", "你希望周几面试？")["question_id"]
    row = db_session.query(AgentTaskEvent).filter(
        AgentTaskEvent.dedupe_key == f"goal_question:{question_id}",
    ).one()
    payload = dict(row.payload or {})
    payload["expires_at"] = (utcnow() - timedelta(hours=1)).isoformat()
    row.payload = payload
    db_session.commit()
    revision = _plan(db_session, task_id).revision

    for turn in ("turn-e1", "turn-e2"):
        result = _reply(db_session, thread_id, question_id, "周三下午", turn)
        assert result["status"] == "question_expired"

    plan = _plan(db_session, task_id)
    assert plan.revision == revision
    assert plan.step("interview").status == GoalStepStatus.WAITING_USER
    assert _events(db_session, task_id, "goal_step_answered") == []
    # 过期标记按 question_id 幂等，重复回复不会写出第二条。
    assert len(_events(db_session, task_id, "goal_question_expired")) == 1


def test_stale_hint_branches_by_reason(db_session) -> None:
    """不同失效原因给不同提示：暂停说可恢复、步骤变化说等新提问、被取代说回答最新的。"""
    paused_thread = _thread(db_session)
    paused_task = _goal(db_session, paused_thread, [_step("interview", "模拟面试")])
    paused_id = _ask(db_session, paused_task, "interview", "你希望周几面试？")["question_id"]
    GoalService(db_session).pause(paused_task)
    db_session.commit()
    paused = _reply(db_session, paused_thread, paused_id, "周三", "turn-h1")
    assert paused["status"] == "question_stale"
    assert "恢复目标后" in paused["hint"] and "不需要重新发起" in paused["hint"]

    changed_thread = _thread(db_session)
    changed_task = _goal(db_session, changed_thread, [_step("interview", "模拟面试")])
    changed_id = _ask(db_session, changed_task, "interview", "你希望周几面试？")["question_id"]
    _retitle(db_session, changed_task, "interview", "模拟面试（改为线上）")
    changed = _reply(db_session, changed_thread, changed_id, "周三", "turn-h2")
    assert changed["reason"] == "step_not_waiting_user"
    assert "已被修改或重置" in changed["hint"] and "新的提问" in changed["hint"]

    superseded_thread = _thread(db_session)
    superseded_task = _goal(db_session, superseded_thread, [_step("interview", "模拟面试")])
    old_id = _ask(db_session, superseded_task, "interview", "你希望周几面试？", trigger_key="k-1")["question_id"]
    _ask(db_session, superseded_task, "interview", "具体几点方便？", trigger_key="k-2")
    superseded = _reply(db_session, superseded_thread, old_id, "周三", "turn-h3")
    assert superseded["reason"] == "superseded"
    assert "最新的那个问题" in superseded["hint"]

    # 计划缺失是独立分支，不能被 "goal_" 前缀兜底成"阻塞或已结束"。
    from aiive.runtime.question_tool import _stale_hint

    assert "目标计划已不存在" in _stale_hint("goal_not_found")
    assert "目标已暂停" in _stale_hint("goal_paused")
