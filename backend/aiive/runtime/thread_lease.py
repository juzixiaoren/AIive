"""线程级 LLM 执行占用：前台 Turn 与后台目标决策共用的互斥机制。

设计边界（复用优先）：
- 互斥事实源是一行持久化租约（thread_execution_leases），而不是“查询有没有
  活跃 Turn”。后者只是读检查，检查通过到真正调用 LLM 之间仍有窗口；
- 抢占是条件更新（租约到期或同 owner 重入才成功），因此不需要跨 LLM 调用
  持有数据库事务——每次抢占/续租/释放都是独立的短事务；
- owner_kind/owner_id 标识持有者，fencing_token 单调递增：过期持有者用旧
  token 续租/释放会被拒绝，避免僵尸写入；
- 进程崩溃后租约自然到期，其他持有者可直接接管，无需人工清理。

使用约定：
- 前台 Turn 在把 TurnRecord 置 running 之前抢占，收尾/中断时释放；
- 后台目标决策在调用模型之前抢占，持有到决策应用与主动消息落库完成；
- 抢占失败的后台作业返回 RETRY_LATER，让出给前台，绝不并发调用模型。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aiive.db.base import SessionLocal
from aiive.db.models import ThreadExecutionLease

logger = logging.getLogger(__name__)

OWNER_TURN = "turn"
OWNER_GOAL_DECISION = "goal_decision"

# 前台 Turn 占用与 TurnRecord 租约同寿命（心跳每 60s 续租一次）。
TURN_LEASE_SECONDS = 300
# 后台决策覆盖“一次模型调用 + 应用事务”，模型超时 30s，留足收尾余量。
GOAL_DECISION_LEASE_SECONDS = 90

ACQUIRE_POLL_SECONDS = 0.1
# 前台等待后台决策的上限：单次决策模型超时 30s，再留一点应用余量。
FOREGROUND_WAIT_SECONDS = 35.0


@dataclass(frozen=True)
class LeaseGrant:
    """一次成功抢占的凭证；fencing_token 必须随续租/释放一起提交。"""

    thread_id: str
    owner_kind: str
    owner_id: str
    fencing_token: int
    lease_expires_at: datetime


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _try_acquire(
    db: Session,
    thread_id: str,
    owner_kind: str,
    owner_id: str,
    ttl_seconds: int,
    now: datetime,
) -> int | None:
    """单次抢占尝试；返回新 fencing_token，失败返回 None。"""
    row = db.query(ThreadExecutionLease).filter(
        ThreadExecutionLease.thread_id == thread_id,
    ).with_for_update().one_or_none()
    expires_at = now + timedelta(seconds=ttl_seconds)

    if row is None:
        db.add(ThreadExecutionLease(
            thread_id=thread_id,
            owner_kind=owner_kind,
            owner_id=owner_id,
            fencing_token=1,
            lease_expires_at=expires_at,
            acquired_at=now,
            updated_at=now,
        ))
        try:
            db.commit()
            return 1
        except IntegrityError:
            # 并发方刚插入同一线程的租约行：回滚后按普通抢占路径重试。
            db.rollback()
            return None

    held_by_other = (
        _aware(row.lease_expires_at) > now
        and not (row.owner_kind == owner_kind and row.owner_id == owner_id)
    )
    if held_by_other:
        # 不在这里 rollback：调用方在 finally 里关闭会话即可结束只读事务，
        # 避免把调用方同会话里尚未提交的业务写入一起回滚。
        return None

    token = int(row.fencing_token or 0) + 1
    affected = db.query(ThreadExecutionLease).filter(
        ThreadExecutionLease.thread_id == thread_id,
        ThreadExecutionLease.fencing_token == int(row.fencing_token or 0),
    ).update({
        ThreadExecutionLease.owner_kind: owner_kind,
        ThreadExecutionLease.owner_id: owner_id,
        ThreadExecutionLease.fencing_token: token,
        ThreadExecutionLease.lease_expires_at: expires_at,
        ThreadExecutionLease.acquired_at: now,
        ThreadExecutionLease.updated_at: now,
    }, synchronize_session=False)
    db.commit()
    if affected != 1:
        # 抢占窗口内被其他持有者改过：本次视为失败，交给调用方重试。
        return None
    return token


def acquire_thread_lease(
    thread_id: str,
    *,
    owner_kind: str,
    owner_id: str,
    ttl_seconds: int,
    wait_seconds: float = 0.0,
) -> LeaseGrant | None:
    """抢占线程级占用；wait_seconds>0 时按短间隔轮询（前台让路用）。

    每次尝试都是独立短事务，函数返回时没有打开的事务，调用方可以放心在
    之后执行模型调用。
    """
    if not thread_id:
        return None
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        db = SessionLocal()
        try:
            now = datetime.now(UTC)
            token = _try_acquire(
                db, thread_id, owner_kind, owner_id, ttl_seconds, now,
            )
            if token is not None:
                return LeaseGrant(
                    thread_id=thread_id,
                    owner_kind=owner_kind,
                    owner_id=owner_id,
                    fencing_token=token,
                    lease_expires_at=now + timedelta(seconds=ttl_seconds),
                )
        finally:
            db.close()
        if time.monotonic() >= deadline:
            return None
        time.sleep(ACQUIRE_POLL_SECONDS)


def renew_thread_lease(
    thread_id: str,
    *,
    owner_kind: str,
    owner_id: str,
    fencing_token: int,
    ttl_seconds: int,
) -> bool:
    """续租；token 不匹配（已被接管）返回 False，持有者必须停止写入。"""
    db = SessionLocal()
    try:
        now = datetime.now(UTC)
        affected = db.query(ThreadExecutionLease).filter(
            ThreadExecutionLease.thread_id == thread_id,
            ThreadExecutionLease.owner_kind == owner_kind,
            ThreadExecutionLease.owner_id == owner_id,
            ThreadExecutionLease.fencing_token == fencing_token,
        ).update({
            ThreadExecutionLease.lease_expires_at: now + timedelta(seconds=ttl_seconds),
            ThreadExecutionLease.updated_at: now,
        }, synchronize_session=False)
        db.commit()
        return affected == 1
    except Exception:
        db.rollback()
        logger.exception("线程占用续租失败: thread_id=%s", thread_id)
        return False
    finally:
        db.close()


def release_thread_lease(
    db: Session,
    thread_id: str,
    *,
    owner_kind: str,
    owner_id: str,
    fencing_token: int | None = None,
) -> bool:
    """释放占用；使用调用方事务，便于与 Turn 收尾/决策应用同一提交。

    只把租约置为已过期而不删除行：fencing_token 必须跨持有者单调递增，
    否则每次释放后 token 归零，旧持有者的过期写入就无法被识别。

    fencing_token 为空时只按 owner 释放（用于 Turn 中断路径：owner_id 是
    不可复用的 TurnRecord 主键，不存在误删新租约的可能）。
    """
    if not thread_id:
        return False
    query = db.query(ThreadExecutionLease).filter(
        ThreadExecutionLease.thread_id == thread_id,
        ThreadExecutionLease.owner_kind == owner_kind,
        ThreadExecutionLease.owner_id == owner_id,
    )
    if fencing_token is not None:
        query = query.filter(ThreadExecutionLease.fencing_token == fencing_token)
    now = datetime.now(UTC)
    affected = query.update({
        ThreadExecutionLease.lease_expires_at: now,
        ThreadExecutionLease.updated_at: now,
    }, synchronize_session=False)
    return affected == 1


def lease_holder(db: Session, thread_id: str) -> dict[str, object] | None:
    """返回仍有效的占用者视图；无占用或已过期返回 None（排障与测试用）。"""
    if not thread_id:
        return None
    row = db.query(ThreadExecutionLease).filter(
        ThreadExecutionLease.thread_id == thread_id,
    ).one_or_none()
    if row is None:
        return None
    if _aware(row.lease_expires_at) <= datetime.now(UTC):
        return None
    return {
        "thread_id": row.thread_id,
        "owner_kind": row.owner_kind,
        "owner_id": row.owner_id,
        "fencing_token": int(row.fencing_token or 0),
        "lease_expires_at": row.lease_expires_at,
    }
