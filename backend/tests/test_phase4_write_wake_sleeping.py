"""Phase 4 缺口回归测试（问题 2）：write 路径 sleeping → wake 闭合。

修复前：用户再次提及一条 sleeping 同键记忆时，`get_active_by_key_scope_locked`
只查 (active, candidate)，找不到 active 记录 → resolver 返回 create → 新建一条
active 记忆，原 sleeping 记录被遗留，造成重复/分叉。

修复后：`write` 与 `_write_single_in_transaction`（write_batch 路径）在 resolve 前置
阶段调用 `_load_existing_or_wake_sleeping`：无 active 同键记录时，唤醒最近更新的
sleeping 同键记录（J.4/J.5 wake 闭合），再 resolve。内容相同 → reinforce 复用原记录，
不新建分叉。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from aiive.context.run_context import RunContext
from aiive.db.models import Event, MemoryLineage, MemoryRecord, Thread
from aiive.memory.memory_types import (
    EvidenceItem,
    EvidenceSourceType,
    LifecycleState,
    LineageOperation,
    MemoryProposal,
    TrustLevel,
)
from aiive.memory.memory_write_service import MemoryWriteService


def _now():
    return datetime.now(timezone.utc)


def _mk_record(db, **kwargs) -> MemoryRecord:
    """构造一条 MemoryRecord（未 commit）。"""
    defaults = dict(
        id=str(uuid.uuid4()),
        memory_type="fact",
        canonical_key="test.key",
        scope_type="global",
        scope_id=None,
        content="content",
        lifecycle_state=LifecycleState.CANDIDATE.value,
        validity_state="valid",
        confidence=0.5,
        importance=0.5,
        retention_policy="normal",
        record_version=1,
        pinned=False,
        stability="contextual",
        observed_at=_now(),
        created_at=_now(),
    )
    defaults.update(kwargs)
    rec = MemoryRecord(**defaults)
    db.add(rec)
    db.flush()
    return rec


def _same_content_proposal(canonical_key: str, content: str) -> MemoryProposal:
    """构造一条能通过 gate（active）且内容确定的 proposal。"""
    prop = MemoryProposal(
        memory_type="knowledge",
        canonical_key=canonical_key,
        scope_type="global",
        scope_id=None,
        content=content,
        confidence=0.9,
        trust_level=TrustLevel.TRUSTED.value,
        evidence=[EvidenceItem(
            source_event_id="evt-wake-1",
            source_type=EvidenceSourceType.USER_ASSERTION.value,
            trust_level=TrustLevel.TRUSTED.value,
        )],
    )
    prop.compute_content_hash()
    return prop


def test_write_wakes_sleeping_same_key_instead_of_creating_duplicate(db):
    """write() 路径：再次确认 sleeping 同键记忆 → 唤醒并 reinforce，不新建分叉。"""
    same_content = "用户偏好使用 Python 编程"
    sleeping = _mk_record(
        db, lifecycle_state=LifecycleState.SLEEPING.value, memory_type="knowledge",
        canonical_key="user.pref.lang.wake", content=same_content,
    )
    db.commit()

    ctx = RunContext(thread_id="t-wake", trace_id="tr-wake")
    # wake 经 log_event 写入 events.thread_id 外键 → 必须存在对应 Thread
    db.add(Thread(id="t-wake"))
    db.flush()
    prop = _same_content_proposal("user.pref.lang.wake", same_content)

    writer = MemoryWriteService(db)
    result = writer.write(prop, ctx)
    db.commit()

    records = db.query(MemoryRecord).filter(
        MemoryRecord.canonical_key == "user.pref.lang.wake"
    ).all()
    assert len(records) == 1, f"期望仅 1 条记录（唤醒复用），实际 {len(records)}"
    assert records[0].id == sleeping.id
    assert records[0].lifecycle_state == LifecycleState.ACTIVE.value
    assert result.operation == "reinforce"

    wake_lin = db.query(MemoryLineage).filter(
        MemoryLineage.predecessor_id == sleeping.id,
        MemoryLineage.operation == LineageOperation.WAKE.value,
    ).count()
    assert wake_lin >= 1, "唤醒应写入 wake 谱系边"


def test_write_single_txn_wakes_sleeping(db):
    """write_batch 路径（_write_single_in_transaction）同样在 resolve 前唤醒 sleeping。"""
    same_content = "用户偏好使用 Rust 编程"
    sleeping = _mk_record(
        db, lifecycle_state=LifecycleState.SLEEPING.value, memory_type="knowledge",
        canonical_key="user.pref.lang.wake2", content=same_content,
    )
    db.commit()

    ctx = RunContext(thread_id="t-wake2", trace_id="tr-wake2")
    db.add(Thread(id="t-wake2"))
    db.flush()
    prop = _same_content_proposal("user.pref.lang.wake2", same_content)

    writer = MemoryWriteService(db)
    result = writer._write_single_in_transaction(prop, ctx)
    db.commit()

    records = db.query(MemoryRecord).filter(
        MemoryRecord.canonical_key == "user.pref.lang.wake2"
    ).all()
    assert len(records) == 1, f"期望仅 1 条记录（唤醒复用），实际 {len(records)}"
    assert records[0].id == sleeping.id
    assert records[0].lifecycle_state == LifecycleState.ACTIVE.value
    assert result.operation == "reinforce"


def test_write_wakes_sleeping_with_invalid_thread_commits_anyway(db):
    """无效 thread_id（事件写入失败）时，wake 状态变更仍应随外层事务提交。

    回归：修复前 `log_event` 因 `events.thread_id` 外键缺失抛 `IntegrityError`，
    未隔离的 flush 失败会污染外层事务，连带回滚 wake 的状态变更。修复后事件写入
    在 savepoint 内隔离，失败仅丢弃该事件本身，不影响 wake 提交。
    """
    same_content = "用户偏好使用 Go 编程"
    sleeping = _mk_record(
        db, lifecycle_state=LifecycleState.SLEEPING.value, memory_type="knowledge",
        canonical_key="user.pref.lang.wake3", content=same_content,
    )
    db.commit()

    # 故意不插入对应 Thread：thread_id 外键缺失，触发事件写入失败
    ctx = RunContext(thread_id="t-nonexistent", trace_id="tr-wake3")
    prop = _same_content_proposal("user.pref.lang.wake3", same_content)

    writer = MemoryWriteService(db)
    result = writer.write(prop, ctx)
    db.commit()

    records = db.query(MemoryRecord).filter(
        MemoryRecord.canonical_key == "user.pref.lang.wake3"
    ).all()
    assert len(records) == 1, f"期望仅 1 条记录（唤醒复用），实际 {len(records)}"
    assert records[0].id == sleeping.id
    assert records[0].lifecycle_state == LifecycleState.ACTIVE.value
    assert result.operation == "reinforce"
    # 事件写入被隔离丢弃：events 表不应残留该 wake 事件（否则会破坏外层事务）
    ev_count = db.query(Event).filter(
        Event.thread_id == "t-nonexistent",
        Event.event_type == "memory.wake",
    ).count()
    assert ev_count == 0, "失败的事件写入不应污染外层事务"
