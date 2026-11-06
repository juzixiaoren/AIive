"""测试基础设施：SQLite 测试会话分发器（供 conftest 与测试模块共用）。

背景：生产模块大多以 ``from aiive.db.base import SessionLocal`` 做模块级绑定，
monkeypatch 无法撤销这种 from-import 绑定。因此在 conftest 加载时（先于任何
aiive 业务模块 import）安装一个稳定的分发函数：运行期查当前测试会话，无测试
会话时回退真实 SessionLocal。

分层使用约定：
- 单元/流程测试：经 autouse fixture 把按用例隔离的 SQLite 会话注册进来
  （见 tests/unit/conftest.py）。该注册是零成本的（不建库、不建表），
  纯逻辑测试不会因 autouse 而付出数据库初始化开销；
  真正需要数据库的用例显式请求 ``db_session`` 才会建库。
- 本地集成测试：不注册，天然使用进程级共享库（Handler 内部新开会话也能读到
  同一物理库，这是集成 fixture 的设计前提）。
"""
from __future__ import annotations

import sys

import aiive.db.base as _db_base
from sqlalchemy.orm import Session

_real_session_local = _db_base.SessionLocal
_current_test_session: Session | None = None


def _dispatching_session_local() -> Session:
    if _current_test_session is not None:
        return _current_test_session
    return _real_session_local()


def install_session_dispatcher() -> None:
    """将 SessionLocal 指向分发函数，并修正已导入 aiive 模块的旧绑定。"""
    _db_base.SessionLocal = _dispatching_session_local
    for mod_name, mod in list(sys.modules.items()):
        if mod_name.startswith("aiive.") and mod is not None:
            if getattr(mod, "SessionLocal", None) is _real_session_local:
                mod.SessionLocal = _dispatching_session_local


def current_test_session() -> Session | None:
    """返回当前用例注册的测试会话（未注册时为 None）。"""
    return _current_test_session


def set_current_test_session(session: Session | None) -> None:
    """注册/注销当前测试会话（仅单元与流程测试使用）。"""
    global _current_test_session
    _current_test_session = session


def persist_running_job(
    db: Session,
    *,
    job_type: str,
    payload: dict,
    trace_id: str | None = None,
    job_id: str | None = None,
    worker_id: str = "test",
    operation_id: str | None = None,
    max_retries: int = 3,
    lease_seconds: int = 300,
):
    """以 running 状态持久化一条 OutboxJob 并返回对应的 ClaimedJob。

    生产 Worker 的 claim fencing 要求 Job 行存在、status=running、claim_token /
    locked_by 严格匹配且 lease 有效，因此直接调用 Handler 的用例必须落一条真
    实 Job，不能只构造内存 ClaimedJob。
    """
    import uuid as _uuid
    from datetime import datetime, timedelta, timezone

    from aiive.db.models import OutboxJob
    from aiive.worker.outbox_dto import ClaimedJob

    job = OutboxJob(
        id=job_id or str(_uuid.uuid4()),
        operation_id=operation_id or f"test:{_uuid.uuid4()}",
        job_type=job_type,
        status="running",
        payload=dict(payload),
        trace_id=trace_id,
        claim_token=str(_uuid.uuid4()),
        locked_by=worker_id,
        lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=lease_seconds),
        max_retries=max_retries,
    )
    db.add(job)
    db.commit()
    return ClaimedJob(
        id=job.id,
        job_type=job_type,
        payload=dict(payload),
        trace_id=trace_id,
        retry_count=0,
        max_retries=max_retries,
        claim_token=job.claim_token,
        schema_version=1,
        worker_id=worker_id,
    )


def claim_existing_job(
    db: Session, job_id: str, *, worker_id: str = "test", lease_seconds: int = 300,
) -> tuple[str, str]:
    """把已存在的 OutboxJob 置为 running 并返回 (claim_token, worker_id)。"""
    import uuid as _uuid
    from datetime import datetime, timedelta, timezone

    from aiive.db.models import OutboxJob

    job = db.query(OutboxJob).filter(OutboxJob.id == job_id).first()
    assert job is not None, f"OutboxJob 不存在: {job_id}"
    job.status = "running"
    job.claim_token = job.claim_token or str(_uuid.uuid4())
    job.locked_by = worker_id
    job.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
    db.commit()
    return job.claim_token, worker_id


def ensure_running_claim(
    claimed: "ClaimedJob", *, lease_seconds: int = 300, db: Session | None = None,
):
    """在测试库把该 ClaimedJob 对应的 OutboxJob 置为 running（claim fencing 前置）。

    直接调用 Handler 的用例必须满足生产 claim 契约：行存在、status=running、
    claim_token / locked_by 严格匹配、lease 未过期。
    """
    from datetime import datetime, timedelta, timezone

    from aiive.db.models import OutboxJob

    session = db or _current_test_session
    if session is None:
        return claimed
    db = session
    row = db.query(OutboxJob).filter(OutboxJob.id == claimed.id).first()
    if row is None:
        row = OutboxJob(
            id=claimed.id,
            operation_id=f"test:{claimed.id}",
            job_type=claimed.job_type,
            status="running",
            payload=dict(claimed.payload or {}),
            trace_id=claimed.trace_id,
            max_retries=claimed.max_retries,
        )
        db.add(row)
    row.status = "running"
    row.claim_token = claimed.claim_token or "test-claim"
    row.locked_by = claimed.worker_id or "test"
    row.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
    db.commit()
    return claimed