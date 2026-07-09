"""Task worker: polls due tasks and wakes the Agent to produce proactive responses."""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from aiive.config import settings
from aiive.core.llm_client import LLMClient
from aiive.db.base import SessionLocal
from aiive.runtime.event_logger import EventLogger
from aiive.runtime.task_manager import TaskManager


class TaskWorker:
    def __init__(self, db: Session):
        self._db = db

    def poll_and_notify(self) -> list[dict]:
        """Poll due tasks. For each due reminder, wake the Agent so it proactively
        replies in the original conversation thread."""
        mgr = TaskManager(self._db)
        due = mgr.get_due(datetime.now(timezone.utc))
        results = []

        for task in due:
            result = mgr.check_now(task.id)

            if result.get("action") == "notify":
                # Choose which thread to post the reminder in
                target_thread_id = task.thread_id or "system"

                logger = EventLogger(self._db)
                logger.log_event(
                    trace_id=task.id,
                    thread_id=target_thread_id,
                    event_type="reminder_triggered",
                    payload={
                        "task_id": task.id,
                        "task_type": task.task_type,
                        "title": task.title,
                    },
                )
                self._db.flush()

                # Wake the Agent: invoke the agent loop with reminder context.
                # The Agent decides how to phrase the proactive message.
                self._wake_agent_for_reminder(task, target_thread_id)

                results.append({
                    "task_id": task.id,
                    "title": task.title,
                    "status": "triggered",
                })

        self._db.commit()
        return results

    def _wake_agent_for_reminder(self, task, target_thread_id: str):
        """Invoke the agent loop with reminder context so the Agent proactively replies."""
        try:
            from aiive.runtime.agent_loop import AgentLoop
            client = LLMClient(
                base_url=settings.aiive_llm_base_url,
                api_key=settings.aiive_llm_api_key,
                default_model=settings.aiive_llm_model,
                timeout_seconds=settings.aiive_llm_timeout_seconds,
            )
            loop = AgentLoop(client, self._db)
            prompt = (
                f"[System Reminder — Do NOT respond with '好的' or confirm receipt. "
                f"Just deliver the reminder naturally.]\n\n"
                f"A scheduled reminder is now due: \"{task.title}\". "
                f"Please proactively inform the user that this reminder has arrived."
            )
            loop.run(message=prompt, thread_id=target_thread_id)
        except Exception:
            # If LLM call fails, fall back to logging a notification event
            logger = EventLogger(self._db)
            logger.log_event(
                trace_id=task.id,
                thread_id=target_thread_id,
                event_type="notification_created",
                payload={
                    "task_id": task.id,
                    "task_type": task.task_type,
                    "title": task.title,
                    "message": f"提醒: {task.title}",
                },
            )


def run_once():
    db = SessionLocal()
    try:
        worker = TaskWorker(db)
        results = worker.poll_and_notify()
        for r in results:
            print(f"[TASK] Triggered: {r['title'][:50]}")
        return results
    finally:
        db.close()
