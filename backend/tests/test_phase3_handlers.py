"""Phase 3: Outbox Handlers 与约束测试（测试矩阵 2/16/61/62/63/67/73/75）。

端到端密封流程：begin_segment_sealing → handle_segment_sealing（伪 LLM）→
Segment sealed + SegmentSummary + 条件性 epoch job 入队。
"""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone
from sqlalchemy.exc import IntegrityError

from aiive.db.models import (
    CheckpointRun,
    CompactionInput,
    CompactionRun,
    Epoch,
    EpochCompactionInput,
    OutboxJob,
    Segment,
    SegmentSummary,
    TurnRecord,
)
from aiive.db import retention_models  # 注册 retention_cleanup_runs 表结构供测试 fixture 建表
from aiive.runtime.epoch_manager import EpochManager
from aiive.worker.handler_registry import HandlerRegistry
from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome
from aiive.worker.outbox_heartbeat import ActiveClaimRegistry
from aiive.worker.outbox_worker import OutboxWorker
from backend.tests._util import (
    FakeLLM,
    add_event,
    add_turn,
    claim_job,
    new_epoch,
    new_segment,
    new_thread,
    new_ws,
)


# ───────────────────────────────────────────────────────────
# 端到端密封集成（覆盖 2 / 16 / 62）
# ───────────────────────────────────────────────────────────

def _setup_sealable_segment(db):
    thread = new_thread(db)
    epoch = new_epoch(db, thread.id)
    seg = new_segment(db, epoch.id, thread.id)
    t = add_turn(db, thread.id, seg.id, 1, status="completed")
    add_event(db, thread.id, t.turn_id, "user_message", {"content": "hi"}, 0)
    add_event(db, thread.id, t.turn_id, "tool_call",
              {"name": "search", "tool_call_id": "tc1"}, 1)
    add_event(db, thread.id, t.turn_id, "tool_result",
              {"name": "search", "tool_call_id": "tc1", "status": "completed"}, 2)
    ws = new_ws(db, thread.id,
                verified_tool_states=[{"tool_call_id": "tc1", "tool_name": "search",
                                        "state": {"ok": True}}])
    db.commit()
    return thread, epoch, seg, ws


def _find_segment_sealing_job(db, seg_id):
    jobs = db.query(OutboxJob).filter(OutboxJob.job_type == "segment_sealing").all()
    return [j for j in jobs if j.payload.get("segment_id") == seg_id][-1]


def _run_segment_sealing(db, seg, monkeypatch):
    from aiive.worker import outbox_handlers as h
    monkeypatch.setattr(h, "_get_llm_client",
                        lambda: FakeLLM({
                            "goal": "g", "outcome": "o", "decisions": [], "entities": [],
                            "tool_result_summaries": [{"item_ref": "tool_1", "result_summary": "ok"}],
                            "failure_explanations": [],
                        }))
    import aiive.runtime.compaction as _compaction
    monkeypatch.setattr(_compaction, "count_summary_tokens", lambda *a, **kw: 5)

    job = _find_segment_sealing_job(db, seg.id)
    claimed = claim_job(db, job)
    result = h.handle_segment_sealing(claimed)
    db.expire_all()
    return result


def test_02_16_segment_sealing_end_to_end(db, monkeypatch):
    """begin_segment_sealing 冻结范围+CompactionInput+新 Segment；Handler 原子提交 Summary+sealed。"""
    thread, epoch, seg, ws = _setup_sealable_segment(db)
    mgr = EpochManager()
    res = mgr.begin_segment_sealing(db, thread.id, seg.id)
    db.commit()
    # 2：冻结生成 CompactionInput 与新 successor Segment
    assert res.compaction_input_id is not None
    assert res.successor_segment_id is not None
    ci = db.get(CompactionInput, res.compaction_input_id)
    assert ci.segment_id == seg.id

    result = _run_segment_sealing(db, seg, monkeypatch)
    # 16：Summary + Segment sealed 原子提交
    assert result.outcome == HandlerOutcome.COMPLETED
    sealed = db.get(Segment, seg.id)
    assert sealed.status == "sealed"
    assert sealed.summary_id is not None
    summary = db.get(SegmentSummary, sealed.summary_id)
    # Phase 5：segment sealed 触发 refresh（op_id 含 source_version，与文档 O 节一致）
    refresh_job = db.query(OutboxJob).filter(
        OutboxJob.operation_id
        == f"retrieval_refresh:segment_summary:{summary.id}:{summary.summary_version}"
    ).one()
    assert refresh_job.job_type == "retrieval_index_refresh"
    assert summary.source_turn_ids == [
        t.turn_id for t in db.query(TurnRecord).filter_by(segment_id=seg.id).all()
    ]
    assert summary.source_hash == sealed.source_hash


def test_62_phase_c_enqueues_epoch_job_atomically(db, monkeypatch):
    """Phase C 在同一事务内事务性写入 rollover Job（提交后可见，证明入队早于崩溃点）。"""
    from aiive.memory.recall_config import RecallConfig as _RC
    from aiive.worker import outbox_handlers as h
    # dataclass 字段默认值不受类属性覆盖影响，改为降低阈值注入 _enqueue_epoch_jobs
    _orig_enqueue = h._enqueue_epoch_jobs

    def _patched_enqueue(db_c, sealed_segment, _cfg):
        return _orig_enqueue(db_c, sealed_segment, _RC(max_segments_per_epoch=1))

    monkeypatch.setattr(h, "_enqueue_epoch_jobs", _patched_enqueue)

    thread, epoch, seg, ws = _setup_sealable_segment(db)
    mgr = EpochManager()
    mgr.begin_segment_sealing(db, thread.id, seg.id)
    db.commit()

    _run_segment_sealing(db, seg, monkeypatch)
    db.expire_all()

    rollover_jobs = db.query(OutboxJob).filter(
        OutboxJob.job_type == "epoch_rollover").all()
    assert len(rollover_jobs) == 1
    assert rollover_jobs[0].payload.get("thread_id") == thread.id


# ───────────────────────────────────────────────────────────
# 约束测试（61 / 75）
# ───────────────────────────────────────────────────────────

def test_61_fk_and_not_null_constraints(db):
    """CompactionRun/CheckpointRun 的 FK 与 NOT NULL 约束生效。"""
    thread = new_thread(db)
    epoch = new_epoch(db, thread.id)
    seg = new_segment(db, epoch.id, thread.id)
    ci = CompactionInput(segment_id=seg.id, start_turn_sequence=0,
                         end_turn_sequence=0, working_state_version=1, source_hash="h")
    db.add(ci)
    # outbox_job_id 为外键，需先存在对应 OutboxJob 以隔离 FK 与 UNIQUE/NOT NULL 验证
    ob_x = OutboxJob(id="job-x", operation_id="op-x", job_type="x", status="pending", payload={}, max_retries=3)
    ob_y = OutboxJob(id="job-y", operation_id="op-y", job_type="y", status="pending", payload={}, max_retries=3)
    db.add_all([ob_x, ob_y])
    db.commit()

    # FK 违例：segment_id 指向不存在的 Segment
    with pytest.raises(IntegrityError):
        db.add(CompactionRun(segment_id="nope", compaction_input_id=ci.id,
                             outbox_job_id="job-x", source_hash="h"))
        db.flush()

    # NOT NULL 违例：outbox_job_id 为空
    db.rollback()
    with pytest.raises(IntegrityError):
        db.add(CompactionRun(segment_id=seg.id, compaction_input_id=ci.id,
                             outbox_job_id=None, source_hash="h"))
        db.flush()

    # CheckpointRun：epoch_id NOT NULL 违例
    db.rollback()
    eci_obj = EpochCompactionInput(epoch_id=epoch.id, boundary_turn_sequence=0,
                  working_state_version=1, source_segment_ids=[],
                  source_hashes=[], snapshot_hash="hh", checkpoint_version=1)
    db.add(eci_obj)
    db.commit()
    with pytest.raises(IntegrityError):
        db.add(CheckpointRun(epoch_id=None, epoch_compaction_input_id=eci_obj.id,
                             outbox_job_id="job-y", boundary_hash="h"))
        db.flush()


def test_75_unique_outbox_job_id_constraints(db):
    """UNIQUE(compaction_runs.outbox_job_id) / UNIQUE(checkpoint_runs.outbox_job_id) 生效。"""
    thread = new_thread(db)
    epoch = new_epoch(db, thread.id)
    seg = new_segment(db, epoch.id, thread.id)
    ci = CompactionInput(segment_id=seg.id, start_turn_sequence=0,
                         end_turn_sequence=0, working_state_version=1, source_hash="h")
    ob = OutboxJob(id="same-job", operation_id="op-same-job", job_type="x", status="pending", payload={}, max_retries=3)
    ob_cp = OutboxJob(id="same-cp-job", operation_id="op-same-cp", job_type="y", status="pending", payload={}, max_retries=3)
    db.add_all([ci, ob, ob_cp])
    db.commit()

    db.add(CompactionRun(segment_id=seg.id, compaction_input_id=ci.id,
                         outbox_job_id="same-job", source_hash="h"))
    db.commit()
    with pytest.raises(IntegrityError):
        db.add(CompactionRun(segment_id=seg.id, compaction_input_id=ci.id,
                             outbox_job_id="same-job", source_hash="h2"))
        db.flush()
    db.rollback()

    eci_obj = EpochCompactionInput(epoch_id=epoch.id, boundary_turn_sequence=0,
                  working_state_version=1, source_segment_ids=[],
                  source_hashes=[], snapshot_hash="hh", checkpoint_version=1)
    db.add(eci_obj)
    db.commit()
    db.add(CheckpointRun(epoch_id=epoch.id, epoch_compaction_input_id=eci_obj.id,
                         outbox_job_id="same-cp-job", boundary_hash="h"))
    db.commit()
    with pytest.raises(IntegrityError):
        db.add(CheckpointRun(epoch_id=epoch.id, epoch_compaction_input_id=eci_obj.id,
                             outbox_job_id="same-cp-job", boundary_hash="h2"))
        db.flush()
    db.rollback()


# ───────────────────────────────────────────────────────────
# 一 Job 一 Run 复用（67）
# ───────────────────────────────────────────────────────────

def test_67_same_job_reuses_same_run(db):
    """同一 OutboxJob 重试与 deadletter 后人工重放均复用同一 Run（attempt_count+1）。"""
    from aiive.worker.outbox_handlers import _resolve_phase3_run

    thread = new_thread(db)
    epoch = new_epoch(db, thread.id)
    seg = new_segment(db, epoch.id, thread.id)
    ci = CompactionInput(segment_id=seg.id, start_turn_sequence=0,
                         end_turn_sequence=0, working_state_version=1, source_hash="h")
    ob = OutboxJob(id="job-67", operation_id="op-67", job_type="x", status="pending", payload={}, max_retries=3)
    db.add_all([ci, ob])
    db.commit()

    # 首次获取
    dec1, run_id = _resolve_phase3_run(
        db, CompactionRun, "job-67",
        {"segment_id": seg.id, "compaction_input_id": ci.id,
         "source_hash": "h", "summary_version": 1}, "token-1")
    db.commit()
    assert dec1 == "acquired"
    run = db.get(CompactionRun, run_id)
    assert run.attempt_count == 1

    # 重试（同一 job，新 token）→ 复用同一 Run，attempt_count+1
    dec2, run_id2 = _resolve_phase3_run(
        db, CompactionRun, "job-67",
        {"segment_id": seg.id, "compaction_input_id": ci.id,
         "source_hash": "h", "summary_version": 1}, "token-2")
    db.commit()
    assert dec2 == "takeover"
    assert run_id2 == run_id
    assert db.get(CompactionRun, run_id).attempt_count == 2

    # 标记为 deadletter 后重放 → 复用同一 Run，返回 deadletter（不新建）
    db.get(CompactionRun, run_id).status = "deadletter"
    db.commit()
    dec3, run_id3 = _resolve_phase3_run(
        db, CompactionRun, "job-67",
        {"segment_id": seg.id, "compaction_input_id": ci.id,
         "source_hash": "h", "summary_version": 1}, "token-3")
    db.commit()
    assert dec3 == "deadletter"
    assert run_id3 == run_id


# ───────────────────────────────────────────────────────────
# 幂等入队（63）
# ───────────────────────────────────────────────────────────

def test_63_reconciler_reenqueue_idempotent(db):
    """reconciler 补发缺 Job 时 operation_id 幂等不重复。"""
    from aiive.worker.outbox_handlers import _insert_job_do_nothing

    _insert_job_do_nothing(db, "epoch_rollover", "op-63", {"thread_id": "t"})
    db.commit()
    _insert_job_do_nothing(db, "epoch_rollover", "op-63", {"thread_id": "t"})
    db.commit()

    count = db.query(OutboxJob).filter(
        OutboxJob.operation_id == "op-63").count()
    assert count == 1


# ───────────────────────────────────────────────────────────
# 注册与幂等（73）
# ───────────────────────────────────────────────────────────

def test_73_epoch_rollover_registered_and_idempotent(db):
    """epoch_rollover Handler 注册 v1，对已无 active Epoch 的线程幂等返回成功。"""
    from aiive.worker import outbox_handlers as h

    registry = HandlerRegistry()
    h.register_all(registry)
    assert registry.is_schema_supported("epoch_rollover", 1) is True
    assert registry.get("epoch_rollover") is h.handle_epoch_rollover

    # 仅有已 sealed 的 Epoch（无 active）→ 幂等成功
    thread = new_thread(db)
    new_epoch(db, thread.id, epoch_no=1, status="sealed")
    db.commit()

    job = OutboxJob(job_type="epoch_rollover", operation_id="op-73", status="pending",
                   payload={"schema_version": 1, "thread_id": thread.id},
                   max_retries=3)
    db.add(job)
    db.commit()
    claimed = claim_job(db, job)
    result = h.handle_epoch_rollover(claimed)
    assert result.outcome == HandlerOutcome.COMPLETED


# ───────────────────────────────────────────────────────────
# deadletter 原子契约（第 4 项：F 节 Run/OutboxJob 同终态）
# ───────────────────────────────────────────────────────────

def test_phase3_run_deadlettered_with_outbox_job(db):
    """Job 被 deadletter 时，关联的 CompactionRun 须在同一事务内原子置 deadletter。"""
    thread = new_thread(db)
    epoch = new_epoch(db, thread.id)
    seg = new_segment(db, epoch.id, thread.id)
    ci = CompactionInput(segment_id=seg.id, start_turn_sequence=0,
                         end_turn_sequence=0, working_state_version=1, source_hash="h")
    db.add(ci)
    db.commit()

    job = OutboxJob(id="job-dl", operation_id="op-dl", job_type="segment_sealing",
                    status="running", payload={"segment_id": seg.id}, max_retries=3,
                    claim_token="tok-dl", locked_by="worker-test",
                    lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=600))
    db.add(ci)
    db.add(job)
    db.commit()
    # 模拟 _mark_phase3_run_failed 早前置入的 failed 终态
    run = CompactionRun(segment_id=seg.id, compaction_input_id=ci.id,
                        outbox_job_id="job-dl", source_hash="h", status="failed",
                        execution_token=None)
    db.add(run)
    db.commit()

    claimed = ClaimedJob(id="job-dl", job_type="segment_sealing", payload={},
                         trace_id=None, retry_count=0, max_retries=3,
                         claim_token="tok-dl", schema_version=1, worker_id="worker-test")

    worker = OutboxWorker(worker_id="w-dl", registry=HandlerRegistry(),
                          claims=ActiveClaimRegistry())
    worker._deadletter_job_and_ingestion_run(claimed, "", error="boom",
                                             terminal_reason="t")

    db.expire_all()
    job2 = db.query(OutboxJob).filter(OutboxJob.id == "job-dl").first()
    run2 = db.query(CompactionRun).filter(
        CompactionRun.outbox_job_id == "job-dl").first()
    assert job2.status == "deadletter"
    assert run2.status == "deadletter"


def test_retrieval_index_rebuild_run_deadlettered_with_outbox_job(db):
    """retrieval_index_rebuild Job 被 deadletter 时，关联的 RetrievalIndexRun 须原子置 deadletter。"""
    from aiive.db.models import RetrievalIndexRun

    job = OutboxJob(id="job-ri", operation_id="retrieval_rebuild:op",
                    job_type="retrieval_index_rebuild", status="running",
                    payload={}, max_retries=3, claim_token="tok-ri",
                    locked_by="worker-test",
                    lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=600))
    db.add(job)
    db.commit()
    run = RetrievalIndexRun(
        outbox_job_id="job-ri", operation_id="retrieval_rebuild:op",
        status="running", index_version=1,
        batch_cursor={"source_type": "memory_record", "last_id": None},
        execution_token="tok-ri",
    )
    db.add(run)
    db.commit()

    claimed = ClaimedJob(id="job-ri", job_type="retrieval_index_rebuild", payload={},
                         trace_id=None, retry_count=3, max_retries=3,
                         claim_token="tok-ri", schema_version=1, worker_id="worker-test")

    worker = OutboxWorker(worker_id="w-ri", registry=HandlerRegistry(),
                          claims=ActiveClaimRegistry())
    worker._deadletter_job_and_ingestion_run(claimed, "", error="boom",
                                             terminal_reason="t")

    db.expire_all()
    job2 = db.query(OutboxJob).filter(OutboxJob.id == "job-ri").first()
    run2 = db.query(RetrievalIndexRun).filter(
        RetrievalIndexRun.outbox_job_id == "job-ri").first()
    assert job2.status == "deadletter"
    assert run2.status == "deadletter"


def test_retention_cleanup_run_deadlettered_with_outbox_job(db):
    """retention_cleanup Job 被 deadletter 时，关联的 RetentionCleanupRun 须原子置 deadletter。"""
    from aiive.db.retention_models import RetentionCleanupRun

    job = OutboxJob(id="job-rc", operation_id="retention:all:v1:bucket",
                    job_type="retention_cleanup", status="running",
                    payload={}, max_retries=3, claim_token="tok-rc",
                    locked_by="worker-test",
                    lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=600))
    db.add(job)
    db.commit()
    run = RetentionCleanupRun(
        outbox_job_id="job-rc", operation_id="retention:all:v1:bucket",
        policy_version=1, policy_snapshot={}, status="running",
        cutoff_at=datetime.now(timezone.utc) - timedelta(days=30),
        execution_token="tok-rc",
    )
    db.add(run)
    db.commit()

    claimed = ClaimedJob(id="job-rc", job_type="retention_cleanup", payload={},
                         trace_id=None, retry_count=3, max_retries=3,
                         claim_token="tok-rc", schema_version=1, worker_id="worker-test")

    worker = OutboxWorker(worker_id="w-rc", registry=HandlerRegistry(),
                          claims=ActiveClaimRegistry())
    worker._deadletter_job_and_ingestion_run(claimed, "", error="boom",
                                             terminal_reason="t")

    db.expire_all()
    job2 = db.query(OutboxJob).filter(OutboxJob.id == "job-rc").first()
    run2 = db.query(RetentionCleanupRun).filter(
        RetentionCleanupRun.outbox_job_id == "job-rc").first()
    assert job2.status == "deadletter"
    assert run2.status == "deadletter"
