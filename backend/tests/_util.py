"""Phase 3 测试公共辅助：构造领域对象、领取 OutboxJob、伪 LLM。

所有辅助均使用 SQLite（由 conftest 设定）与共享引擎，Handler 内部会话可读取同一物理库。
"""
from __future__ import annotations

import json
import types
import uuid
from datetime import datetime, timedelta, timezone

from aiive.db.base import SessionLocal
from aiive.db.models import (
    CheckpointRun,
    CompactionInput,
    CompactionRun,
    Epoch,
    EpochCheckpoint,
    EpochCompactionInput,
    Event,
    OutboxJob,
    Segment,
    SegmentSummary,
    Thread,
    TurnRecord,
    WorkingState,
)
from aiive.runtime.epoch_manager import (
    EpochManager,
    allocate_turn_sequence,
    peek_next_turn_sequence,
)
from aiive.worker.outbox_dto import ClaimedJob


def new_thread(db, **kw) -> Thread:
    tid = kw.get("id") or str(uuid.uuid4())
    t = Thread(id=tid, title=kw.get("title", "thread"))
    db.add(t)
    db.flush()
    return t


def new_epoch(db, thread_id, epoch_no=1, status="active", start=1) -> Epoch:
    e = Epoch(thread_id=thread_id, epoch_no=epoch_no, status=status,
              start_turn_sequence=start)
    db.add(e)
    db.flush()
    return e


def new_segment(db, epoch_id, thread_id, segment_no=1, status="open", start=1) -> Segment:
    s = Segment(epoch_id=epoch_id, thread_id=thread_id, segment_no=segment_no,
                status=status, start_turn_sequence=start)
    db.add(s)
    db.flush()
    return s


def add_turn(db, thread_id, segment_id, turn_sequence, status="completed",
             turn_id=None, **kw) -> TurnRecord:
    tid = turn_id or str(uuid.uuid4())
    t = TurnRecord(thread_id=thread_id, turn_id=tid, turn_sequence=turn_sequence,
                   status=status, segment_id=segment_id, **kw)
    db.add(t)
    db.flush()
    return t


def add_event(db, thread_id, turn_id, event_type, payload, turn_event_index=0,
              trace_id="trace-1") -> Event:
    e = Event(thread_id=thread_id, turn_id=turn_id, event_type=event_type,
              payload=payload, turn_event_index=turn_event_index, trace_id=trace_id)
    db.add(e)
    db.flush()
    return e


def new_ws(db, thread_id, **fields) -> WorkingState:
    defaults = dict(
        thread_id=thread_id,
        current_objective=None,
        open_loops=[],
        active_constraints=[],
        pending_approvals=[],
        artifact_refs=[],
        verified_tool_states=[],
        uncommitted_side_effects=[],
        running_tool_state=[],
        version=1,
        epoch_id=None,
    )
    defaults.update(fields)
    ws = WorkingState(**defaults)
    db.add(ws)
    db.flush()
    return ws


def claim_job(db, job, worker_id="worker-test", lease_seconds=600, token=None) -> ClaimedJob:
    """将 OutboxJob 置为 running 并构造不可变 ClaimedJob DTO。"""
    tok = token or str(uuid.uuid4())
    job.claim_token = tok
    job.locked_by = worker_id
    job.status = "running"
    job.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
    db.flush()
    db.commit()
    return ClaimedJob(
        id=job.id,
        job_type=job.job_type,
        payload=job.payload,
        trace_id=job.trace_id,
        retry_count=job.retry_count,
        max_retries=job.max_retries,
        claim_token=tok,
        schema_version=job.schema_version,
        worker_id=worker_id,
    )


class FakeLLM:
    """返回固定结构化 JSON 的伪 LLM（仅校验 schema 与 item_ref 回填路径用）。"""

    def __init__(self, output: dict):
        self.output = output
        self.calls = 0

    def chat(self, messages, model=None, temperature=0):
        self.calls += 1
        return types.SimpleNamespace(content=json.dumps(self.output))
