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

# ── Mode / Selector 兼容矩阵 ──
# 语义以 docs/architecture/phase_6A.md §G 为准：
#   memory_only 只定位长期记忆表示（memory_ids / canonical_key / scope）；
#   history_only 只定位原始历史（thread / turn_ids / event_ids / time_range）；
#   everywhere 允许全部选择器。
_MEMORY_ONLY_SELECTORS = frozenset({"memory_ids", "canonical_key", "scope"})
_HISTORY_ONLY_SELECTORS = frozenset({"thread", "turn_ids", "event_ids", "time_range"})
_FORGET_MODES = frozenset({"memory_only", "history_only", "everywhere"})


def detect_selector_types(
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
) -> list[str]:
    """返回本次请求实际提供的独立选择器类型列表（稳定顺序）。"""
    types: list[str] = []
    if all_user_data:
        types.append("all_user_data")
    if memory_ids:
        types.append("memory_ids")
    if turn_ids:
        types.append("turn_ids")
    if event_ids:
        types.append("event_ids")
    if canonical_key:
        types.append("canonical_key")
    if thread_id:
        types.append("thread")
    if time_from or time_to:
        types.append("time_range")
    if scope_type and scope_id:
        types.append("scope")
    return types


def validate_selector_combination(
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
) -> str:
    """校验 mode 与 selector 组合，返回唯一的 selector_type。

    API / Tool / Phase A 共用本函数，保证非法组合在创建 Operation 之前被拒绝：
      - 必须且只能提供一种独立选择器（禁止多选择器静默忽略）；
      - memory_only 仅允许 memory_ids / canonical_key / scope；
      - history_only 仅允许 thread / turn_ids / event_ids / time_range；
      - everywhere 允许全部。
    """
    if mode not in _FORGET_MODES:
        raise ValueError(
            f"mode 必须是 memory_only / history_only / everywhere，收到: {mode}"
        )
    if bool(scope_type) != bool(scope_id):
        raise ValueError("scope_type 与 scope_id 必须同时提供")
    if time_from and time_to and time_from > time_to:
        raise ValueError("time_from 不能晚于 time_to")

    types = detect_selector_types(
        memory_ids=memory_ids,
        turn_ids=turn_ids,
        event_ids=event_ids,
        thread_id=thread_id,
        canonical_key=canonical_key,
        scope_type=scope_type,
        scope_id=scope_id,
        time_from=time_from,
        time_to=time_to,
        all_user_data=all_user_data,
    )
    if not types:
        raise ValueError("无法确定 selector_type：所有选择器字段均为空")
    if len(types) > 1:
        raise ValueError(f"只能提供一种选择器，收到多种: {', '.join(types)}")

    selector_type = types[0]
    if mode == "memory_only" and selector_type not in _MEMORY_ONLY_SELECTORS:
        raise ValueError(
            f"memory_only 不支持选择器 {selector_type}；只允许 memory_ids / canonical_key / scope"
        )
    if mode == "history_only" and selector_type not in _HISTORY_ONLY_SELECTORS:
        raise ValueError(
            f"history_only 不支持选择器 {selector_type}；只允许 thread / turn_ids / event_ids / time_range"
        )
    return selector_type


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

    # 严格 Mode / Selector 兼容矩阵（非法组合在创建 Operation 之前拒绝）
    selector_type = validate_selector_combination(
        mode,
        memory_ids=memory_ids,
        turn_ids=turn_ids,
        event_ids=event_ids,
        thread_id=thread_id,
        canonical_key=canonical_key,
        scope_type=scope_type,
        scope_id=scope_id,
        time_from=time_from,
        time_to=time_to,
        all_user_data=all_user_data,
    )

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
