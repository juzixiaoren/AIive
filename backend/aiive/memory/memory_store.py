"""V20 MemoryStore：记忆数据持久化层。

记忆系统是 AIive 的核心组件，负责 Agent 的长期记忆管理和检索。
本模块提供记忆记录的 CRUD 基础操作，是记忆系统中最底层的数据访问层。
上层 MemoryWriteService 和 MemoryMaintenance 通过本模块操作数据库。

重要规则：生产路径中所有记忆写入必须通过 MemoryWriteService，
不应直接调用 MemoryStore.create() 或 MemoryStore.supersede()。
"""
import logging

from datetime import datetime, timezone
from collections.abc import Sequence

from sqlalchemy.orm import Session

from aiive.db.models import MemoryRecord

logger = logging.getLogger(__name__)


class MemoryStore:
    """记忆存储层：封装 MemoryRecord 的数据库操作。

    提供创建、更新、替代、查询等基础操作。
    注意：生产环境应通过 MemoryWriteService 间接使用本类。
    """

    def __init__(self, db: Session):
        """初始化记忆存储。

        Args:
            db: 数据库会话。
        """
        self._db: Session = db

    def create(
        self,
        content: str,
        memory_type: str = "fact",
        lifecycle_state: str = "candidate",
        source_event_id: str | None = None,
        confidence: float = 0.5,
        lineage: str | None = None,
        pinned: bool = False,
        memory_key: str | None = None,
        revision_num: int = 1,
        supersedes: str | None = None,
    ) -> MemoryRecord:
        """创建一条新的记忆记录。

        Args:
            content: 记忆内容文本。
            memory_type: 记忆类型，默认为 "fact"。
            lifecycle_state: 生命周期状态，默认为 "candidate"。
            source_event_id: 源事件 ID，用于追溯。
            confidence: 置信度，0.0-1.0。
            lineage: 谱系信息，用于追踪记忆的来源和演化。
            pinned: 是否固定（固定记忆不受自动清理影响）。
            memory_key: 去重用稳定键。
            revision_num: 修订版本号。
            supersedes: 替代的目标记忆 ID。

        Returns:
            新创建的 MemoryRecord 实例。
        """
        try:
            record = MemoryRecord(
                memory_type=memory_type,
                lifecycle_state=lifecycle_state,
                content=content,
                source_event_id=source_event_id,
                confidence=confidence,
                lineage=lineage,
                pinned=pinned,
                memory_key=memory_key,
                revision_num=revision_num,
                supersedes=supersedes,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            self._db.add(record)
            self._db.flush()
            return record
        except Exception:
            logger.exception("创建记忆记录失败")
            raise

    def update_state(self, memory_id: str, lifecycle_state: str) -> MemoryRecord | None:
        """更新记忆的生命周期状态。

        Args:
            memory_id: 记忆 ID。
            lifecycle_state: 新的生命周期状态。

        Returns:
            更新后的 MemoryRecord，不存在则返回 None。
        """
        record = self._db.get(MemoryRecord, memory_id)
        if record:
            record.lifecycle_state = lifecycle_state
        return record

    def update_content(self, memory_id: str, new_content: str) -> MemoryRecord | None:
        """更新记忆的内容文本。

        Args:
            memory_id: 记忆 ID。
            new_content: 新的内容。
            reason: 更新原因。

        Returns:
            更新后的 MemoryRecord，不存在则返回 None。
        """
        record = self._db.get(MemoryRecord, memory_id)
        if record:
            record.content = new_content
            record.updated_at = datetime.now(timezone.utc)
        return record

    def supersede(self, old_id: str, new_content: str) -> MemoryRecord | None:
        """用新内容替代旧记忆。

        将旧记录标记为 superseded，创建一条新记录并建立双向关联。
        新记录继承旧记录的 memory_type、memory_key 等核心属性，
        版本号递增，置信度设为 1.0。

        Args:
            old_id: 被替代的旧记忆 ID。
            new_content: 新记忆内容。
            reason: 替代原因。

        Returns:
            新创建的 MemoryRecord，旧记录不存在则返回 None。
        """
        try:
            old = self._db.get(MemoryRecord, old_id)
            if not old:
                return None
            # 将旧记录标记为已被替代
            old.lifecycle_state = "superseded"
            old.updated_at = datetime.now(timezone.utc)
            self._db.flush()

            # 创建新记录，继承旧记录的核心属性
            new_rec = self.create(
                content=new_content,
                memory_type=old.memory_type,
                lifecycle_state="active",
                confidence=1.0,
                lineage=f"supersedes:{old_id}",
                memory_key=old.memory_key,
                revision_num=old.revision_num + 1,
                supersedes=old.id,
            )
            # 建立双向关联
            old.superseded_by = new_rec.id
            self._db.flush()
            return new_rec
        except Exception:
            logger.exception("替代记忆失败: old_id=%s", old_id)
            raise

    def get_active(self) -> Sequence[MemoryRecord]:
        """获取所有活跃状态的记忆记录，按更新时间降序排列。

        Returns:
            活跃记忆的序列。
        """
        return (
            self._db.query(MemoryRecord)
            .filter(MemoryRecord.lifecycle_state == "active")
            .order_by(MemoryRecord.updated_at.desc())
            .all()
        )

    def resolve_for_context(self) -> Sequence[MemoryRecord]:
        """获取用于上下文注入的活跃记忆列表。

        只返回活跃且未被替代的记录，供 agent_graph 构建对话上下文（Runtime Identity / User Memory 块）时使用。

        Returns:
            可用于上下文的记忆序列。
        """
        return self.get_active()

    def list_all(self) -> Sequence[MemoryRecord]:
        """列出所有记忆记录（最多 100 条），按更新时间降序排列。

        Returns:
            记忆记录的序列。
        """
        return (
            self._db.query(MemoryRecord)
            .order_by(MemoryRecord.updated_at.desc())
            .limit(100)
            .all()
        )

    def get_by_id(self, memory_id: str) -> MemoryRecord | None:
        """按 ID 获取单条记忆记录。

        Args:
            memory_id: 记忆 ID。

        Returns:
            MemoryRecord 或 None。
        """
        return self._db.get(MemoryRecord, memory_id)
