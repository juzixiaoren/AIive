from datetime import UTC, datetime
from types import SimpleNamespace as Obj

import pytest
from aiive.task_runtime.work_read_model import WorkReadModel, approval_preview


def project(status="queued", goal=None, events=None, approvals=None, actions=None):
    task = Obj(
        id="t",
        thread_id="thread",
        title="task",
        task_type="general",
        status=status,
        task_state={"goal": goal} if goal else {},
        target_node_id=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    return WorkReadModel(None).project(
        task, actions or [], events or [], approvals or [], [], 0
    )


@pytest.mark.parametrize(
    "status,bucket",
    [(s, "active") for s in ["queued", "dispatching", "running", "verifying"]]
    + [(s, "ended") for s in ["succeeded", "partial", "failed", "cancelled"]]
    + [
        (s, "waiting")
        for s in [
            "blocked_approval",
            "blocked_user",
            "blocked_node",
            "reconciling",
            "future",
        ]
    ],
)
def test_task_statuses(status, bucket):
    p = project(status)
    assert p["bucket"] == bucket
    assert p["progress"] is None
    if status == "future":
        assert p["data_warning"]


@pytest.mark.parametrize(
    "status,bucket",
    [
        ("proposed", "attention"),
        ("active", "active"),
        ("paused", "waiting"),
        ("blocked", "attention"),
        ("completed", "ended"),
        ("rejected", "ended"),
        ("abandoned", "ended"),
    ],
)
def test_goal_statuses(status, bucket):
    assert (
        project(goal={"status": status, "steps": [{"id": "s", "title": "step"}]})[
            "bucket"
        ]
        == bucket
    )


def test_parallel_goal_attention():
    p = project(
        goal={
            "status": "active",
            "steps": [
                {"id": "a", "title": "a", "status": "running"},
                {"id": "b", "title": "b", "status": "waiting_time"},
                {
                    "id": "c",
                    "title": "c",
                    "status": "waiting_user",
                    "kind": "interactive",
                },
                {
                    "id": "d",
                    "title": "d",
                    "status": "waiting_user",
                    "kind": "approval_required",
                },
                {"id": "e", "title": "e", "status": "waiting_user", "note": "question"},
            ],
        }
    )
    assert p["bucket"] == "attention"
    assert p["attention_count"] == 3
    assert p["progress"]["total"] == 5


def test_task_question_vs_watcher():
    q = Obj(
        id="event",
        event_type="task_blocked_user",
        created_at=datetime.now(UTC),
        payload={"question": "question"},
    )
    w = Obj(id="watch", event_type="task_watching", payload={})
    assert project("blocked_user", events=[q])["bucket"] == "attention"
    assert project("blocked_user", events=[w, q])["bucket"] == "waiting"


@pytest.mark.parametrize(
    "tool",
    ["desktop_exec", "run_shell", "desktop_fs_delete", "desktop_fs_write", "unknown"],
)
def test_preview_allowlist(tool):
    result = approval_preview(
        Obj(
            tool_name=tool,
            risk_snapshot={},
            tool_args={
                "command": "echo hi",
                "env": {"TOKEN": "SECRET"},
                "other": "SECRET",
                "content": "SECRET",
            },
        )
    )
    assert "SECRET" not in str(result)
    assert "other" not in result
    if tool == "unknown":
        assert "command" not in result


def test_approval_requires_consistent_owners_and_expiry(monkeypatch):
    import hashlib
    from datetime import timedelta

    from aiive.task_runtime.repository import canonical_json

    monkeypatch.setattr(
        "aiive.task_runtime.read_repository.TaskReadRepository.visible_child",
        lambda *args: True,
    )
    action = Obj(
        id="a",
        task_id="t",
        status="awaiting_approval",
        arguments_hash=hashlib.sha256(canonical_json({}).encode()).hexdigest(),
        descriptor_hash="d",
        capability_id="run_shell",
        target_node_id=None,
    )
    approval = Obj(
        id="approval",
        created_at=datetime.now(UTC),
        task_id="t",
        thread_id="thread",
        action_id="a",
        tool_args_hash=action.arguments_hash,
        descriptor_hash="d",
        tool_name="run_shell",
        tool_args={},
        risk_snapshot={},
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    assert (
        project("blocked_approval", actions=[action], approvals=[approval])[
            "attention_count"
        ]
        == 1
    )
    for key, bad in [
        ("task_id", "other"),
        ("thread_id", "other"),
        ("action_id", "other"),
        ("tool_args_hash", "wrong"),
        ("expires_at", datetime.now(UTC) - timedelta(seconds=1)),
    ]:
        previous = getattr(approval, key)
        setattr(approval, key, bad)
        assert (
            project("blocked_approval", actions=[action], approvals=[approval])[
                "attention_count"
            ]
            == 0
        )
        setattr(approval, key, previous)
    assert (
        project("running", actions=[action], approvals=[approval])["attention_count"]
        == 0
    )


@pytest.mark.parametrize(
    "status", ["blocked_node", "reconciling", "blocked_approval", "blocked_user"]
)
@pytest.mark.parametrize("step_status", ["running", "ready"])
def test_goal_task_blocking_precedes_ready_step(status, step_status):
    p = project(
        status,
        goal={
            "status": "active",
            "steps": [{"id": "s", "title": "s", "status": step_status}],
        },
    )
    assert p["bucket"] == "waiting"
    assert p["bucket_reason"] == status


@pytest.mark.parametrize(
    "status", ["blocked_node", "reconciling", "blocked_approval", "blocked_user"]
)
def test_independent_attention_precedes_task_block(status):
    p = project(
        status,
        goal={
            "status": "active",
            "steps": [
                {"id": "r", "title": "r", "status": "running"},
                {
                    "id": "q",
                    "title": "q",
                    "kind": "interactive",
                    "status": "waiting_user",
                },
            ],
        },
    )
    assert p["bucket"] == "attention"
    item = p["attention_items"][0]
    assert item["title"] and item["description"] and item["resource_id"] == "q"
    assert item["created_at"] and item["expires_at"] is None
    assert item["allowed_actions"] == ["start_step"]


@pytest.mark.parametrize(
    "kind", ["goal_confirmation", "goal_blocked", "goal_question", "goal_step_approval"]
)
def test_attention_contract_for_goal_kinds(kind):
    goal = {
        "status": "proposed"
        if kind == "goal_confirmation"
        else "blocked"
        if kind == "goal_blocked"
        else "active",
        "summary": "plan",
        "blocked_reason": "review",
        "steps": [
            {
                "id": "s",
                "title": "step",
                "note": "question",
                "status": "waiting_user",
                "kind": "approval_required"
                if kind == "goal_step_approval"
                else "autonomous",
            }
        ],
    }
    item = project(goal=goal)["attention_items"][0]
    assert item["kind"] == kind
    assert all(
        key in item
        for key in [
            "title",
            "description",
            "resource_id",
            "created_at",
            "expires_at",
            "allowed_actions",
        ]
    )
    assert item["allowed_actions"]
