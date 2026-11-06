"""Outbox 执行授权：与 Forget Phase A 排他锁互斥的 session-level 共享锁。

- PostgreSQL：独立连接上 `pg_advisory_lock_shared(FORGET_LOCK_NAMESPACE, FORGET_LOCK_KEY)`；
  与 `execute_phase_a_shield()` 的事务级 `pg_advisory_xact_lock` 排他锁互斥
  （同一 advisory lock 空间，已用真实 PG 验证）。
- 锁覆盖 LLM 调用与后续持久化提交；所有路径显式 unlock。
- SQLite：no-op，仅用于逻辑测试；并发正确性以 PostgreSQL 为准。
"""
from __future__ import annotations

import functools
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from datetime import datetime, timezone
from collections.abc import Callable, Generator
from typing import Any

from sqlalchemy import text as sa_text

from aiive.forget.concurrency import FORGET_LOCK_KEY, FORGET_LOCK_NAMESPACE
from aiive.worker.outbox_dto import (
    ClaimedJob,
    HandlerOutcome,
    HandlerResult,
)
from aiive.worker.outbox_payload_contract import is_redacted_payload

logger = logging.getLogger(__name__)
_ACTIVE_CONNECTION: ContextVar[Any | None] = ContextVar(
    "aiive_outbox_authorization_connection", default=None,
)


def get_engine() -> Any:
    from aiive.db.base import engine

    return engine


# 标记「该工厂每次都新建 Session」：只有自建 Session 才能 close，
# 否则会关闭测试分发器返回的共享会话（把调用方的 ORM 对象置为 detached）。
_OWNS_SESSION_ATTR = "_aiive_creates_new_session"


def _mark_owns_session(factory: Callable[[], Any]) -> Callable[[], Any]:
    setattr(factory, _OWNS_SESSION_ATTR, True)
    return factory


def _owns_session(factory: Callable[[], Any]) -> bool:
    flag = getattr(factory, _OWNS_SESSION_ATTR, None)
    if flag is not None:
        return bool(flag)
    from sqlalchemy.orm import sessionmaker

    return isinstance(factory, sessionmaker)


def get_session_factory() -> Callable[[], Any]:
    active = _ACTIVE_CONNECTION.get()
    if active is not None:
        from sqlalchemy.orm import Session
        return _mark_owns_session(
            lambda: Session(bind=active, expire_on_commit=False)
        )
    from aiive.db.base import SessionLocal
    return SessionLocal


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


@contextmanager
def shared_execution_authorization() -> Generator[None, None, None]:
    """持共享执行授权直到 LLM 调用与写入提交结束。"""
    engine = get_engine()
    if getattr(engine.dialect, "name", "") != "postgresql":
        yield
        return
    connection = engine.connect()
    params = {"ns": FORGET_LOCK_NAMESPACE, "key": FORGET_LOCK_KEY}
    token = _ACTIVE_CONNECTION.set(connection)
    try:
        connection.execute(
            sa_text("SELECT pg_advisory_lock_shared(:ns, :key)"), params,
        )
        connection.commit()  # session-level lock 不随事务结束释放
        try:
            yield
        finally:
            try:
                connection.execute(
                    sa_text("SELECT pg_advisory_unlock_shared(:ns, :key)"), params,
                )
                connection.commit()
            except Exception:
                logger.exception("释放共享执行授权失败")
                connection.rollback()
    finally:
        _ACTIVE_CONNECTION.reset(token)
        connection.close()


def authorized_session_factory(fallback: Callable[[], Any]) -> Callable[[], Any]:
    """有活跃授权连接时，返回绑定该连接的 Session 工厂；否则回退调用方工厂。"""
    active = _ACTIVE_CONNECTION.get()
    if active is None:
        return fallback
    from sqlalchemy.orm import Session
    return _mark_owns_session(
        lambda: Session(bind=active, expire_on_commit=False)
    )


def refresh_claimed_job(
    claimed: ClaimedJob,
    session_factory: Callable[[], Any] | None = None,
    *,
    require_persisted: bool = True,
) -> tuple[ClaimedJob | None, str]:
    """重新读取 OutboxJob，拒绝继续使用领取时的旧 payload 副本。

    require_persisted=True（内容会进入 LLM 的路径）要求 Job 行必须存在：行缺失
    说明已被清理，必须终止。其余 Handler 的 Job 行缺失只意味着「非持久化直接
    调用」，此时仍执行脱敏校验后继续，避免把单元级调用误判为 claim 丢失。
    """
    # 不能信任领取时的旧 payload 副本：先重新读取 DB，再判断 Forget 标记。
    factory = session_factory or get_session_factory()
    db = factory()
    owns_session = _owns_session(factory)
    try:
        from aiive.db.models import OutboxJob

        job = db.query(OutboxJob).filter(OutboxJob.id == claimed.id).first()
        if job is None:
            if is_redacted_payload(claimed.payload):
                return None, "forget_content_redacted"
            if require_persisted:
                return None, "outbox_job_not_found"
            logger.warning(
                "OutboxJob %s 不在库中，使用调用方提供的 claim 副本继续（非 Worker 领取路径）",
                claimed.id,
            )
            return claimed, ""
        if job.status in ("completed", "deadletter", "failed", "cancelled"):
            return None, f"outbox_job_terminal:{job.status}"
        if job.status not in ("pending", "running"):
            return None, f"outbox_job_invalid_status:{job.status}"
        if job.claim_token and job.claim_token != claimed.claim_token:
            return None, "claim_lost"
        if job.locked_by and job.locked_by != claimed.worker_id:
            return None, "claim_lost"
        lease = _as_utc(job.lease_expires_at)
        if lease is not None and lease <= datetime.now(timezone.utc):
            return None, "lease_expired"
        if is_redacted_payload(job.payload):
            return None, "forget_content_redacted"
        fresh = replace(
            claimed,
            payload=job.payload or {},
            schema_version=int(job.schema_version or claimed.schema_version),
            trace_id=job.trace_id or claimed.trace_id,
        )
        return fresh, ""
    finally:
        if owns_session:
            db.close()


def with_execution_authorization(fn: Callable[..., HandlerResult]) -> Callable[..., HandlerResult]:
    """Handler 装饰器：整个外部调用/写入链持共享执行授权。"""
    @functools.wraps(fn)
    def wrapper(claimed: ClaimedJob, *args: Any, **kwargs: Any) -> HandlerResult:
        with shared_execution_authorization():
            return fn(claimed, *args, **kwargs)
    return wrapper


def guard_handler_payload(
    claimed: ClaimedJob,
    session_factory: Callable[[], Any] | None = None,
    *,
    require_persisted: bool = True,
) -> tuple[ClaimedJob | None, HandlerResult | None]:
    """LLM / 外部副作用前的统一 fencing：返回 (最新 claimed, 终止结果)。"""
    fresh, reason = refresh_claimed_job(
        claimed, session_factory, require_persisted=require_persisted,
    )
    if fresh is not None:
        return fresh, None
    if reason == "forget_content_redacted":
        return None, HandlerResult(HandlerOutcome.COMPLETED, reason)
    return None, HandlerResult(HandlerOutcome.CLAIM_LOST, reason)
