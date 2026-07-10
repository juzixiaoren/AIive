"""V20 MemoryMaintenance：记忆生命周期维护服务。

记忆系统是 AIive 的核心组件，负责 Agent 的长期记忆管理和检索。
本模块负责记忆记录的注销（forget）、休眠（sleep）、归档（archive）和
自动扫描（scan）等生命周期管理操作。

- forget: 永久删除记忆，用墓碑值替换原内容并记录 ForgetRequest 审计日志
- sleep: 将非固定记忆设为休眠状态，暂时不参与上下文构建
- archive: 将记忆归档，长期不活跃时可触发清理
- scan: 扫描活跃和候选记忆，找出应休眠或归档的记录
"""
import logging

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from aiive.db.models import ForgetRequest, MemoryRecord
from typing import Any

logger = logging.getLogger(__name__)


class MemoryMaintenance:
    """记忆维护服务：管理记忆记录的完整生命周期。

    提供遗忘、休眠、归档和扫描功能，确保记忆库的健康运行。
    """

    def __init__(self, db: Session):
        """初始化记忆维护服务。

        Args:
            db: 数据库会话。
        """
        self._db: Session = db

    def forget(self, memory_id: str, reason: str = "") -> dict[str, Any]:
        """永久删除（遗忘）指定记忆。

        将记忆状态设为 forgotten，用墓碑值替换内容，并创建 ForgetRequest 审计记录。
        注意：此操作不可逆，内容将无法恢复。

        Args:
            memory_id: 要遗忘的记忆 ID。
            reason: 遗忘原因。

        Returns:
            包含 ok 状态和 memory_id 的字典。
        """
        try:
            logger.info("[TRACE:forget] memory_id=%s reason=%s", memory_id[:16], reason)
            record = self._db.get(MemoryRecord, memory_id)
            if not record:
                logger.warning("[TRACE:forget] memory_id=%s NOT FOUND (lifecycle_state may not be 'active')", memory_id[:16])
                return {"ok": False, "error": "Memory not found"}

            logger.info(
                "[TRACE:forget] found record id=%s type=%s state=%s content=%s",
                memory_id[:16], record.memory_type, record.lifecycle_state, record.content[:80],
            )

            # 生成墓碑值：包含记忆 ID 前缀和当前时间
            tombstone = f"forgotten:{memory_id[:8]}:{datetime.now(timezone.utc).isoformat()}"
            record.lifecycle_state = "forgotten"
            record.content = tombstone

            # 创建审计记录
            fr = ForgetRequest(
                memory_id=memory_id,
                reason=reason,
                tombstone=tombstone,
            )
            self._db.add(fr)
            logger.info("[TRACE:forget] SUCCESS memory_id=%s tombstone=%s", memory_id[:16], tombstone[:50])
            return {"ok": True, "memory_id": memory_id, "tombstone": tombstone}
        except Exception:
            logger.exception("[TRACE:forget] FAILED memory_id=%s", memory_id[:16])
            raise

    def sleep(self, memory_id: str) -> dict[str, Any]:
        """将指定记忆设为休眠状态。

        休眠的记忆暂时不参与上下文构建，但不会被删除。
        固定的记忆（pinned）不能被休眠。

        Args:
            memory_id: 要休眠的记忆 ID。

        Returns:
            包含 ok 状态和 memory_id 的字典。
        """
        try:
            record = self._db.get(MemoryRecord, memory_id)
            if not record:
                return {"ok": False, "error": "Memory not found"}
            if record.pinned:
                return {"ok": False, "error": "Cannot sleep pinned memory"}
            record.lifecycle_state = "sleep"
            return {"ok": True, "memory_id": memory_id}
        except Exception:
            logger.exception("记忆休眠操作失败: memory_id=%s", memory_id)
            raise

    def archive(self, memory_id: str) -> dict[str, Any]:
        """将指定记忆归档。

        归档的记忆表示长期不需要，可在清理策略中被正式删除。

        Args:
            memory_id: 要归档的记忆 ID。

        Returns:
            包含 ok 状态和 memory_id 的字典。
        """
        try:
            record = self._db.get(MemoryRecord, memory_id)
            if not record:
                return {"ok": False, "error": "Memory not found"}
            record.lifecycle_state = "archive"
            return {"ok": True, "memory_id": memory_id}
        except Exception:
            logger.exception("记忆归档操作失败: memory_id=%s", memory_id)
            raise

    def scan(self) -> dict[str, Any]:
        """扫描所有活跃和候选记忆，找出应休眠或归档的记录。

        扫描规则：
        - 置信度低于 0.3 且创建超过 30 天的 → 归档候选
        - 候选状态超过 7 天仍未激活的 → 休眠候选
        - 固定（pinned）记忆始终跳过

        Returns:
            包含各类型候选数量及 ID 列表的字典。
        """
        try:
            records = (
                self._db.query(MemoryRecord)
                .filter(MemoryRecord.lifecycle_state.in_(["active", "candidate"]))
                .all()
            )
            sleep_candidates = []
            archive_candidates = []

            for r in records:
                if r.pinned:
                    continue  # 固定记忆不受自动扫描影响
                # 确保时区感知的比较
                created = r.created_at.replace(tzinfo=timezone.utc) if r.created_at.tzinfo is None else r.created_at
                age_days = (datetime.now(timezone.utc) - created).days
                if r.confidence < 0.3 and age_days > 30:
                    archive_candidates.append(r.id)
                elif r.lifecycle_state == "candidate" and age_days > 7:
                    sleep_candidates.append(r.id)

            return {
                "total_active": len(records),
                "sleep_candidates": len(sleep_candidates),
                "archive_candidates": len(archive_candidates),
                "sleep_ids": sleep_candidates,
                "archive_ids": archive_candidates,
            }
        except Exception:
            logger.exception("记忆维护扫描失败")
            raise
