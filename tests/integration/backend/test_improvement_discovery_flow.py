"""运行证据驱动的改进发现：调度入队 → Outbox Handler → 候选落库 → 接受复用修复流程。

全部离线：脚本化伪 LLM + 共享 SQLite，不访问网络。
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from aiive.db.models import AgentTask, Event, OutboxJob, SelfDevRequest, Thread
from aiive.selfdev.improvement_discovery import (
    CANDIDATE_STATUS_ACCEPTED,
    CANDIDATE_STATUS_PENDING,
    DISCOVERY_EVENT_TYPE,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_NO_EVIDENCE,
    ImprovementDiscovery,
)
from aiive.selfdev.improvement_evidence import EvidenceCollector
from aiive.worker.handler_registry import HandlerRegistry
from aiive.worker.handlers_improvement import (
    JOB_IMPROVEMENT_DISCOVERY,
    handle_improvement_discovery,
)
from aiive.worker.job_types import ENABLED_OUTBOX_JOB_TYPES
from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome
from aiive.worker.outbox_handlers import register_all


class ScriptedLLM:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        return SimpleNamespace(content=self.content)


def _claimed(trace_id: str = "trace-improve") -> ClaimedJob:
    return ClaimedJob(
        id=str(uuid.uuid4()),
        job_type=JOB_IMPROVEMENT_DISCOVERY,
        payload={"schema_version": 1},
        trace_id=trace_id,
        retry_count=0,
        max_retries=3,
        claim_token=str(uuid.uuid4()),
        schema_version=1,
        worker_id="test-worker",
    )


def _seed_failed_tasks(db, count: int = 2) -> list[str]:
    db.add(Thread(id="thread-run", title="运行线程"))
    db.flush()
    stamp = datetime.now(UTC) - timedelta(hours=1)
    ids: list[str] = []
    for _ in range(count):
        row = AgentTask(
            thread_id="thread-run",
            task_type="research",
            title="研究任务",
            goal="研究",
            status="failed",
            last_error=f"tool crashed id={uuid.uuid4()}",
            created_at=stamp,
            updated_at=stamp,
        )
        db.add(row)
        db.flush()
        ids.append(f"agent_task:{row.id}")
    db.commit()
    return ids


def _candidate(evidence_ids: list[str], occurrences: int = 2) -> dict:
    return {
        "title": "工具崩溃缺少错误隔离",
        "problem": "同一工具在同一任务类型下反复崩溃",
        "impact": "任务失败率上升",
        "evidence_ids": list(evidence_ids),
        "occurrences": occurrences,
        "possible_causes": [{"cause": "缺少参数校验", "confidence": "medium", "uncertain": True}],
        "target": "在工具执行层增加参数校验",
        "acceptance_criteria": ["非法参数不再抛出未捕获异常"],
        "test_method": "单元测试覆盖非法参数",
        "scope": "tools 执行层",
        "risk": "medium",
        "confidence": "medium",
    }


def _discovery(llm, tmp_path, **kwargs) -> ImprovementDiscovery:
    collector = EvidenceCollector(eval_dir=tmp_path / "eval_runs", **kwargs)
    return ImprovementDiscovery(llm, collector=collector)


def test_job_type_is_allowed_and_registered() -> None:
    registry = HandlerRegistry()
    register_all(registry)

    assert JOB_IMPROVEMENT_DISCOVERY in ENABLED_OUTBOX_JOB_TYPES
    assert registry.get(JOB_IMPROVEMENT_DISCOVERY) is handle_improvement_discovery
    assert registry.is_schema_supported(JOB_IMPROVEMENT_DISCOVERY, 1) is True


def test_scheduler_enqueue_is_idempotent_per_window(db, monkeypatch) -> None:
    from aiive.worker import scheduler_daemon as scheduler_module

    enqueued: list[tuple[str, str]] = []

    class StubWorker:
        def enqueue(self, session, job_type, payload, trace_id=None, operation_id=None):
            enqueued.append((job_type, operation_id or ""))
            session.add(OutboxJob(
                operation_id=operation_id or str(uuid.uuid4()),
                job_type=job_type,
                status="pending",
                payload=payload,
            ))
            session.flush()

    monkeypatch.setattr(scheduler_module, "_outbox_worker", StubWorker())
    now = datetime(2026, 1, 1, 7, 0, tzinfo=UTC)

    first = scheduler_module.enqueue_improvement_discovery_job(db, now=now)
    second = scheduler_module.enqueue_improvement_discovery_job(db, now=now + timedelta(minutes=30))
    db.commit()

    assert first == second
    assert len(enqueued) == 1
    assert db.query(OutboxJob).filter(OutboxJob.job_type == JOB_IMPROVEMENT_DISCOVERY).count() == 1


def test_scheduler_scanner_job_enqueues_once(db, monkeypatch) -> None:
    from aiive.worker import scheduler_daemon as scheduler_module

    created: list[str] = []

    class StubWorker:
        def enqueue(self, session, job_type, payload, trace_id=None, operation_id=None):
            created.append(operation_id or "")
            session.add(OutboxJob(
                operation_id=operation_id or str(uuid.uuid4()),
                job_type=job_type,
                status="pending",
                payload=payload,
            ))
            session.flush()

    monkeypatch.setattr(scheduler_module, "_outbox_worker", StubWorker())
    monkeypatch.setattr(scheduler_module, "SessionLocal", lambda: db)

    scheduler_module._improvement_discovery_scanner_job()
    scheduler_module._improvement_discovery_scanner_job()

    assert len(created) == 1


def test_outbox_worker_dispatches_improvement_discovery(db, monkeypatch, tmp_path) -> None:
    """真实 Worker 链路：enqueue → claim → dispatch → finalize。"""
    from aiive.core import llm_client as llm_module
    from aiive.worker.outbox_heartbeat import ActiveClaimRegistry
    from aiive.worker.outbox_worker import OutboxWorker

    monkeypatch.setenv("AIIVE_EVAL_RUNS_DIR", str(tmp_path / "eval_runs"))
    ids = _seed_failed_tasks(db, count=3)
    fake = ScriptedLLM(json.dumps(
        {"candidates": [_candidate(ids, occurrences=3)]}, ensure_ascii=False,
    ))
    monkeypatch.setattr(llm_module, "default_llm_client", lambda: fake)

    registry = HandlerRegistry()
    register_all(registry)
    worker = OutboxWorker(
        worker_id="test-worker",
        registry=registry,
        claims=ActiveClaimRegistry(),
    )
    worker.enqueue(
        db, JOB_IMPROVEMENT_DISCOVERY, {"schema_version": 1},
        operation_id="verify:improvement:e2e",
    )
    db.commit()

    processed = worker.poll(
        max_jobs=1, only_types=frozenset({JOB_IMPROVEMENT_DISCOVERY}),
    )

    db.expire_all()
    stored = db.query(OutboxJob).filter(
        OutboxJob.operation_id == "verify:improvement:e2e"
    ).one()
    assert processed == 1
    assert stored.status == "completed"
    assert stored.retry_count == 0
    assert fake.calls == 1
    assert db.query(SelfDevRequest).filter(
        SelfDevRequest.status == CANDIDATE_STATUS_PENDING
    ).count() == 1


def test_handler_skips_llm_when_no_evidence(db, tmp_path) -> None:
    fake = ScriptedLLM(json.dumps({"candidates": []}, ensure_ascii=False))
    discovery = _discovery(fake, tmp_path)

    result = handle_improvement_discovery(_claimed(), discovery=discovery)

    assert result.outcome == HandlerOutcome.COMPLETED
    assert fake.calls == 0
    assert "no_evidence" in result.reason


def test_handler_creates_candidate_end_to_end(db, tmp_path) -> None:
    ids = _seed_failed_tasks(db, count=3)
    fake = ScriptedLLM(json.dumps(
        {"candidates": [_candidate(ids, occurrences=3)]}, ensure_ascii=False,
    ))
    discovery = _discovery(fake, tmp_path)

    result = handle_improvement_discovery(_claimed(), discovery=discovery)

    assert result.outcome == HandlerOutcome.COMPLETED
    assert fake.calls == 1
    rows = db.query(SelfDevRequest).filter(SelfDevRequest.status == CANDIDATE_STATUS_PENDING).all()
    assert len(rows) == 1
    payload = rows[0].plan["candidate"]
    assert payload["evidence_ids"] == ids
    assert rows[0].plan["evidence_groups"][0]["occurrences"] == 3
    notifications = (
        db.query(Event)
        .filter(Event.event_type == "notification_created")
        .all()
    )
    assert len(notifications) == 1
    assert notifications[0].payload["improvement_candidate_id"] == rows[0].id
    runs = db.query(Event).filter(Event.event_type == DISCOVERY_EVENT_TYPE).all()
    assert len(runs) == 1
    assert runs[0].payload["advanced"] is True


def test_candidate_survives_new_session(db, tmp_path) -> None:
    from aiive.db.base import SessionLocal

    ids = _seed_failed_tasks(db)
    fake = ScriptedLLM(json.dumps({"candidates": [_candidate(ids)]}, ensure_ascii=False))
    discovery = _discovery(fake, tmp_path)
    handle_improvement_discovery(_claimed(), discovery=discovery)

    other = SessionLocal()
    try:
        rows = other.query(SelfDevRequest).filter(
            SelfDevRequest.status == CANDIDATE_STATUS_PENDING
        ).all()
        assert len(rows) == 1
        assert rows[0].plan["candidate"]["evidence_ids"] == ids
        # 新会话也能读到进度水位，说明游标跨进程保留。
        assert other.query(Event).filter(Event.event_type == DISCOVERY_EVENT_TYPE).count() == 1
    finally:
        other.close()


def test_second_handler_run_without_new_evidence_skips_llm(db, tmp_path) -> None:
    ids = _seed_failed_tasks(db)
    fake = ScriptedLLM(json.dumps({"candidates": [_candidate(ids)]}, ensure_ascii=False))
    discovery = _discovery(fake, tmp_path)

    first = handle_improvement_discovery(_claimed(), discovery=discovery)
    second = handle_improvement_discovery(_claimed("trace-2"), discovery=discovery)

    assert RUN_STATUS_COMPLETED in first.reason
    assert RUN_STATUS_NO_EVIDENCE in second.reason
    assert fake.calls == 1
    assert db.query(SelfDevRequest).filter(
        SelfDevRequest.status == CANDIDATE_STATUS_PENDING
    ).count() == 1


def test_accept_reuses_repair_flow_without_promotion(db, tmp_path) -> None:
    ids = _seed_failed_tasks(db)
    fake = ScriptedLLM(json.dumps({"candidates": [_candidate(ids)]}, ensure_ascii=False))
    discovery = _discovery(fake, tmp_path)
    handle_improvement_discovery(_claimed(), discovery=discovery)
    candidate = db.query(SelfDevRequest).filter(
        SelfDevRequest.status == CANDIDATE_STATUS_PENDING
    ).one()

    calls: list[str] = []

    def runner(goal: str, *, trace_id: str | None = None) -> dict:
        calls.append(goal)
        return {
            "run_id": "run-accept",
            "goal": goal,
            "trace_id": trace_id or "trace",
            "status": "tests_failed",
            "ok": False,
            "failure_reason": "targeted tests failed",
            "final_operations": [],
            "promotion": None,
        }

    result = discovery.accept(db, candidate.id, repair_runner=runner)

    assert result["status"] == CANDIDATE_STATUS_ACCEPTED
    assert len(calls) == 1
    assert result["repair"]["promotion"] is None
    stored = db.get(SelfDevRequest, candidate.id)
    assert stored.plan["decision"]["promote"] is False
    repair_rows = db.query(SelfDevRequest).filter(
        SelfDevRequest.status == "tests_failed"
    ).all()
    assert len(repair_rows) == 1
