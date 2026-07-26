"""后台调度器：使用 APScheduler 扫描任务并调度 Outbox 消费。"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aiive.db.base import SessionLocal
from aiive.db.models import (
    Epoch,
    EpochCompactionInput,
    MemoryMaintenanceRun,
    MemoryRecord,
    OutboxJob,
    Segment,
    Thread,
    TurnRecord,
)
from aiive.memory.recall_config import (
    ENABLED_OUTBOX_JOB_TYPES,
    MaintenanceConfig,
    RecallConfig,
)
from aiive.runtime.epoch_manager import EpochManager
from aiive.runtime.task_manager import TaskManager
from aiive.worker.task_worker import enqueue_due_tasks

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler(daemon=True)
_started = False
_MAX_POLL_INTERVAL = 60

# Phase 0.5B: OutboxWorker 周期 poll 调度
_outbox_worker = None  # 由 main.py lifespan 注册，避免循环依赖
_OUTBOX_POLL_INTERVAL = 5       # 秒：poll 之间的间隔
_OUTBOX_INITIAL_DELAY = 3       # 秒：启动后首次 poll 延迟

# 嵌套副作用工具（tool_operation）专用 poll：与主 poll 分片，避免 Worker 自我饿死
_TOOL_OP_TYPES = frozenset({"tool_operation"})
_MAIN_POLL_TYPES = ENABLED_OUTBOX_JOB_TYPES - _TOOL_OP_TYPES
_TOOL_OP_POLL_INTERVAL = 2      # 秒：tool_operation 专用 poll 间隔（快于主 poll）

# Phase 3: Idle Scanner + Enqueue Reconciler
_IDLE_SCAN_INTERVAL = 30        # 秒：idle 扫描周期
# Phase 6B
_RETENTION_INTERVAL = 3600     # 秒：retention 扫描周期（每小时一次）
_RECONCILE_INTERVAL = 60        # 秒：补发缺失 Job 周期
_MAX_SCAN_BATCH = 10

# Phase 4: 维护调度
_MAINTENANCE_SCAN_INTERVAL = 3600   # 秒：维护轮询周期（Daily 间隔同上）
_MAINTENANCE_SCOPE_KEY = "all_user_memories"


def _poll_job():
    """单次轮询：原子入队到期任务并自调度下一次扫描。"""
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        enqueue_due_tasks(db, now)
        db.commit()

        mgr = TaskManager(db)
        next_due = mgr.next_due_at()
        if next_due and next_due > now:
            delay = max(1, min(_MAX_POLL_INTERVAL, (next_due - now).total_seconds()))
        else:
            delay = _MAX_POLL_INTERVAL
    except Exception:
        db.rollback()
        logger.exception("轮询检查异常，60s 后重试")
        delay = 60
    finally:
        db.close()

    scheduler.add_job(
        _poll_job,
        DateTrigger(run_date=datetime.now(timezone.utc) + timedelta(seconds=delay)),
        id="task_poll",
        replace_existing=True,
    )


def start_daemon():
    """启动 APScheduler 后台调度器。幂等操作。"""
    global _started
    if _started:
        return
    scheduler.start()
    _started = True
    # 首次轮询：3 秒后启动（留出应用初始化缓冲）
    scheduler.add_job(
        _poll_job,
        DateTrigger(run_date=datetime.now(timezone.utc) + timedelta(seconds=3)),
        id="task_poll",
    )

    # 若 OutboxWorker 已注册，则同时启动 outbox poll 调度
    if _outbox_worker is not None:
        schedule_outbox_poll()

    # Phase 3: Idle Scanner + Enqueue Reconciler
    scheduler.add_job(
        _idle_scanner_job, IntervalTrigger(seconds=_IDLE_SCAN_INTERVAL),
        id="phase3_idle_scanner", replace_existing=True,
    )
    scheduler.add_job(
        _enqueue_reconciler_job, IntervalTrigger(seconds=_RECONCILE_INTERVAL),
        id="phase3_enqueue_reconciler", replace_existing=True,
    )
    scheduler.add_job(
        _forget_reconcile_scanner_job, IntervalTrigger(seconds=_RECONCILE_INTERVAL),
        id="phase6a_forget_reconciler", replace_existing=True,
    )

    # Phase 4: 维护扫描器
    scheduler.add_job(
        _maintenance_scanner_job, IntervalTrigger(seconds=_MAINTENANCE_SCAN_INTERVAL),
        id="phase4_maintenance_scanner", replace_existing=True,
    )

    # Phase 6B: 保留清理扫描器
    scheduler.add_job(
        _retention_scanner_job, IntervalTrigger(seconds=_RETENTION_INTERVAL),
        id="phase6b_retention_scanner", replace_existing=True,
    )


def set_outbox_worker(worker: Any) -> None:
    """注册 OutboxWorker 实例，供周期 poll 调度使用（main.py lifespan 调用）。"""
    global _outbox_worker
    _outbox_worker = worker


def schedule_outbox_poll() -> None:
    """将 outbox poll 加入 APScheduler（首次延迟 _OUTBOX_INITIAL_DELAY 秒）。

    幂等：若已存在同名 job 则替换。同时注册 tool_operation 专用 poll。
    """
    if _outbox_worker is None:
        logger.warning("OutboxWorker 未注册，跳过 outbox poll 调度")
        return
    scheduler.add_job(
        _poll_outbox,
        DateTrigger(run_date=datetime.now(timezone.utc) + timedelta(seconds=_OUTBOX_INITIAL_DELAY)),
        id="outbox_poll",
        replace_existing=True,
    )
    # 嵌套副作用 tool_operation 专用 poll：独立于主 poll，避免主 poll 被
    # reminder_delivery 等长阻塞 turn 占用时嵌套副作用提交被饿死。
    scheduler.add_job(
        _poll_tool_operations,
        DateTrigger(run_date=datetime.now(timezone.utc) + timedelta(seconds=1)),
        id="tool_operation_poll",
        replace_existing=True,
    )


def _poll_outbox() -> None:
    """单次 outbox poll：claim 并分派待处理作业，随后自调度下一次。

    仅处理 _MAIN_POLL_TYPES（不含 tool_operation），tool_operation 由专用 poll 处理。
    """
    if _outbox_worker is None:
        return
    try:
        _outbox_worker.poll(only_types=_MAIN_POLL_TYPES)
    except Exception:
        logger.exception("Outbox poll 异常")
    finally:
        scheduler.add_job(
            _poll_outbox,
            DateTrigger(run_date=datetime.now(timezone.utc) + timedelta(seconds=_OUTBOX_POLL_INTERVAL)),
            id="outbox_poll",
            replace_existing=True,
        )


def _poll_tool_operations() -> None:
    """专用 poll：仅处理 tool_operation（嵌套副作用工具提交）。

    单独一条 APScheduler 线程运行，与主 outbox poll 解耦。主 poll 被
    reminder_delivery 等长阻塞 turn 占用时，本 poll 仍可及时领取并提交
    remind_alert 等副作用工具的 tool_operation，避免 Worker 自我饿死导致
    status=execution_unknown（进而 WS 不刷新、提醒卡片不显示）。
    """
    if _outbox_worker is None:
        return
    try:
        _outbox_worker.poll(only_types=_TOOL_OP_TYPES)
    except Exception:
        logger.exception("Tool operation poll 异常")
    finally:
        scheduler.add_job(
            _poll_tool_operations,
            DateTrigger(run_date=datetime.now(timezone.utc) + timedelta(seconds=_TOOL_OP_POLL_INTERVAL)),
            id="tool_operation_poll",
            replace_existing=True,
        )


def _reconcile_enqueue(db: Session, job_type: str, operation_id: str, payload: dict[str, Any]) -> None:
    """补发缺失 Job：优先走 OutboxWorker.enqueue（含 allowlist 校验），否则直接插入。"""
    if _outbox_worker is not None:
        try:
            _outbox_worker.enqueue(db, job_type, payload, operation_id=operation_id)
            return
        except ValueError:
            logger.warning("reconciler: job_type=%s 不在 allowlist，跳过", job_type)
            return
    db.add(OutboxJob(
        operation_id=operation_id, job_type=job_type, status="pending",
        payload=payload, max_retries=3,
    ))


def _idle_scanner_job() -> None:
    """周期扫描空闲且可回收的 open Segment，触发 begin_segment_sealing（J 节）。

    短事务、逐候选独立会话；operation_id UNIQUE 保证幂等。
    """
    cfg = RecallConfig()
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=cfg.idle_threshold_seconds)
    db = SessionLocal()
    try:
        thread_ids = db.execute(
            select(Thread.id).where(Thread.last_activity_at <= cutoff)
        ).scalars().all()
        if not thread_ids:
            return
        candidates = (
            db.query(Segment)
            .filter(Segment.thread_id.in_(thread_ids), Segment.status == "open")
            .order_by(Segment.pending_seal_at.asc().nullslast())
            .limit(_MAX_SCAN_BATCH)
            .all()
        )
        # 物化候选 id 列表后关闭读会话（finally 统一 close，异常路径不泄漏连接）
        cand = [(s.thread_id, s.id, s.pending_seal_at) for s in candidates]
    except Exception:
        logger.exception("idle_scanner 查询异常")
        return
    finally:
        db.close()

    mgr = EpochManager()
    for thread_id, seg_id, pending_seal_at in cand:
        sdb = SessionLocal()
        try:
            trc = sdb.query(TurnRecord).filter(TurnRecord.segment_id == seg_id).count()
            if trc == 0:
                continue
            if pending_seal_at is None and trc < cfg.min_segment_turn_records:
                continue
            mgr.begin_segment_sealing(
                sdb, thread_id, seg_id,
                expected_idle_cutoff=cutoff,
            )
            sdb.commit()
        except Exception:
            sdb.rollback()
            logger.exception("idle_scanner 处理 Segment 异常: seg_id=%s", seg_id)
        finally:
            sdb.close()


def _enqueue_reconciler_job() -> None:
    """补发“条件已满足但缺 Job”的 epoch_rollover / epoch_checkpoint / segment_sealing（I.1.1）。

    只读检查，条件满足且无在途 Job 时才补发；operation_id UNIQUE 保证不重复。
    """
    cfg = RecallConfig()
    db: Session | None = None
    try:
        db = SessionLocal()

        # 1) active Epoch 达到阈值但无 epoch_rollover Job
        active_epochs = db.query(Epoch).filter(Epoch.status == "active").all()
        for ep in active_epochs:
            sealed = db.query(Segment).filter(
                Segment.epoch_id == ep.id, Segment.status == "sealed",
            ).count()
            if sealed < cfg.max_segments_per_epoch:
                continue
            exists = db.query(OutboxJob).filter(
                OutboxJob.operation_id == f"epoch_rollover:{ep.id}",
                OutboxJob.status.in_(["pending", "running"]),
            ).count()
            if exists == 0:
                _reconcile_enqueue(
                    db, "epoch_rollover", f"epoch_rollover:{ep.id}",
                    {"thread_id": ep.thread_id},
                )

        # 2) sealing Epoch 全部 Segment 已 sealed 但无 epoch_checkpoint Job
        sealing_epochs = db.query(Epoch).filter(Epoch.status == "sealing").all()
        for ep in sealing_epochs:
            total = db.query(Segment).filter(Segment.epoch_id == ep.id).count()
            non_sealed = db.query(Segment).filter(
                Segment.epoch_id == ep.id, Segment.status != "sealed",
            ).count()
            if total == 0 or non_sealed != 0:
                continue
            exists = db.query(OutboxJob).filter(
                OutboxJob.operation_id == f"epoch_checkpoint:{ep.id}",
                OutboxJob.status.in_(["pending", "running"]),
            ).count()
            if exists == 0:
                eci = (
                    db.query(EpochCompactionInput)
                    .filter(EpochCompactionInput.epoch_id == ep.id)
                    .order_by(EpochCompactionInput.created_at.desc())
                    .first()
                )
                if eci is not None:
                    _reconcile_enqueue(
                        db, "epoch_checkpoint", f"epoch_checkpoint:{ep.id}",
                        {
                            "epoch_id": ep.id,
                            "epoch_compaction_input_id": eci.id,
                            "boundary_hash": eci.snapshot_hash,
                            "checkpoint_version": eci.checkpoint_version,
                        },
                    )

        # 3) open Segment 满足 idle/pending_seal 但无 segment_sealing Job
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=cfg.idle_threshold_seconds)
        idle_threads = db.execute(
            select(Thread.id).where(Thread.last_activity_at <= cutoff)
        ).scalars().all()
        if idle_threads:
            open_segs = (
                db.query(Segment)
                .filter(Segment.thread_id.in_(idle_threads), Segment.status == "open")
                .all()
            )
            for seg in open_segs:
                trc = db.query(TurnRecord).filter(
                    TurnRecord.segment_id == seg.id,
                ).count()
                if trc == 0 or (seg.pending_seal_at is None and trc < cfg.min_segment_turn_records):
                    continue
                exists = db.query(OutboxJob).filter(
                    OutboxJob.operation_id.like(f"segment_sealing:{seg.id}:%"),
                    OutboxJob.status.in_(["pending", "running"]),
                ).count()
                if exists == 0:
                    mgr = EpochManager()
                    try:
                        mgr.begin_segment_sealing(
                            db, seg.thread_id, seg.id,
                            expected_idle_cutoff=cutoff,
                        )
                    except Exception:
                        logger.exception("reconciler: begin_segment_sealing 异常 seg_id=%s", seg.id)

        db.commit()
    except Exception:
        logger.exception("enqueue_reconciler 异常")
    finally:
        if db is not None:
            db.close()


def _forget_reconcile_scanner_job() -> None:
    """Phase 6A：周期对账并自动恢复非终态 Forget Saga。"""
    from aiive.forget.reconcile_service import ForgetReconcileService

    db: Session | None = None
    try:
        db = SessionLocal()
        results = ForgetReconcileService.scan_once(db, limit=_MAX_SCAN_BATCH)
        db.commit()
        for operation_id, result in results:
            if result.action in {
                "duplicate_stage_run",
                "outbox_job_missing",
                "unsupported_job_status",
            }:
                logger.error(
                    "forget_reconciler 无法自动恢复: operation_id=%s action=%s stage=%s",
                    operation_id,
                    result.action,
                    result.stage,
                )
    except Exception:
        if db is not None:
            db.rollback()
        logger.exception("forget_reconciler 异常")
    finally:
        if db is not None:
            db.close()


def enqueue_maintenance_job(
    db: Session,
    window_bucket: str | None = None,
    now: datetime | None = None,
    source_context: dict[str, str] | None = None,
) -> str | None:
    """真正 enqueue 一个 `memory_maintenance` OutboxJob（确定性执行入口）。

    operation_id = `memory_maintenance:all_user_memories:{policy_version}:{window_bucket}`
    UNIQUE 保证幂等；window_bucket 用于 Daily/Idle 去重。
    """
    cfg = MaintenanceConfig()
    now = now or datetime.now(timezone.utc)
    window_bucket = window_bucket or now.strftime("%Y-%m-%d")
    op_id = (
        f"memory_maintenance:{_MAINTENANCE_SCOPE_KEY}"
        f":{cfg.policy_version}:{window_bucket}"
    )
    # 幂等去重：无论是否走 OutboxWorker，都先检查是否已存在同 operation_id 的待处理 Job。
    # 生产态 _outbox_worker 已注册，若不前置此检查，OutboxWorker.enqueue 的裸 db.add
    # 会在 commit 时触发 operation_id UNIQUE 冲突（IntegrityError），使 run_memory_maintenance 报错。
    exists = db.query(OutboxJob).filter(
        OutboxJob.operation_id == op_id,
        OutboxJob.status.in_(["pending", "running"]),
    ).count()
    if exists > 0:
        return op_id
    payload = {"schema_version": 1, "operation_id": op_id}
    payload.update(source_context or {})
    if _outbox_worker is not None:
        try:
            _outbox_worker.enqueue(
                db, "memory_maintenance", payload,
                trace_id=payload.get("trace_id") or None,
                operation_id=op_id,
            )
        except ValueError:
            logger.warning("maintenance enqueue 不在 allowlist，跳过")
            return None
    else:
        db.add(OutboxJob(
            operation_id=op_id, job_type="memory_maintenance",
            status="pending", payload=payload,
            trace_id=payload.get("trace_id") or None,
            max_retries=3,
        ))
    return op_id


# ═══════════════════════════════════════════════════════════════════
# Phase 6B: Retention Cleanup 入队
# ═══════════════════════════════════════════════════════════════════

_RETENTION_POLICY_VERSION = 1


def enqueue_retention_job(
    db: Session,
    window_bucket: str | None = None,
    now: datetime | None = None,
) -> str | None:
    """入队一个 `retention_cleanup` OutboxJob。

    operation_id = `retention:all:{policy_version}:{window_bucket}`
    UNIQUE 保证同窗口幂等。
    """
    now = now or datetime.now(timezone.utc)
    window_bucket = window_bucket or now.strftime("%Y-%m-%d")
    op_id = f"retention:all:{_RETENTION_POLICY_VERSION}:{window_bucket}"

    exists = db.query(OutboxJob).filter(
        OutboxJob.operation_id == op_id,
        OutboxJob.status.in_(["pending", "running"]),
    ).count()
    if exists > 0:
        return op_id

    if _outbox_worker is not None:
        try:
            _outbox_worker.enqueue(
                db, "retention_cleanup",
                {"schema_version": 1, "operation_id": op_id},
                operation_id=op_id,
            )
        except ValueError:
            logger.warning("retention_cleanup enqueue 不在 allowlist，跳过")
            return None
    else:
        db.add(OutboxJob(
            operation_id=op_id, job_type="retention_cleanup",
            status="pending", payload={
                "schema_version": 1, "operation_id": op_id,
            }, max_retries=3,
        ))
    return op_id


def _retention_scanner_job() -> None:
    """Phase 6B：定期检查是否需要入队 retention_cleanup 作业。"""
    db: Session | None = None
    try:
        db = SessionLocal()
        now = datetime.now(timezone.utc)
        bucket = now.strftime("%Y-%m-%d")

        # 检查今天是否已有运行中/待处理的 job
        op_id = f"retention:all:{_RETENTION_POLICY_VERSION}:{bucket}"
        existing = db.query(OutboxJob).filter(
            OutboxJob.operation_id == op_id,
        ).count()
        if existing > 0:
            db.close()
            return

        enqueue_retention_job(db, window_bucket=bucket, now=now)
        db.commit()
    except Exception:
        logger.exception("retention_scanner 异常")
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


def _has_dirty_memory(db: Session, cfg: MaintenanceConfig, now: datetime) -> bool:
    """轻量 EXISTS 判定：任一 lane 是否存在未处理候选（第 8 点）。

    不展开全量；任一 lane 命中即视为有 dirty memory。
    """
    MR = MemoryRecord
    # changed lane：自上次成功维护以来有记录被更新（真正增量信号）。
    # 从未维护过则任何记录都视为 dirty；AccessTracker.touch 不更新 updated_at，不会误触发。
    last_maint = _last_successful_maintenance_at(db)
    if last_maint is not None and last_maint.tzinfo is None:
        last_maint = last_maint.replace(tzinfo=timezone.utc)
    changed_filter: list[Any] = [MR.lifecycle_state.in_(["candidate", "active", "sleeping"])]  # type: ignore[assignment]
    if last_maint is not None:
        changed_filter.append(MR.updated_at > last_maint)
    if db.query(MR.id).filter(*changed_filter).limit(1).first() is not None:
        return True
    # expired_ephemeral
    if db.query(MR.id).filter(
        MR.lifecycle_state.in_(["active", "candidate"]),
        MR.retention_policy == "ephemeral",
        MR.valid_to.isnot(None),
        MR.valid_to <= now,
    ).limit(1).first() is not None:
        return True
    # candidate_due
    deadline = now - timedelta(days=cfg.candidate_ttl_days)
    if db.query(MR.id).filter(
        MR.lifecycle_state == "candidate",
        MR.created_at <= deadline,
    ).limit(1).first() is not None:
        return True
    # sleep_due：存在非 pinned 的 active/sleeping 记录达到冷却期，才值得触发一次维护。
    # 未达冷却则不触发，避免每日创建空转 Run（planner 会按记录正确 no-op）。
    # 冷却判定与 lane 查询一致：effective_last_accessed_at = coalesce(last_accessed_at, observed_at, created_at)。
    sleep_cutoff = now - timedelta(days=cfg.sleep_cooling_days)
    eff = func.coalesce(MR.last_accessed_at, MR.observed_at, MR.created_at)
    if db.query(MR.id).filter(
        MR.lifecycle_state.in_(["active", "sleeping"]),
        MR.pinned == False,  # noqa: E712
        MR.retention_policy != "pinned",
        eff <= sleep_cutoff,
    ).limit(1).first() is not None:
        return True
    return False


def _last_successful_maintenance_at(db: Session) -> datetime | None:
    """最近一次 succeeded 的维护完成时间（第 8 点 due 判定用 completed_at）。"""
    run = (
        db.query(MemoryMaintenanceRun)
        .filter(MemoryMaintenanceRun.status == "succeeded")
        .order_by(MemoryMaintenanceRun.completed_at.desc())
        .first()
    )
    return run.completed_at if run else None


def _maintenance_scanner_job() -> None:
    """周期扫描并触发记忆维护（Daily / Idle 双条件，第 8 点）。

    - Daily：距上次成功维护超过 24h，且无在途 maintenance Job，且存在 dirty memory。
    - Idle：存在空闲线程（last_activity_at 超阈值），上次维护后有 dirty memory，
      且当前 idle window 尚未执行过（operation_id window_bucket 去重）。
    - 无 dirty memory 不创建空 Run/Job（测试 #3）。
    """
    cfg = MaintenanceConfig()
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)

        has_dirty = _has_dirty_memory(db, cfg, now)
        if not has_dirty:
            return

        last_ok = _last_successful_maintenance_at(db)
        # SQLite 读回的 datetime 为 naive，与 aware 的 `now` 比较前补齐 UTC，
        # 避免 offset-naive/offset-aware 相减报错（与 maintenance_hashes/_parse_dt 一致）。
        if last_ok is not None and last_ok.tzinfo is None:
            last_ok = last_ok.replace(tzinfo=timezone.utc)
        pending = db.query(OutboxJob).filter(
            OutboxJob.job_type == "memory_maintenance",
            OutboxJob.status.in_(["pending", "running"]),
        ).count()

        # Daily 条件
        daily_due = (
            last_ok is None
            or (now - last_ok).total_seconds() >= cfg.daily_interval_hours * 3600
        )
        if daily_due and pending == 0:
            enqueue_maintenance_job(db, window_bucket=now.strftime("%Y-%m-%d"), now=now)
            db.commit()
            return

        # Idle 条件：存在空闲线程 + idle window 未执行过
        cutoff = now - timedelta(seconds=cfg.idle_threshold_seconds)
        idle_threads = db.execute(
            select(Thread.id).where(Thread.last_activity_at <= cutoff)
        ).scalars().first()
        if idle_threads is not None:
            idle_bucket = f"idle-{now.strftime('%Y-%m-%d')}"
            exists = db.query(OutboxJob).filter(
                OutboxJob.operation_id.like(f"memory_maintenance:{_MAINTENANCE_SCOPE_KEY}:%:{idle_bucket}"),
                OutboxJob.status.in_(["pending", "running"]),
            ).count()
            if exists == 0:
                enqueue_maintenance_job(db, window_bucket=idle_bucket, now=now)
                db.commit()
    except Exception:
        logger.exception("maintenance_scanner 异常")
    finally:
        # 统一在 finally 关闭会话，异常路径不再泄漏连接
        db.close()


def stop_daemon() -> None:
    """关闭调度器：wait=True 等待在途作业（含 outbox poll）结束后返回。

    即 spec 要求的 wait_active 语义。
    """
    global _started
    try:
        scheduler.shutdown(wait=True)
        _started = False
    except Exception:
        logger.exception("调度器关闭异常")
