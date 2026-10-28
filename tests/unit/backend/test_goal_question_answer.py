"""V58 主动提问与用户回复关联闭环验收测试（全离线，无外部 API）。

覆盖：
1. 单个待回答问题 → 回复关联 → 步骤解除等待、任务唤醒、问题关闭；
2. 多个候选未指明 → 澄清，不猜测绑定；显式 question_id 才绑定；
3. 普通聊天（无待回答问题 / 模型不调用工具）不误关联；
4. 修改计划 / 拒绝回答 → 复用 GoalService，问题关闭且只应用一次；
5. 审批步骤边界 → 对话回答不能替代审批；
6. 过期问题 → 只记录过期标记，不改目标；
7. 重复消息（同 user_event_id）→ 计划只改一次；
8. Outbox 重投 / 进程重启 → 不产生第二个问题、不重复应用回答；
9. Trace：问题、用户回答、LLM 判断（intent）与最终变更都落在事件里。

注意：生产代码会自开 SessionLocal，测试分发器把它映射到同一会话，close 会让
已加载的 ORM 实例 detach，因此只用 id 字符串传递身份，断言时重新查询。
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from aiive.context.run_context import RunContext
from aiive.db.models import AgentTask, AgentTaskEvent, Event, Thread
from aiive.runtime.question_tool import answer_goal_question
from aiive.task_runtime.goal_decision import GoalDecision, GoalDecisionEngine
from aiive.task_runtime.goal_plan import GoalStepStatus
from aiive.task_runtime.goal_questions import (
    QUESTION_ASKED_EVENT,
    QUESTION_RESOLVED_EVENT,
    list_open_questions,
    load_question,
    question_id_for,
)
from aiive.task_runtime.goal_service import GoalService
from aiive.task_runtime.repository import TaskRepository, utcnow


# ----------------------------------------------------------------------
# 夹具
# ----------------------------------------------------------------------
def _thread(db: Session) -> str:
    thread_id = str(uuid.uuid4())
    db.add(Thread(id=thread_id, title="v58"))
    db.commit()
    return thread_id


def _goal(db: Session, thread_id: str, steps: list[dict[str, Any]]) -> str:
    plan = {
        "summary": "提问闭环测试",
        "timezone": "Asia/Shanghai",
        "acceptance": ["完成"],
        "steps": steps,
    }
    task = TaskRepository(db).create_task(
        thread_id=thread_id,
        goal="提问闭环测试",
        title="提问闭环测试",
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
        trace_id="trace-v58",
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
        trace_id="trace-v58",
        turn_id=turn_id,
        turn_record_id="turn-record-v58",
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


# ----------------------------------------------------------------------
# 1. 单个待回答问题：提问 → 回复 → 解除等待
# ----------------------------------------------------------------------
def test_single_pending_question_answer_resumes_step(db_session) -> None:
    thread_id = _thread(db_session)
    task_id = _goal(db_session, thread_id, [_step("interview", "模拟面试")])
    outcome = _ask(db_session, task_id, "interview", "你希望周几面试？")
    assert outcome["status"] == "applied"
    question_id = outcome["question_id"]
    assert question_id == question_id_for(task_id, "interview", "once_at:2026-10-28T00:00:00+00:00")

    open_questions = _open(db_session, thread_id)
    assert [item.question_id for item in open_questions] == [question_id]
    assert open_questions[0].question == "你希望周几面试？"
    assert open_questions[0].message_event_id  # 与真实聊天消息关联

    user_event_id = _user_event(db_session, thread_id, "turn-1", "周三下午吧")
    result = answer_goal_question(
        answer="周三下午",
        question_id=question_id,
        intent="answer",
        ctx=_ctx(thread_id, "turn-1", user_event_id),
    )
    assert result["status"] == "applied"
    assert result["action"] == "answer"
    assert result["user_event_id"] == user_event_id

    plan = _plan(db_session, task_id)
    step = plan.step("interview")
    assert step is not None
    assert step.status == GoalStepStatus.READY
    assert _open(db_session, thread_id) == []

    # 回答按既有输入语义进入 task_state.inputs，下一次 Run 能读到。
    task = db_session.get(AgentTask, task_id)
    assert task is not None
    assert any("周三下午" in item.get("content", "") for item in (task.task_state or {}).get("inputs", []))

    # Trace：问题、用户回答、LLM 判断（intent）与最终变更。
    resolved = _events(db_session, task_id, QUESTION_RESOLVED_EVENT)
    assert len(resolved) == 1
    payload = dict(resolved[0].payload or {})
    assert payload["question_id"] == question_id
    assert payload["answer"] == "周三下午"
    assert payload["intent"] == "answer"
    assert payload["user_event_id"] == user_event_id
    answered = _events(db_session, task_id, "goal_step_answered")
    assert answered and answered[0].payload["result"] == "ready"


def test_answer_requires_explicit_binding_when_multiple_questions(db_session) -> None:
    thread_id = _thread(db_session)
    task_id = _goal(db_session, thread_id, [
        _step("a", "步骤 A"), _step("b", "步骤 B"),
    ])
    first = _ask(db_session, task_id, "a", "A 用哪种方案？", trigger_key="k-a")["question_id"]
    second = _ask(db_session, task_id, "b", "B 什么时候开始？", trigger_key="k-b")["question_id"]
    assert {first, second} == {item.question_id for item in _open(db_session, thread_id)}

    user_event_id = _user_event(db_session, thread_id, "turn-2", "就按第二个来吧")
    ambiguous = answer_goal_question(
        answer="就按第二个来吧", ctx=_ctx(thread_id, "turn-2", user_event_id),
    )
    assert ambiguous["status"] == "clarification_required"
    assert {item["question_id"] for item in ambiguous["candidates"]} == {first, second}

    revision = _plan(db_session, task_id).revision
    assert _plan(db_session, task_id).revision == revision
    assert len(_open(db_session, thread_id)) == 2

    # 模型明确指定后才绑定，且只解除对应步骤。
    bound = answer_goal_question(
        answer="B 从下周一上午开始",
        question_id=second,
        intent="answer",
        ctx=_ctx(thread_id, "turn-2", user_event_id),
    )
    assert bound["status"] == "applied"
    plan = _plan(db_session, task_id)
    assert plan.step("b").status == GoalStepStatus.READY
    assert plan.step("a").status == GoalStepStatus.WAITING_USER
    assert [item.question_id for item in _open(db_session, thread_id)] == [first]


def test_plain_chat_does_not_link_anything(db_session) -> None:
    thread_id = _thread(db_session)
    task_id = _goal(db_session, thread_id, [_step("interview", "模拟面试")])
    before = _plan(db_session, task_id)

    # 没有待回答问题时的普通聊天：即使模型误调用，也不改任何状态。
    user_event_id = _user_event(db_session, thread_id, "turn-3", "今天天气不错")
    result = answer_goal_question(
        answer="今天天气不错", ctx=_ctx(thread_id, "turn-3", user_event_id),
    )
    assert result["status"] == "no_pending_question"
    after = _plan(db_session, task_id)
    assert after.revision == before.revision
    assert after.step("interview").status == GoalStepStatus.READY

    # 有待回答问题但只是闲聊：模型不调用工具 → 计划保持等待态。
    _ask(db_session, task_id, "interview", "你希望周几面试？")
    waiting = _plan(db_session, task_id)
    assert waiting.step("interview").status == GoalStepStatus.WAITING_USER
    assert len(_open(db_session, thread_id)) == 1


# ----------------------------------------------------------------------
# 2. 修改计划 / 拒绝回答
# ----------------------------------------------------------------------
def test_modify_plan_intent_reuses_goal_service(db_session) -> None:
    thread_id = _thread(db_session)
    task_id = _goal(db_session, thread_id, [_step("interview", "模拟面试")])
    question_id = _ask(db_session, task_id, "interview", "你希望周几面试？")["question_id"]
    revision = _plan(db_session, task_id).revision

    user_event_id = _user_event(db_session, thread_id, "turn-4", "改成线上面试吧")
    result = answer_goal_question(
        answer="改成线上面试",
        question_id=question_id,
        intent="modify_plan",
        plan_changes={"summary": "线上面试计划"},
        ctx=_ctx(thread_id, "turn-4", user_event_id),
    )
    assert result["status"] == "applied"
    assert result["action"] == "modify_plan"

    plan = _plan(db_session, task_id)
    assert plan.summary == "线上面试计划"
    assert plan.revision > revision
    assert _open(db_session, thread_id) == []
    resolved = _events(db_session, task_id, QUESTION_RESOLVED_EVENT)
    assert dict(resolved[0].payload or {})["intent"] == "modify_plan"


def test_reject_intent_records_refusal_and_skips_step(db_session) -> None:
    thread_id = _thread(db_session)
    task_id = _goal(db_session, thread_id, [_step("interview", "模拟面试")])
    question_id = _ask(db_session, task_id, "interview", "你希望周几面试？")["question_id"]

    user_event_id = _user_event(db_session, thread_id, "turn-5", "这一步先不做了")
    result = answer_goal_question(
        answer="这一步先不做了",
        question_id=question_id,
        intent="reject",
        ctx=_ctx(thread_id, "turn-5", user_event_id),
    )
    assert result["status"] == "applied"
    assert result["action"] == "reject"

    plan = _plan(db_session, task_id)
    step = plan.step("interview")
    assert step is not None
    assert step.status == GoalStepStatus.SKIPPED
    assert "用户拒绝" in (step.note or "")
    answered = _events(db_session, task_id, "goal_step_answered")
    assert dict(answered[0].payload or {})["result"] == "skipped"
    assert _open(db_session, thread_id) == []


# ----------------------------------------------------------------------
# 3. 审批边界
# ----------------------------------------------------------------------
def test_approval_step_cannot_be_answered_in_chat(db_session) -> None:
    thread_id = _thread(db_session)
    task_id = _goal(db_session, thread_id, [
        _step("approve", "确认发布", kind="approval_required"),
    ])
    question_id = _ask(db_session, task_id, "approve", "确认现在发布吗？")["question_id"]
    revision = _plan(db_session, task_id).revision

    user_event_id = _user_event(db_session, thread_id, "turn-6", "确认")
    result = answer_goal_question(
        answer="确认", question_id=question_id, ctx=_ctx(thread_id, "turn-6", user_event_id),
    )
    assert result["status"] == "rejected"
    assert result["reason"] == "step_requires_approval"
    assert "审批" in result["hint"]

    plan = _plan(db_session, task_id)
    assert plan.revision == revision
    assert plan.step("approve").status == GoalStepStatus.WAITING_USER
    # 审批边界不关闭问题：仍等人工审批，问题保持打开。
    assert len(_open(db_session, thread_id)) == 1


# ----------------------------------------------------------------------
# 4. 过期问题
# ----------------------------------------------------------------------
def test_expired_question_never_modifies_goal(db_session) -> None:
    thread_id = _thread(db_session)
    task_id = _goal(db_session, thread_id, [_step("interview", "模拟面试")])
    question_id = _ask(db_session, task_id, "interview", "你希望周几面试？")["question_id"]
    revision = _plan(db_session, task_id).revision

    row = db_session.query(AgentTaskEvent).filter(
        AgentTaskEvent.dedupe_key == f"goal_question:{question_id}",
    ).one()
    payload = dict(row.payload or {})
    payload["expires_at"] = (utcnow() - timedelta(hours=1)).isoformat()
    row.payload = payload
    db_session.commit()

    assert _open(db_session, thread_id) == []
    assert load_question(db_session, question_id).status == "expired"

    user_event_id = _user_event(db_session, thread_id, "turn-7", "周三吧")
    result = answer_goal_question(
        answer="周三", question_id=question_id, ctx=_ctx(thread_id, "turn-7", user_event_id),
    )
    assert result["status"] == "question_expired"
    plan = _plan(db_session, task_id)
    assert plan.revision == revision
    assert plan.step("interview").status == GoalStepStatus.WAITING_USER
    assert _events(db_session, task_id, "goal_question_expired")


# ----------------------------------------------------------------------
# 5. 重复消息 / Outbox 重投 / 重启
# ----------------------------------------------------------------------
def test_duplicate_user_event_does_not_reapply(db_session) -> None:
    thread_id = _thread(db_session)
    task_id = _goal(db_session, thread_id, [_step("interview", "模拟面试")])
    question_id = _ask(db_session, task_id, "interview", "你希望周几面试？")["question_id"]
    user_event_id = _user_event(db_session, thread_id, "turn-8", "周三下午")

    first = answer_goal_question(
        answer="周三下午", question_id=question_id, ctx=_ctx(thread_id, "turn-8", user_event_id),
    )
    assert first["status"] == "applied"
    revision = _plan(db_session, task_id).revision

    # 同一 user_event_id 重投（Outbox 重试 / 前端重发）：只回报已处理。
    second = answer_goal_question(
        answer="周三下午", question_id=question_id, ctx=_ctx(thread_id, "turn-8", user_event_id),
    )
    assert second["status"] == "already_answered"
    assert _plan(db_session, task_id).revision == revision
    assert len(_events(db_session, task_id, QUESTION_RESOLVED_EVENT)) == 1
    assert len(_events(db_session, task_id, "goal_step_answered")) == 1


def test_replayed_decision_does_not_open_second_question(db_session) -> None:
    thread_id = _thread(db_session)
    task_id = _goal(db_session, thread_id, [_step("interview", "模拟面试")])
    first = _ask(db_session, task_id, "interview", "你希望周几面试？")
    # 决策重放（崩溃后重投）：同一 trigger_key 只对应一个问题、一条消息。
    second = _ask(db_session, task_id, "interview", "你希望周几面试？")
    assert first["question_id"] == second["question_id"]
    assert len(_events(db_session, task_id, QUESTION_ASKED_EVENT)) == 1
    messages = db_session.query(Event).filter(
        Event.thread_id == thread_id, Event.event_type == "llm_response",
    ).all()
    assert len(messages) == 1
    assert len(_open(db_session, thread_id)) == 1


def test_question_survives_restart_and_replay(db_session) -> None:
    thread_id = _thread(db_session)
    task_id = _goal(db_session, thread_id, [_step("interview", "模拟面试")])
    question_id = _ask(db_session, task_id, "interview", "你希望周几面试？")["question_id"]

    # 模拟进程重启：丢弃所有内存态后重新读取。
    db_session.expire_all()
    db_session.rollback()
    recovered = list_open_questions(db_session, thread_id)
    assert [item.question_id for item in recovered] == [question_id]

    user_event_id = _user_event(db_session, thread_id, "turn-9", "周五上午")
    result = answer_goal_question(
        answer="周五上午", question_id=question_id, ctx=_ctx(thread_id, "turn-9", user_event_id),
    )
    assert result["status"] == "applied"

    # 已关闭的问题在重启后仍不可重复应用。
    db_session.expire_all()
    again = answer_goal_question(
        answer="周五上午", question_id=question_id, ctx=_ctx(thread_id, "turn-9", user_event_id),
    )
    assert again["status"] == "already_answered"
    assert len(_events(db_session, task_id, QUESTION_RESOLVED_EVENT)) == 1


# ----------------------------------------------------------------------
# 6. 上下文注入与工具绑定条件
# ----------------------------------------------------------------------
class _FakeTokenCounter:
    def count_messages(self, model, messages, tools=None):
        from aiive.runtime.token_models import TokenCount

        return TokenCount(estimated_tokens=100, safety_margin_tokens=0, safe_tokens=100)


class _StubWorkingState:
    def render_for_context(self, *args, **kwargs) -> str:
        return ""


def _assembler():
    from aiive.runtime.context_assembler import ContextAssembler
    from aiive.runtime.context_budget import ContextBudget, PartitionBudget
    from aiive.runtime.token_models import ModelProfile

    def pb(name: str, n: int) -> PartitionBudget:
        return PartitionBudget(name=name, soft_limit_tokens=n, hard_limit_tokens=n, priority=n)

    return ContextAssembler(
        token_counter=_FakeTokenCounter(),
        budget=ContextBudget(
            model_context_window=200_000,
            reserved_output=pb("reserved_output", 2_000),
            stable_contract=pb("stable_contract", 10_000),
            core_memory=pb("core_memory", 10_000),
            working_state=pb("working_state", 10_000),
            tool_definitions=pb("tool_definitions", 10_000),
            recent_messages=pb("recent_messages", 10_000),
            retrieved_memory=pb("retrieved_memory", 10_000),
            tool_results=pb("tool_results", 10_000),
            epoch_checkpoint=pb("epoch_checkpoint", 10_000),
            segment_summaries=pb("segment_summaries", 10_000),
            sealing_bridge=pb("sealing_bridge", 10_000),
            retrieved_history_summary=pb("retrieved_history_summary", 10_000),
            deep_history_raw=pb("deep_history_raw", 10_000),
        ),
        profile=ModelProfile(
            provider="deepseek", model_id="deepseek-chat",
            full_name="deepseek/deepseek-chat",
            context_window=200_000, max_output_tokens=2_000,
        ),
    )


def _stub_assembler(monkeypatch):
    import aiive.runtime.context_assembler as ca_mod

    assembler = _assembler()
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


def test_context_injects_only_open_questions(db_session, monkeypatch) -> None:
    thread_id = _thread(db_session)
    task_id = _goal(db_session, thread_id, [_step("interview", "模拟面试")])
    question_id = _ask(db_session, task_id, "interview", "你希望周几面试？")["question_id"]
    thread = db_session.get(Thread, thread_id)
    assert thread is not None

    assembled = _stub_assembler(monkeypatch).assemble(
        db=db_session, message="周三吧", thread=thread,
    )
    system_texts = [
        message["content"] for message in assembled.messages if message["role"] == "system"
    ]
    assert any("待回答的问题" in text and question_id in text for text in system_texts)
    # AgentGraph 依据该字段决定是否为本 Turn 绑定 answer_goal_question。
    assert [item.question_id for item in assembled.agent_ctx["open_questions"]] == [question_id]

    # 系统/运行时事件来源不是用户回答：不注入问题上下文，也不绑定工具。
    runtime_turn = _stub_assembler(monkeypatch).assemble(
        db=db_session, message="[事件] 任务完成", thread=thread, _source="runtime_event",
    )
    assert not any("待回答的问题" in message["content"] for message in runtime_turn.messages)
    assert runtime_turn.agent_ctx["open_questions"] == []

    # 普通聊天（无待回答问题）：不注入任何问题上下文。
    other_thread = _thread(db_session)
    plain = _stub_assembler(monkeypatch).assemble(
        db=db_session, message="随便聊聊", thread=db_session.get(Thread, other_thread),
    )
    assert not any(
        "待回答的问题" in message["content"] for message in plain.messages
    )
    assert plain.agent_ctx["open_questions"] == []
