"""Phase 4 维护计划生成器（纯函数、无 DB、无 LLM，第 2/3/4/6/7 点）。

输入为某 Batch 已冻结的 `FrozenInput` 快照集合，输出确定性 `ActionSpec` 列表 +
`input_hash` + `plan_hash`。同一输入集合重算得同一结果，因此崩溃后 B2 可持久化
完全相同的 ActionPlan。

关键确定性保证：
- Action 排序不依赖数据库返回顺序（按 group_key → action_type → subject 排序）。
- `operation_group_id = sha256(batch_id + action_type + sorted(participant_ids) + policy_version)`。
- `input_hash = hash(canonical inputs)`，`plan_hash = hash(input_hash + canonical actions + policy_version)`。
- exact duplicate merge winner 选择固定（effective_pinned > user_required > lifecycle > confidence > reinforce_count > observed_at > id）。
- 重复组存在 >=2 个 effective_pinned → 整体 `no_op(pinned_conflict)`，禁止自动 merge。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .maintenance_hashes import (
    decision_hash,
    effective_last_accessed_at,
    record_state_hash,
)
from aiive.memory.memory_key_registry import get_memory_key_registry


@dataclass
class FrozenInput:
    """Phase A 冻结的单个候选记忆快照（与 MemoryMaintenanceInput 字段对应）。"""

    input_id: str
    input_sequence: int
    memory_record_id: str
    input_role: str
    record_version: int
    canonical_key: str | None
    scope_type: str | None
    scope_id: str | None
    user_required_protected: bool
    user_required_source_ids: list[str]
    # 结构化快照字段
    content: str
    structured_value: dict[str, Any] | None
    content_hash: str | None
    structured_value_hash: str | None
    lifecycle_state: str
    validity_state: str
    confidence: float
    importance: float
    retention_policy: str
    valid_to: datetime | None
    pinned: bool
    stability: str
    stability_score: float | None
    reinforce_count: int
    last_reinforced_at: datetime | None
    observed_at: datetime
    created_at: datetime
    last_accessed_at: datetime | None
    evidence_count: int


@dataclass
class ActionSpec:
    """确定性生成的维护动作规格（尚未持久化）。"""

    action_type: str
    subject_memory_record_id: str
    related_record_ids: list[str]
    source_input_ids: list[str]
    canonical_key: str | None
    scope_type: str | None
    scope_id: str | None
    reason_code: str | None
    expected_record_version: int
    preconditions: dict[str, Any]
    details: dict[str, Any] = field(default_factory=dict)
    participant_ids: list[str] = field(default_factory=list)
    # 由 plan_batch 填充
    action_sequence: int = 0
    operation_group_id: str | None = None


_LIFECYCLE_PRIORITY = {"active": 3, "sleeping": 2, "candidate": 1}

# 维护模块禁止自动 supersede 的记忆类型（与 MemoryMaintenance 模块约束一致：
# 无可信用户证据时不得 create/revise/supersede user_profile / policy / agent_self）。
_PROTECTED_TYPES = frozenset({"user_profile", "policy", "agent_self"})


def effective_pinned(inp: FrozenInput) -> bool:
    """effective_pinned = pinned OR retention_policy=='pinned'。"""
    return inp.pinned or inp.retention_policy == "pinned"


def _group_key(inp: FrozenInput) -> tuple[str, str | None, str | None]:
    return (inp.canonical_key or "", inp.scope_type, inp.scope_id)


def _record_state_hash(inp: FrozenInput) -> str:
    return record_state_hash(
        content=inp.content,
        structured_value=inp.structured_value,
        lifecycle_state=inp.lifecycle_state,
        validity_state=inp.validity_state,
        confidence=inp.confidence,
        importance=inp.importance,
        retention_policy=inp.retention_policy,
        valid_to=inp.valid_to,
        pinned=inp.pinned,
        stability=inp.stability,
        stability_score=inp.stability_score,
        reinforce_count=inp.reinforce_count,
        last_reinforced_at=inp.last_reinforced_at,
    )


def _decision_hash(inp: FrozenInput) -> str:
    eff = effective_last_accessed_at(inp.last_accessed_at, inp.observed_at, inp.created_at)
    return decision_hash(_record_state_hash(inp), inp.last_accessed_at, eff)


def _preconditions_for(inp: FrozenInput) -> dict[str, Any]:
    return {
        inp.memory_record_id: {
            "record_version": inp.record_version,
            "record_state_hash": _record_state_hash(inp),
            "decision_hash": _decision_hash(inp),
        }
    }


def _merge_winner(candidates: list[FrozenInput]) -> FrozenInput:
    """确定性 winner 选择（第 11 点）：不依赖数据库返回顺序。

    优先级元组取最大值（effective_pinned > user_required > lifecycle >
    confidence > reinforce_count > observed_at）；全平（含 observed_at）时，
    取 `memory_record_id` 字典序最小者（稳定 tie-breaker，与设计 G 节 #50 一致）。
    """
    def _key(m: FrozenInput) -> tuple[int, int, int, float, int, datetime]:
        return (
            1 if effective_pinned(m) else 0,
            1 if m.user_required_protected else 0,
            _LIFECYCLE_PRIORITY.get(m.lifecycle_state, 0),
            m.confidence,
            m.reinforce_count,
            m.observed_at,
        )

    best = max(candidates, key=_key)
    tied = [m for m in candidates if _key(m) == _key(best)]
    if len(tied) == 1:
        return best
    return min(tied, key=lambda m: m.memory_record_id)


def _is_single_cardinality_conflict(group: list[FrozenInput]) -> bool:
    """判定同组记录是否构成「单基数内容冲突」，需走 supersede 维护（L 节第 4 点）。

    条件：
    - 存在 canonical_key 且注册为 single cardinality；
    - 记忆类型不在受保护集合（user_profile/policy/agent_self）内；
    - 组内无 effective_pinned 记录（pinned 禁止 supersede removal）；
    - 至少两条记录内容不同（content_hash / structured_value_hash 不等）。
    """
    ck = group[0].canonical_key
    if not ck:
        return False
    registry = get_memory_key_registry()
    if not registry.is_single_cardinality(ck):
        return False
    spec = registry.resolve(ck)
    if spec is not None and spec.memory_type in _PROTECTED_TYPES:
        return False
    if any(effective_pinned(m) for m in group):
        return False
    distinct: set[str] = set()
    for m in group:
        h = m.content_hash or m.structured_value_hash
        distinct.add(h if h else "content:" + (m.content or ""))
    return len(group) >= 2 and len(distinct) >= 2


def _plan_group(group: list[FrozenInput], now: datetime, config: Any) -> list[ActionSpec]:
    """对同 (canonical_key, scope_type, scope_id) 的候选生成动作。"""
    actions: list[ActionSpec] = []
    records = sorted(group, key=lambda m: m.memory_record_id)

    # ── 单基数冲突维护：复用 ConflictResolver 的 latest_value_wins → supersede ──
    # 同 canonical_key 的 single 键存在「内容不同」的多条记录时，保留 winner 内容到
    # 一条新建 active 记录，其余全部置 archived + superseded（L 节第 4 点）。
    # protected/pinned 记录不自动处理；相同 hash 的精确重复走下方 merge_exact_duplicate。
    if _is_single_cardinality_conflict(records):
        winner = _merge_winner(records)
        losers = [m for m in records if m.memory_record_id != winner.memory_record_id]
        preconditions: dict[str, Any] = {}
        for m in records:
            preconditions.update(_preconditions_for(m))
        participant_ids = sorted(
            [winner.memory_record_id] + [l.memory_record_id for l in losers]
        )
        actions.append(ActionSpec(
            action_type="supersede",
            subject_memory_record_id=winner.memory_record_id,
            related_record_ids=[l.memory_record_id for l in losers],
            source_input_ids=[winner.input_id],
            canonical_key=winner.canonical_key,
            scope_type=winner.scope_type,
            scope_id=winner.scope_id,
            reason_code="single_cardinality_superseded",
            expected_record_version=winner.record_version,
            preconditions=preconditions,
            details={
                "winner_id": winner.memory_record_id,
                "superseded_ids": [l.memory_record_id for l in losers],
            },
            participant_ids=participant_ids,
        ))
        return actions

    # 重复邻域：相同 content_hash 或 structured_value_hash
    hash_buckets: dict[str, list[FrozenInput]] = {}
    for m in records:
        h = m.content_hash or m.structured_value_hash
        if not h:
            continue
        hash_buckets.setdefault(h, []).append(m)
    for h, bucket in hash_buckets.items():
        if len(bucket) < 2:
            continue
        pinned_count = sum(1 for m in bucket if effective_pinned(m))
        if pinned_count >= 2:
            # 两个及以上 pinned：禁止自动 merge（第 7 点）
            for m in bucket:
                actions.append(
                    ActionSpec(
                        action_type="no_op",
                        subject_memory_record_id=m.memory_record_id,
                        related_record_ids=[],
                        source_input_ids=[m.input_id],
                        canonical_key=m.canonical_key,
                        scope_type=m.scope_type,
                        scope_id=m.scope_id,
                        reason_code="pinned_conflict",
                        expected_record_version=m.record_version,
                        preconditions=_preconditions_for(m),
                        details={"bucket_hash": h, "pinned_count": pinned_count},
                    )
                )
            continue
        winner = _merge_winner(bucket)
        losers = [m for m in bucket if m.memory_record_id != winner.memory_record_id]
        participant_ids = sorted([winner.memory_record_id] + [l.memory_record_id for l in losers])
        for loser in losers:
            actions.append(
                ActionSpec(
                    action_type="merge_exact_duplicate",
                    subject_memory_record_id=loser.memory_record_id,
                    related_record_ids=[winner.memory_record_id],
                    source_input_ids=[loser.input_id],
                    canonical_key=loser.canonical_key,
                    scope_type=loser.scope_type,
                    scope_id=loser.scope_id,
                    reason_code="duplicate_merged",
                    expected_record_version=loser.record_version,
                    preconditions={
                        **_preconditions_for(loser),
                        winner.memory_record_id: {
                            "record_version": winner.record_version,
                            "record_state_hash": _record_state_hash(winner),
                            "decision_hash": _decision_hash(winner),
                        },
                    },
                    details={
                        "winner_id": winner.memory_record_id,
                        "bucket_hash": h,
                        "user_required_protected": winner.user_required_protected
                        or any(l.user_required_protected for l in losers),
                        "user_required_source_ids": sorted(
                            set(winner.user_required_source_ids or [])
                            | {s for l in losers for s in (l.user_required_source_ids or [])}
                        ),
                    },
                    participant_ids=participant_ids,
                )
            )

    # 单记录生命周期转换
    for m in records:
        if effective_pinned(m):
            continue  # effective_pinned 禁止任何自动降级
        # expired ephemeral
        if (
            m.retention_policy == "ephemeral"
            and m.valid_to is not None
            and m.valid_to <= now
            and m.lifecycle_state in ("active", "candidate")
        ):
            actions.append(
                ActionSpec(
                    action_type="archive_expired",
                    subject_memory_record_id=m.memory_record_id,
                    related_record_ids=[],
                    source_input_ids=[m.input_id],
                    canonical_key=m.canonical_key,
                    scope_type=m.scope_type,
                    scope_id=m.scope_id,
                    reason_code="expired_ephemeral",
                    expected_record_version=m.record_version,
                    preconditions=_preconditions_for(m),
                    participant_ids=[m.memory_record_id],
                )
            )
            continue
        # candidate 晋升 / 过期
        if m.lifecycle_state == "candidate":
            promotable = (
                m.confidence >= config.candidate_promotion_confidence
                and m.evidence_count >= config.candidate_promotion_min_evidence
            )
            if promotable:
                actions.append(
                    ActionSpec(
                        action_type="promote",
                        subject_memory_record_id=m.memory_record_id,
                        related_record_ids=[],
                        source_input_ids=[m.input_id],
                        canonical_key=m.canonical_key,
                        scope_type=m.scope_type,
                        scope_id=m.scope_id,
                        reason_code="candidate_promoted",
                        expected_record_version=m.record_version,
                        preconditions=_preconditions_for(m),
                        participant_ids=[m.memory_record_id],
                    )
                )
            elif m.created_at and (now - m.created_at).days >= config.candidate_ttl_days:
                actions.append(
                    ActionSpec(
                        action_type="archive_candidate",
                        subject_memory_record_id=m.memory_record_id,
                        related_record_ids=[],
                        source_input_ids=[m.input_id],
                        canonical_key=m.canonical_key,
                        scope_type=m.scope_type,
                        scope_id=m.scope_id,
                        reason_code="candidate_expired",
                        expected_record_version=m.record_version,
                        preconditions=_preconditions_for(m),
                        participant_ids=[m.memory_record_id],
                    )
                )
            continue
        # active → sleeping（冷却）
        if m.lifecycle_state == "active":
            cooling_days = (
                config.user_required_sleep_cooling_days
                if m.user_required_protected
                else config.sleep_cooling_days
            )
            eff = effective_last_accessed_at(m.last_accessed_at, m.observed_at, m.created_at)
            if m.importance < config.importance_sleep_threshold and (now - eff).days >= cooling_days:
                actions.append(
                    ActionSpec(
                        action_type="sleep",
                        subject_memory_record_id=m.memory_record_id,
                        related_record_ids=[],
                        source_input_ids=[m.input_id],
                        canonical_key=m.canonical_key,
                        scope_type=m.scope_type,
                        scope_id=m.scope_id,
                        reason_code="cooled_to_sleeping",
                        expected_record_version=m.record_version,
                        preconditions=_preconditions_for(m),
                        participant_ids=[m.memory_record_id],
                    )
                )
    return actions


def _canonical_input_blob(inputs: list[FrozenInput]) -> str:
    items = []
    for m in sorted(inputs, key=lambda x: x.input_sequence):
        items.append(
            "|".join(
                [
                    m.input_id,
                    str(m.input_sequence),
                    m.memory_record_id,
                    m.input_role,
                    str(m.record_version),
                    m.canonical_key or "",
                    m.scope_type or "",
                    m.scope_id or "",
                    "1" if m.user_required_protected else "0",
                    _record_state_hash(m),
                    _decision_hash(m),
                ]
            )
        )
    return "\n".join(items)


def _canonical_action_blob(actions: list[ActionSpec]) -> str:
    items = []
    for a in sorted(actions, key=lambda x: x.action_sequence):
        items.append(
            "|".join(
                [
                    str(a.action_sequence),
                    a.action_type,
                    a.subject_memory_record_id,
                    ",".join(sorted(a.related_record_ids)),
                    a.reason_code or "",
                    a.operation_group_id or "",
                ]
            )
        )
    return "\n".join(items)


def plan_batch(
    batch_id: str,
    inputs: list[FrozenInput],
    config: Any,
    now: datetime,
) -> tuple[list[ActionSpec], str, str]:
    """生成确定性维护计划。

    返回：(动作列表（已赋 action_sequence 与 operation_group_id）, input_hash, plan_hash)
    """
    inputs = sorted(inputs, key=lambda x: x.input_sequence)
    input_hash = hashlib.sha256(_canonical_input_blob(inputs).encode("utf-8")).hexdigest()

    # 按 group 生成动作
    groups: dict[tuple[str, str | None, str | None], list[FrozenInput]] = {}
    for m in inputs:
        groups.setdefault(_group_key(m), []).append(m)

    raw_actions: list[ActionSpec] = []
    for key in sorted(groups.keys()):
        raw_actions.extend(_plan_group(groups[key], now, config))

    # 确定性排序：group_key → action_type → subject
    def _sort_key(a: ActionSpec):
        return (
            a.canonical_key or "",
            a.scope_type or "",
            a.scope_id or "",
            a.action_type,
            a.subject_memory_record_id,
        )

    raw_actions.sort(key=_sort_key)
    for i, a in enumerate(raw_actions):
        a.action_sequence = i

    policy_version = getattr(config, "policy_version", "phase4.v1")
    for a in raw_actions:
        if not a.participant_ids:
            a.participant_ids = [a.subject_memory_record_id]
        a.operation_group_id = hashlib.sha256(
            "|".join(
                [
                    batch_id,
                    a.action_type,
                    ",".join(sorted(a.participant_ids)),
                    policy_version,
                ]
            ).encode("utf-8")
        ).hexdigest()

    plan_hash = hashlib.sha256(
        (input_hash + "|" + _canonical_action_blob(raw_actions) + "|" + policy_version).encode("utf-8")
    ).hexdigest()
    return raw_actions, input_hash, plan_hash
