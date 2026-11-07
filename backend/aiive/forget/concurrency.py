"""Forget Phase A 与 MemoryWrite 的并发控制协议（PostgreSQL advisory lock）。

目的
----
Phase A Shield 与 MemoryWrite 之间必须互斥，否则会出现
「MemoryWrite 检查 Shield 未命中 → Forget 创建 Shield 并提交 →
  MemoryWrite 才提交」的窗口，导致遗忘目标遗漏一条已提交记忆。

协议（transaction-level advisory lock，双 int4 命名空间）
---------------------------------------------------------
  - MemoryWrite.write() / write_batch()：pg_advisory_xact_lock_shared(ns, key)
    —— 共享锁，多个写入可并发；
  - Forget Phase A（execute_phase_a_shield）：pg_advisory_xact_lock(ns, key)
    —— 排他锁，与所有在途写入互斥。

锁由数据库事务持有，随 COMMIT / ROLLBACK 自动释放；不能在执行完检查后立即
释放（那等于没有互斥）。同一会话重复获取同一把 advisory lock 是安全的
（PostgreSQL 对会话内重复获取不做阻塞），因此「同事务先排他后共享」
（MemoryConflictService 的 forget_old 路径）不会自我阻塞，也不构成锁升级。

命名空间：双 int4 形式（pg_locks.objsubid=2），与 canonical_key 的
pg_advisory_xact_lock(bigint)（objsubid=1）属于不同锁空间，互不干扰。

隔离级别
--------
协议正确性依赖 READ COMMITTED（语句级快照）：获得锁之后的每一条查询都能看到
前一个事务已提交的 Shield / Tombstone。若数据库被配置为 REPEATABLE READ /
SERIALIZABLE，等待锁期间不会刷新快照，会静默丢失新提交的 Shield，故
fail-closed 直接报错。SQLite 等非 PostgreSQL 后端为 no-op（仅用于逻辑测试）。

入口约束
--------
  - 所有写入路径必须在任何 Shield 查询之前调用 acquire_forget_shared_lock；
  - execute_phase_a_shield 是唯一的排他锁入口，在计算 cutoff、创建 Operation /
    Shield / Tombstone 之前调用 acquire_forget_exclusive_lock。API、Agent Tool、
    MemoryWriteService.forget 及内部调用都经该函数，因此不会绕过锁协议。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: 全局 Forget 锁命名空间（"AIVE"），与 Memory Key 的 bigint 锁空间隔离。
FORGET_LOCK_NAMESPACE = 0x41495645
#: 全局 Forget 锁 key。
FORGET_LOCK_KEY = 1

#: Session.info 中的隔离级别校验标记（同一 Session 只校验一次）。
_READ_COMMITTED_FLAG = "aiive_forget_read_committed_checked"


def _dialect_name(session: Session) -> str:
    """返回 Session 绑定的方言名；探测失败按非 PostgreSQL 处理。"""
    try:
        bind: Any = session.get_bind()
        return str(bind.dialect.name)
    except Exception:  # noqa: BLE001 - 探测失败只影响锁是否 no-op
        return ""


def ensure_read_committed(session: Session) -> None:
    """校验协议前提：PostgreSQL 事务必须处于 READ COMMITTED。

    非 PostgreSQL 后端直接返回（advisory lock 为 no-op，仅用于逻辑测试）。
    """
    if _dialect_name(session) != "postgresql":
        return
    info = session.info
    if info.get(_READ_COMMITTED_FLAG):
        return
    isolation = session.execute(text("SHOW transaction_isolation")).scalar()
    if str(isolation or "").strip().lower() != "read committed":
        raise RuntimeError(
            f"Forget/MemoryWrite 并发协议要求 READ COMMITTED，当前为 {isolation!r}，无法保证看到新提交的 Shield"
        )
    info[_READ_COMMITTED_FLAG] = True


def acquire_forget_shared_lock(session: Session) -> None:
    """写入路径：获取全局 Forget 共享事务锁。

    必须在任何「读取 Shield / 写记忆」之前调用，并由当前事务一直持有到
    COMMIT / ROLLBACK。多个写入的共享锁互不阻塞；Forget Phase A 的排他锁
    会与所有在途写入互斥。
    """
    if _dialect_name(session) != "postgresql":
        return
    ensure_read_committed(session)
    session.execute(
        text("SELECT pg_advisory_xact_lock_shared(:ns, :key)"),
        {"ns": FORGET_LOCK_NAMESPACE, "key": FORGET_LOCK_KEY},
    )


def acquire_forget_exclusive_lock(session: Session) -> None:
    """Forget Phase A：获取全局排他事务锁。

    必须在计算 cutoff、创建 Operation / Shield / Tombstone 之前调用：等待期间
    所有在途写入已经提交，因此取锁之后计算的 cutoff 能覆盖它们；锁释放发生在
    Phase A 提交之后，之后的写入必然能看到 active Shield。
    """
    if _dialect_name(session) != "postgresql":
        return
    ensure_read_committed(session)
    session.execute(
        text("SELECT pg_advisory_xact_lock(:ns, :key)"),
        {"ns": FORGET_LOCK_NAMESPACE, "key": FORGET_LOCK_KEY},
    )


def forget_revision(session: Session) -> int:
    """Committed Forget operation watermark for short-lived context snapshots.

    Operations are append-only; a changed watermark invalidates the whole snapshot,
    conservatively including unrelated Forget. No lock spans an external model call.
    """
    from sqlalchemy import func
    from aiive.db.forget_models import ForgetOperation
    ensure_read_committed(session)
    return int(session.query(func.count(ForgetOperation.id)).scalar() or 0)
