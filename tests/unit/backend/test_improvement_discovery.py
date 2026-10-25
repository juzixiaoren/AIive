"""改进候选发现：无证据不调用模型、幂等、服务端校验、接受/拒绝（全部离线）。"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from aiive.api import routes_selfdev
from aiive.core.llm_client import LLMClientError
from aiive.db.models import AgentTask, Event, SelfDevRequest, Thread
from aiive.selfdev.improvement_discovery import (
    CANDIDATE_STATUS_ACCEPTED,
    CANDIDATE_STATUS_PENDING,
    CANDIDATE_STATUS_REJECTED,
    DISCOVERY_EVENT_TYPE,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_EMPTY_RESULT,
    RUN_STATUS_INVALID_OUTPUT,
    RUN_STATUS_LLM_FAILED,
    RUN_STATUS_NO_EVIDENCE,
    CandidateNotFound,
    CandidateStateError,
    ImprovementDiscovery,
    _default_repair_runner,
    dedupe_key_for,
)
from aiive.selfdev.improvement_evidence import EvidenceCollector


class ScriptedLLM:
    """按脚本返回固定内容，并记录调用次数与提示词。"""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict] = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return SimpleNamespace(content=self.content)


class BoomLLM:
    def __init__(self) -> None:
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        raise LLMClientError("llm down")


def _now() -> datetime:
    return datetime.now(UTC)


def _discovery(llm, tmp_path, **kwargs) -> ImprovementDiscovery:
    collector = EvidenceCollector(eval_dir=tmp_path / "eval_runs", **kwargs)
    return ImprovementDiscovery(llm, collector=collector)


def _seed_failures(db, count: int = 2) -> list[str]:
    """造 count 条同类失败任务，返回对应证据 ID。"""
    db.add(Thread(id="thread-sys", title="系统线程"))
    db.flush()
    ids: list[str] = []
    stamp = _now() - timedelta(hours=1)
    for _ in range(count):
        row = AgentTask(
            thread_id="thread-sys",
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
    return ids


def _candidate(evidence_ids: list[str], **overrides) -> dict:
    payload = {
        "title": "工具崩溃缺少错误隔离",
        "problem": "同一工具在同一任务类型下反复崩溃，且未隔离失败影响",
        "impact": "任务整体失败率上升",
        "evidence_ids": list(evidence_ids),
        "occurrences": 2,
        "possible_causes": [
            {"cause": "缺少参数校验", "confidence": "medium", "uncertain": True},
        ],
        "target": "在工具执行层增加参数校验与失败隔离",
        "acceptance_criteria": ["同一非法参数不再抛出未捕获异常"],
        "test_method": "新增单元测试覆盖非法参数",
        "scope": "tools 执行层",
        "risk": "medium",
        "confidence": "medium",
    }
    payload.update(overrides)
    return payload


def _response(candidates: list[dict]) -> str:
    return json.dumps({"candidates": candidates}, ensure_ascii=False)


def _events(db) -> list[Event]:
    return (
        db.query(Event)
        .filter(Event.event_type == DISCOVERY_EVENT_TYPE)
        .order_by(Event.created_at.asc())
        .all()
    )


def _candidates(db) -> list[SelfDevRequest]:
    return (
        db.query(SelfDevRequest)
        .filter(SelfDevRequest.status.in_(("candidate_pending", "candidate_accepted", "candidate_rejected")))
        .all()
    )


def test_no_evidence_skips_llm(db_session, tmp_path) -> None:
    fake = ScriptedLLM(_response([]))
    discovery = _discovery(fake, tmp_path)

    summary = discovery.run(db=db_session, now=_now())

    assert summary["status"] == RUN_STATUS_NO_EVIDENCE
    assert summary["llm_called"] is False
    assert fake.calls == []
    assert _candidates(db_session) == []
    events = _events(db_session)
    assert len(events) == 1
    assert events[0].payload["advanced"] is True


def test_second_run_without_new_evidence_skips_llm(db_session, tmp_path) -> None:
    ids = _seed_failures(db_session)
    fake = ScriptedLLM(_response([_candidate(ids)]))
    discovery = _discovery(fake, tmp_path)
    start = _now()

    first = discovery.run(db=db_session, now=start)
    second = discovery.run(db=db_session, now=start + timedelta(minutes=5))

    assert first["status"] == RUN_STATUS_COMPLETED
    assert second["status"] == RUN_STATUS_NO_EVIDENCE
    assert len(fake.calls) == 1
    assert len(_candidates(db_session)) == 1


def test_repeated_failures_produce_one_candidate_with_one_call(db_session, tmp_path) -> None:
    ids = _seed_failures(db_session, count=4)
    fake = ScriptedLLM(_response([_candidate(ids, occurrences=4)]))
    discovery = _discovery(fake, tmp_path)

    summary = discovery.run(db=db_session, now=_now())

    assert summary["status"] == RUN_STATUS_COMPLETED
    assert len(fake.calls) == 1
    assert summary["group_count"] == 1
    assert len(summary["candidate_ids"]) == 1
    rows = _candidates(db_session)
    assert len(rows) == 1
    payload = rows[0].plan["candidate"]
    assert payload["occurrences"] == 4
    assert payload["evidence_ids"] == ids
    assert "缺少参数校验" in payload["repair_goal"] or "参数校验" in payload["repair_goal"]


def test_prompt_contains_evidence_and_rules(db_session, tmp_path) -> None:
    ids = _seed_failures(db_session)
    fake = ScriptedLLM(_response([_candidate(ids)]))
    discovery = _discovery(fake, tmp_path)

    discovery.run(db=db_session, now=_now())

    prompt = fake.calls[0]["messages"][1]["content"]
    assert ids[0] in prompt
    assert "不得编造证据 ID" in prompt
    assert fake.calls[0]["kwargs"]["json_mode"] is True


def test_same_batch_is_idempotent(db_session, tmp_path) -> None:
    ids = _seed_failures(db_session)
    fake = ScriptedLLM(_response([_candidate(ids)]))
    discovery = _discovery(fake, tmp_path)
    start = _now()

    discovery.run(db=db_session, now=start)
    # 抹掉进度记录，强制同一批证据再处理一次（模拟重复扫描）。
    for event in _events(db_session):
        db_session.delete(event)
    db_session.flush()

    second = discovery.run(db=db_session, now=start + timedelta(minutes=1))

    assert second["duplicate_candidate_ids"]
    assert len(_candidates(db_session)) == 1


def test_fabricated_evidence_is_rejected(db_session, tmp_path) -> None:
    _seed_failures(db_session)
    fake = ScriptedLLM(_response([_candidate(["agent_task:not-exist"])]))
    discovery = _discovery(fake, tmp_path)

    summary = discovery.run(db=db_session, now=_now())

    assert summary["candidate_ids"] == []
    assert summary["rejected"][0]["reason"] == "unknown_evidence_ids"
    assert _candidates(db_session) == []


def test_occurrences_exceeding_evidence_is_rejected(db_session, tmp_path) -> None:
    ids = _seed_failures(db_session, count=2)
    fake = ScriptedLLM(_response([_candidate(ids, occurrences=9)]))
    discovery = _discovery(fake, tmp_path)

    summary = discovery.run(db=db_session, now=_now())

    assert summary["rejected"][0]["reason"] == "occurrences_exceed_evidence"
    assert _candidates(db_session) == []


def test_missing_acceptance_criteria_is_rejected(db_session, tmp_path) -> None:
    ids = _seed_failures(db_session)
    fake = ScriptedLLM(_response([_candidate(ids, acceptance_criteria=[])]))
    discovery = _discovery(fake, tmp_path)

    summary = discovery.run(db=db_session, now=_now())

    assert summary["rejected"][0]["reason"] == "missing_acceptance_criteria"


def test_duplicate_group_in_one_response_keeps_one(db_session, tmp_path) -> None:
    ids = _seed_failures(db_session)
    fake = ScriptedLLM(_response([
        _candidate(ids),
        _candidate(ids, title="另一个说法但同一组证据"),
    ]))
    discovery = _discovery(fake, tmp_path)

    summary = discovery.run(db=db_session, now=_now())

    assert len(summary["candidate_ids"]) == 1
    assert summary["rejected"][0]["reason"] == "duplicate_group"


def test_invalid_json_creates_nothing_and_does_not_advance(db_session, tmp_path) -> None:
    _seed_failures(db_session)
    fake = ScriptedLLM("这不是 JSON")
    discovery = _discovery(fake, tmp_path)

    summary = discovery.run(db=db_session, now=_now())

    assert summary["status"] == RUN_STATUS_INVALID_OUTPUT
    assert _candidates(db_session) == []
    assert _events(db_session)[-1].payload["advanced"] is False


def test_llm_failure_does_not_advance_watermark(db_session, tmp_path) -> None:
    _seed_failures(db_session)
    boom = BoomLLM()
    discovery = _discovery(boom, tmp_path)

    summary = discovery.run(db=db_session, now=_now())

    assert summary["status"] == RUN_STATUS_LLM_FAILED
    assert boom.calls == 1
    assert _events(db_session)[-1].payload["advanced"] is False


def test_empty_candidate_list_is_allowed(db_session, tmp_path) -> None:
    _seed_failures(db_session)
    fake = ScriptedLLM(_response([]))
    discovery = _discovery(fake, tmp_path)

    summary = discovery.run(db=db_session, now=_now())

    assert summary["status"] == RUN_STATUS_EMPTY_RESULT
    assert summary["llm_called"] is True
    assert _candidates(db_session) == []


def test_candidate_notification_is_created(db_session, tmp_path) -> None:
    ids = _seed_failures(db_session)
    fake = ScriptedLLM(_response([_candidate(ids)]))
    discovery = _discovery(fake, tmp_path)

    summary = discovery.run(db=db_session, now=_now())

    candidate_id = summary["candidate_ids"][0]
    events = (
        db_session.query(Event)
        .filter(Event.event_type == "notification_created")
        .all()
    )
    assert len(events) == 1
    assert events[0].payload["category"] == "selfdev_improvement"
    assert events[0].payload["improvement_candidate_id"] == candidate_id
    assert events[0].payload["status"] == "pending"


def test_list_and_get_candidate(db_session, tmp_path) -> None:
    ids = _seed_failures(db_session)
    fake = ScriptedLLM(_response([_candidate(ids)]))
    discovery = _discovery(fake, tmp_path)
    summary = discovery.run(db=db_session, now=_now())
    candidate_id = summary["candidate_ids"][0]

    listed = ImprovementDiscovery.list_candidates(db_session)
    detail = ImprovementDiscovery.get_candidate(db_session, candidate_id)

    assert [item["candidate_id"] for item in listed] == [candidate_id]
    assert detail["status"] == CANDIDATE_STATUS_PENDING
    assert detail["evidence_groups"][0]["items"][0]["evidence_id"] == ids[0]
    assert detail["acceptance_criteria"]


def test_accept_runs_repair_and_records_decision(db_session, tmp_path) -> None:
    ids = _seed_failures(db_session)
    fake = ScriptedLLM(_response([_candidate(ids)]))
    discovery = _discovery(fake, tmp_path)
    candidate_id = discovery.run(db=db_session, now=_now())["candidate_ids"][0]

    captured: dict = {}

    def runner(goal: str, *, trace_id: str | None = None) -> dict:
        captured["goal"] = goal
        return {
            "run_id": "run-1",
            "goal": goal,
            "trace_id": trace_id or "trace-1",
            "status": "gate_failed",
            "ok": False,
            "failure_reason": "replay eval failed",
            "final_operations": [],
            "promotion": None,
        }

    result = discovery.accept(db_session, candidate_id, repair_runner=runner)

    assert result["status"] == CANDIDATE_STATUS_ACCEPTED
    assert result["repair"]["status"] == "gate_failed"
    assert "参数校验" in captured["goal"]
    row = db_session.get(SelfDevRequest, candidate_id)
    assert row.status == CANDIDATE_STATUS_ACCEPTED
    assert row.plan["decision"]["promote"] is False
    repair_rows = (
        db_session.query(SelfDevRequest)
        .filter(SelfDevRequest.status == "gate_failed")
        .all()
    )
    assert len(repair_rows) == 1
    assert repair_rows[0].plan["repair"]["run_id"] == "run-1"


def test_accept_twice_is_rejected_by_state(db_session, tmp_path) -> None:
    ids = _seed_failures(db_session)
    fake = ScriptedLLM(_response([_candidate(ids)]))
    discovery = _discovery(fake, tmp_path)
    candidate_id = discovery.run(db=db_session, now=_now())["candidate_ids"][0]

    discovery.accept(db_session, candidate_id, repair_runner=lambda goal, **_: {"run_id": "r"})
    with pytest.raises(CandidateStateError):
        discovery.accept(db_session, candidate_id, repair_runner=lambda goal, **_: {"run_id": "r2"})


def test_reject_candidate(db_session, tmp_path) -> None:
    ids = _seed_failures(db_session)
    fake = ScriptedLLM(_response([_candidate(ids)]))
    discovery = _discovery(fake, tmp_path)
    candidate_id = discovery.run(db=db_session, now=_now())["candidate_ids"][0]

    result = ImprovementDiscovery.reject(db_session, candidate_id, reason="不紧急")

    assert result["status"] == CANDIDATE_STATUS_REJECTED
    row = db_session.get(SelfDevRequest, candidate_id)
    assert row.plan["decision"]["reason"] == "不紧急"
    with pytest.raises(CandidateStateError):
        ImprovementDiscovery.reject(db_session, candidate_id)


def test_repair_rows_are_not_listed_as_candidates(db_session, tmp_path) -> None:
    db_session.add(SelfDevRequest(
        trace_id=str(uuid.uuid4()), goal="旧修复", status="failed", plan={"repair": {}},
    ))
    db_session.flush()

    assert ImprovementDiscovery.list_candidates(db_session) == []
    with pytest.raises(CandidateNotFound):
        ImprovementDiscovery.get_candidate(db_session, "not-a-candidate")


def test_default_repair_runner_never_promotes(monkeypatch) -> None:
    captured: dict = {}

    class FakeLoop:
        def __init__(self, *args, **kwargs) -> None:
            captured["init"] = kwargs

        def run(self, goal, **kwargs):
            captured["run"] = {"goal": goal, **kwargs}
            return {"run_id": "r"}

    monkeypatch.setattr("aiive.selfdev.repair_loop.SelfDevRepairLoop", FakeLoop)

    _default_repair_runner("目标", trace_id="t")

    assert captured["run"]["promote"] is False
    assert captured["run"]["run_gate"] is True


def test_accept_endpoint_requires_trusted_core_admin() -> None:
    route = next(
        item for item in routes_selfdev.router.routes
        if getattr(item, "path", "") == "/api/selfdev/candidates/{candidate_id}/accept"
    )
    names = [
        getattr(dependency.call, "__name__", "")
        for dependency in route.dependant.dependencies
    ]
    assert "require_trusted_core_admin" in names


def test_cursor_ignores_failed_run_events(db_session, tmp_path) -> None:
    fake = ScriptedLLM(_response([]))
    discovery = _discovery(fake, tmp_path)
    good = _now() - timedelta(hours=2)
    db_session.add(Thread(id="system", title="系统"))
    db_session.add(Event(
        trace_id="t1", thread_id="system", event_type=DISCOVERY_EVENT_TYPE,
        payload={"watermark": good.isoformat(), "advanced": True},
        created_at=good,
    ))
    db_session.add(Event(
        trace_id="t2", thread_id="system", event_type=DISCOVERY_EVENT_TYPE,
        payload={"watermark": None, "advanced": False},
        created_at=_now(),
    ))
    db_session.flush()

    assert discovery._load_cursor(db_session) == good


def test_dedupe_key_is_stable_and_order_independent() -> None:
    left = dedupe_key_for(["b", "a"], "同一个问题")
    right = dedupe_key_for(["a", "b"], " 同一个问题 ")
    assert left == right
