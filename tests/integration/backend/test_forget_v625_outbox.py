"""V62.5：Outbox 精确关联、cutoff 新旧 Job、嵌套 List 清理、旧 Payload fencing。"""
from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from aiive.db.forget_models import ForgetTarget
from aiive.db.models import (
    AgentRun,
    AgentTask,
    Event,
    OutboxJob,
)
from aiive.forget.content_scope import load_scope
from aiive.forget.content_scrub import verify_scope
from aiive.worker.handlers_forget import _scrub_wide
from aiive.worker.outbox_payload_contract import (
    is_redacted_payload,
    payload_has_content,
    redact_job_payload,
)
from tests.integration.backend.test_forget_v624_scope import (
    _at,
    _event,
    _id,
    _now,
    _op,
    _thread,
    _turn,
)


def _trace(db: Session, event_id: str) -> str:
    return str(db.query(Event.trace_id).filter_by(id=event_id).scalar() or "")


def test_outbox_nested_list_recursive_redaction(db_session: Session) -> None:
    thread_id = _thread(db_session)
    ev = _event(db_session, thread_id, "secret", _now())
    nested_payload = {
        "operation_id": "nested:1",
        "event_id": ev,
        "thread_id": thread_id,
        "status": "pending",
        "items": [
            {"content": "secret-1", "event_id": ev, "status": "ok"},
            [
                {"user_message": "secret-2", "task_id": "task-1"},
                {"layers": [{"reply": "secret-3", "memory_id": "mem-1"}]},
            ],
            {"deep": {"layers": [{"goal": "secret-4", "turn_id": "turn-1"}]}},
        ],
    }
    job = OutboxJob(
        id=_id(), operation_id="nested:1", job_type="memory_extraction",
        status="pending", payload=nested_payload, trace_id=_trace(db_session, ev),
        created_at=_now(),
    )
    # 顶层 List payload：契约也必须递归清理
    list_payload = [
        {"content": "top-secret", "event_id": ev},
        [{"reply": "nested-secret", "status": "ok"}],
    ]
    list_job = OutboxJob(
        id=_id(), operation_id="nested:2", job_type="goal_event_dispatch",
        status="pending", payload=list_payload, trace_id=_trace(db_session, ev),
        created_at=_now(),
    )
    db_session.add_all([job, list_job])
    op_id = _op(db_session, mode="everywhere", selector="thread",
                payload={"thread_id": thread_id})
    db_session.commit()

    _scrub_wide(db_session, op_id)
    db_session.commit()
    db_session.expire_all()
    scope = load_scope(db_session, op_id, "everywhere")
    assert scope is not None and verify_scope(db_session, scope).ok

    payload = db_session.query(OutboxJob).filter_by(id=job.id).first().payload
    assert is_redacted_payload(payload)
    assert not payload_has_content("memory_extraction", payload)
    assert payload["event_id"] == ev and payload["thread_id"] == thread_id
    assert payload["status"] == "pending"
    assert payload["items"][0]["event_id"] == ev
    assert payload["items"][0]["status"] == "ok"
    assert payload["items"][0]["content"] == "[redacted]"
    assert payload["items"][1][0]["user_message"] == "[redacted]"
    assert payload["items"][1][0]["task_id"] == "task-1"
    assert payload["items"][1][1]["layers"][0]["reply"] == "[redacted]"
    assert payload["items"][1][1]["layers"][0]["memory_id"] == "mem-1"
    assert payload["items"][2]["deep"]["layers"][0]["goal"] == "[redacted]"
    assert payload["items"][2]["deep"]["layers"][0]["turn_id"] == "turn-1"

    list_payload_after = db_session.query(OutboxJob).filter_by(id=list_job.id).first().payload
    assert list_payload_after[0]["content"] == "[redacted]"
    assert list_payload_after[0]["event_id"] == ev
    assert list_payload_after[1][0]["reply"] == "[redacted]"
    assert list_payload_after[1][0]["status"] == "ok"

    # 人为注入深层残留 → Verify 必须失败
    import json
    from sqlalchemy import text as sa_text

    payload["items"][1][1]["layers"][0]["reply"] = "leak"
    db_session.execute(
        sa_text("UPDATE outbox_jobs SET payload = :payload WHERE id = :id"),
        {"payload": json.dumps(payload), "id": job.id},
    )
    db_session.commit()
    assert not verify_scope(db_session, scope).ok


def test_redact_job_payload_top_level_list() -> None:
    payload = [{"content": "x"}, [{"user_message": "y"}]]
    redacted = redact_job_payload("memory_extraction", payload)
    assert redacted[0]["content"] == "[redacted]"
    assert redacted[1][0]["user_message"] == "[redacted]"
    assert not payload_has_content("memory_extraction", redacted)


def test_outbox_precise_matching_and_cutoff_boundaries(db_session: Session) -> None:
    thread_id = _thread(db_session)
    old_turn = _turn(db_session, thread_id, _at(-120))
    old_ev = _event(db_session, thread_id, "old", _at(-120), old_turn.turn_id)
    new_turn = _turn(db_session, thread_id, _at(120))
    new_ev = _event(db_session, thread_id, "new", _at(120), new_turn.turn_id)
    op_id = _op(db_session, mode="everywhere", selector="thread",
                payload={"thread_id": thread_id})

    # cutoff 后新建、仅引用 Thread/新 Event 的 Job → 保留
    new_extract = OutboxJob(
        id=_id(), operation_id="memory_extraction:new", job_type="memory_extraction",
        status="pending",
        payload={
            "user_message": "new secret", "reply": "new reply",
            "thread_id": thread_id, "source_event_ids": [new_ev],
            "source_turn_record_id": new_turn.id,
            "source_turn_id": new_turn.turn_id,
        },
        trace_id="new-trace", created_at=_at(120),
    )
    new_goal = OutboxJob(
        id=_id(), operation_id="goal_event:new", job_type="goal_event_dispatch",
        status="pending",
        payload={
            "event_id": new_ev, "event_type": "user_message",
            "event_payload": {"text": "new event"},
            "thread_id": thread_id,
        },
        trace_id="new-trace", created_at=_at(120),
    )
    # cutoff 后新建但明确引用旧 Event → 清理（派生 Job）
    derived_old = OutboxJob(
        id=_id(), operation_id="goal_event:derived", job_type="goal_event_dispatch",
        status="pending",
        payload={
            "event_id": old_ev, "event_type": "user_message",
            "event_payload": {"text": "old derived"},
            "thread_id": thread_id,
        },
        trace_id="derived-trace", created_at=_at(120),
    )
    # 子串碰撞：operation_id 含旧 Turn ID 前缀但实际不同 token；payload event_id 也只多一个字符
    collision = OutboxJob(
        id=_id(), operation_id=f"memory_extraction:{old_turn.id}EXTRA:nonce",
        job_type="memory_extraction", status="pending",
        payload={"event_id": old_ev + "X", "thread_id": thread_id,
                 "user_message": "collision"},
        trace_id="collision-trace", created_at=_at(120),
    )
    db_session.add_all([new_extract, new_goal, derived_old, collision])
    db_session.commit()

    _scrub_wide(db_session, op_id)
    db_session.commit()
    db_session.expire_all()

    by_id = {j.id: j for j in db_session.query(OutboxJob).all()}
    assert by_id[new_extract.id].payload["user_message"] == "new secret"
    assert not is_redacted_payload(by_id[new_extract.id].payload)
    assert by_id[new_goal.id].payload["event_payload"] == {"text": "new event"}
    assert by_id[derived_old.id].payload["event_payload"] == {}
    assert is_redacted_payload(by_id[derived_old.id].payload)
    assert by_id[collision.id].payload["user_message"] == "collision"


def test_new_memory_extraction_job_still_executes(db_session: Session, monkeypatch) -> None:
    from aiive.worker import outbox_handlers
    from aiive.worker.outbox_dto import ClaimedJob

    thread_id = _thread(db_session)
    turn = _turn(db_session, thread_id, _at(-120))
    old_ev = _event(db_session, thread_id, "old", _at(-120), turn.turn_id)
    op_id = _op(db_session, mode="everywhere", selector="event_ids",
                payload={"event_ids": [old_ev]})
    db_session.add(ForgetTarget(
        id=_id(), forget_operation_id=op_id, target_type="event",
        target_id=old_ev, batch_no=0, frozen_at=_now(),
    ))
    new_turn = _turn(db_session, thread_id, _at(120))
    new_ev = _event(db_session, thread_id, "new", _at(120), new_turn.turn_id)
    job = OutboxJob(
        id=_id(), operation_id="memory_extraction:new2",
        job_type="memory_extraction", status="pending",
        payload={
            "user_message": "new secret", "reply": "new reply",
            "thread_id": thread_id, "source_event_ids": [new_ev],
            "source_turn_record_id": new_turn.id,
            "source_turn_id": new_turn.turn_id,
            "message_source": "user", "extraction_mode": "async",
        },
        trace_id="new2-trace", created_at=_at(120),
    )
    db_session.add(job)
    db_session.commit()
    _scrub_wide(db_session, op_id)
    db_session.commit()
    db_session.expire_all()
    assert db_session.query(OutboxJob).filter_by(id=job.id).first().payload["user_message"] == "new secret"

    captured: dict = {}

    def _fake_llm():
        return object()

    def _extract(self, **kwargs):  # noqa: ANN001, ANN003
        captured.update(kwargs)
        return []

    monkeypatch.setattr(outbox_handlers, "_get_llm_client", _fake_llm)
    monkeypatch.setattr(outbox_handlers.UnifiedMemoryExtractor, "extract", _extract)
    monkeypatch.setattr(outbox_handlers, "SessionLocal",
                        sessionmaker(bind=db_session.get_bind()))
    claimed = ClaimedJob(
        id=job.id, job_type="memory_extraction", payload=job.payload,
        trace_id=job.trace_id, retry_count=0, max_retries=3,
        claim_token="ct", schema_version=1, worker_id="w",
    )
    result = outbox_handlers.handle_memory_extraction(claimed)
    assert "forget_content_redacted" not in result.reason
    assert captured.get("user_message") == "new secret"


def test_partial_task_new_run_job_preserved(db_session: Session) -> None:
    thread_id = _thread(db_session)
    task = AgentTask(
        id=_id(), thread_id=thread_id, task_type="research", title="t",
        goal="g", status="running", executor_type="llm", priority=0,
        task_brief={}, task_state={}, created_at=_at(-3600),
    )
    db_session.add(task)
    db_session.flush()
    old_run = AgentRun(
        id=_id(), task_id=task.id, run_index=0, trigger="user",
        status="running", input_snapshot={"x": 1}, created_at=_at(-60),
    )
    new_run = AgentRun(
        id=_id(), task_id=task.id, run_index=1, trigger="user",
        status="created", input_snapshot={"y": 2}, created_at=_at(120),
    )
    db_session.add_all([old_run, new_run])
    op_id = _op(
        db_session, mode="everywhere", selector="time_range",
        payload={"time_from": _at(-120).isoformat(), "time_to": _at(0).isoformat()},
    )
    new_job = OutboxJob(
        id=_id(), operation_id=f"agent_task_run:{task.id}:v1:new",
        job_type="agent_task_run", status="pending",
        payload={"task_id": task.id, "trigger": "new",
                 "run_id": new_run.id, "goal": "new goal"},
        trace_id="new-run-trace", created_at=_at(120),
    )
    db_session.add(new_job)
    db_session.commit()
    _scrub_wide(db_session, op_id)
    db_session.commit()
    db_session.expire_all()
    assert db_session.query(OutboxJob).filter_by(id=new_job.id).first().payload["goal"] == "new goal"
    assert not is_redacted_payload(db_session.query(OutboxJob).filter_by(id=new_job.id).first().payload)


def test_goal_reminder_handlers_use_fresh_db_payload(
    db_session: Session, monkeypatch,
) -> None:
    from aiive.worker import handlers_goal_events
    from aiive.worker.outbox_dto import ClaimedJob

    thread_id = _thread(db_session)
    ev = _event(db_session, thread_id, "x", _now())
    fresh_payload = {
        "event_id": ev, "event_type": "user_message",
        "event_payload": {"text": "fresh"},
        "thread_id": thread_id,
    }
    job = OutboxJob(
        id=_id(), operation_id="goal_event:fresh", job_type="goal_event_dispatch",
        status="pending", payload=fresh_payload, trace_id="fresh-trace",
        created_at=_now(),
    )
    db_session.add(job)
    db_session.commit()

    stale = ClaimedJob(
        id=job.id, job_type="goal_event_dispatch",
        payload={"forget_content_redacted": True},
        trace_id=job.trace_id, retry_count=0, max_retries=3,
        claim_token="c", schema_version=1, worker_id="w",
    )
    seen: list[dict] = []

    def _capture(payload: dict):  # noqa: ANN001
        seen.append(payload)
        return None

    monkeypatch.setattr(handlers_goal_events, "event_from_job_payload", _capture)
    monkeypatch.setattr(handlers_goal_events, "SessionLocal",
                        sessionmaker(bind=db_session.get_bind()))
    result = handlers_goal_events.handle_goal_event_dispatch(stale)
    assert seen and seen[0]["event_payload"] == {"text": "fresh"}
    assert "forget_content_redacted" not in result.reason

    # DB 中已脱敏但 ClaimedJob 旧副本未脱敏 → guard 必须拒绝旧 payload
    job.payload = {"forget_content_redacted": True, "event_id": ev}
    db_session.commit()
    stale2 = ClaimedJob(
        id=job.id, job_type="goal_event_dispatch", payload=fresh_payload,
        trace_id=job.trace_id, retry_count=0, max_retries=3,
        claim_token="c", schema_version=1, worker_id="w",
    )
    result2 = handlers_goal_events.handle_goal_event_dispatch(stale2)
    assert result2.reason == "forget_content_redacted"


def test_all_user_data_and_time_range_job_boundaries(db_session: Session) -> None:
    thread_id = _thread(db_session)
    old_ev = _event(db_session, thread_id, "old", _at(-120))
    new_ev = _event(db_session, thread_id, "new", _at(120))
    op_thread = _op(db_session, mode="everywhere", selector="thread",
                    payload={"thread_id": thread_id})
    old_job = OutboxJob(
        id=_id(), operation_id="old:1", job_type="memory_extraction",
        status="pending",
        payload={"user_message": "old", "thread_id": thread_id,
                 "source_event_ids": [old_ev]},
        trace_id="old", created_at=_at(-120),
    )
    new_job = OutboxJob(
        id=_id(), operation_id="new:1", job_type="memory_extraction",
        status="pending",
        payload={"user_message": "new", "thread_id": thread_id,
                 "source_event_ids": [new_ev]},
        trace_id="new", created_at=_at(120),
    )
    db_session.add_all([old_job, new_job])
    db_session.commit()
    _scrub_wide(db_session, op_thread)
    db_session.commit()
    db_session.expire_all()
    assert is_redacted_payload(db_session.query(OutboxJob).filter_by(id=old_job.id).first().payload)
    assert not is_redacted_payload(db_session.query(OutboxJob).filter_by(id=new_job.id).first().payload)
