"""谓词语义配置：关系基数（单值 / 多值）与谓词族归一。

图谱是派生层，但"同一主体能否同时拥有多个客体"属于**谓词本身**的语义，
不能由某条新事实的出现顺序推断。因此这里只声明确定性强、可解释的规则：

- 只有在本模块明确声明为 single 的谓词，图谱才可能在"同主体 + 同谓词族 +
  同 Scope 出现不同客体"时结束旧关系；
- 未声明的谓词一律按 multi 处理（多个客体可以并存），宁可并存也不误删；
- 单值只是"允许关闭"的必要条件：真正关闭还必须有记忆层的权威取代结果
  （memory_records.validity_state / superseded_by），见 graph_projection。

扩展方式：在 `_SINGLE_VALUED` 里加一行（canonical 谓词 + 常见别名），或通过
`AIIVE_MEMORY_GRAPH_SINGLE_VALUED_PREDICATES`（逗号分隔）补充业务谓词，
无需改代码路径。
"""
from __future__ import annotations

from collections.abc import Iterable

from aiive.memory.graph_types import normalize_predicate

# canonical 谓词 -> 同义写法（别名只在比较时归一，不重写已落库的 predicate）
_SINGLE_VALUED: dict[str, tuple[str, ...]] = {
    # 当前任职单位：同一时刻只有一家
    "works_at": (
        "work_at", "works_for", "employed_by", "employer",
        "current_employer", "current_company", "currently_works_at",
    ),
    # 当前居住地
    "lives_in": ("live_in", "resides_in", "residence", "current_city", "current_residence"),
    # 出生地 / 出生日期
    "born_in": ("birthplace", "place_of_birth"),
    "born_on": ("date_of_birth", "birthday"),
    # 组织唯一角色与总部
    "ceo_of": ("chief_executive_officer_of",),
    "headquartered_in": ("headquarters", "hq_in"),
    # 版本类：一个项目当前只有一个版本
    "current_version": ("latest_version", "current_release", "current_stable_version"),
}

SINGLE = "single"
MULTI = "multi"


def _extra_single_valued() -> tuple[str, ...]:
    """从 settings 读取业务侧补充的单值谓词（逗号分隔，未知项忽略空值）。"""
    from aiive.config import settings

    raw = str(getattr(settings, "aiive_memory_graph_single_valued_predicates", "") or "")
    names = [normalize_predicate(part) for part in raw.split(",")]
    return tuple(name for name in names if name)


def _registry() -> dict[str, str]:
    """别名 -> canonical 的索引（每次按当前 settings 构建，避免测试间串味）。"""
    index: dict[str, str] = {}
    for canonical, aliases in _SINGLE_VALUED.items():
        index[canonical] = canonical
        for alias in aliases:
            index[normalize_predicate(alias)] = canonical
    for extra in _extra_single_valued():
        index.setdefault(extra, extra)
    return index


def canonical_predicate(predicate: str) -> str:
    """把谓词归一到 canonical 名（未知谓词原样返回归一后的写法）。"""
    normalized = normalize_predicate(predicate)
    return _registry().get(normalized, normalized)


def is_single_valued(predicate: str) -> bool:
    """是否明确声明为单值谓词；未知谓词返回 False（按多值处理）。"""
    return normalize_predicate(predicate) in _registry()


def cardinality(predicate: str) -> str:
    """返回 single / multi（未知谓词 = multi）。"""
    return SINGLE if is_single_valued(predicate) else MULTI


def predicate_family(predicate: str) -> tuple[str, ...]:
    """同一语义谓词的全部已知写法（用于 SQL 层按族比较）。"""
    normalized = normalize_predicate(predicate)
    if not normalized:
        return ()
    canonical = _registry().get(normalized, normalized)
    names = {normalized, canonical}
    names.update(normalize_predicate(alias) for alias in _SINGLE_VALUED.get(canonical, ()))
    names.discard("")
    return tuple(sorted(names))


def single_valued_predicates() -> tuple[str, ...]:
    """当前生效的单值谓词列表（供文档、运维报告展示）。"""
    return tuple(sorted(_registry()))


def conflict_group_key(
    subject_entity_id: str, predicate: str, scope_type: str, scope_id: str | None,
) -> tuple[str, str, str, str]:
    """冲突分组键：同主体 + 同谓词族 + 同 Scope。"""
    return (subject_entity_id, canonical_predicate(predicate), scope_type or "global", scope_id or "")


def conflicted_group_keys(
    rows: Iterable[tuple[str, str, str, str, str, str | None]],
) -> set[tuple[str, str, str, str]]:
    """从 (subject_id, predicate, object_id, status, scope_type, scope_id) 找出冲突组。

    只有单值谓词、且同一组内出现多个不同客体时才算冲突；多值谓词天然允许多客体，
    不进入该集合。调用方据此把关系标成"未确认"，而不是删除或关闭它们。
    """
    groups: dict[tuple[str, str, str, str], set[str]] = {}
    for subject_id, predicate, object_id, status, scope_type, scope_id in rows:
        if status != "active" or not is_single_valued(predicate):
            continue
        key = conflict_group_key(subject_id, predicate, scope_type, scope_id)
        groups.setdefault(key, set()).add(object_id)
    return {key for key, objects in groups.items() if len(objects) > 1}

