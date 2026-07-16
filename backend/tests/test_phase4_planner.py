"""Phase 4 计划生成器与哈希工具的单元测试（纯函数，无需 DB）。

覆盖：第 4 点（operation_group_id / plan_hash 可重复计算且稳定）、
第 6 点（record_state_hash 不受访问时间影响，decision_hash 受影响）、
第 7 点（pinned 冲突禁止自动 merge、winner 继承 user_required 保护）。
"""
from datetime import datetime, timezone

from aiive.memory.maintenance_hashes import (
    compute_hashes,
    decision_hash,
    record_state_hash,
)
from aiive.memory.memory_maintenance_planner import (
    FrozenInput,
    effective_pinned,
    plan_batch,
)
from aiive.memory.recall_config import MaintenanceConfig


def _utc(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _mk_input(
    *,
    iid: str,
    seq: int,
    mid: str,
    role: str = "seed",
    canonical_key: str = "fact:name",
    scope_type: str = "global",
    scope_id: str | None = None,
    lifecycle_state: str = "candidate",
    confidence: float = 0.5,
    importance: float = 0.5,
    retention_policy: str = "normal",
    valid_to=None,
    pinned: bool = False,
    user_required_protected: bool = False,
    user_required_source_ids: list[str] | None = None,
    content_hash: str | None = "h1",
    structured_value_hash: str | None = None,
    created_at: str = "2026-01-01T00:00:00+00:00",
    observed_at: str = "2026-01-01T00:00:00+00:00",
    last_accessed_at=None,
    evidence_count: int = 0,
    record_version: int = 1,
) -> FrozenInput:
    return FrozenInput(
        input_id=iid,
        input_sequence=seq,
        memory_record_id=mid,
        input_role=role,
        record_version=record_version,
        canonical_key=canonical_key,
        scope_type=scope_type,
        scope_id=scope_id,
        user_required_protected=user_required_protected,
        user_required_source_ids=user_required_source_ids or [],
        content=f"content-{mid}",
        structured_value=None,
        content_hash=content_hash,
        structured_value_hash=structured_value_hash,
        lifecycle_state=lifecycle_state,
        validity_state="valid",
        confidence=confidence,
        importance=importance,
        retention_policy=retention_policy,
        valid_to=valid_to,
        pinned=pinned,
        stability="contextual",
        stability_score=None,
        reinforce_count=0,
        last_reinforced_at=None,
        observed_at=_utc(observed_at),
        created_at=_utc(created_at),
        last_accessed_at=_utc(last_accessed_at) if last_accessed_at else None,
        evidence_count=evidence_count,
    )


def test_plan_deterministic_operation_group_and_plan_hash():
    cfg = MaintenanceConfig()
    now = _utc("2026-06-01T00:00:00+00:00")
    inputs = [
        _mk_input(iid="i1", seq=0, mid="m1", lifecycle_state="candidate", confidence=0.8, evidence_count=2),
        _mk_input(iid="i2", seq=1, mid="m2", lifecycle_state="active", importance=0.1),
    ]
    a1, ih1, ph1 = plan_batch("batch-x", inputs, cfg, now)
    # 调换输入顺序（相同集合）应得相同 input_hash / plan_hash / operation_group_id
    a2, ih2, ph2 = plan_batch("batch-x", list(reversed(inputs)), cfg, now)
    assert ih1 == ih2
    assert ph1 == ph2
    assert [x.operation_group_id for x in a1] == [x.operation_group_id for x in a2]
    # action_sequence 连续且从 0 开始
    assert [x.action_sequence for x in a1] == list(range(len(a1)))


def test_pinned_conflict_forbids_auto_merge():
    cfg = MaintenanceConfig()
    now = _utc("2026-06-01T00:00:00+00:00")
    # 两个 pinned 且内容相同的重复记录
    inputs = [
        _mk_input(iid="i1", seq=0, mid="m1", lifecycle_state="active", pinned=True, content_hash="dup"),
        _mk_input(iid="i2", seq=1, mid="m2", lifecycle_state="active", pinned=True, content_hash="dup"),
    ]
    actions, _, _ = plan_batch("batch-y", inputs, cfg, now)
    assert len(actions) == 2
    assert all(a.action_type == "no_op" for a in actions)
    assert all(a.reason_code == "pinned_conflict" for a in actions)


def test_winner_inherits_user_required_protection():
    cfg = MaintenanceConfig()
    now = _utc("2026-06-01T00:00:00+00:00")
    # 两条相同内容重复记录，其中 loser 为 user_required
    inputs = [
        _mk_input(iid="i1", seq=0, mid="m1", lifecycle_state="active", confidence=0.9, content_hash="dup"),
        _mk_input(
            iid="i2", seq=1, mid="m2", lifecycle_state="candidate", confidence=0.5,
            content_hash="dup", user_required_protected=True,
            user_required_source_ids=["prop-1"],
        ),
    ]
    actions, _, _ = plan_batch("batch-z", inputs, cfg, now)
    merges = [a for a in actions if a.action_type == "merge_exact_duplicate"]
    assert len(merges) == 1
    loser = merges[0]
    # winner 应为 m2（user_required 优先于 confidence），m1 被合并进 m2
    assert loser.subject_memory_record_id == "m1"
    assert loser.related_record_ids == ["m2"]
    # winner（m2）本身即 user_required，合并动作标记继承保护
    assert loser.details["user_required_protected"] is True
    assert "prop-1" in loser.details["user_required_source_ids"]


def test_record_state_hash_stable_under_access_but_decision_hash_changes():
    base = dict(
        content="c", structured_value=None, lifecycle_state="active", validity_state="valid",
        confidence=0.5, importance=0.5, retention_policy="normal", valid_to=None,
        pinned=False, stability="contextual", stability_score=None, reinforce_count=0,
        last_reinforced_at=None,
    )
    rsh_a, dhash_a = compute_hashes(
        last_accessed_at=None, observed_at=_utc("2026-01-01T00:00:00+00:00"),
        created_at=_utc("2026-01-01T00:00:00+00:00"), **base,
    )
    # 仅 touch last_accessed_at：record_state_hash 不变，decision_hash 变
    rsh_b, dhash_b = compute_hashes(
        last_accessed_at=_utc("2026-06-01T00:00:00+00:00"),
        observed_at=_utc("2026-01-01T00:00:00+00:00"),
        created_at=_utc("2026-01-01T00:00:00+00:00"), **base,
    )
    assert rsh_a == rsh_b
    assert dhash_a != dhash_b
