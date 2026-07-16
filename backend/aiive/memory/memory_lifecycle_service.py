"""Phase 4 记忆生命周期服务（MemoryLifecycleService）。

依赖 `MemoryMutationExecutor` + `MemoryStore`（仅读），提供确定性生命周期动作：
`promote` / `sleep` / `wake` / `archive` / `merge_exact_duplicate` / `supersede` / `no_op`。

- 每个动作带 precondition 校验；复合动作（merge）按 id 升序锁全部参与记录；
- `wake` 必须 bump `record_version`、更新 `updated_at`、写 lineage/event、刷新投影（J.4）；
- exact duplicate merge 的 winner 选择由 planner 决定，本服务只执行；
- 每条成功动作在「同一事务」内 `enqueue core_memory_refresh`（J.5）；
- **不得反向依赖 `MemoryWriteService`**（第 7 点）。
"""
from __future__ import annotations

import logging
import uuid as _uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from aiive.db.models import MemoryMaintenanceAction, MemoryRecord
from aiive.memory.memory_mutation import MemoryMutationExecutor, hashes_for_record
from aiive.memory.memory_types import LifecycleState, LineageOperation, ValidityState

logger = logging.getLogger(__name__)


class MemoryLifecycleService:
    """维护生命周期动作执行器（确定性、无 LLM）。"""

    def __init__(
        self,
        db: Session,
        executor: MemoryMutationExecutor | None = None,
        registry: Any = None,
    ) -> None:
        self._db: Session = db
        self._executor: MemoryMutationExecutor = executor or MemoryMutationExecutor(
            db, registry=registry
        )

    # ------------------------------------------------------------------
    # 主入口：消费一个已持久化的维护动作
    # ------------------------------------------------------------------

    def apply_maintenance_action(
        self, action: MemoryMaintenanceAction, trace_id: str = "",
    ) -> str:
        """执行单个维护动作。

        返回：`applied` / `skip_stale` / `no_op`。
        调用方（Outbox handler）负责把 `skip_stale` 传播到同一 operation_group 的全部动作。
        """
        action_type = action.action_type
        if action_type == "no_op":
            return "no_op"

        participant_ids = [action.subject_memory_record_id]
        if action.related_record_ids:
            participant_ids.extend(action.related_record_ids)
        participant_ids = [p for p in participant_ids if p]

        locked: dict[str, MemoryRecord] = self._executor.lock_records(participant_ids)

        preconditions: dict[str, Any] = action.preconditions or {}
        enforce_decision = action_type in ("sleep", "wake")
        for pid in participant_ids:
            rec = locked.get(pid)
            entry = preconditions.get(pid) or {}
            if rec is None or not entry:
                return "skip_stale"
            expected_version = entry.get("record_version", action.expected_record_version)
            if not self._executor.check_preconditions(
                rec, expected_version, entry, enforce_decision
            ):
                return "skip_stale"

        # 全部参与记录 precondition 通过 → 执行
        self._apply(action, locked, trace_id)
        return "applied"

    # ------------------------------------------------------------------
    # 动作实现
    # ------------------------------------------------------------------

    def _apply(
        self,
        action: MemoryMaintenanceAction,
        locked: dict[str, MemoryRecord],
        trace_id: str,
    ) -> None:
        if not action.subject_memory_record_id:
            return
        subject = locked.get(action.subject_memory_record_id)
        if subject is None:
            return
        now = datetime.now(timezone.utc)

        if action.action_type == "promote":
            subject.lifecycle_state = LifecycleState.ACTIVE.value
            subject.confidence = min(1.0, (subject.confidence or 0.5) + 0.1)
            subject.record_version += 1
            subject.updated_at = now
            self._executor.write_lineage(
                subject.id, subject.id, LineageOperation.PROMOTE.value,
                action.reason_code or "candidate_promoted",
            )
            self._executor.log_event(trace_id, "memory.promoted", subject.id)
            self._executor.enqueue_core_refresh(subject)

        elif action.action_type in ("archive_expired", "archive_candidate"):
            subject.lifecycle_state = LifecycleState.ARCHIVED.value
            subject.validity_state = "expired"
            subject.record_version += 1
            subject.updated_at = now
            self._executor.write_lineage(
                subject.id, subject.id, LineageOperation.ARCHIVE.value,
                action.reason_code or "archived",
            )
            self._executor.log_event(trace_id, "memory.archived", subject.id)
            self._executor.enqueue_core_refresh(subject)

        elif action.action_type == "sleep":
            subject.lifecycle_state = LifecycleState.SLEEPING.value
            subject.record_version += 1
            subject.updated_at = now
            self._executor.write_lineage(
                subject.id, subject.id, LineageOperation.SLEEP.value,
                action.reason_code or "cooled_to_sleeping",
            )
            self._executor.log_event(trace_id, "memory.sleep", subject.id)
            self._executor.enqueue_core_refresh(subject)

        elif action.action_type == "merge_exact_duplicate":
            winner = locked.get(action.related_record_ids[0]) if action.related_record_ids else None
            subject.lifecycle_state = LifecycleState.ARCHIVED.value
            subject.validity_state = "superseded"
            subject.record_version += 1
            subject.updated_at = now
            self._executor.write_lineage(
                subject.id,
                winner.id if winner else subject.id,
                LineageOperation.MERGE.value,
                action.reason_code or "duplicate_merged",
            )
            self._executor.log_event(trace_id, "memory.merged", subject.id,
                                     payload={"loser_id": subject.id,
                                              "winner_id": winner.id if winner else None})
            # 仅 loser 改变生命周期，winner 不变；投影以 loser 为准重建
            self._executor.enqueue_core_refresh(subject)

        elif action.action_type == "supersede":
            # 单基数冲突：新建一条 active winner 记录（复制 winner 内容），
            # 旧记录全部置 archived + superseded（与 MemoryWriteService 的 supersede 一致）。
            winner_src = locked.get(action.subject_memory_record_id or "") or subject
            new_record = MemoryRecord(
                id=str(_uuid.uuid4()),
                memory_type=winner_src.memory_type,
                canonical_key=winner_src.canonical_key,
                cardinality=winner_src.cardinality or "single",
                scope_type=winner_src.scope_type,
                scope_id=winner_src.scope_id,
                content=winner_src.content,
                structured_value=winner_src.structured_value,
                lifecycle_state=LifecycleState.ACTIVE.value,
                validity_state=ValidityState.VALID.value,
                trust_level=winner_src.trust_level,
                stability=winner_src.stability,
                stability_score=winner_src.stability_score,
                confidence=winner_src.confidence,
                importance=winner_src.importance,
                record_version=1,
                reinforce_count=winner_src.reinforce_count or 0,
                source_event_id=winner_src.source_event_id,
                created_from=winner_src.created_from,
                supersedes=winner_src.id,
                revision_num=(winner_src.revision_num or 1),
                valid_from=now,
                valid_to=winner_src.valid_to,
                retention_policy=winner_src.retention_policy,
                pinned=bool(winner_src.pinned),
                content_hash=winner_src.content_hash,
                structured_value_hash=winner_src.structured_value_hash,
                observed_at=now,
                created_at=now,
                updated_at=now,
            )
            self._db.add(new_record)
            self._db.flush()
            for old in locked.values():
                old.lifecycle_state = LifecycleState.ARCHIVED.value
                old.validity_state = ValidityState.SUPERSEDED.value
                old.superseded_by = new_record.id
                old.record_version += 1
                old.updated_at = now
                self._executor.write_lineage(
                    old.id, new_record.id, LineageOperation.SUPERSEDE.value,
                    action.reason_code or "single_cardinality_superseded",
                )
                self._executor.enqueue_projection(
                    old, "memory.superseded", invalidate_cache=True,
                )
            self._executor.enqueue_projection(new_record, "memory.created", invalidate_cache=True)

        # 写 after_hash（动作完成后主记录态哈希）
        try:
            action.after_hash = hashes_for_record(subject)[0]
        except Exception:
            logger.exception("after_hash 计算失败")

    # ------------------------------------------------------------------
    # AccessTracker 触发的 wake（J.4）
    # ------------------------------------------------------------------

    def wake(self, memory_id: str, trace_id: str = "", thread_id: str = "") -> bool:
        """将 sleeping 记忆唤醒为 active。返回是否实际发生唤醒。

        wake 是生命周期动作：bump `record_version`、更新 `updated_at`、
        写 lineage/event、刷新投影。与普通 touch（不 bump）区分。
        """
        locked = self._executor.lock_records([memory_id])
        record = locked.get(memory_id)
        if record is None:
            return False
        if record.lifecycle_state != LifecycleState.SLEEPING.value:
            return False
        now = datetime.now(timezone.utc)
        record.lifecycle_state = LifecycleState.ACTIVE.value
        record.record_version += 1
        record.updated_at = now
        record.observed_at = now
        self._executor.write_lineage(
            record.id, record.id, LineageOperation.WAKE.value, "woken_by_access"
        )
        self._executor.log_event(trace_id, "memory.wake", record.id, thread_id=thread_id)
        self._executor.enqueue_projection(record, "memory.wake")
        return True

    # ------------------------------------------------------------------
    # 交互式单记录生命周期动作（供 MemoryWriteService 委托，走共享 executor）
    # ------------------------------------------------------------------

    def promote(
        self, memory_id: str, trace_id: str = "", thread_id: str = "",
        reason: str = "candidate_promoted",
    ) -> tuple[bool, str]:
        """将 candidate 记忆提升为 active。

        走共享 executor：加锁 → bump `record_version` → 写 lineage/event →
        即时刷新投影（J.5）。返回 (是否执行, reason)。
        """
        locked = self._executor.lock_records([memory_id])
        record = locked.get(memory_id)
        if record is None:
            return False, "Memory not found"
        if record.lifecycle_state != LifecycleState.CANDIDATE.value:
            return False, f"Cannot promote: current state is {record.lifecycle_state}"
        now = datetime.now(timezone.utc)
        record.lifecycle_state = LifecycleState.ACTIVE.value
        record.confidence = min(1.0, (record.confidence or 0.5) + 0.1)
        record.record_version += 1
        record.observed_at = now
        record.updated_at = now
        self._executor.write_lineage(
            record.id, record.id, LineageOperation.PROMOTE.value, reason
        )
        self._executor.log_event(trace_id, "memory.promoted", record.id, thread_id=thread_id)
        self._executor.enqueue_projection(record, "memory.promoted")
        return True, reason

    def sleep(
        self, memory_id: str, trace_id: str = "", thread_id: str = "",
        reason: str = "sleep",
    ) -> tuple[bool, str]:
        """将记忆置为 sleeping。走共享 executor（bump version + lineage + 投影）。"""
        locked = self._executor.lock_records([memory_id])
        record = locked.get(memory_id)
        if record is None:
            return False, "Memory not found"
        now = datetime.now(timezone.utc)
        record.lifecycle_state = LifecycleState.SLEEPING.value
        record.record_version += 1
        record.updated_at = now
        self._executor.write_lineage(
            record.id, record.id, LineageOperation.SLEEP.value, reason
        )
        self._executor.log_event(trace_id, "memory.sleep", record.id, thread_id=thread_id)
        self._executor.enqueue_projection(record, "memory.sleep", invalidate_cache=True)
        return True, reason

    def archive(
        self, memory_id: str, trace_id: str = "", thread_id: str = "",
        reason: str = "archived",
    ) -> tuple[bool, str]:
        """将记忆归档并置 validity=expired。走共享 executor。"""
        locked = self._executor.lock_records([memory_id])
        record = locked.get(memory_id)
        if record is None:
            return False, "Memory not found"
        now = datetime.now(timezone.utc)
        record.lifecycle_state = LifecycleState.ARCHIVED.value
        record.validity_state = ValidityState.EXPIRED.value
        record.record_version += 1
        record.updated_at = now
        self._executor.write_lineage(
            record.id, record.id, LineageOperation.ARCHIVE.value, reason
        )
        self._executor.log_event(trace_id, "memory.archived", record.id, thread_id=thread_id)
        self._executor.enqueue_projection(record, "memory.archived", invalidate_cache=True)
        return True, reason
