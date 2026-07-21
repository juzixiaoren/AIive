"""Phase 4 共享底层 mutation 执行器（MemoryMutationExecutor）。

抽离「锁 + record_version 校验 + record_state_hash/decision_hash 校验 +
写 lineage + event + 投影 enqueue」为独立组件，**单向被**
`MemoryWriteService` 与 `MemoryLifecycleService` 共用：**不含业务策略**。

- 并发安全：始终按 id 升序 `SELECT FOR UPDATE` 锁定全部参与记录。
- 双哈希校验：`record_state_hash`（结构性，merge/supersede 依赖）与
  `decision_hash`（含访问时间，仅 sleep/cooling 类决策依赖）。普通 touch
  只改 `last_accessed_at`，不会令 `record_state_hash` 失效（第 6 点）。
- 校验失败抛出 `StalePlanError`，由调用方决定整组 `skip_stale`。
"""
from __future__ import annotations

import logging
import uuid as _uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from aiive.db.models import MemoryLineage, MemoryRecord, OutboxJob, Thread
from aiive.memory.maintenance_hashes import compute_hashes
from aiive.memory.memory_key_registry import MemoryKeyRegistry, get_memory_key_registry
from aiive.memory.memory_types import LifecycleState
from aiive.runtime.event_logger import EventLogger

logger = logging.getLogger(__name__)


class StalePlanError(Exception):
    """precondition 校验失败：维护计划已过期，应整组 skip_stale。"""


# 仅这些动作依赖访问时间维度（decision_hash），其余只依赖 record_state_hash。
_DECISION_DEPENDENT_ACTIONS: frozenset[str] = frozenset({
    "sleep",
    "wake",
})


def hashes_for_record(record: MemoryRecord) -> tuple[str, str]:
    """从实时 MemoryRecord 计算 (record_state_hash, decision_hash)。"""
    return compute_hashes(
        content=record.content or "",
        structured_value=record.structured_value,
        lifecycle_state=record.lifecycle_state,
        validity_state=record.validity_state,
        confidence=record.confidence or 0.5,
        importance=record.importance or 0.5,
        retention_policy=record.retention_policy or "normal",
        valid_to=record.valid_to,
        pinned=bool(record.pinned),
        stability=record.stability or "contextual",
        stability_score=record.stability_score,
        reinforce_count=record.reinforce_count or 0,
        last_reinforced_at=record.last_reinforced_at,
        last_accessed_at=record.last_accessed_at,
        observed_at=record.observed_at or record.created_at,
        created_at=record.created_at,
    )


class MemoryMutationExecutor:
    """共享底层 mutation 执行器（无业务策略）。

    被 `MemoryWriteService` 与 `MemoryLifecycleService` 单向共用。
    只负责：加锁、版本/哈希校验、写 lineage/event、enqueue 投影。
    """

    def __init__(
        self,
        db: Session,
        registry: MemoryKeyRegistry | None = None,
        logger: EventLogger | None = None,
    ) -> None:
        self._db: Session = db
        self._registry: MemoryKeyRegistry = registry or get_memory_key_registry()
        self._event_logger: EventLogger = logger or EventLogger(db)

    # ------------------------------------------------------------------
    # 加锁
    # ------------------------------------------------------------------

    def lock_records(self, record_ids: list[str]) -> dict[str, MemoryRecord]:
        """按 id 升序 `SELECT FOR UPDATE` 锁定全部参与记录。

        返回 id→record 字典；缺失记录不在字典内（调用方据此判定 stale）。
        """
        ordered = sorted(set(record_ids))
        if not ordered:
            return {}
        rows: list[MemoryRecord] = (
            self._db.query(MemoryRecord)
            .filter(MemoryRecord.id.in_(ordered))
            .with_for_update()
            .all()
        )
        return {r.id: r for r in rows}

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------

    def check_preconditions(
        self,
        record: MemoryRecord,
        expected_version: int,
        entry: dict[str, Any],
        enforce_decision_hash: bool = False,
    ) -> bool:
        """校验单条记录的 precondition。

        - `record_version` 必须相等；
        - `record_state_hash` 必须相等（结构性校验）；
        - 仅当 `enforce_decision_hash=True`（sleep/cooling 类）时校验 `decision_hash`。
        """
        if record.record_version != expected_version:
            return False
        state_hash, decision_hash = hashes_for_record(record)
        expected_state = entry.get("record_state_hash")
        if expected_state and state_hash != expected_state:
            return False
        if enforce_decision_hash:
            expected_decision = entry.get("decision_hash")
            if expected_decision and decision_hash != expected_decision:
                return False
        return True

    # ------------------------------------------------------------------
    # lineage / event / 投影
    # ------------------------------------------------------------------

    def write_lineage(
        self,
        predecessor_id: str,
        successor_id: str,
        operation: str,
        reason: str,
    ) -> None:
        """写入 MemoryLineage 谱系记录。"""
        self._db.add(MemoryLineage(
            predecessor_id=predecessor_id,
            successor_id=successor_id,
            operation=operation,
            reason=reason,
        ))

    def log_event(
        self,
        trace_id: str,
        event_type: str,
        memory_id: str,
        payload: dict[str, Any] | None = None,
        thread_id: str = "",
    ) -> None:
        """写入一条记忆生命周期事件。

        `events.thread_id` 为指向 `threads.id` 的非空外键。维护后台没有会话线程
        （thread_id 缺省为空）→ 跳过事件表，审计由 `MemoryLineage` +
        `MemoryMaintenanceAction` 承担；交互式写入路径委托而来时，仅当 `thread_id`
        对应 `Thread` 真实存在才写入。

        防事务污染：缺失的 thread 会让事件写入的 `flush()` 因外键失败，而 SQLAlchemy
        中一旦 `flush` 失败 session 即被标记为需回滚、无法用 SAVEPOINT 隔离恢复，会
        连带回滚整段外层维护事务（wake/sleep 等状态变更丢失）。因此**预检查** thread
        存在，从源头避免无效 flush，而非事后捕获异常。
        """
        if not thread_id:
            return
        # 预检查 thread 存在：避免 events.thread_id 外键失败污染外层维护事务。
        # no_autoflush 防止本次查询意外 flush 其他 pending 变更。
        with self._db.no_autoflush:
            thread = self._db.get(Thread, thread_id)
        if thread is None:
            logger.warning("事件写入跳过：thread %s 不存在，外层维护事务不受影响", thread_id)
            return
        try:
            self._event_logger.log_event(
                trace_id=trace_id,
                thread_id=thread_id,
                event_type=event_type,
                payload=payload or {"memory_id": memory_id, "operation": event_type},
            )
        except Exception:
            logger.exception("维护事件写入失败: %s", event_type)

    def enqueue_core_refresh(self, record: MemoryRecord) -> None:
        """若 canonical_key 属于 core memory key，则 enqueue core_memory_refresh。

        每条成功生命周期动作在「同一事务」内入队（J.5），由
        `handle_core_memory_refresh` + `CoreMemoryProjection.refresh` 消费。
        """
        if not record.canonical_key:
            return
        if record.canonical_key not in self._registry.get_core_memory_keys():
            return
        self._db.add(OutboxJob(
            operation_id=f"core:{record.id}:{record.record_version}:{_uuid.uuid4().hex[:8]}",
            job_type="core_memory_refresh",
            status="pending",
            payload={"memory_id": record.id, "record_version": record.record_version},
            trace_id=record.id,
            max_retries=3,
        ))

    def enqueue_projection(
        self,
        record: MemoryRecord,
        event_type: str,
        invalidate_cache: bool = False,
    ) -> None:
        """入队当前已支持的异步投影 outbox job（core / markdown / cache）。

        每条成功生命周期动作在「同一事务」内即时刷新投影（J.5）；可选投影类型受
        `get_projection_capabilities()` 的 capability flag 控制，未启用者不入队。
        投影 job 携带 `memory_id + record_version` 供消费端做 stale 检测。

        本方法是投影入队的**唯一共享入口**，`MemoryLifecycleService` 与
        `MemoryWriteService` 共用，避免出现两套投影语义。
        """
        from aiive.memory.recall_config import get_projection_capabilities

        caps = get_projection_capabilities()
        base_key = f"{record.id}:{record.record_version}"

        # core memory refresh（仅 core key，逻辑复用）
        self.enqueue_core_refresh(record)

        # 记忆向量投影：写入和下线都由同一 refresh handler 回源决定 upsert/delete。
        from aiive.config import settings
        if settings.aiive_memory_vector_enabled and record.lifecycle_state != LifecycleState.CANDIDATE.value:
            op_id = f"memory_vector_refresh:{record.id}:{record.record_version}"
            if not any(
                isinstance(obj, OutboxJob) and obj.operation_id == op_id
                for obj in self._db.new
            ) and self._db.query(OutboxJob).filter_by(operation_id=op_id).first() is None:
                self._db.add(OutboxJob(
                    operation_id=op_id,
                    job_type="memory_vector_refresh",
                    status="pending",
                    payload={
                        "schema_version": 1,
                        "memory_id": record.id,
                        "record_version": record.record_version,
                    },
                    trace_id=record.id,
                    max_retries=3,
                ))

        # 写入类事件：markdown 投影
        if caps.markdown_projection_enabled and event_type in (
            "memory.created", "memory.reinforced", "memory.merged", "memory.wake"
        ):
            self._db.add(OutboxJob(
                operation_id=f"md:{base_key}:{_uuid.uuid4().hex[:8]}",
                job_type="memory_markdown_project",
                status="pending",
                payload={"memory_id": record.id, "record_version": record.record_version},
                trace_id=record.id,
                max_retries=3,
            ))

        # 缓存失效
        if caps.cache_projection_enabled and invalidate_cache:
            self._db.add(OutboxJob(
                operation_id=f"cache:{base_key}:{_uuid.uuid4().hex[:8]}",
                job_type="memory_cache_invalidate",
                status="pending",
                payload={"memory_id": record.id, "record_version": record.record_version},
                trace_id=record.id,
                max_retries=3,
            ))

        # Phase 5: 统一检索索引刷新（memory_record）—— candidate 不进入索引
        self._enqueue_retrieval_refresh(record, event_type)

    def _enqueue_retrieval_refresh(self, record: MemoryRecord, event_type: str) -> None:
        """入队 retrieval_index_refresh（以 source + 版本为粒度幂等合并）。

        candidate 记忆尚未晋升，不索引；其余生命周期变化（含 forgotten/tombstone）
        均入队，由 handler 决定 upsert 或 tombstone。

        operation_id 含 `record_version`：每次生命周期变更都会 bump `record_version`
        （wake/archive/sleep/supersede/forget 等），因此每个版本对应独立 job。已完成的
        旧版本 job 不会阻塞新版本刷新，避免「首次刷新完成后后续变更被永久丢弃」——
        尤其保证 forgotten 的 tombstone 一定会被执行（revision 7 隐私承诺）。

        同事务幂等：pending 或已持久化中已有相同 operation_id 则跳过，合并同一版本内
        的多次变更（如 wake + reinforce 先后入队），避免 `operation_id` 唯一约束冲突
        导致 flush 失败、污染外层维护事务。
        """
        if record.lifecycle_state == LifecycleState.CANDIDATE.value:
            return
        op_id = f"retrieval_refresh:memory_record:{record.id}:{record.record_version}"
        # 同事务幂等：pending 或已持久化中已有相同 operation_id 则跳过，避免
        # wake + reinforce 对同一记录重复入队触发唯一约束冲突（wake 的事件写入
        # 会提前 flush 全部 pending，使前一条 OutboxJob 已落库）。
        for obj in self._db.new:
            if isinstance(obj, OutboxJob) and obj.operation_id == op_id:
                return
        if self._db.query(OutboxJob).filter_by(operation_id=op_id).first() is not None:
            return
        self._db.add(OutboxJob(
            operation_id=op_id,
            job_type="retrieval_index_refresh",
            status="pending",
            payload={
                "schema_version": 1,
                "source_type": "memory_record",
                "source_id": record.id,
                "event_type": event_type,
            },
            trace_id=record.id,
            max_retries=3,
        ))

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)
