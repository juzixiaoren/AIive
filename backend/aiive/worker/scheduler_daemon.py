"""
后台调度守护进程：每 10 秒轮询一次到期任务，触发真实通知。
使用独立线程运行，在应用启动时由 start_daemon() 激活。
"""

import logging
import threading
import time

from aiive.db.base import SessionLocal

logger = logging.getLogger(__name__)


def _poll_loop():
    """轮询循环：无限循环，每 10 秒检查一次到期任务并触发通知。"""
    while True:
        try:
            db = SessionLocal()
            try:
                from aiive.worker.task_worker import TaskWorker
                TaskWorker(db).poll_and_notify()
            finally:
                db.close()
        except Exception:
            logger.exception("调度守护进程轮询异常，将在 10 秒后重试")
        time.sleep(10)


_daemon_started = False


def start_daemon():
    """启动后台调度守护线程。

    使用 daemon 线程，当主线程退出时自动结束。
    多次调用只会启动一次（幂等操作）。
    """
    global _daemon_started
    if _daemon_started:
        return
    t = threading.Thread(target=_poll_loop, daemon=True, name="task-scheduler")
    t.start()
    _daemon_started = True
