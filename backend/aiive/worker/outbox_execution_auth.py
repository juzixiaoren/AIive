"""Outbox 执行授权：与 Forget Phase A 排他锁互斥的 session-level 共享锁。

- PostgreSQL：独立连接上 `pg_advisory_lock_shared(FORGET_LOCK_NAMESPACE, FORGET_LOCK_KEY)`；
  与 `execute_phase_a_shield()` 的事务级 `pg_advisory_xact_lock` 排他锁互斥。
- 授权覆盖「重读 Job → 来源可见性检查 → 外部调用/副作用 → 结果提交」整段。
- 嵌套 Forget（ConflictReply / Agent Task / Forget Tool）走显式交接：
  `execute_phase_a_shield()` 进入前释放共享授权（`release_execution_authorization`），
  Phase A 提交后由调用方 `resume_execution_authorization()` 重新获取，避免同线程死锁。
- 释放失败时必须 invalidate 物理连接，绝不把仍持锁的连接归还连接池。
- SQLite：no-op，仅用于逻辑测试；并发正确性以 PostgreSQL 为准。
"""
from __future__ import annotations

import functools
import logging
import concurrent.futures
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text as sa_text

from aiive.forget.concurrency import FORGET_LOCK_KEY, FORGET_LOCK_NAMESPACE
from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome, HandlerResult
from aiive.worker.outbox_payload_contract import is_redacted_payload

logger = logging.getLogger(__name__)

_LOCK_PARAMS = {"ns": FORGET_LOCK_NAMESPACE, "key": FORGET_LOCK_KEY}
# 嵌套 Forget 交接后重新取锁的最长等待：拿不到就保持「未授权」状态，
# 由调用方在事务提交后重试，绝不在持排他锁的事务里无限等待。
RESUME_LOCK_TIMEOUT_MS = 2000


@dataclass
class _AuthorizationState:
    """单个 Context 内共享授权连接的状态（支持嵌套 Forget 交接）。"""

    connection: Any | None
    suspended_for_forget: bool = False
    gate: Any = None
    claimed: ClaimedJob | None = None
    owner_thread_id: int = field(default_factory=threading.get_ident)

    def assert_owner(self) -> None:
        if self.owner_thread_id != threading.get_ident():
            raise RuntimeError("execution_authorization_wrong_thread")


_STATE: ContextVar[_AuthorizationState | None] = ContextVar(
    "aiive_outbox_authorization_state", default=None,
)


def get_engine() -> Any:
    from aiive.db.base import engine

    return engine


def _is_postgres() -> bool:
    return getattr(get_engine().dialect, "name", "") == "postgresql"


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _acquire_shared(timeout_ms: int | None = None) -> Any:
    """在新连接上取 session-level 共享锁并返回该连接。"""
    connection = get_engine().connect()
    try:
        if timeout_ms is not None:
            connection.execute(sa_text(f"SET LOCAL lock_timeout = {int(timeout_ms)}"))
        connection.execute(
            sa_text("SELECT pg_advisory_lock_shared(:ns, :key)"), _LOCK_PARAMS,
        )
        # session-level 锁独立于事务，commit 只为结束该只读事务
        connection.commit()
        return connection
    except Exception:
        connection.invalidate()
        raise


def _release_shared(state: _AuthorizationState) -> None:
    """释放共享锁；任何失败都 invalidate 连接，禁止带锁归还连接池。"""
    state.assert_owner()
    connection = state.connection
    state.connection = None
    if connection is None:
        return
    try:
        granted = connection.execute(
            sa_text("SELECT pg_advisory_unlock_shared(:ns, :key)"), _LOCK_PARAMS,
        ).scalar()
        connection.commit()
        if granted is not True:
            logger.error("pg_advisory_unlock_shared 返回 %r，强制 invalidate 连接", granted)
            connection.invalidate()
    except Exception:
        logger.exception("释放共享执行授权失败，强制 invalidate 连接")
        connection.invalidate()
    finally:
        try:
            connection.close()
        except Exception:  # pragma: no cover - 关闭失败无补救手段
            logger.exception("关闭授权连接失败")


@contextmanager
def shared_execution_authorization() -> Generator[None, None, None]:
    """持共享执行授权直到外部调用与写入提交结束（可重入）。"""
    existing = _STATE.get()
    if existing is not None:
        require_execution_authorization()
        yield
        return
    state = _AuthorizationState(connection=_acquire_shared() if _is_postgres() else None)
    token = _STATE.set(state)
    try:
        yield
    finally:
        _release_shared(state)
        _STATE.reset(token)


def release_execution_authorization() -> None:
    """为嵌套 Forget 让路：释放共享授权并标记交接。

    由 `execute_phase_a_shield()` 在获取排他锁之前调用：同一个线程先持共享锁
    再排队等排他锁会永久自锁，必须先放掉共享授权。
    """
    state = _STATE.get()
    if state is not None and state.gate is not None:
        # The gate contains only thread-safe control state, never a DB connection.
        with state.gate.lock:
            if state.gate.cancelled:
                raise RuntimeError("forget_cancelled_before_phase_a")
            state.gate.forget_started = True
    if state is None or state.connection is None:
        return
    logger.debug("释放共享执行授权以进入 Forget Phase A 排他锁")
    _release_shared(state)
    state.suspended_for_forget = True


def resume_execution_authorization(*, timeout_ms: int | None = None) -> bool:
    """Phase A 事务提交后重新获取共享授权。返回是否已持有。

    拿不到（例如调用方事务仍未提交）时保持未授权并返回 False，由调用方在
    真正需要保护的外部调用前重试；绝不阻塞等待以免与自身事务互锁。
    """
    state = _STATE.get()
    if state is None or not _is_postgres():
        return True
    state.assert_owner()
    if state.connection is not None:
        return True
    budget = RESUME_LOCK_TIMEOUT_MS if timeout_ms is None else timeout_ms
    try:
        state.connection = _acquire_shared(timeout_ms=budget)
        state.suspended_for_forget = False
        return True
    except Exception:
        logger.warning("重新获取共享执行授权失败（保持未授权，稍后重试）", exc_info=True)
        return False


def has_execution_authorization() -> bool:
    if not _is_postgres():
        return True
    state = _STATE.get()
    return state is not None and state.connection is not None


def require_execution_authorization() -> None:
    """A suspended worker must stop before another model call or side effect."""
    state = _STATE.get()
    if state is not None and not resume_execution_authorization():
        raise RuntimeError("execution_authorization_restore_failed")


class _ToolGate:
    def __init__(self) -> None:
        self.lock: Any = threading.Lock()
        self.cancelled: bool = False
        self.forget_started: bool = False


def execute_in_tool_thread(executor: Any, invoke: Callable[[], Any], timeout: float) -> Any:
    """Transfer execution, never Connection ownership, to a pool worker.

    The parent releases its own connection before submission. The child acquires
    and closes its own connection, rechecks the claim/sources, and can therefore
    enter Phase A without a parent/child lock cycle. No Context is copied.
    Timeout revokes permission to *start* Forget under the same gate lock. Once
    Phase A starts, await its definitive commit/rollback rather than report a
    failure while a destructive operation is still executing in the background.
    """
    parent = _STATE.get()
    claimed = parent.claimed if parent else None
    gate = _ToolGate()
    if parent is not None:
        _release_shared(parent)
        parent.suspended_for_forget = True

    def child() -> Any:
        with shared_execution_authorization():
            state = _STATE.get()
            if state is not None:
                state.gate = gate
                state.claimed = claimed
            with gate.lock:
                if gate.cancelled:
                    raise concurrent.futures.CancelledError()
            if claimed is not None:
                from aiive.worker.job_source_guard import guard_job_sources_visible

                _, blocked = guard_job_sources_visible(claimed)
                if blocked is not None:
                    raise RuntimeError(blocked.reason)
            return invoke()

    try:
        future = executor.submit(child)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            with gate.lock:
                started = gate.forget_started
                if not started:
                    gate.cancelled = True
            if started:
                return future.result()
            future.cancel()
            raise
    finally:
        # Runs on the parent thread even if the child timed out or raised.
        require_execution_authorization()


def get_session_factory() -> Callable[[], Any]:
    state = _STATE.get()
    if state is not None and state.connection is not None:
        state.assert_owner()
        from sqlalchemy.orm import Session

        return _mark_owns_session(lambda: Session(bind=state.connection, expire_on_commit=False))
    from aiive.db.base import SessionLocal

    return SessionLocal


# 标记「该工厂每次都新建 Session」：只有自建 Session 才能 close，
# 否则会关闭测试分发器返回的共享会话（把调用方的 ORM 对象置为 detached）。
_OWNS_SESSION_ATTR = "_aiive_creates_new_session"


def _mark_owns_session(factory: Callable[[], Any]) -> Callable[[], Any]:
    setattr(factory, _OWNS_SESSION_ATTR, True)
    return factory


def owns_session_factory(factory: Callable[[], Any]) -> bool:
    """该工厂是否每次新建 Session（只有自建 Session 才能 close）。"""
    flag = getattr(factory, _OWNS_SESSION_ATTR, None)
    if flag is not None:
        return bool(flag)
    from sqlalchemy.orm import sessionmaker

    return isinstance(factory, sessionmaker)


def session_is_ours(db: Any) -> bool:
    """判断 Session 是否由本模块新建（可安全 close）。

    判据是「会话本身尚未开启事务、且其绑定的连接也没有外部事务」：
      - 自己新建的 Session(bind=engine/connection) → 两者都无事务 → 归本模块；
      - 单元测试的共享会话绑在调用方的外层事务上 → 不归本模块，绝不能 close。
    """  # noqa: D401
    try:
        if db.get_transaction() is not None:
            return False
        bind = db.get_bind()
        in_tx = getattr(bind, "in_transaction", None)
        if in_tx is None:
            return True
        return not bool(in_tx())
    except Exception:  # pragma: no cover - 探测失败按「非自建」处理更安全
        return False


def authorized_session_factory(fallback: Callable[[], Any]) -> Callable[[], Any]:
    """有活跃授权连接时，返回绑定该连接的 Session 工厂；否则回退调用方工厂。"""
    state = _STATE.get()
    if state is None or state.connection is None:
        return fallback
    state.assert_owner()
    from sqlalchemy.orm import Session

    return _mark_owns_session(lambda: Session(bind=state.connection, expire_on_commit=False))


def verify_claim(db: Any, claimed: ClaimedJob) -> tuple[ClaimedJob | None, str]:
    """在给定 Session 上严格校验 claim，返回 (最新 Job, 失败原因)。"""
    from aiive.db.models import OutboxJob

    job = db.query(OutboxJob).filter(OutboxJob.id == claimed.id).first()
    if job is None:
        return None, "outbox_job_not_found"
    if job.status != "running":
        return None, f"outbox_job_not_running:{job.status}"
    if not claimed.claim_token or job.claim_token != claimed.claim_token:
        return None, "claim_token_mismatch"
    if not claimed.worker_id or job.locked_by != claimed.worker_id:
        return None, "claim_owner_mismatch"
    lease = _as_utc(job.lease_expires_at)
    if lease is None:
        return None, "claim_lease_missing"
    if lease <= datetime.now(timezone.utc):
        return None, "claim_lease_expired"
    if is_redacted_payload(job.payload):
        return None, "forget_content_redacted"
    return replace(
        claimed,
        payload=job.payload or {},
        schema_version=int(job.schema_version or claimed.schema_version),
        trace_id=job.trace_id or claimed.trace_id,
    ), ""


def refresh_claimed_job(
    claimed: ClaimedJob,
    session_factory: Callable[[], Any] | None = None,
) -> tuple[ClaimedJob | None, str]:
    """严格重读 OutboxJob 并校验 claim，拒绝任何旧副本。

    生产 Worker 路径必须满足：行存在、status=running、claim_token 严格相等、
    locked_by 严格等于领取者、lease 存在且未过期。任何一项不满足即终止，
    绝不继续使用领取时的 payload 副本。
    """
    factory = session_factory or get_session_factory()
    db = factory()
    # 归属判定看事务状态：调用方（如单元测试的共享会话）已开事务时不 close，
    # 自己新建的会话必须在本函数内关闭，避免把连接连同打开的事务留在池里。
    owns_session = session_is_ours(db)
    try:
        return verify_claim(db, claimed)
    finally:
        if owns_session:
            db.close()


def with_execution_authorization(fn: Callable[..., HandlerResult]) -> Callable[..., HandlerResult]:
    """Handler 装饰器：整个外部调用/写入链持共享执行授权。"""

    @functools.wraps(fn)
    def wrapper(claimed: ClaimedJob, *args: Any, **kwargs: Any) -> HandlerResult:
        with shared_execution_authorization():
            state = _STATE.get()
            if state is not None:
                state.claimed = claimed
            return fn(claimed, *args, **kwargs)

    return wrapper


def guard_handler_payload(
    claimed: ClaimedJob,
    session_factory: Callable[[], Any] | None = None,
) -> tuple[ClaimedJob | None, HandlerResult | None]:
    """外部副作用前的统一 fencing：返回 (最新 claimed, 终止结果)。"""
    fresh, reason = refresh_claimed_job(claimed, session_factory)
    if fresh is not None:
        return fresh, None
    if reason == "forget_content_redacted":
        return None, HandlerResult(HandlerOutcome.COMPLETED, reason)
    return None, HandlerResult(HandlerOutcome.CLAIM_LOST, reason)
