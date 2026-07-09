"""
任务工作器：轮询到期任务并唤醒 Agent 主动生成回复。
用于处理提醒等定时任务，到期后通过 Agent Loop 在原始对话线程中发布消息。
"""

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from aiive.db.base import SessionLocal
from aiive.runtime.event_logger import EventLogger
from aiive.runtime.task_manager import TaskManager

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
        self._db = db

    def poll_and_notify(self) -> list[dict]:
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
                # 选择目标线程：优先使用任务关联的线程，否则使用 system
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

                # 唤醒 Agent：由 Agent 决定如何表达主动消息
                self._wake_agent_for_reminder(task, target_thread_id)

                results.append({
                    "task_id": task.id,
                    "title": task.title,
                    "status": "triggered",
                })

        self._db.commit()
        return results

    def _wake_agent_for_reminder(self, task, target_thread_id: str):
        """调用 Agent Loop 处理提醒，让 Agent 在对话线程中主动回复。

        构造系统提示词，要求 Agent 先调用 remind_alert 工具激活提醒，
        再以自然语言告知用户。成功后通过 WebSocket 推送到前端。

        参数:
            task: 到期任务对象
            target_thread_id: 目标对话线程 ID
        """
        try:
            from aiive.core.llm_client import default_llm_client
            from aiive.runtime.agent_loop import AgentLoop
            from aiive.db.models import Event
            client = default_llm_client()
            recent_reminders = (
                self._db.query(Event)
                .filter(Event.event_type == "reminder_created")
                .order_by(Event.created_at.desc())
                .limit(20)
                .all()
            )
            reminder_event = next(
                (e for e in recent_reminders if (e.payload or {}).get("task_id") == task.id),
                None,
            )
            reminder_id = reminder_event.id if reminder_event else ""

            loop = AgentLoop(client, self._db)
            prompt = (
                f"[System Reminder — You MUST call the remind_alert tool with the reminder_id below. "
                f"Do NOT just say '好的' or confirm receipt. "
                f"Call remind_alert first, then deliver the reminder naturally.]\n\n"
                f"A scheduled reminder is now due: \"{task.title}\".\n"
                f"reminder_id: {reminder_id}\n\n"
                f"First call remind_alert(reminder_id=\"{reminder_id}\") to activate the alert, "
                f"then inform the user naturally."
            )
            result = loop.run(message=prompt, thread_id=target_thread_id)
            logger.info(
                "提醒 Agent 唤醒成功: task_id=%s title=%s thread_id=%s",
                task.id, task.title, target_thread_id,
            )

            # 通过 WebSocket 推送 LLM 回复 + action_cards 到前端
            import asyncio
            from aiive.api.ws_manager import ws_manager
            try:
                asyncio.run(ws_manager.broadcast_to_thread(
                    target_thread_id,
                    "new_message",
                    {
                        "reply": result.get("reply", ""),
                        "thread_id": result.get("thread_id", target_thread_id),
                        "trace_id": result.get("trace_id", ""),
                        "action_cards": result.get("action_cards", []),
                    },
                ))
            except Exception:
                logger.exception(
                    "提醒 WebSocket 推送失败: task_id=%s thread_id=%s",
                    task.id, target_thread_id,
                )
        except Exception:
            logger.exception(
                "提醒 Agent 唤醒失败，回退为 notification_created: task_id=%s title=%s",
                task.id, task.title,
            )
            event_logger = EventLogger(self._db)
            event_logger.log_event(
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
            # 回退情况下也推送到 WebSocket
            try:
                import asyncio
                from aiive.api.ws_manager import ws_manager
                asyncio.run(ws_manager.broadcast_to_thread(
                    target_thread_id,
                    "new_message",
                    {
                        "reply": f"⏰ 提醒: {task.title}（通知已记录，请在通知页查看）",
                        "thread_id": target_thread_id,
                        "action_cards": [],
                    },
                ))
            except Exception:
                logger.exception(
                    "提醒回退 WebSocket 推送也失败: task_id=%s thread_id=%s",
                    task.id, target_thread_id,
                )


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
