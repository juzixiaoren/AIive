"""时序知识图谱的纯数据契约与规范化工具。

图谱是 memory_records 的**派生层**：唯一真相源仍是记忆记录本身
（memory_records.graph_facts 保存结构化关系事实），两张图谱表只承担
可查询投影。本模块不访问数据库、不含业务策略，供提取、投影、扩展、
重建四条链路共用，避免四处各写一套规范化规则。

时间语义（刻意分开，不得混淆）：

- valid_from / valid_to：**事实有效时间**（世界时间）。模型无法从原文
  确定时必须是 None，绝不退化为“写入时刻”。
- recorded_at：**系统记录时间**（本行写入图谱的时刻），由投影写入。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

ENTITY_NAME_MAX = 200
PREDICATE_MAX = 64
SOURCE_SPAN_MAX = 600
MAX_FACTS_PER_MEMORY = 12

_WHITESPACE_RE = re.compile(r"\s+")
_PREDICATE_ALLOWED_RE = re.compile(r"[^a-z0-9_.]+")
_QUOTES = "\"'\u201c\u201d\u2018\u2019`"


def normalize_entity_name(raw: str) -> str:
    """实体规范化：去引号、折叠空白、大小写归一。

    只做确定性的字符串归一（不调用 LLM、不做同义词推断），保证同一实体
    在不同记忆里落成同一个 key；别名匹配在查询侧按 aliases 精确比较。
    """
    text = str(raw or "").strip().strip(_QUOTES).strip()
    text = _WHITESPACE_RE.sub(" ", text)
    return text.casefold()[:ENTITY_NAME_MAX]


def display_entity_name(raw: str) -> str:
    """实体展示名：保留原始大小写，仅清理引号与多余空白。"""
    text = str(raw or "").strip().strip(_QUOTES).strip()
    return _WHITESPACE_RE.sub(" ", text)[:ENTITY_NAME_MAX]


def normalize_predicate(raw: str) -> str:
    """关系类型规范化：小写、空格与连字符转下划线、仅保留 a-z0-9_. 。"""
    text = str(raw or "").strip().lower()
    text = _WHITESPACE_RE.sub("_", text)
    text = _PREDICATE_ALLOWED_RE.sub("_", text)
    text = re.sub(r"_+", "_", text).strip("_.")
    return text[:PREDICATE_MAX]


def parse_fact_time(value: Any) -> datetime | None:
    """把模型给出的时间解析为 UTC datetime；无法确定时返回 None。

    只接受 YYYY-MM-DD 或 ISO8601 时间串。**绝不**在解析失败时回退到
    当前时间——未知的事实时间必须保持未知，不能伪装成已知。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).strip()
    if not text:
        return None
    candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def make_relation_key(
    *,
    source_memory_id: str,
    subject: str,
    predicate: str,
    object_: str,
    valid_from: datetime | None,
) -> str:
    """稳定关系标识：来源记忆 + 三元组 + 事实起始时间。

    同一来源、同一版本内容重复投影会得到同一个 key（幂等 upsert）；
    不同记忆说同一件事则 key 不同（保留多条来源证据）。
    """
    stamp = valid_from.isoformat() if valid_from else ""
    raw = "|".join([
        str(source_memory_id),
        normalize_entity_name(subject),
        normalize_predicate(predicate),
        normalize_entity_name(object_),
        stamp,
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


class GraphFact(BaseModel):
    """一条结构化关系事实（提取结果 → 提案 → canonical 记录）。

    约束：
    - 三元组任一为空则视为无效，由调用方丢弃；
    - source_span 必须是来源文本中可直接定位的连续片段，空 span 表示
      该关系缺少原始依据，**不得**作为确定事实进入上下文；
    - valid_from / valid_to 未知时保持 None。
    """

    subject: str = Field(min_length=1, max_length=ENTITY_NAME_MAX)
    predicate: str = Field(min_length=1, max_length=PREDICATE_MAX)
    object: str = Field(min_length=1, max_length=ENTITY_NAME_MAX)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    source_span: str = Field(default="", max_length=SOURCE_SPAN_MAX)
    valid_from: datetime | None = None
    valid_to: datetime | None = None

    @field_validator("subject", "object", mode="before")
    @classmethod
    def _clean_entity(cls, value: Any) -> Any:
        return display_entity_name(str(value)) if value is not None else value

    @field_validator("predicate", mode="before")
    @classmethod
    def _clean_predicate(cls, value: Any) -> Any:
        return normalize_predicate(str(value)) if value is not None else value

    def normalized_key(self, source_memory_id: str) -> str:
        """该事实在来源记忆下的稳定关系 key。"""
        return make_relation_key(
            source_memory_id=source_memory_id,
            subject=self.subject,
            predicate=self.predicate,
            object_=self.object,
            valid_from=self.valid_from,
        )

    def is_complete(self) -> bool:
        """三元组是否完整（规范化后仍非空）。"""
        return bool(
            normalize_entity_name(self.subject)
            and normalize_predicate(self.predicate)
            and normalize_entity_name(self.object)
        )


class GraphFactPayload(BaseModel):
    """LLM 输出侧的关系事实（宽进严出）。

    提取模型只能给字符串时间，因此在解析层先用宽松类型接住，再由
    to_fact() 转成 GraphFact；解析失败即丢弃该事实，不猜。
    """

    subject: str = ""
    predicate: str = ""
    object: str = ""
    confidence: float = 0.5
    source_span: str = ""
    valid_from: str = ""
    valid_to: str = ""

    def to_fact(self) -> GraphFact | None:
        """转换为 GraphFact；三元组不完整或置信度非法时返回 None。"""
        try:
            confidence = float(self.confidence)
        except (TypeError, ValueError):
            confidence = 0.5
        if confidence < 0.0 or confidence > 1.0:
            confidence = min(max(confidence, 0.0), 1.0)
        try:
            fact = GraphFact(
                subject=self.subject,
                predicate=self.predicate,
                object=self.object,
                confidence=confidence,
                source_span=(self.source_span or "")[:SOURCE_SPAN_MAX],
                valid_from=parse_fact_time(self.valid_from),
                valid_to=parse_fact_time(self.valid_to),
            )
        except (ValidationError, TypeError, ValueError):  # 三元组为空 / 超长 → 丢弃该事实，不伪造
            return None
        if not fact.is_complete():
            return None
        if fact.valid_from and fact.valid_to and fact.valid_to < fact.valid_from:
            fact.valid_to = None
        return fact


@dataclass(frozen=True)
class GraphConfig:
    """图谱链路的可调参数（全部来自 settings，不在业务代码写死）。"""

    enabled: bool = True
    max_hops: int = 2
    batch_size: int = 200
    max_seed_entities: int = 32
    min_confidence: float = 0.6
    budget_ratio: float = 0.35
    max_relations_per_expansion: int = 2000

    @classmethod
    def from_settings(cls) -> GraphConfig:
        from aiive.config import settings

        return cls(
            enabled=bool(getattr(settings, "aiive_memory_graph_enabled", True)),
            max_hops=max(0, int(getattr(settings, "aiive_memory_graph_max_hops", 2))),
            batch_size=max(1, int(getattr(settings, "aiive_memory_graph_batch_size", 200))),
            max_seed_entities=max(1, int(getattr(settings, "aiive_memory_graph_max_seed_entities", 32))),
            min_confidence=float(getattr(settings, "aiive_memory_graph_min_confidence", 0.6)),
            budget_ratio=min(1.0, max(0.0, float(getattr(settings, "aiive_memory_graph_budget_ratio", 0.35)))),
            max_relations_per_expansion=max(
                1, int(getattr(settings, "aiive_memory_graph_max_relations", 2000)),
            ),
        )


@dataclass
class GraphRelationView:
    """读侧的一条关系（已通过生命周期/有效性/遗忘/Scope 校验）。"""

    relation_id: str
    subject: str
    predicate: str
    object: str
    subject_entity_id: str = ""
    object_entity_id: str = ""
    confidence: float = 0.0
    source_memory_id: str = ""
    source_memory_type: str = ""
    source_span: str = ""
    scope_type: str = "global"
    scope_id: str | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    recorded_at: datetime | None = None
    status: str = "active"
    hop: int = 0
    score: float = 0.0
    token_cost: int = 0
    is_current: bool = True
    # 谓词基数：single 表示同主体同谓词只能有一个有效客体
    cardinality: str = "multi"
    # 单值谓词出现多个有效客体时为 True（保留证据，但不得当作确定事实）
    is_conflicted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "relation_id": self.relation_id,
            "subject": self.subject,
            "predicate": self.predicate,
            "object": self.object,
            "confidence": round(self.confidence, 4),
            "source_memory_id": self.source_memory_id,
            "source_memory_type": self.source_memory_type,
            "source_span": self.source_span,
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "recorded_at": self.recorded_at.isoformat() if self.recorded_at else None,
            "status": self.status,
            "hop": self.hop,
            "score": round(self.score, 4),
            "is_current": self.is_current,
            "cardinality": self.cardinality,
            "is_conflicted": self.is_conflicted,
        }


@dataclass
class GraphExpansionResult:
    """一次关系扩展的结果：当前有效关系 + 必要的历史对照 + 统计。"""

    relations: list[GraphRelationView] = field(default_factory=list)
    seed_memory_ids: list[str] = field(default_factory=list)
    seed_entity_ids: list[str] = field(default_factory=list)
    matched_query_entities: list[str] = field(default_factory=list)
    hops_used: int = 0
    truncated: bool = False
    visited_entities: int = 0
    # 单值谓词出现多个有效客体的关系条数（冲突，仅作证据呈现）
    conflicted: int = 0

    def is_empty(self) -> bool:
        return not self.relations

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed_memory_ids": self.seed_memory_ids,
            "seed_entity_ids": self.seed_entity_ids,
            "matched_query_entities": self.matched_query_entities,
            "hops_used": self.hops_used,
            "truncated": self.truncated,
            "visited_entities": self.visited_entities,
            "conflicted": self.conflicted,
            "relation_count": len(self.relations),
            "relations": [item.to_dict() for item in self.relations],
        }
