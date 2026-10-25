"""运行证据采集：聚合、阈值与一次性故障过滤（全部离线）。"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

from aiive.db.models import AgentTask, OutboxJob, SelfDevRequest, Thread
from aiive.selfdev.improvement_evidence import (
    CATEGORY_EVAL,
    CATEGORY_SELFDEV,
    CATEGORY_TASK,
    EvidenceCollector,
    is_transient,
    normalize_signature,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _thread(db, thread_id: str = "thread-evidence") -> str:
    db.add(Thread(id=thread_id, title="证据线程"))
    db.flush()
    return thread_id


def _task(
    db,
    thread_id: str,
    *,
    status: str = "failed",
    task_type: str = "research",
    error: str = "tool crashed",
    when: datetime | None = None,
) -> AgentTask:
    stamp = when or _now()
    row = AgentTask(
        thread_id=thread_id,
        task_type=task_type,
        title="跑一次研究任务",
        goal="研究某个主题",
        status=status,
        last_error=error,
        created_at=stamp,
        updated_at=stamp,
    )
    db.add(row)
    db.flush()
    return row


def _job(db, *, job_type: str = "memory_extraction", error: str = "bad payload") -> OutboxJob:
    row = OutboxJob(
        operation_id=f"op-{uuid.uuid4().hex}",
        job_type=job_type,
        status="deadletter",
        payload={},
        error_message=error,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(row)
    db.flush()
    return row


def _selfdev(db, *, status: str = "gate_failed", goal: str = "修复 X") -> SelfDevRequest:
    row = SelfDevRequest(
        trace_id=str(uuid.uuid4()),
        goal=goal,
        status=status,
        plan={"repair": {"failure_reason": "replay eval failed"}},
        created_at=_now(),
    )
    db.add(row)
    db.flush()
    return row


def _collector(tmp_path, **kwargs) -> EvidenceCollector:
    return EvidenceCollector(eval_dir=tmp_path / "eval_runs", **kwargs)


def test_signature_ignores_ids_and_numbers() -> None:
    left = normalize_signature("tool crashed id=3f1a9c2b-1111-2222-3333-444455556666 at step 12")
    right = normalize_signature("tool crashed id=9a8b7c6d-7777-8888-9999-000011112222 at step 99")
    assert left == right


def test_transient_detection() -> None:
    assert is_transient("read timeout after 30s")
    assert is_transient("connection reset by peer")
    assert not is_transient("schema mismatch on memory_records")


def test_repeated_failures_are_aggregated_into_one_group(db_session, tmp_path) -> None:
    thread_id = _thread(db_session)
    for _ in range(3):
        _task(db_session, thread_id, error=f"tool crashed id={uuid.uuid4()}")

    batch = _collector(tmp_path).collect(db_session)

    groups = [group for group in batch.groups if group.category == CATEGORY_TASK]
    assert len(groups) == 1
    assert groups[0].occurrences == 3
    assert len(groups[0].evidence_ids) == 3


def test_single_failure_is_not_a_system_problem(db_session, tmp_path) -> None:
    thread_id = _thread(db_session)
    _task(db_session, thread_id, error="tool crashed once")

    batch = _collector(tmp_path).collect(db_session)

    assert batch.is_empty
    assert batch.dropped.get("below_min_occurrences") == 1


def test_single_transient_failure_is_dropped(db_session, tmp_path) -> None:
    thread_id = _thread(db_session)
    _task(db_session, thread_id, error="connection reset by peer")

    batch = _collector(tmp_path).collect(db_session)

    assert batch.is_empty


def test_repeated_transient_failures_are_kept_but_flagged(db_session, tmp_path) -> None:
    thread_id = _thread(db_session)
    for _ in range(3):
        _task(db_session, thread_id, error="connection reset by peer")

    batch = _collector(tmp_path).collect(db_session)

    assert len(batch.groups) == 1
    assert batch.groups[0].transient_ratio == 1.0


def test_selfdev_failure_is_structural_and_kept_once(db_session, tmp_path) -> None:
    _selfdev(db_session)

    batch = _collector(tmp_path).collect(db_session)

    assert len(batch.groups) == 1
    assert batch.groups[0].category == CATEGORY_SELFDEV
    assert batch.groups[0].structural is True


def test_candidate_rows_are_not_treated_as_failures(db_session, tmp_path) -> None:
    _selfdev(db_session, status="candidate_pending", goal="候选")

    batch = _collector(tmp_path).collect(db_session)

    assert batch.is_empty


def test_outbox_deadletter_is_collected(db_session, tmp_path) -> None:
    _job(db_session, error="payload missing field")
    _job(db_session, error="payload missing field")

    batch = _collector(tmp_path).collect(db_session)

    assert len(batch.groups) == 1
    assert batch.groups[0].occurrences == 2
    assert batch.groups[0].capability == "memory_extraction"


def test_eval_failure_is_collected_from_files(db_session, tmp_path) -> None:
    eval_dir = tmp_path / "eval_runs"
    eval_dir.mkdir()
    (eval_dir / "eval_20260101T000000Z.json").write_text(
        json.dumps({
            "ok": False,
            "error": "",
            "contracts": [{"name": "kernel_import", "ok": False, "detail": "boom"}],
            "replay": None,
            "candidate_eval": None,
            "notes": [],
        }),
        encoding="utf-8",
    )

    batch = _collector(tmp_path).collect(db_session)

    assert len(batch.groups) == 1
    assert batch.groups[0].category == CATEGORY_EVAL
    assert "kernel_import" in batch.groups[0].error_signature


def test_window_filters_old_records(db_session, tmp_path) -> None:
    thread_id = _thread(db_session)
    old = _now() - timedelta(days=10)
    for _ in range(3):
        _task(db_session, thread_id, error="tool crashed", when=old)

    batch = _collector(tmp_path, lookback_hours=72).collect(db_session)

    assert batch.is_empty


def test_group_cap_keeps_structural_first(db_session, tmp_path) -> None:
    thread_id = _thread(db_session)
    for index in range(5):
        for _ in range(2):
            _task(db_session, thread_id, task_type=f"cap-{index}", error=f"fail {index}")
    _selfdev(db_session)

    batch = _collector(tmp_path, max_groups=3).collect(db_session)

    assert len(batch.groups) == 3
    assert batch.groups[0].category == CATEGORY_SELFDEV
    assert batch.dropped.get("over_group_cap") == 3
