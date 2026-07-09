"""V20 MemoryWriteService：统一的记忆写入管线。

记忆系统是 AIive 的核心组件，负责 Agent 的长期记忆管理和检索。
本模块是所有记忆写入的唯一入口——确保每个写入操作都经过 MemoryGate 决策，
并记录完整的审计事件。

重要：所有生产环境中的记忆写入必须通过本服务。禁止直接调用
MemoryStore.create() 或 MemoryStore.supersede()。
"""
import logging

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from aiive.memory.memory_gate import MemoryGateDecision
from aiive.memory.memory_store import MemoryStore
from aiive.runtime.event_logger import EventLogger

logger = logging.getLogger(__name__)


class MemoryWriteService:
    """记忆写入服务：根据 MemoryGate 的准入决策执行实际写入操作。

    统一处理三种决策结果：
    - reject: 记录拒绝日志，不写入
    - candidate: 以候选状态写入，等待后续确认
    - active: 根据 update_mode 执行 create/upsert/supersede
    """

    def __init__(self, db: Session):
        """初始化记忆写入服务。

        Args:
            db: 数据库会话。
        """
        self._db = db
        self._store = MemoryStore(db)
        self._logger = EventLogger(db)

    def write(self, decision: MemoryGateDecision, content: str, thread_id: str = "") -> dict:
        """执行 MemoryGate 的准入决策，返回可观察的结果。

        根据 decision.decision 的值，分三个分支处理：
        1. reject: 记录 memory.rejected 事件，返回 written=False
        2. candidate: 创建候选状态记忆，记录 memory.candidate_created 事件
        3. active: 按 update_mode 分三种模式执行：
           - supersede: 替代旧记忆，记录 superseded + created 事件
           - upsert: 先停用同键旧记录再创建新记录，记录 created 事件
           - create: 直接创建，记录 created 事件

        Args:
            decision: MemoryGate 的准入决策结果。
            content: 要写入的记忆内容文本。
            thread_id: 当前线程 ID，用于事件日志关联（必须为有效的线程 ID）。

        Returns:
            包含 written（是否写入）、state（状态）、memory_id 等字段的字典。
        """
        # 防护：空的 thread_id 会违反数据库外键约束
        tid = thread_id or ""

        try:
            # 分支 1：拒绝写入
            if decision.decision == "reject":
                if tid:
                    self._logger.log_event(
                        trace_id=decision.trace_id or "memory",
                        thread_id=tid,
                        event_type="memory.rejected",
                        payload={
                            "reason": decision.reason,
                            "blocked_reason": decision.blocked_reason,
                            "content_preview": content[:200],
                        },
                    )
                return {"written": False, "reason": decision.reason}

            # 分支 2：候选队列写入（待后续确认）
            if decision.decision == "candidate":
                record = self._store.create(
                    content=content,
                    memory_type=decision.memory_type or "fact",
                    lifecycle_state="candidate",
                    confidence=decision.confidence,
                    memory_key=decision.memory_key,
                    lineage=f"trace:{decision.trace_id}",
                )
                if tid:
                    self._logger.log_event(
                        trace_id=decision.trace_id or "memory",
                        thread_id=tid,
                        event_type="memory.candidate_created",
                        payload={
                            "memory_id": record.id,
                            "memory_key": decision.memory_key,
                            "confidence": decision.confidence,
                        },
                    )
                return {"written": True, "state": "candidate", "memory_id": record.id}

            # 分支 3：活跃记录写入
            # ---- 模式 A：替代旧记录 ----
            if decision.update_mode == "supersede" and decision.supersede_memory_ids:
                old_id = decision.supersede_memory_ids[0]
                new_rec = self._store.supersede(old_id, content)
                if tid:
                    self._logger.log_event(
                        trace_id=decision.trace_id or "memory",
                        thread_id=tid,
                        event_type="memory.superseded",
                        payload={
                            "old_memory_id": old_id,
                            "new_memory_id": new_rec.id if new_rec else "",
                            "memory_key": decision.memory_key,
                            "reason": decision.reason,
                        },
                    )
                    self._logger.log_event(
                        trace_id=decision.trace_id or "memory",
                        thread_id=tid,
                        event_type="memory.created",
                        payload={
                            "memory_id": new_rec.id if new_rec else "",
                            "memory_key": decision.memory_key,
                            "memory_type": decision.memory_type,
                            "update_mode": "supersede",
                        },
                    )
                return {
                    "written": True, "state": "active",
                    "memory_id": new_rec.id if new_rec else "",
                    "superseded_old": True,
                }

            # ---- 模式 B：插入或更新（upsert） ----
            # 查找并停用所有同键的旧活跃记录
            if decision.update_mode == "upsert":
                for old in self._store.get_active():
                    if old.memory_key == decision.memory_key:
                        old.lifecycle_state = "superseded"
                        old.superseded_by = "pending"
                        old.updated_at = datetime.now(timezone.utc)

                record = self._store.create(
                    content=content,
                    memory_type=decision.memory_type or "fact",
                    lifecycle_state="active",
                    confidence=decision.confidence,
                    memory_key=decision.memory_key,
                    lineage=f"trace:{decision.trace_id}",
                )
                if tid:
                    self._logger.log_event(
                        trace_id=decision.trace_id or "memory",
                        thread_id=tid,
                        event_type="memory.created",
                        payload={
                            "memory_id": record.id,
                            "memory_key": decision.memory_key,
                            "memory_type": decision.memory_type,
                            "update_mode": "upsert",
                        },
                    )
                return {"written": True, "state": "active", "memory_id": record.id}

            # ---- 模式 C：直接创建 ----
            record = self._store.create(
                content=content,
                memory_type=decision.memory_type or "fact",
                lifecycle_state="active",
                confidence=decision.confidence,
                memory_key=decision.memory_key,
                lineage=f"trace:{decision.trace_id}",
            )
            if tid:
                self._logger.log_event(
                    trace_id=decision.trace_id or "memory",
                    thread_id=tid,
                    event_type="memory.created",
                    payload={
                        "memory_id": record.id,
                        "memory_key": decision.memory_key,
                        "memory_type": decision.memory_type,
                        "update_mode": "create",
                    },
                )
            return {"written": True, "state": "active", "memory_id": record.id}
        except Exception:
            logger.exception("记忆写入失败: decision=%s", decision.decision)
            raise
