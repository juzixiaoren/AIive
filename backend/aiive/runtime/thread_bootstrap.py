"""
运行时层 - Thread 启动引导服务。

确保 Thread 在工具执行前已提交到数据库，使得 _db_handler 装饰器创建的
独立 DB 会话能通过 FK 约束校验。
"""
import logging
import uuid
from datetime import datetime, timezone

from aiive.db.base import SessionLocal
from aiive.db.models import Thread

logger = logging.getLogger(__name__)

# 系统专用线程 ID，用于记录系统级事件（如提醒触发、通知等）
SYSTEM_THREAD_ID = "system"


class ThreadBootstrapService:
    """Thread 生命周期管理：在独立短事务中创建/验证并提交 Thread。

    与 ThreadState 不同，本服务只负责确保 Thread 行已 committed，
    不参与 AgentLoop 主会话的事务管理。
    """

    @staticmethod
    def ensure_committed_thread(thread_id: str | None) -> str:
        """确保指定 thread 已提交到数据库，对独立 DB 会话可见。

        对于已有 thread_id: 验证存在性，不存在则报错。
        对于新建 thread: 创建并 commit，返回新 ID。

        Args:
            thread_id: 线程 ID，None 则创建新线程

        Returns:
            已 committed 的 thread_id

        Raises:
            ValueError: 传入的 thread_id 在数据库中不存在
        """
        db = SessionLocal()
        try:
            if thread_id:
                existing = db.get(Thread, thread_id)
                if existing:
                    return thread_id
                raise ValueError(f"Thread {thread_id} 不存在")
            new_id = str(uuid.uuid4())
            thread = Thread(
                id=new_id,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            db.add(thread)
            db.commit()
            logger.info("已创建并提交 thread: %s", new_id)
            return new_id
        except ValueError:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            logger.exception("ensure_committed_thread 失败: thread_id=%s", thread_id)
            raise
        finally:
            db.close()

    @staticmethod
    def ensure_system_thread() -> str:
        """确保系统线程存在（如不存在则创建并提交）。

        用于启动时初始化，以及 TaskWorker 等系统级事件的 thread 关联。

        Returns:
            "system" (系统线程 ID)
        """
        db = SessionLocal()
        try:
            existing = db.get(Thread, SYSTEM_THREAD_ID)
            if existing is None:
                system_thread = Thread(
                    id=SYSTEM_THREAD_ID,
                    title="System Events",
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
                db.add(system_thread)
                db.commit()
                logger.info("已创建系统线程 system")
        except Exception:
            db.rollback()
            logger.exception("系统线程创建失败")
        finally:
            db.close()
        return SYSTEM_THREAD_ID
