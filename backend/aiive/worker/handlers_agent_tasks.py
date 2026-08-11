"""Outbox Persistent Task handler。"""
from __future__ import annotations

import logging

from aiive.db.base import SessionLocal
from aiive.db.models import AgentRun
from aiive.task_runtime.repository import TaskRepository
from aiive.task_runtime.runtime import PersistentTaskRuntime
from aiive.task_runtime.schemas import RunStatus, TaskStatus
from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome, HandlerResult

logger = logging.getLogger(__name__)


def handle_agent_task_run(claimed: ClaimedJob) -> HandlerResult:
    task_id = str((claimed.payload or {}).get("task_id") or "")
    trigger = str((claimed.payload or {}).get("trigger") or "dispatch")
    if not task_id:
        return HandlerResult(
            HandlerOutcome.NON_RETRYABLE,
            reason="agent_task_run missing task_id",
            terminal_reason="invalid_payload",
        )
    db = SessionLocal()
    try:
        task = TaskRepository(db).get_task(task_id)
        if task is None:
            return HandlerResult(
                HandlerOutcome.NON_RETRYABLE,
                reason="agent_task_not_found",
                terminal_reason="source_missing",
            )
        if task.status in {
            TaskStatus.SUCCEEDED.value, TaskStatus.PARTIAL.value,
            TaskStatus.FAILED.value, TaskStatus.CANCELLED.value,
            TaskStatus.BLOCKED_APPROVAL.value, TaskStatus.BLOCKED_USER.value,
            TaskStatus.BLOCKED_NODE.value, TaskStatus.RECONCILING.value,
        }:
            return HandlerResult(HandlerOutcome.COMPLETED, reason=f"task_{task.status}")
        PersistentTaskRuntime(db).run(task_id, trigger=trigger)
        return HandlerResult(HandlerOutcome.COMPLETED, reason="task_run_advanced")
    except ValueError as error:
        if str(error) in {"task_terminal", "task_not_found"}:
            return HandlerResult(HandlerOutcome.COMPLETED, reason=str(error))
        logger.warning("Agent Task 状态冲突: task_id=%s error=%s", task_id, error)
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, reason=str(error))
    except Exception as error:
        logger.exception("Agent Task Run 失败: task_id=%s", task_id)
        db.rollback()
        try:
            repo = TaskRepository(db)
            task = repo.get_task(task_id, for_update=True)
            if task is not None and task.status in {TaskStatus.DISPATCHING.value, TaskStatus.RUNNING.value}:
                run = db.get(AgentRun, task.current_run_id) if task.current_run_id else None
                if run is not None and run.status == RunStatus.RUNNING.value:
                    repo.finish_run(run, status=RunStatus.FAILED.value, error=str(error))
                if task.status == TaskStatus.DISPATCHING.value:
                    repo.transition_task(task, TaskStatus.QUEUED.value)
                else:
                    repo.transition_task(task, TaskStatus.QUEUED.value)
                repo.append_event(task.id, "run_failed", {"error": str(error)[:1000]})
                repo.checkpoint(task.id, reason="run_failed")
                db.commit()
        except Exception:
            db.rollback()
            logger.exception("Agent Task 失败状态回收失败: task_id=%s", task_id)
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, reason=str(error))
    finally:
        db.close()
