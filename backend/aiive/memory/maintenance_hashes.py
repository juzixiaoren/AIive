"""Phase 4 维护哈希工具：记录态哈希与决策哈希分离（第 6 点）。

- `record_state_hash`：仅含记录结构性字段，**不含访问时间**；
  merge/supersede 等结构性动作主要依赖它。
- `decision_hash`：在记录态哈希基础上纳入访问时间维度；
  仅 sleep/cooling 类决策依赖，普通读（仅 touch `last_accessed_at`）
  不会令 merge/supersede 变 stale。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any


def _norm(value: Any) -> str:
    """将任意字段规范化为稳定字符串，便于 sha256 拼接。

    datetime 一律折算到 UTC 后再序列化：naive 值视为 UTC（SQLite 无时区，
    从库中读回为 naive），aware 值转换到 UTC。这样无论后端方言或读写路径，
    同一时刻的 datetime 都产生一致的哈希，保证 freeze/plan/execute 三处
    计算出的 record_state_hash / decision_hash 完全一致。
    """
    if value is None:
        return "∅"
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, float):
        return f"{value:.6f}"
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def record_state_hash(
    *,
    content: str,
    structured_value: dict[str, Any] | None,
    lifecycle_state: str,
    validity_state: str,
    confidence: float,
    importance: float,
    retention_policy: str,
    valid_to: datetime | None,
    pinned: bool,
    stability: str,
    stability_score: float | None,
    reinforce_count: int,
    last_reinforced_at: datetime | None,
) -> str:
    """记录态哈希：不含访问时间维度。"""
    parts = [
        f"content={_norm(content)}",
        f"structured={_norm(structured_value)}",
        f"lifecycle={_norm(lifecycle_state)}",
        f"validity={_norm(validity_state)}",
        f"confidence={_norm(confidence)}",
        f"importance={_norm(importance)}",
        f"retention={_norm(retention_policy)}",
        f"valid_to={_norm(valid_to)}",
        f"pinned={_norm(pinned)}",
        f"stability={_norm(stability)}",
        f"stability_score={_norm(stability_score)}",
        f"reinforce_count={_norm(reinforce_count)}",
        f"last_reinforced_at={_norm(last_reinforced_at)}",
    ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest


def effective_last_accessed_at(
    last_accessed_at: datetime | None,
    observed_at: datetime,
    created_at: datetime,
) -> datetime:
    """冷却基准时间回退：last_accessed_at → observed_at → created_at。"""
    return last_accessed_at or observed_at or created_at


def decision_hash(
    state_hash: str,
    last_accessed_at: datetime | None,
    effective_accessed_at: datetime,
) -> str:
    """决策哈希：在记录态哈希基础上纳入访问时间维度。"""
    parts = [
        f"state={state_hash}",
        f"last_accessed_at={_norm(last_accessed_at)}",
        f"effective_accessed_at={_norm(effective_accessed_at)}",
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def compute_hashes(
    *,
    content: str,
    structured_value: dict[str, Any] | None,
    lifecycle_state: str,
    validity_state: str,
    confidence: float,
    importance: float,
    retention_policy: str,
    valid_to: datetime | None,
    pinned: bool,
    stability: str,
    stability_score: float | None,
    reinforce_count: int,
    last_reinforced_at: datetime | None,
    last_accessed_at: datetime | None,
    observed_at: datetime,
    created_at: datetime,
) -> tuple[str, str]:
    """一次性计算 (record_state_hash, decision_hash)。"""
    rsh = record_state_hash(
        content=content,
        structured_value=structured_value,
        lifecycle_state=lifecycle_state,
        validity_state=validity_state,
        confidence=confidence,
        importance=importance,
        retention_policy=retention_policy,
        valid_to=valid_to,
        pinned=pinned,
        stability=stability,
        stability_score=stability_score,
        reinforce_count=reinforce_count,
        last_reinforced_at=last_reinforced_at,
    )
    eff = effective_last_accessed_at(last_accessed_at, observed_at, created_at)
    dhash = decision_hash(rsh, last_accessed_at, eff)
    return rsh, dhash
