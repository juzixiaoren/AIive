"""Phase 4 记忆访问追踪器（MemoryAccessTracker，第 6/9 点）。

只 touch **实际注入上下文**的 memory id（非全部召回候选）；批量短事务、
最小 touch 间隔去重；使用专用 SQL
`SET last_accessed_at=:ts, updated_at=updated_at`（显式保持 `updated_at` 不变，
绕过 ORM `onupdate`）——因此 touch **不进入 `updated_at` dirty-set**、**不 bump
`record_version`**、**不触发投影**、失败仅记日志不阻断 Turn。

sleeping 记忆被实际注入上下文时，best-effort 走 `MemoryLifecycleService.wake()`
（wake 是生命周期动作，单独 bump version + 投影）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import bindparam, text

from aiive.db.base import SessionLocal
from aiive.db.models import MemoryRecord
from aiive.memory.memory_lifecycle_service import MemoryLifecycleService
from aiive.memory.memory_types import LifecycleState
from aiive.memory.recall_config import MaintenanceConfig

logger = logging.getLogger(__name__)


class MemoryAccessTracker:
    """记忆访问追踪：仅触达实际注入上下文的记忆。"""

    def __init__(self, config: MaintenanceConfig | None = None) -> None:
        self._cfg: MaintenanceConfig = config or MaintenanceConfig()

    def touch(self, memory_ids: list[str], now: datetime | None = None) -> None:
        """Touch 实际注入上下文的 memory id 列表。

        独立短事务，失败不阻断调用方。sleeping 记忆另走 wake。
        """
        now = now or datetime.now(timezone.utc)
        ids = list(dict.fromkeys(memory_ids))  # 去重，保序
        if not ids:
            return
        try:
            db = SessionLocal()
            rows = (
                db.query(
                    MemoryRecord.id,
                    MemoryRecord.lifecycle_state,
                    MemoryRecord.last_accessed_at,
                )
                .filter(MemoryRecord.id.in_(ids))
                .all()
            )
            by_id = {r.id: r for r in rows}

            # 1) sleeping → best-effort wake（生命周期动作，单独处理）
            for r in rows:
                if r.lifecycle_state == LifecycleState.SLEEPING.value:
                    try:
                        MemoryLifecycleService(db).wake(r.id)
                    except Exception:
                        logger.exception("AccessTracker wake 失败: memory=%s", r.id)

            # 2) 普通 touch：间隔去重 + 专用 SQL（不碰 updated_at / record_version）
            to_touch: list[str] = []
            for mid in ids:
                r = by_id.get(mid)
                if r is None or r.lifecycle_state == LifecycleState.SLEEPING.value:
                    continue
                la = r.last_accessed_at
                if (
                    la is not None
                    and (now - la).total_seconds() < self._cfg.min_access_touch_interval_seconds
                ):
                    continue
                to_touch.append(mid)

            if to_touch:
                sql = "UPDATE memory_records SET last_accessed_at=:ts, updated_at=updated_at WHERE id IN :ids"
                stmt = text(sql).bindparams(bindparam("ids", expanding=True))
                db.execute(stmt, {"ts": now, "ids": to_touch})

            db.commit()
        except Exception:
            logger.exception("MemoryAccessTracker.touch 异常")
