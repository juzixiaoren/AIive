"""Phase 5：统一检索编排器（UnifiedRetriever）。

唯一检索编排入口（revision 5）：ContextAssembler 与工具均通过本类发起检索，
绝不直接调用 AutomaticRecallEngine。

- memory_record 路由：复用 AutomaticRecallEngine 作为适配器（不重复实现语义召回）。
- segment_summary / epoch_checkpoint 路由：走 RetrievalIndexManager 倒排（lexical）。
- exact 路由：按 canonical_key + scope_id 精确命中（复用 query_exact）。
- 结果统一为 RetrievalHit，先超取（factor ×3）→ 批量回源 fail-closed 校验
  → 过滤陈旧/下线源 → 确定性去重 → 按 final_score 排序 → token budget 打包。
- DEEP 模式：先取命中，再经 RawHistoryExpander 二阶段回溯原始 Turn/Event；
  返回结果同时包含驱动展开的 parent 命中与 raw expansion。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from aiive.db.models import EpochCheckpoint, MemoryRecord, SegmentSummary
from aiive.memory.automatic_recall import AutomaticRecallEngine
from aiive.memory.recall_config import RecallConfig, RetrievalConfig
from aiive.memory.recall_models import MemoryRecallItem, MemoryRecallRequest
from aiive.retrieval.index_tokenizer import tokenize_for_index
from aiive.retrieval.raw_history_expander import RawHistoryExpander
from aiive.retrieval.retrieval_index import RetrievalIndexManager
from aiive.retrieval.retrieval_types import (
    RetrievalHit,
    RetrievalMode,
    RetrievalRequest,
    RetrievalResult,
)
from aiive.retrieval.source_version import epoch_checkpoint_source_hashes_hash
from aiive.runtime.token_counter import LiteLLMTokenCounter

logger = logging.getLogger(__name__)

# ── 超取因子：先取 max_results × OVERFETCH 候选，批量回源校验后截断 ──
_OVERFETCH = 3


def _safe_int(v: str) -> int:
    try:
        return int(v)
    except (ValueError, TypeError):
        return 0


class UnifiedRetriever:
    """统一检索编排器。"""

    def __init__(
        self,
        db: Session,
        config: RetrievalConfig | None = None,
        recall_config: RecallConfig | None = None,
    ) -> None:
        self._db: Session = db
        self._config: RetrievalConfig = config or RetrievalConfig()
        self._recall_config: RecallConfig = recall_config or RecallConfig()
        self._index: RetrievalIndexManager = RetrievalIndexManager()
        self._raw: RawHistoryExpander = RawHistoryExpander(db)

    # ── 公共入口 ──

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        """执行统一检索；编排层异常返回可观测的空降级结果，不泄漏到调用方重试。"""
        try:
            if request.mode == RetrievalMode.DEEP:
                return self._retrieve_deep(request)
            if request.mode == RetrievalMode.SEARCH:
                return self._retrieve_indexed(request, include_sleeping=True)
            return self._retrieve_auto(request)
        except Exception:
            logger.exception("UnifiedRetriever 编排流水线失败，返回 fail-closed 空结果")
            return RetrievalResult(
                request_id=request.request_id,
                hits=[],
                token_count=0,
                degraded=True,
                notes=["retrieval_pipeline_degraded", "all_routes_degraded"],
            )

    # ── AUTO ──

    def _retrieve_auto(self, request: RetrievalRequest) -> RetrievalResult:
        cfg = self._config
        req = RetrievalRequest(
            query=request.query,
            mode=RetrievalMode.AUTO,
            scope_context=request.scope_context,
            thread_id=request.thread_id,
            source_types=request.source_types,
            include_sleeping=cfg.include_sleeping_auto,
            max_results=cfg.auto_max_results,
            token_budget=cfg.auto_token_budget,
            request_id=request.request_id,
        )
        return self._retrieve_indexed(req, include_sleeping=cfg.include_sleeping_auto)

    # ── SEARCH / AUTO 共享：记忆路由 + exact + lexical + 去重 + 预算 ──

    def _retrieve_indexed(
        self, request: RetrievalRequest, include_sleeping: bool,
    ) -> RetrievalResult:
        notes: list[str] = []
        max_results = request.max_results or self._config.auto_max_results
        overfetch = max(1, max_results * _OVERFETCH)

        # 1. 各路超取；只统计本次请求实际启用的路由。
        allowed = set(request.source_types or [])
        exact_enabled = bool(request.query.strip())
        memory_enabled = request.scope_context is not None and (
            not allowed or "memory_record" in allowed
        )
        index_enabled = not allowed or bool(
            allowed.intersection({"segment_summary", "epoch_checkpoint"})
        )
        attempted_routes = sum((exact_enabled, memory_enabled, index_enabled))
        degraded_routes = 0

        try:
            exact_hits = self._exact_route(request) if exact_enabled else []
        except Exception:
            logger.exception("UnifiedRetriever exact 路由失败，降级为空")
            exact_hits = []
            degraded_routes += 1
            notes.append("exact_route_degraded")

        try:
            memory_hits = self._memory_route(request, include_sleeping, overfetch) if memory_enabled else []
        except Exception:
            logger.exception("UnifiedRetriever 记忆路由失败，降级为空")
            memory_hits = []
            degraded_routes += 1
            notes.append("memory_route_degraded")

        try:
            index_hits = self._index_route(request, include_sleeping, overfetch) if index_enabled else []
        except Exception:
            logger.exception("UnifiedRetriever 索引路由失败，降级为空")
            index_hits = []
            degraded_routes += 1
            notes.append("index_route_degraded")

        if attempted_routes > 0 and degraded_routes == attempted_routes:
            notes.append("all_routes_degraded")

        # 1.5 K 节评分融合：先对三路命中计算 final_score（RRF + 归一化加权），
        # 再合并/回源/去重，保证跨路由融合基于各路由原始排名而非合并后状态。
        self._fuse_scores(exact_hits, memory_hits, index_hits, self._config)

        # 2. 合并 + 初筛去重
        raw_merged = self._merge_keep_best(exact_hits + memory_hits + index_hits)

        # 2.5 上下文装配去重：排除 legacy 热分区已加载的最近摘要/检查点 source_id
        exclude = request.exclude_source_ids
        if exclude:
            raw_merged = [
                h for h in raw_merged
                if not (
                    h.source_type in ("segment_summary", "epoch_checkpoint")
                    and h.source_id in exclude
                )
            ]

        # 3. 批量回源 fail-closed 校验（实时过滤 archived/forgotten/superseded/源缺失/hash 不一致）
        validated = self._revalidate_candidates(
            raw_merged, include_sleeping, request.include_archived,
        )
        invalid_count = len(raw_merged) - len(validated)
        if invalid_count > 0:
            notes.append(f"fail_closed_filtered:{invalid_count}")

        # 4. 最终去重 + token budget 打包
        hits = self._dedup_and_budget(validated, max_results, request.token_budget)
        degradation_notes = {
            "exact_route_degraded", "memory_route_degraded", "index_route_degraded",
            "all_routes_degraded", "retrieval_pipeline_degraded",
        }
        return RetrievalResult(
            request_id=request.request_id,
            hits=hits,
            token_count=sum(h.token_count for h in hits),
            degraded=any(note in degradation_notes for note in notes),
            notes=notes,
        )

    # ── Exact 路由（按 canonical_key 精确命中）──

    def _exact_route(self, request: RetrievalRequest) -> list[RetrievalHit]:
        if not request.query or not request.query.strip():
            return []
        gen = self._index.get_active_generation(self._db)
        if gen is None:
            return []
        scope_id = None
        if request.scope_context and hasattr(request.scope_context, "scope_id"):
            scope_id = getattr(request.scope_context, "scope_id", None)
        entries = self._index.query_exact(
            self._db, gen.index_version, request.query.strip(), scope_id,
        )
        return [self._entry_to_hit(e, lexical=1.0) for e in entries]

    # ── Memory route（AutomaticRecallEngine 适配器）──

    def _memory_route(
        self, request: RetrievalRequest, include_sleeping: bool, top_k: int,
    ) -> list[RetrievalHit]:
        if request.source_types and "memory_record" not in request.source_types:
            return []
        scope = request.scope_context
        if scope is None:
            return []
        rc = RecallConfig(
            automatic_recall_top_k=top_k,
            automatic_recall_token_budget=self._config.auto_token_budget,
        )
        eng_req = MemoryRecallRequest(
            query=request.query,
            scope_context=scope,
            top_k=top_k,
            token_budget=self._config.auto_token_budget,
        )
        engine = AutomaticRecallEngine(self._db, rc)
        pack, _ = engine.recall(
            eng_req,
            include_sleeping=include_sleeping,
            include_archived=request.include_archived,
        )
        return [self.memory_item_to_hit(it) for it in pack.items]

    # ── Lexical index route（倒排 + entry 查询）──

    def _index_route(
        self, request: RetrievalRequest, include_sleeping: bool, top_k: int,
    ) -> list[RetrievalHit]:
        allowed = request.source_types
        if allowed is not None and not (
            "segment_summary" in allowed or "epoch_checkpoint" in allowed
        ):
            return []
        gen = self._index.get_active_generation(self._db)
        if gen is None:
            return []
        tokens = tokenize_for_index(request.query)
        if not tokens:
            return []
        token_strs = [t for t, _ in tokens]
        scored = self._index.query_tokens(self._db, gen.index_version, token_strs, top_k)
        if not scored:
            return []
        max_hits = max(h for _, h in scored) or 1
        entry_ids = [eid for eid, _ in scored]
        entries = self._index.fetch_entries_by_ids(self._db, gen.index_version, entry_ids)
        entry_by_id = {e.id: e for e in entries}
        hits: list[RetrievalHit] = []
        for eid, h in scored:
            e = entry_by_id.get(eid)
            if e is None:
                continue
            if allowed is not None and e.source_type not in allowed:
                continue
            if not include_sleeping and e.lifecycle_state == "sleeping":
                continue
            lexical = round(min(1.0, h / max_hits), 3)
            hits.append(self._entry_to_hit(e, lexical))
        return hits

    # ── 批量回源 fail-closed 校验 ──

    def _revalidate_candidates(
        self, hits: list[RetrievalHit], include_sleeping: bool,
        include_archived: bool = False,
    ) -> list[RetrievalHit]:
        """按 source_type 分桶，批量 IN 查询回源校验（不 N+1）。

        过滤规则：
        - MemoryRecord:
            * forgotten / superseded / expired → 始终过滤
            * archived → 仅 include_archived=True 时保留（AUTO 永远过滤，不自动 wake）
            * sleeping → 仅 include_sleeping=True 时保留
        - SegmentSummary/EpochCheckpoint: 源不存在、source_hash 不一致、
          EpochCheckpoint source_hashes 不一致 → 过滤
        - 被过滤的陈旧命中 best-effort 入队 refresh
        """
        memory_ids: list[str] = []
        summary_ids: list[str] = []
        checkpoint_ids: list[str] = []

        for h in hits:
            if h.source_type == "memory_record":
                memory_ids.append(h.source_id)
            elif h.source_type == "segment_summary":
                summary_ids.append(h.source_id)
            elif h.source_type == "epoch_checkpoint":
                checkpoint_ids.append(h.source_id)

        # 批量查询源表
        valid_memory: dict[str, Any] = {}
        if memory_ids:
            rows = (
                self._db.query(
                    MemoryRecord.id,
                    MemoryRecord.lifecycle_state,
                    MemoryRecord.validity_state,
                    MemoryRecord.record_version,
                    MemoryRecord.content_hash,
                    MemoryRecord.created_at,
                    MemoryRecord.sensitivity,
                )
                .filter(MemoryRecord.id.in_(list(set(memory_ids))))
                .all()
            )
            for r in rows:
                valid_memory[r[0]] = r

        valid_summary: dict[str, Any] = {}
        if summary_ids:
            rows = (
                self._db.query(
                    SegmentSummary.id,
                    SegmentSummary.summary_version,
                    SegmentSummary.source_hash,
                    SegmentSummary.created_at,
                )
                .filter(SegmentSummary.id.in_(list(set(summary_ids))))
                .all()
            )
            for r in rows:
                valid_summary[r[0]] = r

        valid_checkpoint: dict[str, Any] = {}
        if checkpoint_ids:
            rows = (
                self._db.query(
                    EpochCheckpoint.id,
                    EpochCheckpoint.version,
                    EpochCheckpoint.source_hashes,
                    EpochCheckpoint.created_at,
                )
                .filter(EpochCheckpoint.id.in_(list(set(checkpoint_ids))))
                .all()
            )
            for r in rows:
                valid_checkpoint[r[0]] = r

        # Phase 6A fail-closed：被 active Shield（含 all_user_data）或 Tombstone 屏蔽的
        # 命中一律丢弃，防止被忘内容经检索路径泄漏。
        from aiive.forget.visibility_service import ForgetVisibilityService
        from aiive.memory.memory_policy import MemoryPolicyEngine, MemoryReadChannel

        memory_policy = MemoryPolicyEngine()
        blocked_memory = ForgetVisibilityService.blocked_target_ids(
            self._db, "memory_record",
            [(r[0], r[5]) for r in valid_memory.values()],
        )
        blocked_summary = ForgetVisibilityService.blocked_target_ids(
            self._db, "segment_summary",
            [(r[0], r[3]) for r in valid_summary.values()],
        )
        blocked_checkpoint = ForgetVisibilityService.blocked_target_ids(
            self._db, "epoch_checkpoint",
            [(r[0], r[3]) for r in valid_checkpoint.values()],
        )

        # 逐条校验
        kept: list[RetrievalHit] = []
        stale: list[tuple[str, str, str]] = []  # (source_type, source_id, source_version)

        for h in hits:
            if h.source_type == "memory_record":
                if h.source_id in blocked_memory:
                    stale.append(("memory_record", h.source_id, ""))
                    continue
                src = valid_memory.get(h.source_id)
                if src is None:
                    stale.append(("memory_record", h.source_id, ""))
                    continue
                lifecycle: str = src[1] or ""
                if lifecycle in ("forgotten", "superseded", "expired"):
                    stale.append(("memory_record", h.source_id, str(src[3])))
                    continue
                if lifecycle == "archived" and not include_archived:
                    stale.append(("memory_record", h.source_id, str(src[3])))
                    continue
                if lifecycle == "sleeping" and not include_sleeping:
                    continue
                if not memory_policy.decide_read(
                    src[6], MemoryReadChannel.TOOL,
                ).allowed:
                    continue
                kept.append(h)

            elif h.source_type == "segment_summary":
                if h.source_id in blocked_summary:
                    stale.append(("segment_summary", h.source_id, ""))
                    continue
                src = valid_summary.get(h.source_id)
                if src is None:
                    stale.append(("segment_summary", h.source_id, ""))
                    continue
                # source_hash 一致性校验
                expected_hash = src[2] or ""
                indexed_hash = h.provenance.get("source_hash")
                # 若命中未携带 source_hash，信任索引已校验（refresh 时 fencing 已保证）
                if expected_hash and indexed_hash and expected_hash != indexed_hash:
                    stale.append(("segment_summary", h.source_id, str(src[1])))
                    continue
                kept.append(h)

            elif h.source_type == "epoch_checkpoint":
                if h.source_id in blocked_checkpoint:
                    stale.append(("epoch_checkpoint", h.source_id, ""))
                    continue
                src = valid_checkpoint.get(h.source_id)
                if src is None:
                    stale.append(("epoch_checkpoint", h.source_id, ""))
                    continue
                # 版本号精确 equality
                if _safe_int(h.source_version) != _safe_int(str(src[1] or 0)):
                    stale.append(("epoch_checkpoint", h.source_id, str(src[1])))
                    continue
                # source_hashes 一致性校验（spec M/L）：重新计算当前源 source_hashes 的
                # 散列，与索引命中所携带的 source_hash 比对，不一致则视为陈旧过滤。
                source_hashes = src[2] or []
                expected = epoch_checkpoint_source_hashes_hash(
                    EpochCheckpoint(id=h.source_id, version=src[1] or 0, source_hashes=source_hashes)
                )
                indexed_hash = h.provenance.get("source_hash")
                if expected and indexed_hash and expected != indexed_hash:
                    stale.append(("epoch_checkpoint", h.source_id, str(src[1])))
                    continue
                kept.append(h)

            else:
                # 非标准 source_type → 保留（raw_event 等由 deep 路径生成，不在此校验）
                kept.append(h)

        # best-effort 入队 refresh（陈旧命中补刷新）
        if stale:
            self._enqueue_stale_refresh(stale)

        return kept

    def _enqueue_stale_refresh(self, stale_sources: list[tuple[str, str, str]]) -> None:
        """best-effort 为陈旧 source 入队 refresh（幂等，不阻断检索返回）。

        operation_id 含 source_version：每个版本独立 job，已完成的旧版本不会阻塞新版本
        刷新。存在性检查覆盖全部状态（含 completed）——若同版本 job 已存在则跳过，
        既避免重复刷新，也避免与已完成 job 的 `operation_id` UNIQUE 冲突导致 flush
        抛错污染当轮检索会话。单条插入用 SAVEPOINT 隔离，失败仅回滚该插入。
        """
        try:
            from aiive.db.models import OutboxJob

            for source_type, source_id, source_version in stale_sources:
                op_id = f"retrieval_refresh:{source_type}:{source_id}:{source_version}"
                # 幂等：任意状态（含 completed）的同版本 job 已存在则跳过，避免
                # UNIQUE 冲突；也避免与 _enqueue_retrieval_refresh 同版本 job 重复。
                existing = (
                    self._db.query(OutboxJob)
                    .filter(OutboxJob.operation_id == op_id)
                    .first()
                )
                if existing is not None:
                    continue
                sp = None
                try:
                    sp = self._db.begin_nested()
                    self._db.add(OutboxJob(
                        operation_id=op_id,
                        job_type="retrieval_index_refresh",
                        status="pending",
                        payload={
                            "schema_version": 1,
                            "source_type": source_type,
                            "source_id": source_id,
                            "event_type": "revalidate.stale",
                        },
                        trace_id=source_id,
                        max_retries=3,
                    ))
                    self._db.flush()
                    sp.commit()
                except Exception:
                    if sp is not None:
                        sp.rollback()
                    logger.warning("best-effort stale refresh 单条入队失败（不阻断检索）", exc_info=True)
        except Exception:
            logger.warning("best-effort stale refresh 入队失败（不阻断检索）", exc_info=True)

    # ── DEEP ──

    def _retrieve_deep(self, request: RetrievalRequest) -> RetrievalResult:
        cfg = self._config
        idx_req = RetrievalRequest(
            query=request.query,
            mode=RetrievalMode.SEARCH,
            scope_context=request.scope_context,
            thread_id=request.thread_id,
            source_types=["segment_summary", "epoch_checkpoint"],
            include_sleeping=True,
            include_archived=request.include_archived,
            max_results=cfg.deep_max_results,
            token_budget=cfg.deep_history_raw_token_budget,
            request_id=request.request_id,
        )
        indexed = self._retrieve_indexed(idx_req, include_sleeping=True)
        notes = list(indexed.notes)
        degraded = indexed.degraded
        # 二阶段原始回溯独立降级，保留已成功取得的 parent 命中。
        try:
            raw = self._raw.expand(
                self._db, indexed.hits, cfg,
                deep_max_turns=cfg.deep_max_turns,
                token_budget=cfg.deep_history_raw_token_budget,
            )
        except Exception:
            logger.exception("UnifiedRetriever 原始历史回溯失败，保留父级命中")
            raw = []
            degraded = True
            notes.append("raw_history_route_degraded")
        # parent 命中 + raw expansion 共同返回（保持父子 provenance）
        parent_hits = [h for h in indexed.hits if h.source_type in ("segment_summary", "epoch_checkpoint")]
        return RetrievalResult(
            request_id=request.request_id,
            hits=parent_hits + raw,
            token_count=sum(h.token_count for h in parent_hits + raw),
            degraded=degraded,
            notes=notes,
        )

    # ── K 节评分融合：RRF（跨路由排名融合）+ 归一化确定性加权 ──

    def _fuse_scores(
        self,
        exact_hits: list[RetrievalHit],
        memory_hits: list[RetrievalHit],
        index_hits: list[RetrievalHit],
        cfg: RetrievalConfig,
    ) -> None:
        """原地计算每条命中的 final_score（K 节要求：RRF + 归一化加权，不量纲混加）。

        1. 各路由先按自身原生分（h.score）降序排名；RRF 分量 = k/(k+rank)。
        2. 同一 source 可能出现在多路，RRF 取各路分量均值（跨路由融合收益）。
        3. 确定性分量（lexical / exact / recency / authority / scope）均已归一化到
           [0,1]，按 retrieval_route_weights 加权求和；权重和归一以保证 final_score ∈ [0,1]。
        4. scope 信号当前未接到 RetrievalHit，以中性 0.5 占位（仅贡献常数基线，
           不影响相对排序；后续接 scope_score 后自然生效）。
        """
        routes: dict[str, list[RetrievalHit]] = {
            "exact": exact_hits,
            "memory": memory_hits,
            "lexical": index_hits,
        }
        rrf_k = cfg.rrf_k
        # 每 (source_type, source_id) 在各路由中的 RRF 分量集合
        rrf_by_key: dict[tuple[str, str], list[float]] = {}
        for hits in routes.values():
            ranked = sorted(hits, key=lambda h: h.score, reverse=True)
            for rank, h in enumerate(ranked, start=1):
                key = (h.source_type, h.source_id)
                rrf_by_key.setdefault(key, []).append(rrf_k / (rrf_k + rank))

        exact_keys = {(h.source_type, h.source_id) for h in exact_hits}
        w = cfg.retrieval_route_weights
        w_sum = sum(w.values()) or 1.0
        scope_neutral = 0.5  # scope 信号暂未接线，中性基线
        for hits in routes.values():
            for h in hits:
                key = (h.source_type, h.source_id)
                contribs = rrf_by_key.get(key, [])
                rrf_score = sum(contribs) / len(contribs) if contribs else 0.0
                exact_boost = 1.0 if key in exact_keys else 0.0
                final = (
                    w["rrf"] * rrf_score
                    + w["lexical"] * h.lexical_score
                    + w["exact"] * exact_boost
                    + w["scope"] * scope_neutral
                    + w["recency"] * h.recency_score
                    + w["authority"] * h.authority_score
                )
                final = final / w_sum  # 权重和归一，保证 final_score ∈ [0,1]
                h.final_score = round(final, 4)
                h.score = h.final_score

    # ── 合并（初筛：按 source_type+source_id 保留最高 score）──

    @staticmethod
    def _merge_keep_best(hits: list[RetrievalHit]) -> list[RetrievalHit]:
        best: dict[tuple[str, str], RetrievalHit] = {}
        for h in hits:
            key = (h.source_type, h.source_id)
            if key not in best or h.final_score > best[key].final_score:
                best[key] = h
        return list(best.values())

    # ── 去重 + token budget 打包 ──

    def _dedup_and_budget(
        self, hits: list[RetrievalHit], max_results: int | None,
        token_budget: int | None,
    ) -> list[RetrievalHit]:
        best: dict[tuple[str, str], RetrievalHit] = {}
        for h in hits:
            key = (h.source_type, h.source_id)
            if key not in best or h.final_score > best[key].final_score:
                best[key] = h
        # K 节稳定 tie-breaker：final_score 降序，同分按 (source_type, source_id) 升序，
        # 保证预算截断时同分命中排序确定（元组键确定性，不依赖哈希顺序）。
        ordered = sorted(
            best.values(),
            key=lambda x: (-x.final_score, x.source_type, x.source_id),
        )
        out: list[RetrievalHit] = []
        used = 0
        for h in ordered:
            if max_results is not None and len(out) >= max_results:
                break
            if token_budget is not None and used + h.token_count > token_budget:
                break
            out.append(h)
            used += h.token_count
        return out

    # ── 转换（使用真实 TokenCounter）──

    @staticmethod
    def memory_item_to_hit(it: MemoryRecallItem) -> RetrievalHit:
        final = it.fused_score or it.relevance_score
        content = it.content or ""
        tcount = LiteLLMTokenCounter.count_text(content)
        return RetrievalHit(
            source_type="memory_record",
            source_id=it.memory_id,
            source_version=str(it.record_version),
            retrieval_tier="warm" if it.validity_state == "valid" else "cold",
            score=final,
            lexical_score=it.relevance_score,
            recency_score=it.recency_score,
            authority_score=it.importance_score,
            final_score=final,
            title=it.canonical_key or "",
            snippet=content[:200],
            scope_type=it.scope_type,
            scope_id=it.scope_id,
            memory_record_id=it.memory_id,
            provenance={
                "route": it.route,
                "record_version": it.record_version,
                "memory_type": it.memory_type,
            },
            retrieval_reason="memory route",
            token_count=tcount,
            route=it.route or "memory",
            lifecycle_state=it.lifecycle_state,
        )

    @staticmethod
    def _entry_to_hit(e: Any, lexical: float) -> RetrievalHit:
        title = e.title or ""
        snippet = e.snippet or ""
        combined = f"{title} {snippet}"
        tcount = LiteLLMTokenCounter.count_text(combined)
        return RetrievalHit(
            source_type=e.source_type,
            source_id=e.source_id,
            source_version=str(e.source_version),
            retrieval_tier=getattr(e, "retrieval_tier", "warm") or "warm",
            score=lexical,
            lexical_score=lexical,
            recency_score=0.5,
            authority_score=0.5,
            final_score=lexical,
            title=title,
            snippet=snippet,
            scope_type=getattr(e, "scope_type", "global") or "global",
            scope_id=getattr(e, "scope_id", None),
            thread_id=getattr(e, "thread_id", None),
            epoch_id=getattr(e, "epoch_id", None),
            segment_id=getattr(e, "segment_id", None),
            memory_record_id=getattr(e, "memory_record_id", None),
            provenance={
                "index_version": getattr(e, "index_version", None),
                "source_hash": getattr(e, "source_hash", None),
            },
            retrieval_reason="lexical index",
            token_count=tcount,
            route="lexical",
            lifecycle_state=e.lifecycle_state or "active",
        )

    @staticmethod
    def memory_hit_to_recall_item(h: RetrievalHit) -> MemoryRecallItem:
        """将 memory 命中还原为 MemoryRecallItem（供 ContextAssembler 复用既有渲染与 touch）。"""
        version = 1
        if h.source_version.isdigit():
            version = int(h.source_version)
        return MemoryRecallItem(
            memory_id=h.memory_record_id or h.source_id,
            content=h.snippet,
            memory_type="",
            canonical_key=h.title,
            scope_type=h.scope_type,
            scope_id=h.scope_id,
            relevance_score=h.lexical_score,
            scope_score=0.0,
            recency_score=h.recency_score,
            importance_score=h.authority_score,
            validity_state="valid",
            record_version=version,
            token_cost=h.token_count,
            route=h.route,
            fused_score=h.final_score,
        )
