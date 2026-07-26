"""Phase 5：DEEP 模式原始历史二阶段回溯（revision 6）。

从统一检索命中的 segment_summary / epoch_checkpoint 反向定位其底层 Segment，
按 TurnRecord.turn_sequence, Event.turn_event_index 顺序加载原始 Turn/Event，
产出 is_raw=True 的 RetrievalHit。受 deep_max_turns / token_budget 约束。

不索引原始事件；仅在 DEEP 模式按需回溯，避免 100k 规模全表扫描。

回源校验（revision 6 全面落实）：
- Event 必须属于命中 Summary 的 source_event_ids 来源集合；
- 逐条重算 event content hash，与 CompactionInput.event_manifest 比对；
- 校验整体 source hash（CompactionInput.source_hash == SegmentSummary.source_hash）；
- 不跨 Segment；
- 任意校验失败 → 不返回该 Segment 的 raw 内容，父 Summary 保留并标注
  verification_status = degraded | stale（stale：manifest/来源缺失无法校验）。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from aiive.db.models import CompactionInput, Event, SegmentSummary, TurnRecord
from aiive.retrieval.retrieval_types import RetrievalHit
from aiive.runtime.compaction import event_content_hash
from aiive.runtime.token_counter import LiteLLMTokenCounter

logger = logging.getLogger(__name__)


class RawHistoryExpander:
    """DEEP 模式原始历史回溯器。"""

    def __init__(self, db: Session) -> None:
        self._db: Session = db

    def expand(
        self,
        db: Session,
        parent_hits: list[RetrievalHit],
        _cfg: Any,
        deep_max_turns: int,
        token_budget: int,
    ) -> list[RetrievalHit]:
        """对命中的 summary/checkpoint 回溯其原始 Turn/Event，并做不可变 manifest 校验。

        父命中（segment_summary / epoch_checkpoint）被就地标注 verification_status，
        调用方会保留父 Summary，仅 raw 内容受校验结果约束。
        """
        candidates = [h for h in parent_hits if h.segment_id or h.epoch_id]
        if not candidates:
            return []

        raw: list[RetrievalHit] = []
        used = 0
        seen_event: set[str] = set()
        # 每命中最多回溯的 event 数上限（按 turn 估算，放宽到 turn*8）
        per_hit_limit = max(50, deep_max_turns * 8)

        for h in candidates:
            # 初始化 verification 状态（父命中就地标注）
            prov = dict(h.provenance)
            prov.setdefault("verification_status", "verified")
            h.provenance = prov

            seg_ids = self._segments_for_hit(db, h)
            seg_statuses: list[str] = []
            # #19：segment_summary 命中优先使用命中自身的 Summary 版本校验，
            # 不再按 segment_id 无排序 .first() 取任意版本。
            hit_summary_id = h.source_id if h.source_type == "segment_summary" else None
            for seg_id in seg_ids:
                status, events = self._load_verified_events(
                    db, seg_id, per_hit_limit, summary_id=hit_summary_id,
                )
                if status is None:
                    # 缺失 Summary / CompactionInput / manifest → 无法校验
                    seg_statuses.append("stale")
                    continue
                seg_statuses.append(status)
                for ev in events:
                    if ev.id in seen_event:
                        continue
                    seen_event.add(ev.id)
                    content = self._event_text(ev)
                    if not content:
                        continue
                    tcount = LiteLLMTokenCounter.count_text(content)
                    if used + tcount > token_budget:
                        return raw
                    raw.append(RetrievalHit(
                        source_type="raw_event",
                        source_id=ev.id,
                        source_version="",
                        retrieval_tier="cold",
                        score=1.0,
                        lexical_score=1.0,
                        final_score=1.0,
                        title=f"raw@{ev.event_type}",
                        snippet=content,
                        thread_id=ev.thread_id,
                        epoch_id=h.epoch_id,
                        segment_id=h.segment_id,
                        provenance={"source_hit": h.source_id, "turn_id": ev.turn_id},
                        retrieval_reason="deep raw backtrack",
                        token_count=tcount,
                        route="deep_raw",
                        is_raw=True,
                    ))
                    used += tcount
            # 汇总该父命中的 verification 状态（stale 优先于 degraded）
            if "stale" in seg_statuses:
                h.provenance["verification_status"] = "stale"
            elif "degraded" in seg_statuses:
                h.provenance["verification_status"] = "degraded"

        return raw

    def _segments_for_hit(self, db: Session, h: RetrievalHit) -> list[str]:
        if h.segment_id:
            return [h.segment_id]
        if h.epoch_id:
            from aiive.db.models import Segment
            rows = db.query(Segment.id).filter(
                Segment.epoch_id == h.epoch_id,
            ).all()
            return [r[0] for r in rows]
        return []

    def _load_verified_events(
        self, db: Session, seg_id: str, limit: int,
        summary_id: str | None = None,
    ) -> tuple[str | None, list[Any]]:
        """回溯并校验单个 Segment 的原始 Event。

        Summary 选取（#19）：
        1. 优先用命中 hit 的 source_id（summary_id）加载对应版本；
        2. 否则用 Segment.summary_id 指向的当前版本；
        3. 最后回退到该 segment 的最高 summary_version（确定性，不再任意 .first()）。

        返回 (status, events)：
        - None            → 无法校验（缺失 Summary / CompactionInput / manifest）→ stale
        - "verified"      → 全部通过
        - "degraded"      → 数据存在但某项校验失败（source_hash / event 不在 manifest / hash 不一致）
        校验失败时不返回对应 raw 内容。
        """
        summary: SegmentSummary | None = None
        if summary_id:
            candidate = db.get(SegmentSummary, summary_id)
            if candidate is not None and candidate.segment_id == seg_id:
                summary = candidate
        if summary is None:
            from aiive.db.models import Segment
            seg = db.get(Segment, seg_id)
            if seg is not None and seg.summary_id:
                candidate = db.get(SegmentSummary, seg.summary_id)
                if candidate is not None and candidate.segment_id == seg_id:
                    summary = candidate
        if summary is None:
            summary = (
                db.query(SegmentSummary)
                .filter(SegmentSummary.segment_id == seg_id)
                .order_by(SegmentSummary.summary_version.desc())
                .first()
            )
        if summary is None or not summary.source_event_ids:
            return (None, [])
        ci = (
            db.query(CompactionInput)
            .filter(
                CompactionInput.segment_id == seg_id,
                CompactionInput.summary_version == summary.summary_version,
            )
            .first()
        )
        if ci is None:
            ci = (
                db.query(CompactionInput)
                .filter(CompactionInput.segment_id == seg_id)
                .order_by(CompactionInput.summary_version.desc())
                .first()
            )
        if ci is None or not ci.event_manifest:
            return (None, [])

        # 整体 source hash 校验：manifest 来源快照散列必须与 Summary 断言一致
        if ci.source_hash != summary.source_hash:
            return ("degraded", [])

        manifest_by_id = {m.get("event_id"): m for m in (ci.event_manifest or [])}
        source_event_ids = set(summary.source_event_ids)
        events = self._load_events(db, [seg_id], limit)

        # Phase 6A fail-closed：被 active Shield（含 all_user_data）或 Tombstone 屏蔽的
        # 原始 Event 不得回源泄漏。
        from aiive.forget.visibility_service import ForgetVisibilityService

        blocked_events = ForgetVisibilityService.blocked_target_ids(
            db, "event",
            [(ev.id, getattr(ev, "created_at", None)) for ev in events],
        )

        verified: list[Any] = []
        status = "verified"
        for ev in events:
            if ev.id in blocked_events:
                # 被忘来源 → 该 Segment 视为 degraded，且不返回该 raw 内容
                status = "degraded"
                continue
            # Event 必须属于命中 Summary 的来源集合
            if ev.id not in source_event_ids:
                status = "degraded"
                continue
            m = manifest_by_id.get(ev.id)
            if m is None:
                status = "degraded"
                continue
            # 逐条重算 event content hash，与 manifest 比对
            if event_content_hash(ev) != m.get("content_hash"):
                status = "degraded"
                continue
            verified.append(ev)
        return (status, verified)

    def _load_events(self, db: Session, seg_ids: list[str], limit: int) -> list[Any]:
        """关联 TurnRecord 取 turn_sequence，按 (turn_sequence, turn_event_index) 排序。

        仅加载归属指定 Segment 的原始事件；Event 无 segment_id，需经
        TurnRecord.segment_id 关联（revision 6 的确定性排序）。
        """
        if not seg_ids:
            return []
        rows = (
            db.query(Event)
            .join(TurnRecord, TurnRecord.turn_id == Event.turn_id)
            .filter(TurnRecord.segment_id.in_(seg_ids))
            .order_by(TurnRecord.turn_sequence.asc(), Event.turn_event_index.asc())
            .limit(limit)
            .all()
        )
        return rows

    @staticmethod
    def _event_text(ev: Any) -> str:
        pl = ev.payload or {}
        et = ev.event_type
        if et in ("user_message", "llm_response"):
            return str(pl.get("content", "")).strip()
        if et == "tool_call":
            name = pl.get("name", "")
            params = pl.get("params") or {}
            if isinstance(params, dict):
                summary = " ".join(f"{k}={v}" for k, v in list(params.items())[:5])
            else:
                summary = str(params)
            return f"[tool_call] {name} {summary}".strip()
        if et == "tool_result":
            name = pl.get("name", "")
            status = pl.get("status", "")
            content = pl.get("content") or pl.get("result") or ""
            if isinstance(content, dict):
                content = str(content.get("content", ""))
            return f"[tool_result] {name} -> {status}: {content}".strip()
        return ""
