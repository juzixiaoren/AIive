"""
任务工作器：轮询到期任务并唤醒 Agent 主动生成回复。
用于处理提醒等定时任务，到期后通过 Agent Loop 在原始对话线程中发布消息。
"""

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from aiive.db.base import SessionLocal
from aiive.db.models import Event, Task
from aiive.runtime.event_logger import EventLogger
from aiive.runtime.task_manager import TaskManager
from aiive.runtime.thread_bootstrap import ThreadBootstrapService

logger = logging.getLogger(__name__)


class TaskWorker:
    """任务轮询工作器，负责检查到期任务并触发 Agent 响应。

    主要职责：
    - 轮询到期任务（如提醒）
    - 当提醒到期时，写入 reminder_triggered 事件
    - 调用 Agent Loop 让 Agent 主动在对话中通知用户
    - LLM 调用失败时，回退为记录 notification_created 事件
    """

    def __init__(self, db: Session):
        """初始化任务工作器。

        参数:
            db: 数据库会话
        """
        self._db: Session = db

    def poll_and_notify(self) -> list[dict[str, Any]]:
        """轮询到期任务并触发通知。

        对于每个到期的提醒：
        1. 标记任务为已检查
        2. 记录 reminder_triggered 事件
        3. 唤醒 Agent Loop 以在对话线程中主动回复

        返回:
            已触发任务的列表，每项包含 task_id、title、status
        """
        mgr = TaskManager(self._db)
        due = mgr.get_due(datetime.now(timezone.utc))
        results = []

        for task in due:
            result = mgr.check_now(task.id)

            if result.get("action") == "notify":
                # 选择目标线程：优先使用任务关联的线程
                # 历史数据 task.thread_id 可能为 NULL，迁移期临时 fallback 到 system
                if task.thread_id:
                    target_thread_id = task.thread_id
                else:
                    logger.warning(
                        "task.thread_id 为空，fallback 到 system 线程: task_id=%s title=%s",
                        task.id, task.title,
                    )
                    target_thread_id = "system"

                # 确保目标 thread 已 committed，避免 FK 违规
                try:
                    ThreadBootstrapService.ensure_committed_thread(target_thread_id)
                except ValueError:
                    logger.warning(
                        "目标线程不存在，跳过提醒: task_id=%s target_thread=%s",
                        task.id, target_thread_id,
                    )
                    continue

                event_logger = EventLogger(self._db)
                event_logger.log_event(
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

                # 唤醒 Agent：由 Agent 决定如何表达主动消息
                self._wake_agent_for_reminder(task, target_thread_id)

                results.append({
                    "task_id": task.id,
                    "title": task.title,
                    "status": "triggered",
                })

        self._db.commit()
        return results

    def _wake_agent_for_reminder(self, task: Task, target_thread_id: str):
        """委托给模块级函数，保留实例方法以兼容 poll_and_notify 内部调用。"""
        _wake_agent_for_reminder(task.id, task.title, task.task_type, target_thread_id)


def _wake_agent_for_reminder(task_id: str, task_title: str, task_type: str, target_thread_id: str):
    """在独立 DB 会话中运行 AgentGraph 处理提醒，线程安全。

    不使用任何外部传入的会话，全部使用 SessionLocal() 独立管理。
    可被 scheduler_daemon 的 ThreadPoolExecutor 安全并发调用。

    参数:
        task_id: 到期任务的 ID
        task_title: 任务标题
        target_thread_id: 目标对话线程 ID
    """
    # 1. 查找 reminder_created 事件（独立短会话）
    lookup_db = SessionLocal()
    try:
        recent_reminders = (
            lookup_db.query(Event)
            .filter(Event.event_type == "reminder_created")
            .order_by(Event.created_at.desc())
            .limit(20)
            .all()
        )
        reminder_event = next(
            (e for e in recent_reminders if (e.payload or {}).get("task_id") == task_id),
            None,
        )
        reminder_id = reminder_event.id if reminder_event else ""
    finally:
        lookup_db.close()

    # 2. AgentGraph 运行（独立会话）
    try:
        from aiive.core.llm_client import default_llm_client
        from aiive.runtime.agent_graph import AgentGraph, RuntimeEvent
        from aiive.runtime.thread_bootstrap import ThreadBootstrapService
        from aiive.api.ws_manager import ws_manager

        ThreadBootstrapService.ensure_committed_thread(target_thread_id)
        client = default_llm_client()

        agent_db = SessionLocal()
        try:
            graph = AgentGraph(client, agent_db)
            event = RuntimeEvent(
                event_type="reminder",
                reminder_id=reminder_id,
                content=task_title,
                required_backend_action="remind_alert",
                source="scheduler",
            )
            result = graph.run_runtime_event(event, target_thread_id)
            reply_text = result.get("reply", "")
            agent_db.commit()
            logger.info(
                "提醒 Agent 唤醒成功: task_id=%s title=%s thread_id=%s",
                task_id, task_title, target_thread_id,
            )

            ws_manager.broadcast_to_thread_sync(
                target_thread_id,
                "new_message",
                {
                    "reply": reply_text,
                    "thread_id": result.get("thread_id", target_thread_id),
                    "trace_id": result.get("trace_id", ""),
                    "action_cards": result.get("action_cards", []),
                },
            )
        finally:
            agent_db.close()
    except Exception:
        logger.exception(
            "提醒 Agent 唤醒失败，回退为 notification_created: task_id=%s title=%s",
            task_id, task_title,
        )
        fallback_db = SessionLocal()
        try:
            EventLogger(fallback_db).log_event(
                trace_id=task_id,
                thread_id=target_thread_id,
                event_type="notification_created",
                payload={
                    "task_id": task_id,
                    "task_type": task_type,
                    "title": task_title,
                    "message": f"提醒: {task_title}",
                    "status": "alerting",
                },
            )
            fallback_db.commit()
        except Exception:
            logger.exception("回退事件写入失败")
        finally:
            fallback_db.close()


def run_once():
    """手动执行一次任务轮询（用于命令行调试或测试）。"""
    db = SessionLocal()
    try:
        worker = TaskWorker(db)
        results = worker.poll_and_notify()
        for r in results:
            print(f"[TASK] Triggered: {r['title'][:50]}")
        return results
    finally:
        db.close()
