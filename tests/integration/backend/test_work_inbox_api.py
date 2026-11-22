from datetime import UTC, datetime

import pytest
from aiive.db.models import AgentTask, AgentTaskEvent, ConversationState, Thread
from aiive.task_runtime.work_read_model import WorkReadModel


def seed(db, count=3):
    db.add(Thread(id="thread", title="work"))
    db.flush()
    db.add(ConversationState(id="default", current_thread_id="thread"))
    now = datetime.now(UTC)
    for i in range(count):
        db.add(
            AgentTask(
                id=f"{i:05d}",
                thread_id="thread",
                title="work",
                goal="work",
                status="queued",
                updated_at=now,
            )
        )
    db.flush()
    return WorkReadModel(db)


def test_keyset_equal_timestamps_and_read_only(db_session):
    model = seed(db_session)
    first = model.inbox("active", 1)
    second = model.inbox("active", 1, first["next_cursor"])
    assert first["items"][0]["task_id"] == "00002"
    assert second["items"][0]["task_id"] == "00001"
    for bucket in ["waiting", "attention"]:
        with pytest.raises(ValueError):
            model.inbox(bucket, 1, first["next_cursor"])
    with pytest.raises(ValueError):
        model.inbox("active", 1, "garbage")
    db_session.get(ConversationState, "default").current_thread_id = "other"
    with pytest.raises(ValueError):
        model.inbox("active", 1, first["next_cursor"])
    db_session.get(ConversationState, "default").current_thread_id = "thread"


def test_scan_cap_resumes(db_session):
    model = seed(db_session, 2050)
    first = model.inbox("ended", 1)
    assert first["items"] == [] and first["has_more"]
    second = model.inbox("ended", 1, first["next_cursor"])
    assert second == {"items": [], "has_more": False, "next_cursor": None}


def test_empty_inbox_does_not_create_thread(db_session):
    assert WorkReadModel(db_session).inbox()["items"] == []
    assert db_session.query(Thread).count() == 0


def test_timeline_raw_highwater(db_session):
    model = seed(db_session, 1)
    for seq, kind, visibility in [
        (1, "task_created", "task"),
        (2, "run_started", "internal"),
        (3, "unknown", "task"),
    ]:
        db_session.add(
            AgentTaskEvent(
                task_id="00000",
                sequence=seq,
                event_type=kind,
                visibility=visibility,
                payload={"secret": "SECRET"},
            )
        )
    db_session.flush()
    result = model.timeline("00000", 0, 2)
    assert result["next_sequence"] == 2 and result["has_more"]
    assert len(result["items"]) == 1
    assert "SECRET" not in str(result)
    assert model.timeline("00000", 2, 2)["next_sequence"] == 3
    view = model.work_view(db_session.get(AgentTask, "00000"))
    assert "task_state" not in str(view)
    assert not db_session.new and not db_session.dirty and not db_session.deleted


def test_route_contract(db_session):
    from aiive.api.routes_agent_tasks import router
    from aiive.db.base import get_db
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db_session
    seed(db_session, 1)
    client = TestClient(app)
    assert (
        client.get("/api/agent-tasks/inbox?bucket=active").json()["items"][0]["task_id"]
        == "00000"
    )
    assert client.get("/api/agent-tasks/inbox?bucket=bad").status_code == 422
    assert client.get("/api/agent-tasks/inbox?limit=51").status_code == 422
    assert client.get("/api/agent-tasks/inbox?cursor=garbage").status_code == 400
    assert client.get("/api/agent-tasks/00000/work-view").status_code == 200
    assert client.get("/api/agent-tasks/missing/timeline").status_code == 404

    invalid_cursors = ("evidence?cursor=garbage", "timeline?before_sequence=2&after_sequence=1")
    for path in invalid_cursors:
        assert client.get(f"/api/agent-tasks/00000/{path}").status_code == 400
    db_session.get(AgentTask, "00000").task_state = {"inputs": ["unproven"]}
    db_session.flush()
    for path in invalid_cursors:
        assert client.get(f"/api/agent-tasks/00000/{path}").status_code == 404
        assert client.get(f"/api/agent-tasks/missing/{path}").status_code == 404


def test_batch_questions_32_and_lifecycle(db_session):
    from datetime import timedelta

    from aiive.task_runtime.goal_questions import (
        list_open_questions,
        list_open_questions_for_tasks,
        record_question,
        resolve_question,
    )

    seed(db_session, 1)
    task = db_session.get(AgentTask, "00000")
    task.task_state = {
        "goal": {
            "status": "active",
            "steps": [
                {"id": f"s{i}", "title": "step", "status": "waiting_user"}
                for i in range(32)
            ],
        }
    }
    db_session.flush()
    for i in range(32):
        record_question(
            db_session,
            task_id=task.id,
            step_id=f"s{i}",
            decision_id="d",
            trigger_key="one",
            question=f"q{i}",
        )
    questions = list_open_questions_for_tasks(db_session, [task.id])[task.id]
    assert len(questions) == 32
    assert len(list_open_questions(db_session, "thread")) == 5
    from aiive.db.models import Event

    db_session.add(
        Event(
            id="user",
            thread_id="thread",
            trace_id="test",
            event_type="user_message",
            payload={"text": "yes"},
        )
    )
    db_session.flush()
    resolve_question(
        db_session,
        question=questions[0],
        user_event_id="user",
        answer="yes",
        intent="answer",
    )
    assert len(list_open_questions_for_tasks(db_session, [task.id])[task.id]) == 31
    record_question(
        db_session,
        task_id=task.id,
        step_id=questions[1].step_id,
        decision_id="d",
        trigger_key="two",
        question="replacement",
    )
    current = list_open_questions_for_tasks(db_session, [task.id])[task.id]
    assert len(current) == 31
    assert questions[1].question_id not in {q.question_id for q in current}
    assert (
        list_open_questions_for_tasks(
            db_session, [task.id], now=datetime.now(UTC) + timedelta(days=4)
        )[task.id]
        == []
    )
    task.task_state = {"goal": {**task.task_state["goal"], "status": "paused"}}
    db_session.flush()
    assert list_open_questions_for_tasks(db_session, [task.id])[task.id] == []


def test_reads_issue_no_business_writes(db_session):
    from sqlalchemy import event

    model = seed(db_session, 1)
    task = db_session.get(AgentTask, "00000")
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().split()[0].upper())

    connection = db_session.connection()
    event.listen(connection, "before_cursor_execute", capture)
    try:
        model.inbox("active")
        model.work_view(task)
        model.timeline(task.id)
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert statements and set(statements) <= {"SELECT"}
    assert not db_session.new and not db_session.dirty and not db_session.deleted


def test_rich_view_and_shared_timeline(db_session):
    from aiive.db.models import (
        AgentAction,
        AgentRun,
        DesktopNode,
        Event,
        TaskArtifact,
        TaskEvidence,
    )
    from aiive.forget.durable_sources import persist_sources
    from aiive.task_runtime.goal_plan import GoalPlan

    model = seed(db_session, 1)
    task = db_session.get(AgentTask, "00000")
    task.task_state = {
        "goal": {
            "status": "active",
            "steps": [{"id": "s", "title": "s", "status": "done"}],
        }
    }
    task.report = {"summary": "安全结果", "raw": "SECRET"}
    task.last_error = "SECRET stacktrace"
    db_session.add(
        DesktopNode(id="node", name="开发电脑", platform="windows", arch="x64")
    )
    db_session.flush()
    task.target_node_id = "node"
    origin = Event(
        id="origin",
        thread_id="thread",
        trace_id="test",
        event_type="user_message",
        payload={"text": "source"},
    )
    db_session.add(origin)
    db_session.add(AgentRun(id="run", task_id=task.id, run_index=1))
    db_session.flush()
    # A report-bearing task requires durable source proof under the existing Forget policy.
    persist_sources(db_session, "agent_task", task.id, {"source_event_id": "origin"})
    db_session.add(
        AgentAction(
            id="action",
            task_id=task.id,
            run_id="run",
            sequence=1,
            capability_id="run_shell",
            arguments_hash="h",
            descriptor_hash="d",
            idempotency_key="key",
            status="succeeded",
        )
    )
    db_session.add(
        TaskArtifact(
            id="artifact",
            task_id=task.id,
            name="result.txt",
            artifact_type="file",
            summary="文件成果",
            object_bucket="SECRET",
            object_key="SECRET",
            content_hash="hash",
        )
    )
    db_session.add(
        TaskEvidence(
            id="evidence",
            task_id=task.id,
            kind="tool_result",
            summary="执行证明",
            content_hash="hash",
            inline_payload={"secret": "SECRET"},
        )
    )
    db_session.flush()
    persist_sources(
        db_session, "task_artifact", "artifact", {"source_event_id": "origin"}
    )
    persist_sources(
        db_session, "task_evidence", "evidence", {"source_event_id": "origin"}
    )
    for seq, kind in enumerate(
        [
            "goal_step_done",
            "goal_step_failed",
            "action_planned",
            "action_dispatched",
            "action_running",
            "action_succeeded",
            "action_failed",
        ],
        1,
    ):
        db_session.add(
            AgentTaskEvent(
                task_id=task.id,
                sequence=seq,
                event_type=kind,
                action_id="action" if kind.startswith("action_") else None,
                payload={"command": "SECRET", "summary": "SECRET"},
            )
        )
    db_session.flush()
    view = model.work_view(task)
    assert (
        view["goal"]["progress"]
        == GoalPlan.model_validate(task.task_state["goal"]).progress()
    )
    assert view["target_device"]["name"] == "开发电脑"
    assert view["target_device"]["platform"] == "windows"
    assert view["target_device"]["relationship"] == "configured"
    assert view["artifacts"][0]["name"] == "result.txt"
    assert view["evidence"][0]["summary"] == "执行证明"
    assert len(view["timeline"]) == 7 and all(e["summary"] for e in view["timeline"])
    assert view["timeline"] == model.timeline(task.id)["items"]
    assert "SECRET" not in str(view)


def test_questions_ttl_applied_in_candidate_query(db_session, monkeypatch):
    from datetime import timedelta

    from aiive.task_runtime import goal_questions

    seed(db_session, 1)
    old = datetime.now(UTC) - timedelta(hours=73)
    db_session.add(
        AgentTaskEvent(
            task_id="00000",
            sequence=1,
            event_type="goal_question_asked",
            created_at=old,
            payload={"question_id": "old"},
        )
    )
    db_session.flush()
    examined = []
    original = goal_questions._to_question

    def capture(*args, **kwargs):
        examined.append(args[0].id)
        return original(*args, **kwargs)

    monkeypatch.setattr(goal_questions, "_to_question", capture)
    assert goal_questions.list_open_questions_for_tasks(db_session, ["00000"]) == {
        "00000": []
    }
    assert examined == []


def test_history_query_count_is_bounded(db_session):
    from aiive.db.models import AgentAction, AgentRun
    from sqlalchemy import event, insert

    model = seed(db_session, 1)
    db_session.add(AgentRun(id="run", task_id="00000", run_index=1))
    db_session.flush()

    def add_actions(start, stop):
        db_session.execute(
            insert(AgentAction),
            [
                {
                    "id": f"a{i}",
                    "task_id": "00000",
                    "run_id": "run",
                    "sequence": i,
                    "capability_id": "run_shell",
                    "arguments_hash": "h",
                    "descriptor_hash": "d",
                    "idempotency_key": f"key{i}",
                    "status": "succeeded",
                }
                for i in range(start, stop)
            ],
        )
        db_session.flush()

    def count_reads():
        statements = []

        def capture(*args):
            statements.append(args[2])

        connection = db_session.connection()
        event.listen(connection, "before_cursor_execute", capture)
        try:
            model.project_many([db_session.get(AgentTask, "00000")])
        finally:
            event.remove(connection, "before_cursor_execute", capture)
        return len(statements)

    add_actions(1, 101)
    baseline = count_reads()
    add_actions(101, 1001)
    assert count_reads() <= baseline + 1


def test_work_view_history_budgets(db_session):
    model = seed(db_session, 1)
    for seq in range(1, 601):
        db_session.add(
            AgentTaskEvent(
                task_id="00000",
                sequence=seq,
                event_type="unknown",
                visibility="internal",
                payload={},
            )
        )
    db_session.flush()
    view = model.work_view(db_session.get(AgentTask, "00000"))
    assert view["timeline"] == []
    assert not view["truncated"]["timeline"]
    assert view["history_cursor"] is None
    assert view["last_sequence"] == 600


ACTION_STATUSES = ("running", "dispatched", "awaiting_approval", "succeeded", "failed", "unknown")


def seed_provenance(db, size):
    from hashlib import sha256

    from aiive.db.forget_models import ContentProvenanceRef, ForgetOperation, ForgetShield, ForgetTombstone
    from aiive.db.models import AgentAction, AgentRun, ApprovalRequest, DesktopNode, Event, MemoryRecord, TaskArtifact, TaskEvidence, TurnRecord

    seed(db, 7)
    task = db.get(AgentTask, "00000")
    task.status = "blocked_approval"
    task.task_state = {"goal": {"status": "active", "revision": 2, "steps": [
        {"id": f"s{i}", "title": "step", "status": "waiting_user"} for i in range(32)
    ]}}
    db.add_all([
        DesktopNode(id="node", name="Desktop", platform="windows", arch="x64"),
        TurnRecord(id="turn", turn_id="logical-turn", thread_id="thread"),
        ForgetOperation(id="forget", operation_key="forget", mode="everywhere", selector_type="event", selector_hash="hash"),
    ])
    db.add_all(Event(id=key, thread_id="thread", trace_id=key, event_type="user_message",
                     turn_id="logical-turn" if key == "turn-event" else None, payload={})
               for key in ("task-input", "plan-input", "turn-event", "memory-source", "shielded", "tombstoned"))
    db.flush()
    db.add_all([
        ForgetShield(forget_operation_id="forget", selector_type="event", target_type="event",
                     target_id="shielded", cutoff_created_at=datetime.now(UTC)),
        ForgetTombstone(forget_operation_id="forget", target_type="event", target_id="tombstoned"),
        MemoryRecord(id="memory", content="source memory", memory_type="fact", scope_type="thread",
                     scope_id="thread", canonical_key="bench", source_event_id="memory-source"),
    ])

    def proof(owner, identity, *sources):
        db.add_all(ContentProvenanceRef(owner_type=owner, owner_id=identity, source_type=kind, source_id=key)
                   for kind, key in sources)

    proof("memory_record", "memory", ("event", "memory-source"))
    proof("goal_plan", task.id, ("event", "plan-input"))
    args_hash = sha256(b"{}").hexdigest()
    for task_index in range(7):
        task_id = f"{task_index:05d}"
        run_id = f"run-{task_index}"
        db.add(AgentRun(id=run_id, task_id=task_id, run_index=1))
        proof("agent_task", task_id, ("event", "task-input"), ("turn_record", "turn"))
        db.flush()
        statuses = (*ACTION_STATUSES, *(ACTION_STATUSES[i % 6] for i in range(size if not task_index else 0)))
        for seq, status in enumerate(statuses, 1):
            key = f"{task_index}-{seq:04d}"
            action_id, evidence_id, artifact_id = f"a-{key}", f"e-{key}", f"f-{key}"
            db.add(AgentAction(
                id=action_id, task_id=task_id, run_id=run_id, sequence=seq,
                capability_id="run_shell", status=status, arguments_hash=args_hash,
                descriptor_hash="descriptor", idempotency_key=action_id,
                target_node_id="node" if status == "running" else None,
                result_summary={"evidence_id": evidence_id} if status == "succeeded" else None,
            ))
            db.add_all([
                TaskEvidence(id=evidence_id, task_id=task_id, content_hash="hash", inline_payload={"private": "inline"}),
                TaskArtifact(id=artifact_id, task_id=task_id, name=f"{key}.txt", content_hash="hash",
                             object_bucket="private", object_key="private"),
                AgentTaskEvent(id=f"t-{key}", task_id=task_id, sequence=seq, event_type=f"action_{status}",
                               action_id=action_id, payload={"private": "event"}),
            ])
            origin = f"origin-{key}"
            db.add(Event(id=origin, thread_id="thread", trace_id=origin, event_type="task_input", payload={}))
            source = {2: "shielded", 5: "tombstoned"}.get(seq, origin)
            proof("task_evidence", evidence_id, ("event", source), ("memory_record", "memory"))
            proof("task_artifact", artifact_id, ("event", source), ("turn_record", "turn"))
            if status == "awaiting_approval" and task_index == 0:
                db.add(ApprovalRequest(id=f"p-{key}", task_id=task_id, thread_id="thread", action_id=action_id,
                                       tool_name="run_shell", tool_args={}, tool_args_hash=args_hash,
                                       descriptor_hash="descriptor"))
                proof("approval_request", f"p-{key}", ("event", "plan-input"))
        db.add(AgentTaskEvent(id=f"block-{task_id}", task_id=task_id, sequence=len(statuses)+1,
                              event_type="task_blocked_user", payload={"question": "blocked?"}))
        proof("agent_task_event", f"block-{task_id}", ("event", "plan-input"))
        db.flush()
    for i in range(32):
        question_id = f"question-{i}"
        db.add(AgentTaskEvent(id=question_id, task_id=task.id, sequence=10_000+i*2,
                              event_type="goal_question_asked", dedupe_key=f"goal_question:{question_id}",
                              visibility="internal", payload={"question_id": question_id, "step_id": f"s{i}", "question": question_id}))
        proof("agent_task_event", question_id, ("event", "plan-input"))
        if i % 2 == 0:
            db.add(AgentTaskEvent(id=f"resolution-{i}", task_id=task.id, sequence=10_001+i*2,
                                  event_type="goal_question_resolved", dedupe_key=f"goal_question_resolved:{question_id}",
                                  visibility="internal", payload={"question_id": question_id}))
            proof("agent_task_event", f"resolution-{i}", ("event", "plan-input"))
    db.flush()
    db.expire_all()


@pytest.mark.parametrize("size", [1, 10, 100, 500])
def test_provenance_query_budget(db_session, size):
    import re
    from collections import Counter
    from time import perf_counter
    from sqlalchemy import event

    seed_provenance(db_session, size)
    connection = db_session.connection()
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    counts = {}

    def measure(name, read, budget):
        db_session.expire_all()
        statements.clear()
        started = perf_counter()
        result = read(WorkReadModel(db_session))
        elapsed = perf_counter() - started
        verbs = Counter(sql.split()[0].upper() for sql in statements)
        tables = Counter(re.search(r"\bFROM\s+(\w+)", sql, re.I)[1] for sql in statements)
        print(f"work-read n={size} {name}: {len(statements)} SQL {elapsed*1000:.1f} ms {dict(verbs)} {dict(tables)}")
        assert verbs.keys() == {"SELECT"}
        counts[name] = len(statements)
        assert counts[name] <= budget, tables
        assert not any(column in sql for sql in statements for column in (
            "task_evidence.inline_payload", "task_evidence.object_bucket", "task_evidence.object_key",
            "task_artifacts.object_bucket", "task_artifacts.object_key",
        ))
        return result

    event.listen(connection, "before_cursor_execute", capture)
    try:
        inbox = measure("Inbox", lambda model: model.inbox(), 100)
        detail = measure("WorkView", lambda model: model.work_view("00000"), 100)
        incremental = measure("Timeline", lambda model: model.timeline("00000"), 30)
        history = measure("Timeline history", lambda model: model.timeline_history("00000"), 30)
        if size >= 100:
            previous = measure("Timeline older", lambda model: model.timeline_history("00000", history["next_sequence"]), 30)
            assert previous["items"][-1]["sequence"] < history["items"][0]["sequence"]
            for kind in ("evidence", "artifacts"):
                page = measure(kind, lambda model: model.metadata_page("00000", kind, detail["metadata_cursors"][kind]), 30)
                assert len(page["items"]) == 50
                assert {item["id"] for item in page["items"]}.isdisjoint(item["id"] for item in detail[kind])
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert counts["Inbox"] <= 30 and counts["WorkView"] <= 42
    assert len(inbox["items"]) == 7
    first = next(item for item in inbox["items"] if item["task_id"] == "00000")
    assert first["attention_count"] == len(detail["attention_items"])
    assert first["primary_attention"] == detail["attention_items"][0]
    assert sum(item["kind"] == "goal_question" for item in detail["attention_items"]) == 16
    assert {action["status"] for action in detail["actions"]} == set(ACTION_STATUSES)
    assert history["items"] == detail["timeline"]
    assert [item["sequence"] for item in incremental["items"]] == sorted(item["sequence"] for item in incremental["items"])
    assert [item["sequence"] for item in history["items"]] == sorted(item["sequence"] for item in history["items"])
    for kind, prefix in (("evidence", "e"), ("artifacts", "f")):
        ids = {item["id"] for item in detail[kind]}
        assert f"{prefix}-0-0002" not in ids and f"{prefix}-0-0005" not in ids
        assert len(ids) == min(50, size + 4)
        if size >= 100:
            assert detail["metadata_cursors"][kind]
