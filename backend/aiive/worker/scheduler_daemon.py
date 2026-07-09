"""Background daemon worker: polls due tasks every 10 seconds, fires real notifications."""

import threading
import time

from aiive.db.base import SessionLocal


def _poll_loop():
    while True:
        try:
            db = SessionLocal()
            try:
                from aiive.worker.task_worker import TaskWorker
                TaskWorker(db).poll_and_notify()
            finally:
                db.close()
        except Exception:
            pass
        time.sleep(10)


_daemon_started = False


def start_daemon():
    global _daemon_started
    if _daemon_started:
        return
    t = threading.Thread(target=_poll_loop, daemon=True, name="task-scheduler")
    t.start()
    _daemon_started = True
