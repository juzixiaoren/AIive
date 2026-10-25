"""运行证据采集：把真实运行记录收敛成可验证、可聚合的失败证据。

设计边界：

- 只读取结构化运行记录（任务终态报告、Action 失败、Outbox 死信、自进化修复失败、
  Eval 安全门失败），不扫描聊天原文。用户纠正只有在已有结构化记录时才进入证据；
  本模块不解析自然语言。
- 先做确定性过滤与聚合，再交给模型：同一能力 + 同一错误签名归为一个证据组，
  一次性故障（单次超时/连接重置等）在进入模型前就被丢弃，避免把临时网络抖动
  当成系统缺陷。
- 本模块只读：不做任何写入，也不调用模型。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from aiive.db.models import AgentAction, AgentTask, OutboxJob, SelfDevRequest

logger = logging.getLogger(__name__)

# ── 证据类别 ────────────────────────────────────────────────────────
CATEGORY_TASK = "agent_task_failure"
CATEGORY_ACTION = "action_failure"
CATEGORY_JOB = "job_failure"
CATEGORY_SELFDEV = "selfdev_failure"
CATEGORY_EVAL = "eval_failure"

# 结构性失败：来自显式的门禁/流程判定，单次出现即说明问题（不是随机抖动）。
STRUCTURAL_CATEGORIES: frozenset[str] = frozenset({CATEGORY_SELFDEV, CATEGORY_EVAL})

DEFAULT_MIN_OCCURRENCES = 2
DEFAULT_MIN_TRANSIENT_OCCURRENCES = 3
DEFAULT_MAX_GROUPS = 12
DEFAULT_PER_SOURCE_LIMIT = 200
# 没有游标时的默认回看窗口：只看最近一段运行，避免第一次就把历史全量拉进来。
DEFAULT_LOOKBACK_HOURS = 72

_TASK_FAILURE_STATUSES: tuple[str, ...] = ("failed", "partial")
_ACTION_FAILURE_STATUSES: tuple[str, ...] = ("failed", "unknown")
_JOB_FAILURE_STATUSES: tuple[str, ...] = ("deadletter", "failed")

_SIGNATURE_MAX = 120
_SUMMARY_MAX = 400
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
_HEX_RE = re.compile(r"\b[0-9a-f]{16,}\b", re.IGNORECASE)
_DIGITS_RE = re.compile(r"\d+")
_WS_RE = re.compile(r"\s+")

# 一次性/环境类故障标记：用于判断这条证据是不是临时抖动。
_TRANSIENT_MARKERS: tuple[str, ...] = (
    "timeout", "timed out", "read timeout", "connect timeout",
    "connection reset", "connection refused", "connection aborted",
    "econnreset", "econnrefused", "broken pipe",
    "temporary failure", "name or service not known", "getaddrinfo",
    "network is unreachable", "no route to host",
    "rate limit", "too many requests", "429", "502", "503", "504",
    "gateway timeout", "bad gateway", "service unavailable",
    "ssl", "proxy", "dns",
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _text(value: Any, limit: int = _SUMMARY_MAX) -> str:
    """把任意值压成有界单行文本（证据摘要不需要保留全文）。"""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = " / ".join(str(item) for item in value if str(item).strip())
    text = _WS_RE.sub(" ", str(value)).strip()
    return text[:limit]


def normalize_signature(value: Any) -> str:
    """把错误文本归一成稳定的分组签名。

    去掉 UUID / 长十六进制 / 数字与多余空白：同一类错误的不同实例应落进同一组，
    否则聚合失效、模型会看到大量重复证据。
    """
    text = _text(value, 400).lower()
    if not text:
        return ""
    text = _UUID_RE.sub("<id>", text)
    text = _HEX_RE.sub("<hex>", text)
    text = _DIGITS_RE.sub("<n>", text)
    text = _WS_RE.sub(" ", text).strip()
    return text[:_SIGNATURE_MAX]


def is_transient(value: Any) -> bool:
    """是否属于一次性/环境类故障（单次出现不足以判定系统缺陷）。"""
    text = _text(value, 400).lower()
    if not text:
        return False
    return any(marker in text for marker in _TRANSIENT_MARKERS)


@dataclass
class EvidenceItem:
    """一条真实运行记录，对应一个可回溯的数据库/文件 ID。"""

    evidence_id: str
    category: str
    group_key: str
    capability: str
    error_signature: str
    summary: str
    source_id: str
    observed_at: datetime | None = None
    transient: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "category": self.category,
            "capability": self.capability,
            "summary": self.summary,
            "source_id": self.source_id,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "transient": self.transient,
        }


@dataclass
class EvidenceGroup:
    """同一能力 + 同一错误签名的一组失败证据（聚合后的最小分析单元）。"""

    group_key: str
    category: str
    capability: str
    error_signature: str
    items: list[EvidenceItem] = field(default_factory=list)

    @property
    def structural(self) -> bool:
        return self.category in STRUCTURAL_CATEGORIES

    @property
    def occurrences(self) -> int:
        return len(self.items)

    @property
    def evidence_ids(self) -> list[str]:
        return [item.evidence_id for item in self.items]

    @property
    def transient_ratio(self) -> float:
        if not self.items:
            return 0.0
        return sum(1 for item in self.items if item.transient) / len(self.items)

    @property
    def first_seen(self) -> datetime | None:
        stamps = [item.observed_at for item in self.items if item.observed_at]
        return min(stamps) if stamps else None

    @property
    def last_seen(self) -> datetime | None:
        stamps = [item.observed_at for item in self.items if item.observed_at]
        return max(stamps) if stamps else None

    def to_prompt_dict(self) -> dict[str, Any]:
        """给模型看的证据组：只给可验证事实，不给猜测。"""
        return {
            "group_key": self.group_key,
            "category": self.category,
            "capability": self.capability,
            "occurrences": self.occurrences,
            "evidence_ids": self.evidence_ids,
            "first_seen": self.first_seen.isoformat() if self.first_seen else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "transient_ratio": round(self.transient_ratio, 2),
            "samples": [item.summary for item in self.items[:3]],
        }

    def to_public_dict(self) -> dict[str, Any]:
        """给用户/API 看的证据组（含逐条证据，可回溯）。"""
        payload = self.to_prompt_dict()
        payload["items"] = [item.to_dict() for item in self.items]
        return payload


@dataclass
class EvidenceBatch:
    """一次发现轮次的证据输入；groups 只包含通过过滤的组。"""

    groups: list[EvidenceGroup] = field(default_factory=list)
    window_start: datetime | None = None
    window_end: datetime | None = None
    scanned: dict[str, int] = field(default_factory=dict)
    dropped: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.groups

    @property
    def evidence_ids(self) -> set[str]:
        return {item.evidence_id for group in self.groups for item in group.items}

    def group_for_evidence(self, evidence_id: str) -> EvidenceGroup | None:
        for group in self.groups:
            if evidence_id in group.evidence_ids:
                return group
        return None

    def max_occurrences_for(self, evidence_ids: list[str]) -> int:
        """本次输入里这些证据 ID 能支撑的最大发生次数（模型不得夸大）。"""
        matched: set[str] = set()
        for eid in evidence_ids:
            group = self.group_for_evidence(eid)
            if group is not None:
                matched.add(group.group_key)
        total = 0
        for group in self.groups:
            if group.group_key in matched:
                total = max(total, group.occurrences)
        return total

    def primary_group_keys(self, evidence_ids: list[str]) -> list[str]:
        keys: list[str] = []
        for eid in evidence_ids:
            group = self.group_for_evidence(eid)
            if group is not None and group.group_key not in keys:
                keys.append(group.group_key)
        return keys

    def to_prompt_payload(self) -> dict[str, Any]:
        return {
            "window_start": self.window_start.isoformat() if self.window_start else None,
            "window_end": self.window_end.isoformat() if self.window_end else None,
            "groups": [group.to_prompt_dict() for group in self.groups],
        }


class EvidenceCollector:
    """从真实运行记录采集证据并做确定性聚合。

    所有阈值都在构造函数里，测试可直接注入，避免依赖生产常量。
    """

    def __init__(
        self,
        *,
        eval_dir: Any = None,
        per_source_limit: int = DEFAULT_PER_SOURCE_LIMIT,
        min_occurrences: int = DEFAULT_MIN_OCCURRENCES,
        min_transient_occurrences: int = DEFAULT_MIN_TRANSIENT_OCCURRENCES,
        max_groups: int = DEFAULT_MAX_GROUPS,
        lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    ) -> None:
        self._eval_dir: Any = eval_dir
        self._per_source_limit: int = max(1, int(per_source_limit))
        self._min_occurrences: int = max(1, int(min_occurrences))
        self._min_transient_occurrences: int = max(1, int(min_transient_occurrences))
        self._max_groups: int = max(1, int(max_groups))
        self._lookback_hours: int = max(1, int(lookback_hours))

    # ── 入口 ────────────────────────────────────────────────────────
    def collect(
        self,
        db: Session,
        *,
        since: datetime | None = None,
        now: datetime | None = None,
    ) -> EvidenceBatch:
        """采集 + 聚合 + 过滤。since 为上次处理水位（None 表示用默认回看窗口）。"""
        now = now or _utcnow()
        window_start = since or (now - timedelta(hours=self._lookback_hours))
        batch = EvidenceBatch(window_start=window_start, window_end=now)

        items: list[EvidenceItem] = []
        for collect_source in (
            self._task_items,
            self._action_items,
            self._job_items,
            self._selfdev_items,
            self._eval_items,
        ):
            try:
                items.extend(collect_source(db, window_start, batch.scanned))
            except Exception:  # 单个来源异常不应让整轮发现失败
                logger.exception("证据采集失败: %s", collect_source.__name__)
                batch.notes.append(f"source_failed:{collect_source.__name__}")

        groups = self._group(items)
        batch.groups = self._select(groups, batch.dropped)
        return batch

    # ── 各来源 ──────────────────────────────────────────────────────
    def _task_items(
        self, db: Session, since: datetime, scanned: dict[str, int]
    ) -> list[EvidenceItem]:
        """持久任务的 partial / failed 终态及其未解决问题。"""
        rows = (
            db.query(AgentTask)
            .filter(
                AgentTask.status.in_(_TASK_FAILURE_STATUSES),
                AgentTask.updated_at >= since,
            )
            .order_by(AgentTask.updated_at.desc())
            .limit(self._per_source_limit)
            .all()
        )
        scanned["agent_task"] = len(rows)
        items: list[EvidenceItem] = []
        for task in rows:
            report = task.report if isinstance(task.report, dict) else {}
            unresolved = report.get("unresolved")
            detail = _text(task.last_error) or _text(report.get("summary")) or _text(unresolved)
            if not detail:
                detail = f"task ended {task.status}"
            signature = normalize_signature(detail)
            capability = _text(task.task_type, 64) or "general"
            items.append(EvidenceItem(
                evidence_id=f"agent_task:{task.id}",
                category=CATEGORY_TASK,
                group_key=f"task:{capability}:{signature}",
                capability=capability,
                error_signature=signature,
                summary=_text(f"[{task.status}] {task.title}: {detail}"),
                source_id=task.id,
                observed_at=task.updated_at or task.created_at,
                transient=is_transient(detail),
            ))
        return items

    def _action_items(
        self, db: Session, since: datetime, scanned: dict[str, int]
    ) -> list[EvidenceItem]:
        """重复出现的 Action 失败（工具错误证据）。"""
        rows = (
            db.query(AgentAction)
            .filter(
                AgentAction.status.in_(_ACTION_FAILURE_STATUSES),
                AgentAction.updated_at >= since,
            )
            .order_by(AgentAction.updated_at.desc())
            .limit(self._per_source_limit)
            .all()
        )
        scanned["agent_action"] = len(rows)
        items: list[EvidenceItem] = []
        for action in rows:
            detail = _text(action.error_message) or _text(action.result_summary) or action.status
            signature = normalize_signature(detail)
            capability = _text(action.capability_id, 128) or "unknown"
            items.append(EvidenceItem(
                evidence_id=f"agent_action:{action.id}",
                category=CATEGORY_ACTION,
                group_key=f"action:{capability}:{signature}",
                capability=capability,
                error_signature=signature,
                summary=_text(f"[{action.status}] {capability}: {detail}"),
                source_id=action.id,
                observed_at=action.updated_at or action.created_at,
                transient=is_transient(detail),
            ))
        return items

    def _job_items(
        self, db: Session, since: datetime, scanned: dict[str, int]
    ) -> list[EvidenceItem]:
        """Outbox 死信/失败：后台链路自身的缺陷证据。"""
        rows = (
            db.query(OutboxJob)
            .filter(
                OutboxJob.status.in_(_JOB_FAILURE_STATUSES),
                OutboxJob.updated_at >= since,
            )
            .order_by(OutboxJob.updated_at.desc())
            .limit(self._per_source_limit)
            .all()
        )
        scanned["outbox_job"] = len(rows)
        items: list[EvidenceItem] = []
        for job in rows:
            detail = _text(job.error_message) or _text(job.terminal_reason) or job.status
            signature = normalize_signature(detail)
            job_type = _text(job.job_type, 64) or "unknown"
            items.append(EvidenceItem(
                evidence_id=f"outbox_job:{job.id}",
                category=CATEGORY_JOB,
                group_key=f"job:{job_type}:{signature}",
                capability=job_type,
                error_signature=signature,
                summary=_text(f"[{job.status}] {job_type}: {detail}"),
                source_id=job.id,
                observed_at=job.updated_at or job.created_at,
                transient=is_transient(detail),
            ))
        return items

    def _selfdev_items(
        self, db: Session, since: datetime, scanned: dict[str, int]
    ) -> list[EvidenceItem]:
        """自进化修复失败 / 安全门阻断（结构性证据）。"""
        rows = (
            db.query(SelfDevRequest)
            .filter(
                SelfDevRequest.created_at >= since,
            )
            .order_by(SelfDevRequest.created_at.desc())
            .limit(self._per_source_limit)
            .all()
        )
        failed = [row for row in rows if _is_selfdev_failure(row.status)]
        scanned["selfdev_request"] = len(failed)
        items: list[EvidenceItem] = []
        for row in failed:
            plan = dict(row.plan or {})
            raw_repair = plan.get("repair")
            repair: dict[str, Any] = raw_repair if isinstance(raw_repair, dict) else {}
            reason = _text(repair.get("failure_reason")) or _text(row.status)
            signature = normalize_signature(reason) or normalize_signature(row.status)
            items.append(EvidenceItem(
                evidence_id=f"selfdev_request:{row.id}",
                category=CATEGORY_SELFDEV,
                group_key=f"selfdev:{row.status}:{signature}",
                capability="selfdev_repair",
                error_signature=signature,
                summary=_text(f"[{row.status}] {_text(row.goal, 160)}: {reason}"),
                source_id=row.id,
                observed_at=row.created_at,
                transient=is_transient(reason),
            ))
        return items

    def _eval_items(
        self, _db: Session, since: datetime, scanned: dict[str, int]
    ) -> list[EvidenceItem]:
        """Eval 安全门失败（结构性证据，来自文件型运行记录）。"""
        from aiive.eval.persistence import load_runs_with_mtime

        try:
            runs = load_runs_with_mtime(self._eval_dir)
        except Exception:
            logger.exception("读取 Eval 运行记录失败")
            scanned["eval_run"] = 0
            return []

        items: list[EvidenceItem] = []
        counted = 0
        for summary, mtime in runs:
            if summary.get("ok") is not False:
                continue
            observed = datetime.fromtimestamp(mtime, tz=UTC)
            if observed < since:
                continue
            counted += 1
            if counted > self._per_source_limit:
                break
            detail = _text(summary.get("error")) or _eval_failure_detail(summary)
            signature = normalize_signature(detail) or "gate_failed"
            run_id = _text(summary.get("run_id"), 64) or f"eval_{int(mtime)}"
            items.append(EvidenceItem(
                evidence_id=f"eval_run:{run_id}",
                category=CATEGORY_EVAL,
                group_key=f"eval:{signature}",
                capability="eval_gate",
                error_signature=signature,
                summary=_text(f"[gate failed] {detail}"),
                source_id=run_id,
                observed_at=observed,
                transient=False,
            ))
        scanned["eval_run"] = counted
        return items

    # ── 聚合与过滤 ──────────────────────────────────────────────────
    @staticmethod
    def _group(items: list[EvidenceItem]) -> list[EvidenceGroup]:
        groups: dict[str, EvidenceGroup] = {}
        for item in items:
            group = groups.get(item.group_key)
            if group is None:
                group = EvidenceGroup(
                    group_key=item.group_key,
                    category=item.category,
                    capability=item.capability,
                    error_signature=item.error_signature,
                )
                groups[item.group_key] = group
            group.items.append(item)
        return list(groups.values())

    def _select(
        self, groups: list[EvidenceGroup], dropped: dict[str, int]
    ) -> list[EvidenceGroup]:
        """按确定性规则丢弃噪声，只留下值得交给模型的组。"""
        kept: list[EvidenceGroup] = []
        for group in groups:
            if group.structural:
                kept.append(group)
                continue
            if group.occurrences < self._min_occurrences:
                dropped["below_min_occurrences"] = dropped.get("below_min_occurrences", 0) + 1
                continue
            # 全部是环境类故障且次数不够：判定为一次性抖动，不当成系统缺陷。
            if group.transient_ratio >= 1.0 and group.occurrences < self._min_transient_occurrences:
                dropped["transient_only"] = dropped.get("transient_only", 0) + 1
                continue
            kept.append(group)

        kept.sort(
            key=lambda g: (
                0 if g.structural else 1,
                -g.occurrences,
                -(g.last_seen.timestamp() if g.last_seen else 0.0),
            )
        )
        if len(kept) > self._max_groups:
            dropped["over_group_cap"] = (
                dropped.get("over_group_cap", 0) + (len(kept) - self._max_groups)
            )
            kept = kept[: self._max_groups]
        return kept


def _is_selfdev_failure(status: str | None) -> bool:
    value = (status or "").strip().lower()
    if not value:
        return False
    return value == "failed" or value.endswith("_failed")


def _eval_failure_detail(summary: dict[str, Any]) -> str:
    """从 Eval 摘要里提取最具体的失败原因（先契约、再场景）。"""
    contracts = summary.get("contracts")
    if isinstance(contracts, list):
        for item in contracts:
            if isinstance(item, dict) and item.get("ok") is False:
                return f"contract {item.get('name')}: {item.get('detail') or 'failed'}"
    for key in ("replay", "candidate_eval"):
        run = summary.get(key)
        if not isinstance(run, dict):
            continue
        if run.get("error"):
            return f"{key} crashed: {run.get('error')}"
        cases = run.get("cases")
        if isinstance(cases, list):
            for case in cases:
                if isinstance(case, dict) and case.get("ok") is False:
                    return f"{key} {case.get('scenario_id')}: {case.get('error') or 'failed'}"
    return "gate failed"
