"""PostgreSQL 专项：Forget Phase A 与 MemoryWrite 的并发安全实测。

默认跳过；设置 AIIVE_PG_TESTS=1 后由 CI 的 postgres-contract job 执行。
"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone

import pytest
import aiive.forget.phase_a_shield as phase_a_module
from aiive.api.routes_forget import ForgetRequest, forget_endpoint
from aiive.context.run_context import RunContext
from aiive.db.forget_models import ForgetOperation, ForgetShield
from aiive.db.models import (
    Event,
    MemoryEvidence,
    MemoryProposal as MemoryProposalModel,
    MemoryRecord,
    Thread,
)
from aiive.forget.phase_a_shield import execute_phase_a_shield
from aiive.forget.visibility_service import ForgetVisibilityService
from aiive.memory.memory_types import (
    EvidenceItem,
    MemoryProposal,
    TrustLevel,
    WriteOutcome,
)
from aiive.memory.memory_write_service import (
    FORGET_LOCK_KEY,
    FORGET_LOCK_NAMESPACE,
    MemoryWriteService,
)
from aiive.tools import forget_tool
from aiive.tools.forget_tool import handle_forget
from sqlalchemy import text

pytestmark = pytest.mark.pg

def _new_id() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _thread_row(pg_sessionmaker) -> str:
    thread_id = _new_id()
    s = pg_sessionmaker()
    try:
        s.add(Thread(id=thread_id))
        s.commit()
    finally:
        s.close()
    return thread_id


def _event_row(pg_sessionmaker, thread_id: str, content: str = "secret") -> str:
    eid = _new_id()
    s = pg_sessionmaker()
    try:
        s.add(Event(
            id=eid, trace_id=_new_id(), thread_id=thread_id,
            event_type="user_message", payload={"content": content},
            created_at=_utcnow(),
        ))
        s.commit()
    finally:
        s.close()
    return eid


def _proposal(
    canonical_key: str, content: str, *, event_id: str | None = None,
) -> MemoryProposal:
    evidence = [
        EvidenceItem(
            source_event_id=event_id,
            source_type="user_assertion",
            trust_level=TrustLevel.TRUSTED.value,
        )
    ] if event_id else []
    prop = MemoryProposal(
        memory_type="knowledge",
        canonical_key=canonical_key,
        scope_type="global",
        scope_id=None,
        content=content,
        confidence=0.9,
        trust_level=TrustLevel.TRUSTED.value,
        evidence=evidence,
        source_event_ids=[event_id] if event_id else [],
    )
    prop.compute_request_idempotency()
    return prop


def _ctx(thread_id: str) -> RunContext:
    return RunContext(thread_id=thread_id, trace_id=_new_id())


def _advisory_locks(pg_engine) -> list[tuple[int, int, int, str, bool]]:
    """读取当前 advisory 锁状态：(classid, objid, objsubid, mode, granted)。"""
    with pg_engine.connect() as c:
        rows = c.execute(text(
            "SELECT classid, objid, objsubid, mode, granted FROM pg_locks "
            "WHERE locktype = 'advisory'"
        )).all()
    return [(int(r[0]), int(r[1]), int(r[2]), str(r[3]), bool(r[4])) for r in rows]


def _has_lock(pg_engine, mode: str, granted: bool) -> bool:
    return any(
        cls == FORGET_LOCK_NAMESPACE and obj == FORGET_LOCK_KEY
        and sub == 2 and m == mode and g is granted
        for cls, obj, sub, m, g in _advisory_locks(pg_engine)
    )


def _wait_lock(pg_engine, mode: str, granted: bool, timeout: float = 5.0) -> bool:
    """等待 advisory 锁进入目标状态（避免线程调度造成的瞬时误判）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _has_lock(pg_engine, mode, granted):
            return True
        time.sleep(0.05)
    return _has_lock(pg_engine, mode, granted)

# ---------------------------------------------------------------------------
# 情况 A：MemoryWrite 先获得共享锁
# ---------------------------------------------------------------------------


def test_writer_shared_lock_blocks_forget_until_commit(
    pg_session, pg_sessionmaker, pg_engine,
):
    """写事务持有共享锁时 Forget 必须等待；提交后 Forget 覆盖该写入。"""
    thread_id = _thread_row(pg_sessionmaker)
    key = "pg.forget.case-a"

    written = threading.Event()
    release_writer = threading.Event()
    forget_done = threading.Event()
    out: dict[str, object] = {}

    def writer() -> None:
        s = pg_sessionmaker()
        try:
            svc = MemoryWriteService(s)
            out["write"] = svc.write(_proposal(key, "case-a secret"), _ctx(thread_id))
            written.set()
            release_writer.wait(20)
            s.commit()
        except Exception as exc:  # noqa: BLE001 - 并发异常收集用于断言
            out["write_err"] = exc
            s.rollback()
        finally:
            s.close()

    def forgetter() -> None:
        s = pg_sessionmaker()
        try:
            s.execute(text("SET lock_timeout = '30s'"))
            svc = MemoryWriteService(s)
            started = time.monotonic()
            out["forget"] = svc.forget(
                mode="memory_only", canonical_key=key, reason="case-a",
            )
            out["forget_wait"] = time.monotonic() - started
            s.commit()
        except Exception as exc:  # noqa: BLE001
            out["forget_err"] = exc
            s.rollback()
        finally:
            s.close()
            forget_done.set()

    tw = threading.Thread(target=writer, daemon=True)
    tw.start()
    assert written.wait(20), "写入线程未完成前置写入"

    tf = threading.Thread(target=forgetter, daemon=True)
    tf.start()

    # Forget 必须被共享锁挡住；pg_locks 中应同时看到已授予的 ShareLock 与
    # 等待中的 ExclusiveLock。
    assert not forget_done.wait(1.5), "Forget 未等待在途 MemoryWrite 就获得了排他锁"
    assert _has_lock(pg_engine, "ShareLock", True), "未观察到写入方持有的共享锁"
    assert _has_lock(pg_engine, "ExclusiveLock", False), "未观察到 Forget 等待排他锁"

    release_writer.set()
    tw.join(20)
    tf.join(30)

    assert "write_err" not in out, out.get("write_err")
    assert "forget_err" not in out, out.get("forget_err")
    assert forget_done.is_set()

    write_res = out["write"]
    forget_res = out["forget"]
    assert getattr(write_res, "written", False)
    assert getattr(forget_res, "written", False)
    memory_id = getattr(write_res, "memory_id")
    assert memory_id

    # cutoff 在获得排他锁之后计算：等待期间提交的写入一定 <= cutoff。
    pg_session.expire_all()
    memory = pg_session.get(MemoryRecord, memory_id)
    assert memory is not None
    manifest = pg_session.execute(text(
        "SELECT cutoff_created_at FROM forget_selector_manifests "
        "ORDER BY frozen_at DESC LIMIT 1"
    )).scalar()
    assert manifest is not None
    assert manifest >= memory.created_at, (
        "cutoff 早于等待期间提交的写入，遗忘会遗漏该记忆"
    )

    # 该写入被本次 Forget 的 canonical_key 选择器覆盖（读取侧 fail-closed）。
    blocked = ForgetVisibilityService.blocked_target_ids(
        pg_session, "memory_record", [(memory_id, None)],
    )
    assert memory_id in blocked, "等待期间提交的写入未被本次 Forget 覆盖"

# ---------------------------------------------------------------------------
# 情况 B：Forget 先获得排他锁
# ---------------------------------------------------------------------------


def test_forget_exclusive_lock_blocks_writer_until_shield_commit(
    pg_session, pg_sessionmaker,
):
    """Forget 持排他锁时写入必须等待；提交后写入读到 active Shield 并拒绝旧来源。"""
    thread_id = _thread_row(pg_sessionmaker)
    event_id = _event_row(pg_sessionmaker, thread_id, "case-b secret")
    key = "pg.forget.case-b"

    shielded = threading.Event()
    release_forget = threading.Event()
    write_done = threading.Event()
    out: dict[str, object] = {}

    def forgetter() -> None:
        s = pg_sessionmaker()
        try:
            svc = MemoryWriteService(s)
            out["forget"] = svc.forget(
                mode="history_only", event_ids=[event_id], reason="case-b",
            )
            shielded.set()
            release_forget.wait(20)
            s.commit()
        except Exception as exc:  # noqa: BLE001
            out["forget_err"] = exc
            s.rollback()
        finally:
            s.close()

    def writer() -> None:
        s = pg_sessionmaker()
        try:
            s.execute(text("SET lock_timeout = '30s'"))
            svc = MemoryWriteService(s)
            out["write"] = svc.write(
                _proposal(key, "case-b secret", event_id=event_id), _ctx(thread_id),
            )
            s.commit()
        except Exception as exc:  # noqa: BLE001
            out["write_err"] = exc
            s.rollback()
        finally:
            s.close()
            write_done.set()

    tf = threading.Thread(target=forgetter, daemon=True)
    tf.start()
    assert shielded.wait(20), "Forget 未完成 Phase A"

    tw = threading.Thread(target=writer, daemon=True)
    tw.start()
    assert not write_done.wait(1.5), "写入未等待 Forget 排他锁即继续"

    release_forget.set()
    tf.join(30)
    tw.join(30)

    assert "forget_err" not in out, out.get("forget_err")
    assert "write_err" not in out, out.get("write_err")
    assert write_done.is_set()

    write_res = out["write"]
    assert getattr(write_res, "outcome") == WriteOutcome.GATE_REJECTED
    assert "Reingestion blocked" in (getattr(write_res, "reason", "") or "")

    pg_session.expire_all()
    assert pg_session.query(MemoryRecord).filter(
        MemoryRecord.canonical_key == key,
    ).count() == 0
    assert pg_session.query(MemoryEvidence).filter(
        MemoryEvidence.source_event_id == event_id,
    ).count() == 0
    assert pg_session.query(MemoryProposalModel).filter(
        MemoryProposalModel.canonical_key == key,
    ).count() == 0

# ---------------------------------------------------------------------------
# 共享锁并发性 / 锁释放 / 锁顺序
# ---------------------------------------------------------------------------


def test_shared_locks_do_not_serialize_writers(pg_sessionmaker):
    """一个写入持共享锁未提交时，另一个写入仍可立即完成（共享锁不互斥）。"""
    thread_id = _thread_row(pg_sessionmaker)
    first_written = threading.Event()
    release_first = threading.Event()
    second_done = threading.Event()
    out: dict[str, object] = {}

    def first_writer() -> None:
        s = pg_sessionmaker()
        try:
            svc = MemoryWriteService(s)
            out["first"] = svc.write(
                _proposal("pg.forget.shared-a", "shared a"), _ctx(thread_id),
            )
            first_written.set()
            release_first.wait(20)
            s.commit()
        except Exception as exc:  # noqa: BLE001
            out["first_err"] = exc
            s.rollback()
        finally:
            s.close()

    def second_writer() -> None:
        s = pg_sessionmaker()
        try:
            s.execute(text("SET lock_timeout = '10s'"))
            svc = MemoryWriteService(s)
            out["second"] = svc.write(
                _proposal("pg.forget.shared-b", "shared b"), _ctx(thread_id),
            )
            s.commit()
        except Exception as exc:  # noqa: BLE001
            out["second_err"] = exc
            s.rollback()
        finally:
            s.close()
            second_done.set()

    t1 = threading.Thread(target=first_writer, daemon=True)
    t1.start()
    assert first_written.wait(20)

    t2 = threading.Thread(target=second_writer, daemon=True)
    t2.start()
    assert second_done.wait(10), "第二个写入被第一个写入的共享锁串行化"

    release_first.set()
    t1.join(20)
    t2.join(20)

    assert "first_err" not in out, out.get("first_err")
    assert "second_err" not in out, out.get("second_err")
    assert getattr(out["first"], "written", False)
    assert getattr(out["second"], "written", False)


def test_rollback_releases_shared_lock(pg_sessionmaker):
    """写入事务回滚后共享锁立即释放，Forget 可以立刻获得排他锁。"""
    _thread_row(pg_sessionmaker)
    s = pg_sessionmaker()
    try:
        MemoryWriteService(s)._acquire_shared_write_lock()
        assert _has_lock_any(pg_sessionmaker, "ShareLock", True)
        s.rollback()
    finally:
        s.close()

    s2 = pg_sessionmaker()
    try:
        s2.execute(text("SET lock_timeout = '10s'"))
        result = MemoryWriteService(s2).forget(
            mode="memory_only", canonical_key="pg.forget.rollback", reason="rollback",
        )
        s2.commit()
    finally:
        s2.close()
    assert result.written


def test_exception_after_lock_releases_forget_lock(pg_sessionmaker):
    """写入在持锁后抛异常并回滚：排他锁不被卡死，后续 Forget 正常完成。"""
    s = pg_sessionmaker()
    try:
        MemoryWriteService(s)._acquire_shared_write_lock()
        raise RuntimeError("boom after lock")
    except RuntimeError:
        s.rollback()
    finally:
        s.close()

    s2 = pg_sessionmaker()
    try:
        s2.execute(text("SET lock_timeout = '10s'"))
        result = MemoryWriteService(s2).forget(
            mode="memory_only", canonical_key="pg.forget.exception", reason="exception",
        )
        s2.commit()
    finally:
        s2.close()
    assert result.written


def test_concurrent_writes_and_forget_no_deadlock(pg_sessionmaker):
    """多写入（各自 Memory Key 锁）+ 并发 Forget：全部完成，无死锁。"""
    thread_id = _thread_row(pg_sessionmaker)
    n = 4
    barrier = threading.Barrier(n + 1)
    results: dict[int, object] = {}
    errors: list[str] = []

    def writer(i: int) -> None:
        s = pg_sessionmaker()
        try:
            s.execute(text("SET lock_timeout = '20s'"))
            svc = MemoryWriteService(s)
            barrier.wait(20)
            results[i] = svc.write(
                _proposal(f"pg.forget.deadlock.{i}", f"deadlock {i}"), _ctx(thread_id),
            )
            s.commit()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"writer-{i}:{type(exc).__name__}:{exc}")
            s.rollback()
        finally:
            s.close()

    def forgetter() -> None:
        s = pg_sessionmaker()
        try:
            s.execute(text("SET lock_timeout = '20s'"))
            svc = MemoryWriteService(s)
            barrier.wait(20)
            svc.forget(
                mode="memory_only", canonical_key="pg.forget.deadlock.0", reason="dl",
            )
            s.commit()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"forget:{type(exc).__name__}:{exc}")
            s.rollback()
        finally:
            s.close()

    threads = [threading.Thread(target=writer, args=(i,), daemon=True) for i in range(n)]
    threads.append(threading.Thread(target=forgetter, daemon=True))
    for t in threads:
        t.start()
    for t in threads:
        t.join(40)

    assert not errors, errors
    assert len(results) == n
    for res in results.values():
        assert getattr(res, "outcome") in (
            WriteOutcome.WRITTEN, WriteOutcome.GATE_REJECTED,
        )


def _has_lock_any(pg_sessionmaker, mode: str, granted: bool) -> bool:
    """在不持有测试引擎夹具时读取 advisory 锁状态。"""
    s = pg_sessionmaker()
    try:
        rows = s.execute(text(
            "SELECT classid, objid, objsubid, mode, granted FROM pg_locks "
            "WHERE locktype = 'advisory'"
        )).all()
    finally:
        s.close()
    return any(
        int(r[0]) == FORGET_LOCK_NAMESPACE and int(r[1]) == FORGET_LOCK_KEY
        and int(r[2]) == 2 and str(r[3]) == mode and bool(r[4]) is granted
        for r in rows
    )


# ---------------------------------------------------------------------------
# 真实生产入口：handle_forget / REST 路由 / 直接调用 execute_phase_a_shield
# ---------------------------------------------------------------------------


def test_handle_forget_waits_for_inflight_write_and_covers_it(
    pg_session, pg_sessionmaker, pg_engine, monkeypatch,
):
    """MemoryWrite 持共享锁时 handle_forget 必须等待；提交后 Shield 覆盖该写入。"""
    monkeypatch.setattr(forget_tool, "SessionLocal", pg_sessionmaker)
    thread_id = _thread_row(pg_sessionmaker)
    key = "pg.forget.tool.case-a"

    written = threading.Event()
    release_writer = threading.Event()
    forget_done = threading.Event()
    out: dict[str, object] = {}

    def writer() -> None:
        s = pg_sessionmaker()
        try:
            out["write"] = MemoryWriteService(s).write(
                _proposal(key, "tool case-a secret"), _ctx(thread_id),
            )
            written.set()
            release_writer.wait(20)
            s.commit()
        except Exception as exc:  # noqa: BLE001
            out["write_err"] = exc
            s.rollback()
        finally:
            s.close()

    def forgetter() -> None:
        try:
            started = time.monotonic()
            out["forget"] = handle_forget(
                mode="memory_only", canonical_key=key, reason="tool-case-a",
            )
            out["forget_wait"] = time.monotonic() - started
        except Exception as exc:  # noqa: BLE001
            out["forget_err"] = exc
        finally:
            forget_done.set()

    tw = threading.Thread(target=writer, daemon=True)
    tw.start()
    assert written.wait(20), "写入线程未完成前置写入"

    tf = threading.Thread(target=forgetter, daemon=True)
    tf.start()

    assert not forget_done.wait(1.5), "handle_forget 未等待在途 MemoryWrite 就提交了 Shield"
    assert _wait_lock(pg_engine, "ShareLock", True), "未观察到写入方持有的共享锁"
    assert _wait_lock(pg_engine, "ExclusiveLock", False), "handle_forget 未等待排他锁"

    release_writer.set()
    tw.join(20)
    tf.join(30)

    assert "write_err" not in out, out.get("write_err")
    assert "forget_err" not in out, out.get("forget_err")
    forget_res = out["forget"]
    assert isinstance(forget_res, dict) and forget_res.get("ok") is True, forget_res

    write_res = out["write"]
    assert getattr(write_res, "written", False)
    memory_id = getattr(write_res, "memory_id", "")
    assert memory_id

    pg_session.expire_all()
    blocked = ForgetVisibilityService.blocked_target_ids(
        pg_session, "memory_record", [(memory_id, None)],
    )
    assert memory_id in blocked, "handle_forget 提交的 Shield 未覆盖等待期间写入的记忆"
    assert pg_session.query(ForgetOperation).count() == 1
    assert pg_session.query(ForgetShield).count() >= 1


def test_handle_forget_exclusive_lock_blocks_writer_until_shield_commit(
    pg_engine, pg_sessionmaker, monkeypatch,
):
    """handle_forget 持排他锁时写入必须等待；提交后写入读到 Shield 并拒绝旧来源。"""
    monkeypatch.setattr(forget_tool, "SessionLocal", pg_sessionmaker)
    thread_id = _thread_row(pg_sessionmaker)
    event_id = _event_row(pg_sessionmaker, thread_id, "tool case-b secret")
    key = "pg.forget.tool.case-b"

    original = phase_a_module._materialize_selector_shields
    locked = threading.Event()
    release = threading.Event()

    def gated(*args, **kwargs):  # noqa: ANN002, ANN003 - 透传原函数参数
        locked.set()
        assert release.wait(20), "测试未释放 handle_forget 的临界区"
        return original(*args, **kwargs)

    # 在取得排他锁之后、写 Shield 之前卡住，制造「Forget 在途」窗口。
    monkeypatch.setattr(phase_a_module, "_materialize_selector_shields", gated)

    write_done = threading.Event()
    out: dict[str, object] = {}

    def forgetter() -> None:
        try:
            out["forget"] = handle_forget(
                mode="history_only", event_ids=[event_id], reason="tool-case-b",
            )
        except Exception as exc:  # noqa: BLE001
            out["forget_err"] = exc

    def writer() -> None:
        s = pg_sessionmaker()
        try:
            s.execute(text("SET lock_timeout = '30s'"))
            out["write"] = MemoryWriteService(s).write(
                _proposal(key, "tool case-b secret", event_id=event_id), _ctx(thread_id),
            )
            s.commit()
        except Exception as exc:  # noqa: BLE001
            out["write_err"] = exc
            s.rollback()
        finally:
            s.close()
            write_done.set()

    tf = threading.Thread(target=forgetter, daemon=True)
    tf.start()
    assert locked.wait(20), "handle_forget 未进入 Phase A"
    assert _wait_lock(pg_engine, "ExclusiveLock", True), "handle_forget 未持有排他锁"

    tw = threading.Thread(target=writer, daemon=True)
    tw.start()
    assert not write_done.wait(1.5), "写入未等待 handle_forget 的排他锁即继续"
    assert _wait_lock(pg_engine, "ShareLock", False), "未观察到写入方等待共享锁"

    release.set()
    tf.join(30)
    tw.join(30)

    assert "forget_err" not in out, out.get("forget_err")
    assert "write_err" not in out, out.get("write_err")
    forget_res = out.get("forget")
    assert isinstance(forget_res, dict) and forget_res.get("ok") is True
    assert getattr(out["write"], "outcome") == WriteOutcome.GATE_REJECTED
    assert "Reingestion blocked" in (getattr(out["write"], "reason", "") or "")

    check = pg_sessionmaker()
    try:
        assert check.query(MemoryRecord).filter(MemoryRecord.canonical_key == key).count() == 0
        assert check.query(MemoryEvidence).filter(
            MemoryEvidence.source_event_id == event_id,
        ).count() == 0
        assert check.query(MemoryProposalModel).filter(
            MemoryProposalModel.canonical_key == key,
        ).count() == 0
        assert check.query(ForgetShield).filter(
            ForgetShield.target_type == "event", ForgetShield.target_id == event_id,
        ).count() >= 1
    finally:
        check.close()


def test_rest_route_forget_uses_same_exclusive_lock(
    pg_session, pg_sessionmaker, pg_engine, monkeypatch,
):
    """REST 路由 forget_endpoint 走 handle_forget，同样受共享锁阻塞。"""
    monkeypatch.setattr(forget_tool, "SessionLocal", pg_sessionmaker)
    thread_id = _thread_row(pg_sessionmaker)
    key = "pg.forget.rest.case-a"

    written = threading.Event()
    release_writer = threading.Event()
    route_done = threading.Event()
    out: dict[str, object] = {}

    def writer() -> None:
        s = pg_sessionmaker()
        try:
            out["write"] = MemoryWriteService(s).write(
                _proposal(key, "rest case-a secret"), _ctx(thread_id),
            )
            written.set()
            release_writer.wait(20)
            s.commit()
        except Exception as exc:  # noqa: BLE001
            out["write_err"] = exc
            s.rollback()
        finally:
            s.close()

    def caller() -> None:
        try:
            out["route"] = forget_endpoint(
                ForgetRequest(mode="memory_only", canonical_key=key, reason="rest-case-a")
            )
        except Exception as exc:  # noqa: BLE001
            out["route_err"] = exc
        finally:
            route_done.set()

    tw = threading.Thread(target=writer, daemon=True)
    tw.start()
    assert written.wait(20)

    tr = threading.Thread(target=caller, daemon=True)
    tr.start()
    assert not route_done.wait(1.5), "REST forget 未等待在途 MemoryWrite 就提交了 Shield"
    assert _wait_lock(pg_engine, "ShareLock", True)
    assert _wait_lock(pg_engine, "ExclusiveLock", False)

    release_writer.set()
    tw.join(20)
    tr.join(30)

    assert "write_err" not in out, out.get("write_err")
    assert "route_err" not in out, out.get("route_err")
    route_res = out["route"]
    assert isinstance(route_res, dict) and route_res.get("ok") is True, route_res

    memory_id = getattr(out["write"], "memory_id", "")
    assert memory_id
    pg_session.expire_all()
    blocked = ForgetVisibilityService.blocked_target_ids(
        pg_session, "memory_record", [(memory_id, None)],
    )
    assert memory_id in blocked, "REST 路径创建的 Shield 未覆盖等待期间写入的记忆"


def test_direct_phase_a_call_cannot_bypass_exclusive_lock(pg_sessionmaker):
    """直接调用 execute_phase_a_shield 也必须拿排他锁，拿不到就失败且零写入。"""
    _thread_row(pg_sessionmaker)  # 保证外键/线程上下文存在
    key = "pg.forget.direct"

    holder = pg_sessionmaker()
    out: dict[str, object] = {}
    done = threading.Event()

    def direct() -> None:
        s = pg_sessionmaker()
        try:
            s.execute(text("SET lock_timeout = '700ms'"))
            out["result"] = execute_phase_a_shield(
                session=s, mode="memory_only", canonical_key=key, reason="direct",
            )
            s.commit()
        except Exception as exc:  # noqa: BLE001
            out["error"] = exc
            s.rollback()
        finally:
            s.close()
            done.set()

    try:
        MemoryWriteService(holder)._acquire_shared_write_lock()
        t = threading.Thread(target=direct, daemon=True)
        t.start()
        assert done.wait(10), "直接调用 execute_phase_a_shield 绕过了排他锁"
        t.join(5)
    finally:
        holder.rollback()
        holder.close()

    assert "result" not in out, "排他锁被占用时直接调用不应成功"
    error = out.get("error")
    assert error is not None
    assert "lock timeout" in str(error).lower() or "canceling statement" in str(error).lower()

    check = pg_sessionmaker()
    try:
        assert check.query(ForgetOperation).count() == 0
        assert check.query(ForgetShield).count() == 0
    finally:
        check.close()


def test_forget_old_same_transaction_exclusive_then_shared(
    pg_session, pg_sessionmaker,
):
    """forget_old 同事务「先排他后共享」不自我阻塞、不死锁，且事务原子提交。"""
    thread_id = _thread_row(pg_sessionmaker)
    old_key = "pg.forget.forget-old.old"
    new_key = "pg.forget.forget-old.new"

    seed = pg_sessionmaker()
    try:
        old_res = MemoryWriteService(seed).write(
            _proposal(old_key, "old content"), _ctx(thread_id),
        )
        seed.commit()
    finally:
        seed.close()
    assert old_res.written
    old_id = old_res.memory_id

    out: dict[str, object] = {}
    done = threading.Event()

    def flow() -> None:
        s = pg_sessionmaker()
        try:
            s.execute(text("SET lock_timeout = '20s'"))
            writer = MemoryWriteService(s)
            out["forget"] = writer.forget(memory_id=old_id, reason="forget_old")
            out["write"] = writer.write(_proposal(new_key, "new content"), _ctx(thread_id))
            s.commit()
        except Exception as exc:  # noqa: BLE001
            out["error"] = exc
            s.rollback()
        finally:
            s.close()
            done.set()

    t = threading.Thread(target=flow, daemon=True)
    t.start()
    assert done.wait(30), "同事务先排他后共享发生自我阻塞或死锁"
    t.join(5)

    assert "error" not in out, out.get("error")
    assert getattr(out.get("forget"), "written", False)
    assert getattr(out.get("write"), "written", False)

    pg_session.expire_all()
    old = pg_session.get(MemoryRecord, old_id)
    assert old is not None
    assert (old.lifecycle_state or "") == "forgotten"
    new = pg_session.query(MemoryRecord).filter(
        MemoryRecord.canonical_key == new_key,
    ).one()
    assert (new.lifecycle_state or "") in {"active", "candidate"}
    assert pg_session.query(ForgetOperation).count() == 1


