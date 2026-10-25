"""运行证据驱动的改进候选发现。

流程（与 Goal 对齐）：

1. Phase A（短事务）：读上次处理水位 + 采集/聚合证据，随后关闭会话。
2. 没有新证据时**不调用模型**，只写一条运行事件推进水位。
3. Phase B（无会话）：把聚合后的证据一次性交给模型，产出少量候选；
   绝不为每条运行记录单独发起反思调用。
4. Phase C（短事务）：服务端逐条校验候选（证据 ID 必须属于本次输入、次数不得
   超过证据支撑、验收标准必须可验证），再落库去重并写运行事件。

候选复用 selfdev_requests 表（不新增表）：id 由去重键派生（uuid5），
status 使用 candidate_* 前缀与修复运行的 failed/applied 等状态区分；
plan["candidate"] 保存候选正文与证据，plan["decision"] 保存接受/拒绝结果。

本模块不修改代码、不晋升、不触碰活跃槽位：接受候选只是把候选变成
SelfDevRepairLoop 的明确目标，其余权限与安全门全部沿用既有实现。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aiive.db.models import Event, SelfDevRequest, Thread
from aiive.selfdev.improvement_evidence import EvidenceBatch, EvidenceCollector

logger = logging.getLogger(__name__)

DISCOVERY_EVENT_TYPE = "improvement_discovery_run"
DISCOVERY_CATEGORY = "selfdev_improvement"

CANDIDATE_STATUS_PENDING = "candidate_pending"
CANDIDATE_STATUS_ACCEPTED = "candidate_accepted"
CANDIDATE_STATUS_REJECTED = "candidate_rejected"
CANDIDATE_STATUSES: frozenset[str] = frozenset({
    CANDIDATE_STATUS_PENDING,
    CANDIDATE_STATUS_ACCEPTED,
    CANDIDATE_STATUS_REJECTED,
})

RUN_STATUS_NO_EVIDENCE = "no_evidence"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_EMPTY_RESULT = "empty_result"
RUN_STATUS_INVALID_OUTPUT = "invalid_output"
RUN_STATUS_LLM_FAILED = "llm_failed"

DEFAULT_MAX_CANDIDATES = 3
_MAX_CANDIDATE_TITLE = 200
_MAX_GOAL = 900
_NAMESPACE = uuid.UUID("6f1a7d3c-2b4e-5a89-9c01-7e5b2d4f8a13")

_JSON_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_CONFIDENCE_LEVELS: tuple[str, ...] = ("low", "medium", "high")


class CandidateNotFound(Exception):
    """候选不存在。"""


class CandidateStateError(Exception):
    """候选当前状态不允许该操作（例如已接受/已拒绝）。"""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: Any) -> datetime | None:
    """把从数据库读回的（可能是 naive 的）时间统一成 UTC aware。"""
    if value is None:
        return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _clean(value: Any, limit: int) -> str:
    if value is None:
        return ""
    return _WS_RE.sub(" ", str(value)).strip()[:limit]


def _normalize_title(value: str) -> str:
    return _WS_RE.sub(" ", value).strip().lower()


def candidate_identity(dedupe_key: str) -> str:
    """去重键 → 稳定的候选 ID（同一条问题永远落在同一行，重复处理不新增）。"""
    return str(uuid.uuid5(_NAMESPACE, dedupe_key))


def dedupe_key_for(primary_group_keys: list[str], title: str) -> str:
    """候选去重键：证据组 + 归一化标题。

    同一批证据 + 同一个问题 → 同一个键 → 同一个 ID，天然幂等；
    证据组不同（问题不同）时不会被误合并。
    """
    seed = "|".join(sorted(primary_group_keys)) + "::" + _normalize_title(title)
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:40]


@dataclass
class CandidateDraft:
    """通过服务端校验、可以落库的候选。"""

    dedupe_key: str
    title: str
    problem: str
    impact: str
    evidence_ids: list[str]
    occurrences: int
    possible_causes: list[dict[str, Any]] = field(default_factory=list)
    target: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)
    test_method: str = ""
    scope: str = "unknown"
    risk: str = "low"
    confidence: str = "low"
    primary_group_keys: list[str] = field(default_factory=list)

    @property
    def candidate_id(self) -> str:
        return candidate_identity(self.dedupe_key)

    @property
    def repair_goal(self) -> str:
        """交给 SelfDevRepairLoop 的明确目标（只描述目标，不含补丁）。"""
        parts = [self.title, f"问题：{self.problem}"]
        if self.impact:
            parts.append(f"影响：{self.impact}")
        if self.target:
            parts.append(f"建议改进目标：{self.target}")
        if self.acceptance_criteria:
            parts.append("验收标准：" + "；".join(self.acceptance_criteria))
        if self.test_method:
            parts.append(f"建议测试方法：{self.test_method}")
        return _clean("。".join(parts), _MAX_GOAL)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "dedupe_key": self.dedupe_key,
            "title": self.title,
            "problem": self.problem,
            "impact": self.impact,
            "evidence_ids": list(self.evidence_ids),
            "occurrences": self.occurrences,
            "possible_causes": [dict(item) for item in self.possible_causes],
            "target": self.target,
            "acceptance_criteria": list(self.acceptance_criteria),
            "test_method": self.test_method,
            "scope": self.scope,
            "risk": self.risk,
            "confidence": self.confidence,
            "primary_group_keys": list(self.primary_group_keys),
            "repair_goal": self.repair_goal,
        }


def candidate_payload(row: SelfDevRequest) -> dict[str, Any]:
    """从 selfdev_requests 行里取回候选正文。"""
    plan = dict(row.plan or {})
    payload = plan.get("candidate")
    return payload if isinstance(payload, dict) else {}


def is_candidate_row(row: SelfDevRequest) -> bool:
    return (row.status or "").startswith("candidate_")


class ImprovementDiscovery:
    """证据 → 候选 的编排器（依赖可注入，便于离线测试）。"""

    def __init__(
        self,
        llm_client: Any = None,
        *,
        collector: EvidenceCollector | None = None,
        session_factory: Callable[[], Session] | None = None,
        max_candidates: int = DEFAULT_MAX_CANDIDATES,
        eval_dir: Any = None,
    ) -> None:
        self._llm: Any = llm_client
        self._collector: EvidenceCollector = collector or EvidenceCollector(eval_dir=eval_dir)
        if session_factory is None:
            from aiive.db.base import SessionLocal

            session_factory = SessionLocal
        self._session_factory: Callable[[], Session] = session_factory
        self._max_candidates: int = max(
            1, min(int(max_candidates), DEFAULT_MAX_CANDIDATES)
        )

    # ── 主流程 ──────────────────────────────────────────────────────
    def run(
        self,
        *,
        now: datetime | None = None,
        trace_id: str | None = None,
        db: Session | None = None,
    ) -> dict[str, Any]:
        """执行一轮发现。返回可审计摘要（不返回候选全文）。

        db 只给测试/调用方托管会话用：传入时不在本方法内关闭，也不跨阶段复用
        同一个未提交事务语义（生产路径始终由本方法自己开关短事务）。
        """
        now = _as_utc(now) or _utcnow()
        trace_id = trace_id or str(uuid.uuid4())
        owned = db is None

        def _session() -> Session:
            if owned:
                return self._session_factory()
            assert db is not None
            return db

        def _close(session: Session) -> None:
            if owned:
                session.close()

        def _persist_once(**kwargs: Any) -> tuple[list[str], list[str]]:
            session = _session()
            try:
                return self._persist(db=session, **kwargs)
            finally:
                _close(session)

        # Phase A：短事务读水位 + 采集证据（模型调用不持有会话）。
        read_session = _session()
        try:
            cursor = self._load_cursor(read_session)
            batch = self._collector.collect(read_session, since=cursor, now=now)
        finally:
            _close(read_session)

        if batch.is_empty:
            summary = self._summary(
                status=RUN_STATUS_NO_EVIDENCE, batch=batch, llm_called=False,
                created=[], rejected=[], duplicates=[], cursor=cursor, now=now,
            )
            _persist_once(now=now, trace_id=trace_id, batch=batch, drafts=[],
                          summary=summary, llm_called=False, advance=True)
            return summary

        # Phase B：一次模型调用（没有证据时根本不会走到这里）。
        try:
            drafts, rejected = self._ask_model(batch, trace_id=trace_id)
        except (TypeError, ValueError) as exc:
            logger.warning("改进候选解析失败: %s", exc)
            summary = self._summary(
                status=RUN_STATUS_INVALID_OUTPUT, batch=batch, llm_called=True,
                created=[], rejected=[{"index": -1, "reason": str(exc)}],
                duplicates=[], cursor=cursor, now=now,
            )
            _persist_once(now=now, trace_id=trace_id, batch=batch, drafts=[],
                          summary=summary, llm_called=True, advance=False)
            return summary
        except Exception as exc:  # 模型/网络失败：不推进水位，下轮重试同一批
            logger.exception("改进候选生成失败")
            summary = self._summary(
                status=RUN_STATUS_LLM_FAILED, batch=batch, llm_called=True,
                created=[], rejected=[{"index": -1, "reason": type(exc).__name__}],
                duplicates=[], cursor=cursor, now=now,
            )
            _persist_once(now=now, trace_id=trace_id, batch=batch, drafts=[],
                          summary=summary, llm_called=True, advance=False)
            return summary

        # Phase C：短事务落库（服务端已校验过的候选）。
        created, duplicates = _persist_once(
            now=now, trace_id=trace_id, batch=batch, drafts=drafts,
            summary=None, llm_called=True, advance=True,
        )
        status = RUN_STATUS_COMPLETED if created else RUN_STATUS_EMPTY_RESULT
        summary = self._summary(
            status=status, batch=batch, llm_called=True,
            created=created, rejected=rejected, duplicates=duplicates,
            cursor=cursor, now=now,
        )
        # 运行事件里补上最终结论（候选 ID 与拒绝原因要能追溯）。
        _persist_once(now=now, trace_id=trace_id, batch=batch, drafts=[],
                      summary=summary, llm_called=True, advance=True, only_event=True)
        return summary

    # ── 模型调用与校验 ──────────────────────────────────────────────
    def _ask_model(
        self, batch: EvidenceBatch, *, trace_id: str
    ) -> tuple[list[CandidateDraft], list[dict[str, Any]]]:
        client = self._llm
        if client is None:
            from aiive.core.llm_client import default_llm_client

            client = default_llm_client()
        messages = self._build_messages(batch)
        response = client.chat(
            messages,
            temperature=0.2,
            trace_id=trace_id,
            json_mode=True,
        )
        raw = getattr(response, "content", "") or ""
        payload = self._parse_payload(raw)
        drafts, rejected = self._validate_payload(payload, batch)
        return drafts, rejected

    def _build_messages(self, batch: EvidenceBatch) -> list[dict[str, Any]]:
        evidence_json = json.dumps(batch.to_prompt_payload(), ensure_ascii=False, indent=2)
        system = (
            "你是 AIive 的运行改进分析器。你会收到一批来自真实运行记录的失败证据，"
            "任务是判断其中是否有值得改进的系统性问题，并给出可验证的改进目标。"
        )
        rules = [
            "1. 只依据给定证据判断，不得编造证据 ID、能力名或发生次数。",
            "2. evidence_ids 只能取自证据组里出现的 ID；occurrences 不得超过该证据组的 occurrences。",
            "3. 同一类问题只输出一个候选，不要为同一组证据重复生成。",
            "4. 没有值得改进的问题时输出空列表，宁可为空也不要凑数。",
            "5. 只描述建议改进的目标，不要写具体代码补丁或文件内容。",
            "6. 单次、可自愈的偶发故障（超时、连接重置、限流等）不是系统缺陷。",
            f"7. 最多输出 {self._max_candidates} 个候选，按影响从大到小排序。",
            "8. 无法从证据确认的原因必须标注 uncertain=true，不要写成结论。",
        ]
        schema = (
            '{"candidates": [{"title": "问题标题", "problem": "问题描述", "impact": "影响",'
            ' "evidence_ids": ["agent_task:..."], "occurrences": 2,'
            ' "possible_causes": [{"cause": "可能原因", "confidence": "low|medium|high",'
            ' "uncertain": true}], "target": "建议改进目标",'
            ' "acceptance_criteria": ["可验证的验收标准"], "test_method": "建议测试方法",'
            ' "scope": "影响范围", "risk": "low|medium|high",'
            ' "confidence": "low|medium|high"}], "notes": ""}'
        )
        user = "\n".join([
            *rules,
            "",
            "证据（确定性聚合结果，已过滤一次性故障）：",
            evidence_json,
            "",
            "只输出 JSON，结构如下：",
            schema,
        ])
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    @staticmethod
    def _parse_payload(raw: str) -> dict[str, Any]:
        text = _JSON_FENCE_RE.sub("", raw.strip())
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no_json_object")
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid_json:{exc.msg}") from exc
        if not isinstance(payload, dict):
            raise TypeError("not_an_object")
        return payload

    def _validate_payload(
        self, payload: dict[str, Any], batch: EvidenceBatch
    ) -> tuple[list[CandidateDraft], list[dict[str, Any]]]:
        raw_candidates = payload.get("candidates")
        if raw_candidates is None:
            return [], []
        if not isinstance(raw_candidates, list):
            raise TypeError("candidates_not_list")

        drafts: list[CandidateDraft] = []
        rejected: list[dict[str, Any]] = []
        seen_groups: set[str] = set()
        for index, raw in enumerate(raw_candidates):
            if len(drafts) >= self._max_candidates:
                rejected.append({"index": index, "reason": "over_candidate_cap"})
                continue
            draft, reason = self._validate_candidate(raw, batch)
            if draft is None:
                rejected.append({"index": index, "reason": reason})
                continue
            # 同一证据组只保留一个候选（避免同一问题重复出现）。
            overlap = set(draft.primary_group_keys) & seen_groups
            if overlap:
                rejected.append({"index": index, "reason": "duplicate_group"})
                continue
            seen_groups.update(draft.primary_group_keys)
            drafts.append(draft)
        return drafts, rejected

    def _validate_candidate(
        self, raw: Any, batch: EvidenceBatch
    ) -> tuple[CandidateDraft | None, str]:
        """服务端硬校验：模型说的事实必须能被本次证据证明。"""
        if not isinstance(raw, dict):
            return None, "invalid_shape"
        title = _clean(raw.get("title"), _MAX_CANDIDATE_TITLE)
        problem = _clean(raw.get("problem"), 1200)
        if not title or not problem:
            return None, "missing_title_or_problem"

        raw_ids = raw.get("evidence_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            return None, "missing_evidence_ids"
        evidence_ids = [_clean(item, 128) for item in raw_ids if _clean(item, 128)]
        allowed = batch.evidence_ids
        if any(eid not in allowed for eid in evidence_ids):
            # 编造或引用了本次输入之外的证据：整条候选作废。
            return None, "unknown_evidence_ids"

        occurrences = raw.get("occurrences")
        if isinstance(occurrences, bool) or not isinstance(occurrences, int) or occurrences < 1:
            return None, "invalid_occurrences"
        supported = batch.max_occurrences_for(evidence_ids)
        if occurrences > supported:
            return None, "occurrences_exceed_evidence"

        criteria_raw = raw.get("acceptance_criteria")
        if not isinstance(criteria_raw, list):
            return None, "missing_acceptance_criteria"
        criteria = [_clean(item, 300) for item in criteria_raw if _clean(item, 300)]
        if not criteria:
            return None, "missing_acceptance_criteria"

        target = _clean(raw.get("target"), 600)
        if not target:
            return None, "missing_target"

        primary = batch.primary_group_keys(evidence_ids)
        if not primary:
            return None, "no_matching_group"

        return CandidateDraft(
            dedupe_key=dedupe_key_for(primary, title),
            title=title,
            problem=problem,
            impact=_clean(raw.get("impact"), 600),
            evidence_ids=evidence_ids,
            occurrences=occurrences,
            possible_causes=self._normalize_causes(raw.get("possible_causes")),
            target=target,
            acceptance_criteria=criteria,
            test_method=_clean(raw.get("test_method"), 600),
            scope=_clean(raw.get("scope"), 200) or "unknown",
            risk=_normalize_level(raw.get("risk"), default="low"),
            confidence=_normalize_level(raw.get("confidence"), default="low"),
            primary_group_keys=primary,
        ), ""

    @staticmethod
    def _normalize_causes(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        causes: list[dict[str, Any]] = []
        for item in value[:5]:
            if isinstance(item, dict):
                cause = _clean(item.get("cause"), 300)
                if not cause:
                    continue
                causes.append({
                    "cause": cause,
                    "confidence": _normalize_level(item.get("confidence"), default="low"),
                    "uncertain": bool(item.get("uncertain", True)),
                })
            else:
                cause = _clean(item, 300)
                if cause:
                    causes.append({"cause": cause, "confidence": "low", "uncertain": True})
        return causes

    # ── 持久化 ──────────────────────────────────────────────────────
    def _persist(
        self,
        *,
        db: Session,
        now: datetime,
        trace_id: str,
        batch: EvidenceBatch,
        drafts: list[CandidateDraft],
        summary: dict[str, Any] | None,
        llm_called: bool,
        advance: bool,
        only_event: bool = False,
    ) -> tuple[list[str], list[str]]:
        """短事务落库：候选（去重）+ 运行事件（推进水位）。会话由调用方开关。"""
        created: list[str] = []
        duplicates: list[str] = []
        try:
            if not only_event:
                for draft in drafts:
                    if self._candidate_exists(db, draft):
                        duplicates.append(draft.candidate_id)
                        continue
                    row = SelfDevRequest(
                        id=draft.candidate_id,
                        trace_id=trace_id,
                        status=CANDIDATE_STATUS_PENDING,
                        goal=draft.repair_goal,
                        plan={
                            "candidate": {
                                **draft.to_dict(),
                                "discovered_at": now.isoformat(),
                                "discovery_trace_id": trace_id,
                            },
                            "evidence_groups": [
                                group.to_public_dict()
                                for group in batch.groups
                                if group.group_key in set(draft.primary_group_keys)
                            ],
                        },
                    )
                    try:
                        with db.begin_nested():
                            db.add(row)
                            db.flush()
                    except IntegrityError:
                        duplicates.append(draft.candidate_id)
                        continue
                    created.append(draft.candidate_id)
                    self._publish_notification(db, row, draft)
                db.commit()
            if summary is not None or only_event:
                self._write_run_event(
                    db, now=now, trace_id=trace_id, batch=batch,
                    summary=summary or {}, llm_called=llm_called, advance=advance,
                )
                db.commit()
        except Exception:
            db.rollback()
            raise
        return created, duplicates

    @staticmethod
    def _candidate_exists(db: Session, draft: CandidateDraft) -> bool:
        if db.get(SelfDevRequest, draft.candidate_id) is not None:
            return True
        # 同一证据组已有待处理候选时也不再重复创建（防止同一问题换个措辞再来一遍）。
        pending = (
            db.query(SelfDevRequest)
            .filter(SelfDevRequest.status == CANDIDATE_STATUS_PENDING)
            .limit(200)
            .all()
        )
        for row in pending:
            payload = candidate_payload(row)
            keys = payload.get("primary_group_keys")
            if isinstance(keys, list) and set(keys) & set(draft.primary_group_keys):
                return True
        return False

    def _publish_notification(
        self, db: Session, row: SelfDevRequest, draft: CandidateDraft
    ) -> None:
        """复用现有通知读模型：候选进入系统线程的待办通知。"""
        from aiive.runtime.thread_bootstrap import SYSTEM_THREAD_ID

        _ensure_system_thread(db)
        message = f"发现可改进问题：{draft.title}"
        db.add(Event(
            trace_id=row.id,
            thread_id=SYSTEM_THREAD_ID,
            event_type="notification_created",
            payload={
                "title": "发现一个可改进问题",
                "content": message,
                "message": message,
                "status": "pending",
                "category": DISCOVERY_CATEGORY,
                "improvement_candidate_id": row.id,
                "confidence": draft.confidence,
                "risk": draft.risk,
            },
        ))
        db.flush()
        try:
            from aiive.api.routes_notifications import count_pending_notifications
            from aiive.api.ws_manager import ws_manager

            ws_manager.broadcast_notification_sync(count_pending_notifications(db))
        except Exception:
            logger.warning("改进候选通知角标广播失败（不影响落库）", exc_info=True)

    def _write_run_event(
        self,
        db: Session,
        *,
        now: datetime,
        trace_id: str,
        batch: EvidenceBatch,
        summary: dict[str, Any],
        llm_called: bool,
        advance: bool,
    ) -> None:
        from aiive.runtime.thread_bootstrap import SYSTEM_THREAD_ID

        # 进度记录必须落得下：系统线程与启动引导用的是同一个固定 ID。
        _ensure_system_thread(db)
        db.add(Event(
            trace_id=trace_id,
            thread_id=SYSTEM_THREAD_ID,
            event_type=DISCOVERY_EVENT_TYPE,
            payload={
                "watermark": now.isoformat() if advance else None,
                "advanced": bool(advance),
                "llm_called": bool(llm_called),
                "status": summary.get("status", ""),
                "group_count": len(batch.groups),
                "evidence_count": len(batch.evidence_ids),
                "candidate_ids": list(summary.get("candidate_ids") or []),
                "rejected": list(summary.get("rejected") or []),
                "duplicates": list(summary.get("duplicate_candidate_ids") or []),
                "scanned": dict(batch.scanned),
                "dropped": dict(batch.dropped),
            },
        ))
        db.flush()

    def _load_cursor(self, db: Session) -> datetime | None:
        """上次成功处理的水位（最近一条带 watermark 的运行事件）。

        失败/非法输出的运行事件不推进水位，因此这里必须跳过它们，否则一次模型
        故障就会把进度退回到"全量回看"，重复分析同一批证据。
        """
        events = (
            db.query(Event)
            .filter(Event.event_type == DISCOVERY_EVENT_TYPE)
            .order_by(Event.created_at.desc())
            .limit(50)
            .all()
        )
        for event in events:
            payload = event.payload or {}
            watermark = _as_utc(_parse_iso(payload.get("watermark")))
            if watermark is not None:
                return watermark
        return None

    # ── 查询 / 接受 / 拒绝 ──────────────────────────────────────────
    @staticmethod
    def list_candidates(
        db: Session, *, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        query = db.query(SelfDevRequest)
        if status:
            query = query.filter(SelfDevRequest.status == status)
        else:
            query = query.filter(SelfDevRequest.status.in_(tuple(CANDIDATE_STATUSES)))
        rows = query.order_by(SelfDevRequest.created_at.desc()).limit(max(1, min(limit, 200))).all()
        return [describe_candidate(row) for row in rows]

    @staticmethod
    def get_candidate(db: Session, candidate_id: str) -> dict[str, Any]:
        row = db.get(SelfDevRequest, candidate_id)
        if row is None or not is_candidate_row(row):
            raise CandidateNotFound(candidate_id)
        return describe_candidate(row)

    def accept(
        self,
        db: Session,
        candidate_id: str,
        *,
        repair_runner: Callable[..., dict[str, Any]] | None = None,
        trace_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """接受候选：把它作为明确目标交给既有 SelfDevRepairLoop（不自动晋升）。"""
        now = _as_utc(now) or _utcnow()
        row = db.get(SelfDevRequest, candidate_id)
        if row is None or not is_candidate_row(row):
            raise CandidateNotFound(candidate_id)
        if row.status != CANDIDATE_STATUS_PENDING:
            raise CandidateStateError(row.status)
        payload = candidate_payload(row)
        goal = str(payload.get("repair_goal") or row.goal or "").strip()
        if not goal:
            raise CandidateStateError("candidate_goal_missing")

        runner = repair_runner or _default_repair_runner
        record = runner(goal, trace_id=trace_id or row.trace_id)

        from aiive.selfdev.repair_loop import persist_repair_run

        plan = dict(row.plan or {})
        plan["decision"] = {
            "status": "accepted",
            "decided_at": now.isoformat(),
            "repair_run_id": record.get("run_id"),
            "repair_status": record.get("status"),
            "promote": False,
        }
        row.plan = plan
        row.status = CANDIDATE_STATUS_ACCEPTED
        db.flush()
        # persist_repair_run 内部提交：候选状态与修复记录同一个事务落库。
        persist_repair_run(db, record)
        plan["decision"]["repair_request_id"] = record.get("request_id")
        row.plan = plan
        db.commit()
        return {
            "candidate_id": candidate_id,
            "status": row.status,
            "goal": goal,
            "repair": {
                "run_id": record.get("run_id"),
                "status": record.get("status"),
                "ok": record.get("ok"),
                "request_id": record.get("request_id"),
                "promotion": record.get("promotion"),
            },
        }

    @staticmethod
    def reject(
        db: Session,
        candidate_id: str,
        *,
        reason: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = _as_utc(now) or _utcnow()
        row = db.get(SelfDevRequest, candidate_id)
        if row is None or not is_candidate_row(row):
            raise CandidateNotFound(candidate_id)
        if row.status != CANDIDATE_STATUS_PENDING:
            raise CandidateStateError(row.status)
        plan = dict(row.plan or {})
        plan["decision"] = {
            "status": "rejected",
            "decided_at": now.isoformat(),
            "reason": _clean(reason, 300),
        }
        row.plan = plan
        row.status = CANDIDATE_STATUS_REJECTED
        db.commit()
        return {"candidate_id": candidate_id, "status": row.status}

    # ── 摘要 ────────────────────────────────────────────────────────
    @staticmethod
    def _summary(
        *,
        status: str,
        batch: EvidenceBatch,
        llm_called: bool,
        created: list[str],
        rejected: list[dict[str, Any]],
        duplicates: list[str],
        cursor: datetime | None,
        now: datetime,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "llm_called": bool(llm_called),
            "candidate_ids": list(created),
            "duplicate_candidate_ids": list(duplicates),
            "rejected": list(rejected),
            "group_count": len(batch.groups),
            "evidence_count": len(batch.evidence_ids),
            "scanned": dict(batch.scanned),
            "dropped": dict(batch.dropped),
            "cursor": cursor.isoformat() if cursor else None,
            "watermark": now.isoformat(),
            "notes": list(batch.notes),
        }


def describe_candidate(row: SelfDevRequest) -> dict[str, Any]:
    """候选的对外表示（含证据组与决策，便于用户核对）。"""
    plan = dict(row.plan or {})
    payload = candidate_payload(row)
    return {
        "candidate_id": row.id,
        "status": row.status,
        "title": payload.get("title", ""),
        "problem": payload.get("problem", ""),
        "impact": payload.get("impact", ""),
        "target": payload.get("target", ""),
        "repair_goal": payload.get("repair_goal", row.goal),
        "evidence_ids": list(payload.get("evidence_ids") or []),
        "occurrences": payload.get("occurrences"),
        "possible_causes": list(payload.get("possible_causes") or []),
        "acceptance_criteria": list(payload.get("acceptance_criteria") or []),
        "test_method": payload.get("test_method", ""),
        "scope": payload.get("scope", ""),
        "risk": payload.get("risk", ""),
        "confidence": payload.get("confidence", ""),
        "primary_group_keys": list(payload.get("primary_group_keys") or []),
        "evidence_groups": list(plan.get("evidence_groups") or []),
        "discovered_at": payload.get("discovered_at"),
        "decision": plan.get("decision") or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _ensure_system_thread(db: Session) -> None:
    """确保固定 ID 的系统线程存在（与 ThreadBootstrapService 同一条记录）。"""
    from aiive.runtime.thread_bootstrap import SYSTEM_THREAD_ID

    if db.get(Thread, SYSTEM_THREAD_ID) is None:
        db.add(Thread(id=SYSTEM_THREAD_ID, title="系统"))
        db.flush()


def _normalize_level(value: Any, *, default: str) -> str:
    text = _clean(value, 16).lower()
    return text if text in _CONFIDENCE_LEVELS else default


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _default_repair_runner(goal: str, *, trace_id: str | None = None) -> dict[str, Any]:
    """默认修复执行器：既有闭环，显式关闭自动晋升。"""
    from aiive.selfdev.repair_loop import SelfDevRepairLoop

    loop = SelfDevRepairLoop()
    return loop.run(goal, trace_id=trace_id, promote=False, run_gate=True)
