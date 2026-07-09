"""
发件箱工作器：异步处理后台作业。
负责从 OutboxJob 表中取出待处理作业，匹配对应的 handler 函数执行，
支持重试机制和死信队列。
"""

from datetime import datetime, timezone
from typing import Callable

from sqlalchemy.orm import Session

from aiive.db.models import OutboxJob

MAX_RETRIES = 3


class OutboxWorker:
    """发件箱作业处理器，轮询并执行 pending 状态的 OutboxJob。

    功能：
    - 注册不同 job_type 的 handler 处理函数
    - 从数据库取出 pending 作业并执行
    - 执行成功标记为 completed，失败重试，超过最大重试次数标记为 deadletter
    - 使用 skip_locked 避免并发冲突
    """

    def __init__(self, db_session_factory: Callable[[], Session]):
        """初始化发件箱工作器。

        参数:
            db_session_factory: 数据库会话工厂函数
        """
        self._factory = db_session_factory
        self._handlers: dict[str, Callable] = {}

    def register_handler(self, job_type: str, handler: Callable) -> None:
        """注册一个作业类型的处理函数。

        参数:
            job_type: 作业类型标识符，如 "memory_extraction"
            handler: 处理函数，签名为 handler(db, payload, trace_id)
        """
        self._handlers[job_type] = handler

    def enqueue(
        self, db: Session, job_type: str, payload: dict,
        trace_id: str | None = None, operation_id: str | None = None,
    ) -> OutboxJob:
        """将新作业加入发件箱队列。

        参数:
            db: 数据库会话
            job_type: 作业类型
            payload: 作业数据负载
            trace_id: 跟踪 ID
            operation_id: 操作 ID，未提供时自动生成

        返回:
            创建的 OutboxJob 实例
        """
        import uuid

        op_id = operation_id or f"{job_type}:{uuid.uuid4()}"
        job = OutboxJob(
            operation_id=op_id,
            job_type=job_type,
            status="pending",
            payload=payload,
            trace_id=trace_id,
            max_retries=MAX_RETRIES,
        )
        db.add(job)
        return job

    def process_one(self, db: Session) -> int:
        """处理一个 pending 作业。

        使用 with_for_update(skip_locked=True) 实现乐观锁，
        避免多个工作器同时处理同一作业。

        参数:
            db: 数据库会话

        返回:
            处理的作业数量（0 或 1）
        """
        # 按创建时间升序获取第一个 pending 作业，跳过已被锁定的行
        job = (
            db.query(OutboxJob)
            .filter(OutboxJob.status == "pending")
            .order_by(OutboxJob.created_at.asc())
            .with_for_update(skip_locked=True)
            .first()
        )
        if not job:
            return 0

        handler = self._handlers.get(job.job_type)
        if not handler:
            # 未找到对应 handler，标记为死信
            job.status = "deadletter"
            job.error_message = f"No handler for job_type: {job.job_type}"
            return 1

        job.status = "running"
        db.flush()

        try:
            handler(db, job.payload, job.trace_id)
            job.status = "completed"
        except Exception as e:
            # 处理失败：重试次数 +1，超过上限则进死信
            job.retry_count += 1
            job.error_message = str(e)[:500]
            if job.retry_count >= job.max_retries:
                job.status = "deadletter"
            else:
                job.status = "pending"
        finally:
            job.updated_at = datetime.now(timezone.utc)
            db.flush()

        return 1

    def process_all(self, db: Session, max_jobs: int = 10) -> int:
        """批量处理最多 max_jobs 个 pending 作业。

        参数:
            db: 数据库会话
            max_jobs: 单次最多处理的作业数量

        返回:
            实际处理的作业数量
        """
        processed = 0
        for _ in range(max_jobs):
            if self.process_one(db) == 0:
                break
            processed += 1
        return processed
