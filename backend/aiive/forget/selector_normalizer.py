"""选择器规范化与确定性哈希。

职责:
  - normalize_selector: 将非结构化 forget 参数规范化为不可变 selector_payload JSON
  - compute_selector_hash: 对 selector_payload 计算确定性 SHA-256 哈希
  - compute_normalized_shield_key: 为 forget_shields 生成幂等键
"""

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

# 手动输入的 max inline shield targets 上限
MAX_INLINE_SHIELD_TARGETS = 100


def _sort_list(items: list[str]) -> list[str]:
    """去重、排序、保持确定性输出。"""
    return sorted(set(items))


def _normalize_datetime(dt: datetime | None) -> str | None:
    """将 datetime 规范化为 ISO 8601 UTC 字符串。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _canonical_json(obj: Any) -> str:
    """生成确定性 JSON（key 排序，无空格，确保一致性）。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_selector_hash(payload: dict[str, Any]) -> str:
    """对 selector_payload 计算确定性 SHA-256 哈希。"""
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def compute_normalized_shield_key(
    forget_operation_id: str,
    selector_type: str,
    *,
    target_type: str | None = None,
    target_id: str | None = None,
    thread_id: str | None = None,
    scope_type: str | None = None,
    scope_id: str | None = None,
    cutoff_created_at: datetime | None = None,
) -> str:
    """为 forget_shields 生成规范化幂等键。

    基于 (operation_id, selector_type, 实体标识/scope/thread/cutoff) 计算确定性哈希。
    禁止依赖包含 nullable 列的普通 UNIQUE 保证幂等。
    """
    key_parts = [
        forget_operation_id,
        selector_type,
        target_type or "",
        target_id or "",
        thread_id or "",
        scope_type or "",
        scope_id or "",
        _normalize_datetime(cutoff_created_at) or "",
    ]
    raw = "|".join(key_parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_selector(
    mode: str,
    *,
    memory_ids: list[str] | None = None,
    turn_ids: list[str] | None = None,
    event_ids: list[str] | None = None,
    thread_id: str | None = None,
    canonical_key: str | None = None,
    scope_type: str | None = None,
    scope_id: str | None = None,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
    all_user_data: bool = False,
) -> dict[str, Any]:
    """将 forget 参数规范化为不可变 selector_payload。

    不保存匹配原文；仅保存 IDs/scope/时间范围/canonical_key 等结构化条件。
    Phase B 只能从该 payload 物化 Target。

    Returns:
        normalized payload dict (可直接 JSON 序列化)
    """
    ids_sorted: list[str] = []
    if memory_ids:
        ids_sorted = _sort_list(memory_ids)
    turn_ids_sorted: list[str] = []
    if turn_ids:
        turn_ids_sorted = _sort_list(turn_ids)
    event_ids_sorted: list[str] = []
    if event_ids:
        event_ids_sorted = _sort_list(event_ids)

    # 确定选择器类型
    if all_user_data:
        selector_type = "all_user_data"
    elif ids_sorted:
        selector_type = "memory_ids"
    elif turn_ids_sorted:
        selector_type = "turn_ids"
    elif event_ids_sorted:
        selector_type = "event_ids"
    elif canonical_key:
        selector_type = "canonical_key"
    elif thread_id:
        selector_type = "thread"
    elif time_from or time_to:
        selector_type = "time_range"
    elif scope_type and scope_id:
        selector_type = "scope"
    else:
        raise ValueError("无法确定 selector_type：所有选择器字段均为空")

    payload: dict[str, Any] = {
        "mode": mode,
        "selector_type": selector_type,
    }

    if ids_sorted:
        payload["memory_ids"] = ids_sorted
    if turn_ids_sorted:
        payload["turn_ids"] = turn_ids_sorted
    if event_ids_sorted:
        payload["event_ids"] = event_ids_sorted
    if thread_id:
        payload["thread_id"] = thread_id
    if canonical_key:
        payload["canonical_key"] = canonical_key
    if scope_type and scope_id:
        payload["scope_type"] = scope_type
        payload["scope_id"] = scope_id
    if time_from:
        payload["time_from"] = _normalize_datetime(time_from)
    if time_to:
        payload["time_to"] = _normalize_datetime(time_to)
    if all_user_data:
        payload["all_user_data"] = True

    return payload


def is_inline_shield_candidate(selector_payload: dict[str, Any]) -> bool:
    """判断 selector_payload 是否允许 Phase A 内联实体级 Shield。

    True 的条件:
      - selector_type ∈ {memory_ids, turn_ids, event_ids}
      - ID 数量 ≤ MAX_INLINE_SHIELD_TARGETS

    返回 False 时，selector 自动转 selector Shield + Cascade Batch。
    """
    selector_type = selector_payload.get("selector_type", "")
    if selector_type not in ("memory_ids", "turn_ids", "event_ids"):
        return False

    key = {
        "memory_ids": "memory_ids",
        "turn_ids": "turn_ids",
        "event_ids": "event_ids",
    }.get(selector_type, "")

    id_list: list[str] = selector_payload.get(key, [])
    return len(id_list) <= MAX_INLINE_SHIELD_TARGETS
