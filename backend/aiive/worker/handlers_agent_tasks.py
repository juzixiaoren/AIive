"""Outbox Persistent Task handler。"""
from __future__ import annotations

import logging

from aiive.db.base import SessionLocal
from aiive.db.models import AgentRun, AgentAction
from aiive.task_runtime.repository import TaskRepository, recover_stale_tasks
from aiive.task_runtime.errors import TaskNotRunnableError, TaskTerminalError, TaskNotFoundError
from aiive.task_runtime.read_repository import TaskContentUnavailable
from aiive.task_runtime.runtime import PersistentTaskRuntime
from aiive.task_runtime.schemas import ActionStatus, RunStatus, TaskStatus
from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome, HandlerResult
from aiive.worker.job_source_guard import BLOCKED_REASON, guard_job_sources_visible
from aiive.worker.outbox_execution_auth import (
    resume_execution_authorization,
    with_execution_authorization,
)

logger = logging.getLogger(__name__)


@with_execution_authorization
def handle_agent_task_run(claimed: ClaimedJob) -> HandlerResult:
    fresh, blocked = guard_job_sources_visible(claimed, SessionLocal)
    if blocked is not None:
        return blocked
    assert fresh is not None
    claimed = fresh
    task_id = str((claimed.payload or {}).get("task_id") or "")
    trigger = str((claimed.payload or {}).get("trigger") or "dispatch")
    if not task_id:
        return HandlerResult(
            HandlerOutcome.NON_RETRYABLE,
            reason="agent_task_run missing task_id",
            terminal_reason="invalid_payload",
        )
    db = SessionLocal()
    runtime: PersistentTaskRuntime | None = None
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
        if task.status in {TaskStatus.RUNNING.value, TaskStatus.DISPATCHING.value}:
            # Recovery checks current_run + run status + lease, and routes unknown effects
            # to reconciliation. A valid owner is deduplicated without Outbox retries.
            recover_stale_tasks(db)
            db.commit()
            return HandlerResult(HandlerOutcome.COMPLETED, reason="task_run_deduplicated_or_recovered")
        runtime = PersistentTaskRuntime(db)
        runtime.run(task_id, trigger=trigger)
        # 本次运行可能嵌套 Forget：提交后重新获取共享执行授权
        if not resume_execution_authorization():
            raise RuntimeError("execution_authorization_restore_failed")
        return HandlerResult(HandlerOutcome.COMPLETED, reason="task_run_advanced")
    except TaskContentUnavailable:
        # A tool can commit Forget, or a model snapshot can expire mid-run.
        # Discard pending copies and finish this job without replaying its input.
        # The Forget cascade owns cleanup; recovery must not read the hidden Task.
        db.rollback()
        return HandlerResult(
            HandlerOutcome.COMPLETED,
            reason=f"{BLOCKED_REASON}:task_content_unavailable",
        )
    except (TaskTerminalError, TaskNotFoundError) as error:
        db.rollback()
        return HandlerResult(HandlerOutcome.COMPLETED, reason=str(error))
    except TaskNotRunnableError as error:
        db.rollback()
        recover_stale_tasks(db)
        db.commit()
        return HandlerResult(HandlerOutcome.COMPLETED, reason=str(error))
    except Exception as error:
        logger.exception("Agent Task Run 失败: task_id=%s", task_id)
        db.rollback()
        converged_cancelled = False
        try:
            repo = TaskRepository(db)
            task = repo.get_task(task_id, for_update=True)
            if (task is not None and runtime is not None
                    and task.current_run_id == runtime.active_run_id
                    and task.status in {
                        TaskStatus.DISPATCHING.value, TaskStatus.RUNNING.value,
                        TaskStatus.CANCELLED.value,
                    }):
                run = db.get(AgentRun, task.current_run_id) if task.current_run_id else None
                cancelled = task.status == TaskStatus.CANCELLED.value
                if run is not None and run.status == RunStatus.RUNNING.value:
                    # 已取消任务只把 Run 收敛为 interrupted，绝不回到可执行状态。
                    repo.finish_run(
                        run,
                        status=RunStatus.INTERRUPTED.value if cancelled else RunStatus.FAILED.value,
                        error=str(error),
                        token_usage=run.token_usage, model=run.model,
                    )
                uncertain = db.query(AgentAction).filter(
                    AgentAction.task_id == task.id,
                    AgentAction.status.in_([ActionStatus.DISPATCHED.value, ActionStatus.RUNNING.value, ActionStatus.UNKNOWN.value]),
                ).all()
                if cancelled:
                    for action in uncertain:
                        if action.status != ActionStatus.UNKNOWN.value:
                            repo.transition_action(action, ActionStatus.UNKNOWN.value, error="cancelled_worker_failed")
                    repo.append_event(
                        task.id, "cancelled_execution_recovered",
                        {"reason": "worker_failed_after_cancel", "action_ids": [a.id for a in uncertain]},
                        visibility="conversation",
                    )
                    repo.checkpoint(
                        task.id, reason="cancelled_execution_recovery",
                        pending_action_refs=[a.id for a in uncertain],
                    )
                elif uncertain:
                    for action in uncertain:
                        if action.status != ActionStatus.UNKNOWN.value:
                            repo.transition_action(action, ActionStatus.UNKNOWN.value, error="run_failed")
                    if task.status == TaskStatus.DISPATCHING.value:
                        repo.transition_task(task, TaskStatus.RUNNING.value)
                    repo.transition_task(task, TaskStatus.RECONCILING.value)
                    repo.append_event(task.id, "task_reconciliation_required", {"action_ids": [a.id for a in uncertain]})
                else:
                    repo.transition_task(task, TaskStatus.QUEUED.value)
                if not cancelled:
                    repo.append_event(task.id, "run_failed", {"error": str(error)[:1000]})
                    repo.checkpoint(task.id, reason="run_failed")
                db.commit()
                # 只有提交成功后才确认"这次投递已经完成了实际工作"。
                converged_cancelled = cancelled
        except Exception:
            db.rollback()
            converged_cancelled = False
            logger.exception("Agent Task 失败状态回收失败: task_id=%s", task_id)
        if converged_cancelled:
            # 取消终态已收敛并提交：不需要 Outbox 再投递一次去读同一个终态。
            return HandlerResult(
                HandlerOutcome.COMPLETED, reason="cancelled_execution_recovered",
            )
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, reason=str(error))
    finally:
        db.close()
