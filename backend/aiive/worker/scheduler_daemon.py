"""
后台调度器：基于 APScheduler 精确调度 + 并行投递到期任务。

架构：
- APScheduler BackgroundScheduler 替代 while/sleep 循环
- 每轮查询最早到期时间，精确 schedule 到该时刻（消除盲等）
- 多个同时到期提醒通过 ThreadPoolExecutor 并行投递
- 每个提醒使用独立 DB 会话，线程安全
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger

from aiive.db.base import SessionLocal
from aiive.runtime.task_manager import TaskManager
from aiive.runtime.thread_bootstrap import ThreadBootstrapService
from aiive.worker.task_worker import _wake_agent_for_reminder

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler(daemon=True)
_started = False
_MAX_WORKERS = 5
_MAX_POLL_INTERVAL = 60


def _poll_job():
    """单次轮询：批量标记到期任务 + 并行投递 + 自调度下一次。"""
    db = SessionLocal()
    wake_tasks: list[tuple] = []
    try:
        mgr = TaskManager(db)
        now = datetime.now(timezone.utc)
        due = mgr.get_due(now)

        for task in due:
            result = mgr.check_now(task.id)
            if result.get("action") != "notify":
                continue

            tid = task.thread_id or "system"
            try:
                ThreadBootstrapService.ensure_committed_thread(tid)
            except ValueError:
                logger.warning(
                    "目标线程不存在，跳过提醒: task_id=%s target_thread=%s",
                    task.id, tid,
                )
                continue

            # event_type="reminder_triggered" 由 task_worker 在真正投递时写入，
            # 此处只负责协调调度，不写重复事件。
            # 传递基本值而非 ORM 对象，避免 session 关闭后 DetachedInstanceError
            wake_tasks.append((task.id, task.title, tid))

        db.commit()

        # 计算下次轮询时间：精确到最早到期任务
        next_due = mgr.next_due_at()
        if next_due and next_due > now:
            delay = max(1, min(_MAX_POLL_INTERVAL, (next_due - now).total_seconds()))
        else:
            delay = _MAX_POLL_INTERVAL
    except Exception:
        logger.exception("轮询检查异常，60s 后重试")
        delay = 60
    finally:
        db.close()

    # 并行投递（每个提醒使用独立 DB 会话 + LLM）
    if wake_tasks:
        if len(wake_tasks) == 1:
            tid_val, title, target = wake_tasks[0]
            try:
                _wake_agent_for_reminder(tid_val, title, target)
            except Exception:
                logger.exception("提醒投递异常: task_id=%s", tid_val)
        else:
            with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(wake_tasks))) as pool:
                futures = {
                    pool.submit(_wake_agent_for_reminder, tid_val, title, target): (tid_val,)
                    for tid_val, title, target in wake_tasks
                }
                for f in as_completed(futures):
                    tid_val = futures[f][0]
                    try:
                        f.result(timeout=120)
                    except Exception:
                        logger.exception("提醒并行投递异常: task_id=%s", tid_val)

    # 自调度下一次
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
