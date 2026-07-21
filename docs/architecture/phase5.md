# AIive Phase 5：冷热历史分层与统一检索增强 —— 审计与实施方案

> 本文档为**第一次回复**：仅做真实代码审计 + 实施方案设计，未修改任何代码、未生成 migration、未启动索引 backfill。
> 所有表名/字段名/类名/函数名均来自对 `backend/aiive/**` 的真实交叉核对（已读：`db/models.py`、`runtime/context_assembler.py`、`context_budget.py`、`epoch_manager.py`、`memory/automatic_recall.py`、`memory_store.py`、`memory_lifecycle_service.py`、`memory_access_tracker.py`、`core_memory_projection.py`、`recall_config.py`、`recall_fusion.py`、`recall_models.py`、`worker/outbox_*.py`、`worker/scheduler_daemon.py`、`tools/builtin_tools.py`、`knowledge/*`、`alembic/versions/*`）。

---

## 0. 概念名 → 真实实现 映射表（必须先看）

| 文档概念 | 真实存在？ | 真实实现 |
|---|---|---|
| `ContextAssembler` | ✅ | `aiive.runtime.context_assembler.ContextAssembler.assemble()`，唯一上下文组装入口 |
| Core Memory 投影 | ✅ | `aiive.memory.core_memory_projection`（`load_core_memory` / `CoreMemoryProjection.refresh`），表 `core_memory_blocks` |
| `AutomaticRecallEngine` | ✅ | `aiive.memory.automatic_recall.AutomaticRecallEngine.recall()`；routes：exact / lexical / vector（OpenAI + pgvector）/ recent_episode；temporal_graph 未实现 |
| `memory_search`（语义检索） | ✅（仅 MemoryRecord） | `aiive.tools.builtin_tools._handle_memory_search` → `AutomaticRecallEngine.recall(include_sleeping=True)` |
| `memory_timeline` | ✅ | `_handle_memory_timeline`（按 id/key 取版本历史） |
| `memory_event_log`（原始事件搜索） | ✅（仅 episodic MemoryRecord） | `_handle_memory_event_log`，对 `memory_type='episodic'` 做 ILIKE 子串匹配 |
| SegmentSummary 检索 | ❌（无检索） | `ContextAssembler._load_segment_summaries` 仅按 `start_turn_sequence.desc().limit(N)` 时间加载 |
| EpochCheckpoint 检索 | ❌（无检索） | `ContextAssembler._load_epoch_checkpoint` 仅取 `created_at.desc()` 第一条 |
| Raw Turn / Event 检索接口 | ❌（无独立搜索） | 仅在 compaction 事务内 `_reconstruct_source_turns`（按 `event_manifest`）；工具侧无 `search_raw_history` |
| MemoryRecord 默认召回过滤 | ✅ | `AutomaticRecallEngine._lifecycle_states(include_sleeping)` → 默认 `active`；expanded 模式加 `sleeping`；`archived/forgotten/candidate` 永不召回 |
| Outbox | ✅ | `aiive.worker.outbox_job/outbox_handlers/outbox_worker/handler_registry`；allowlist `recall_config.ENABLED_OUTBOX_JOB_TYPES` |
| P3/P4 生命周期变更通知下游 | ✅ | `MemoryMutationExecutor.enqueue_projection()` 只生产已有消费者的 `core_memory_refresh` 与 `retrieval_index_refresh`；未实现的向量/图投影不设开关、不创建任务 |
| `RetrievalIndexEntry` | ❌ | **新增表** `retrieval_index_entries`（本文 D/G） |
| `RetrievalIndexRun` | ❌ | **新增表** `retrieval_index_runs`（本文 P） |
| `RetrievalHit` | ❌ | **新增** `retrieval/retrieval_types.py` 中的 Pydantic 模型 |
| 语义检索/embedding | ⚠️ 未接入 | `knowledge/embedding_client.py`（`FakeEmbeddingClient`，SHA-256 伪向量，**测试专用**）、`knowledge/qdrant_indexer.py`（内存版）均与记忆无关；记忆召回未注册向量 adapter |
| `source_version` | ✅ | MemoryRecord：`record_version`+`content_hash`；SegmentSummary：`summary_version`+`source_hash`；EpochCheckpoint：`version`+`source_hashes`；Event：compaction `event_content_hash`+`turn_event_index` |
| 维护 Run 模式 | ✅ | `MemoryMaintenanceRun/Batch/Input/Action`（claim/lease/fencing/续跑），**Phase 5 索引 Run 复用同一套范式** |
| `TokenCounter` | ✅ | `aiive.runtime.token_counter.LiteLLMTokenCounter.count_text()`；Phase 5 检索命中 `token_count` 均使用真实 TokenCounter 计算（非 `len//4`） |

---

## A. 真实检索调用链

### A.1 每轮对话（自动召回）
```
AgentGraph → ContextAssembler.assemble(db, message, thread, ...)
  └─ _load_agent_context()
       ├─ resolve_identity() / resolve_policies()
       ├─ load_core_memory()                        # core_memory_blocks 投影
       ├─ AutomaticRecallEngine.recall(request)     # MemoryRecord 专属：exact/fts/episode
       │     └─ fuse_and_pack() → MemoryRecallPack  # RRF 融合 + token 打包
       └─ assemble_system_content(stable_contract, core_blocks, pack)
  ├─ WorkingStateService.render_for_context()
  ├─ _load_epoch_checkpoint()                       # 仅最近 1 条 EpochCheckpoint
  ├─ _load_segment_summaries()                      # 仅最近 N 条（max_segment_summaries=5）
  ├─ _load_sealing_bridge()
  ├─ _load_history_bounded()                        # 有界原始 Turn（keyset + token budget）
  └─ _touch_injected_memory(pack)                   # 仅 touch 实际注入的 MemoryRecord
```
**结论**：自动召回链**只对 MemoryRecord 做检索**；SegmentSummary / EpochCheckpoint **只按时间加载，不检索**；原始 Turn/Event **只按 token 预算有界加载最近若干条**。

### A.2 工具显式检索
- `memory_search` → `AutomaticRecallEngine.recall(include_sleeping=True)`（仍仅 MemoryRecord）。
- `memory_timeline` → 按 id/key 取 MemoryRecord 版本历史（不是检索）。
- `memory_event_log` → `memory_type='episodic'` 的 MemoryRecord 做 ILIKE（不是原始 Event）。

### A.3 后台索引/投影链
- `OutboxWorker.poll()` 每 5s claim，按 `HandlerRegistry` 分派；handler 不 finalize（Worker 负责）。
- 已注册 job_type：`memory_extraction`/`core_memory_refresh`/`segment_sealing`/`epoch_rollover`/`epoch_checkpoint`/`memory_maintenance`。
- `enqueue_projection()` 里的 `memory_vector_*` 等因 allowlist 限制**永不执行**。

---

## B. 当前 ContextAssembler 分区

来自 `context_budget.py` 的 `ContextBudget.all_partitions`（顺序即优先级）：

| 分区 | soft / hard token | 当前加载内容 |
|---|---|---|
| stable_contract | 3500 / 4000 | 内核契约（identity+policies） |
| core_memory | 500 / 600 | `core_memory_blocks` 投影（仅 active+valid 的 core 键） |
| working_state | 1500 / 2000 | `WorkingStateService.render_for_context` |
| tool_definitions | 4000 / 6000 | 工具 schema（priority 截断） |
| recent_messages | 85000 / 97000 | 有界原始 Turn（`_load_history_bounded`） |
| retrieved_memory | 1000 / 1200 | `AutomaticRecallEngine` 返回的 MemoryRecord pack |
| tool_results | 5000 / 8000 | history 中的 tool 结果 |
| epoch_checkpoint | 500 / 800 | **最近 1 条** EpochCheckpoint（时间取，非检索） |
| segment_summaries | 1200 / 2000 | **最近 5 条** SegmentSummary（时间取，非检索） |
| sealing_bridge | 1500 / 2300 | sealing Segment 桥接（Summary 或 bounded raw tail） |

**新增分区计划**（Phase 5，见 Q 节）：在 warm 检索结果可落入 `retrieved_memory` / `retrieved_history_summary`，deep 原始回溯单独分区 `deep_history_raw`（auto 模式不使用）。

---

## C. 当前可检索数据源

| 源 | 是否可被检索 | 检索方式 | 真实入口 |
|---|---|---|---|
| MemoryRecord | ✅ | exact(key/scope) + FTS(ILIKE) + episode | `automatic_recall.py` |
| SegmentSummary | ❌ | 仅时间加载 | `context_assembler._load_segment_summaries` |
| EpochCheckpoint | ❌ | 仅时间加载 | `context_assembler._load_epoch_checkpoint` |
| Raw Turn | ❌ | 仅最近有界加载 | `_load_history_bounded` / compaction 内 |
| Raw Event | ❌ | 无工具入口 | compaction `_reconstruct_source_turns` |
| CoreMemoryBlock | ✅（投影） | 仅 core 键加载 | `load_core_memory` |
| Chunk（知识库） | ✅（独立） | `search_knowledge` / Qdrant（文档，非记忆） | `knowledge/*`（与 Phase 5 无关） |

---

## D. 当前检索技术与依赖（真实现状）

- **FTS**：仅 `MemoryRecord.content.ilike(f"%{t}%")` + `canonical_key.ilike` 子串匹配（`_query_scope_text`）。**无 `to_tsvector`、无 GIN、无 `pg_trgm`、无 `tsvector` 列**。中文靠 `_tokenize_cjk_aware`（CJK 二元 + `\W+`）在 Python 侧 tokenize。
- **向量**：记忆召回使用 OpenAI Embeddings + PostgreSQL pgvector；独立 `memory_vector_projections` 表通过 Outbox 最终一致维护，查询必须回源校验 scope、生命周期、版本和 Forget 状态。
- **时序图**：仍未实现，不进入生产召回链路。
- **依赖**：已引入 `pgvector` Python 包；本地数据库使用 `pgvector/pgvector:pg16`。知识库的 FakeEmbedding/Qdrant 不参与记忆生产路径。
- **数据库**：Alembic head 在 `backend/alembic/versions`（如 `p4a0b1c2d3e4f_phase4_memory_maintenance.py`、`p4b2c3d4e5f6_thread_last_activity.py`）；ORM 同时兼容 Postgres（prod）与 **SQLite（测试）**。
- **检索索引表**：无。`retrieval_runs`/`retrieval_candidates` 是**知识库文档检索**专用，非记忆检索。
- **投影表**：`core_memory_blocks`（已存在，可重建）。无统一检索投影。
- **索引版本/重建命令**：无。

---

## E. 热/温/冷逻辑分层（真实字段落地）

Phase 5 v1 只做**逻辑分层 + 检索投影**，不物理搬迁/删除原始数据（禁止物理删除）。

- **Hot**（保持 P1/P3/P4，不扩大）：
  - `WorkingState`、`Segment.status='open'` 的有界原始 Turn、`Segment.status='sealing'` 的桥接（≤1）、最近有效 `EpochCheckpoint`、最近 N 个 `SegmentSummary`（`Segment.status='sealed'`）、active/pinned Core Memory、当前用户消息。
- **Warm**（可自动检索，不默认全注入）：
  - 较早 `SegmentSummary`（`sealed`）、较早 `EpochCheckpoint`、active `MemoryRecord`、高相关 `sleeping MemoryRecord`、近期已 sealed 对话摘要。
- **Cold**（仅高相关/显式 deep 读取）：
  - 较老 `SegmentSummary`/`EpochCheckpoint`、`sealed Segment` 对应的原始 Turn/Event、`sleeping MemoryRecord`、`archived MemoryRecord` 的**审计视图**（仅显式参数）。
- 规则：
  - `archived` 不进自动召回；仅显式 `include_archived=True` 可返回（audit/search 模式）。
  - `forgotten` 永不返回。
  - 原始 Turn/Event 不进自动召回；仅作 Summary 命中后的**二阶段回溯**或显式 `deep`。
  - 任何一次原始回溯都有独立数量 + token 上限。
  - **tier 存于检索投影**（`retrieval_index_entries.retrieval_tier`），不修改所有源表。

---

## F. 索引技术选型对比与最终建议

| 方案 | 适用性 | 本项目现状 | 结论 |
|---|---|---|---|
| A. PostgreSQL 原生 FTS（`tsvector`/GIN/`pg_trgm`） | 个人级、低运维、关键词为主 | 当前仅 ILIKE，无 tsvector；需新增列/索引；中文需 `pg_trgm` 或 simple+二元 | **v1 主选（便携版）** |
| B. PostgreSQL + pgvector | 需稳定 embedding、同库事务 | 无记忆向量 adapter、无 provider、无 pgvector 依赖 | v1 不启用；保留后续扩展设计 |
| C. Qdrant 外部库 | 需外部部署 | 仅内存伪实现、未接记忆、无部署 | v1 不采用 |

**最终建议（v1）**：
1. 主方案 = **方案 A 的「应用层可移植词汇检索」变体**：在 `retrieval_index_entries` 上存一个**规范化 `search_text`**（Python 侧用 `_tokenize_cjk_aware` 生成空白分隔 token，复用 `automatic_recall` 既有函数），检索时用 `ILIKE`/trigram 在 `search_text` 上做**确定性词汇匹配**（Postgres 与 SQLite 均支持，测试不破）。
2. 不引入 `tsvector`/GIN 到 **v1 的强制路径**（SQLite 不支持），但 `retrieval_index_entries` 预留 `tsvector` 生成列钩子（Postgres 专用迁移可选，v1 不阻塞）。
3. 第二阶段新增独立 `memory_vector_projections`，生产 PostgreSQL 使用 1536 维 pgvector；SQLite 定向测试仅作类型兼容，不执行语义 SQL。OpenAI provider 通过配置启用，禁止生产使用 FakeEmbedding。
4. 评分融合采用文档推荐的**确定性加权 + RRF**（复用 `recall_fusion` 范式），绝不不同量纲直接相加。

---

## G. 统一检索投影 `RetrievalIndexEntry`

新增表 `retrieval_index_entries`（概念名 `RetrievalIndexEntry`）：

```text
id                      String(36) PK
source_type             String(32)   # memory_record | segment_summary | epoch_checkpoint
source_id               String(36)
source_version          String(64)   # 见 H 节真实 source_version 字符串化
source_hash             String(64)   # 内容哈希，用于陈旧校验
index_version           int          # 全局索引版本，rebuild 时并行构建
thread_id               String(36)   # 来自源
epoch_id                String(36) | None
segment_id              String(36) | None
memory_record_id        String(36) | None  # source_type=memory_record 时
scope_type              String(32)
scope_id                String(128) | None
canonical_key           String(256) | None
lifecycle_state         String(32)   # 快照；fail-closed 仍以源记录为准
validity_state          String(32)
retrieval_tier          String(16)   # hot | warm | cold（由 source 派生的逻辑分层）
title                   Text
search_text             Text         # 规范化可检索文本（CJK 二元归一）
snippet                 Text         # 命中文档预览（非 provenance 替代）
metadata                JSON
created_source_at       DateTime
updated_source_at       DateTime
indexed_at              DateTime
is_current              Boolean      # 每个 source 只允许一个 current（is_current=True）
is_searchable           Boolean      # lifecycle/archived/forgotten 控制
# 预留（v1 不填充，保留 hybrid 缝）：
embedding               JSON | None
embedding_model         String(128) | None
embedding_dimension     int | None
embedding_version       String(32) | None
```

**唯一约束**：
```text
UNIQUE(source_type, source_id, source_version, index_version)
```
**current 保证**：通过 `(source_type, source_id, is_current)` 业务约束 + 重建时“先建新版本全部 is_current=False，再原子 UPDATE 翻 is_current=True（旧版本置 False）”实现；保留历史版本便于审计/重建。

**是否保留历史版本**：采用**历史版本保留**方案（审计 + 重建安全），靠 `is_current` 切换；不得只覆盖旧行。重建与实时更新竞争处理见 P 节。

**使用单行 upsert 的备选（若团队偏好更简单）**：仅在 `source_version` 比较规则明确（`source_version` 单调递增字符串/数字）、旧 OutboxJob 不得覆盖新版本（fencing）、重建与实时更新用 `index_version` 隔离时，才允许；本方案默认用「历史版本 + is_current」。

---

## H. 真实 source version 映射（禁止只依赖 updated_at）

| 源 | 真实 version 字段 | 真实 hash 字段 | 索引提交前校验 |
|---|---|---|---|
| MemoryRecord | `record_version`（每次生命周期/内容变更 +1） | `content_hash` / `structured_value_hash` | 提交前查 `record_version` + `content_hash` |
| SegmentSummary | `summary_version`（P3 唯一约束 `uq_segment_summary_segment_version`） | `source_hash` | 校验 `summary_version` + `source_hash` |
| EpochCheckpoint | `version`（P3 唯一约束 `uq_epoch_checkpoint_epoch_version`） | `source_hashes`（list） | 校验 `version` + `source_hashes` |
| Raw Turn/Event | 无单条 version；用 `turn_event_index` + `turn_id` | compaction `event_content_hash`（`compaction.event_content_hash`） | v1 **不索引原始事件**（见 L），回溯经 Summary provenance |

**fencing**：索引 Handler 提交前再次读取源记录的最新 `source_version`/`source_hash`；若入队时携带的版本 < 源当前版本 → **跳过（陈旧 Job 不覆盖新数据）**。

---

## I. 统一 `RetrievalHit`

新增 `retrieval/retrieval_types.py` 的 Pydantic 模型（字段名服从现有 `recall_models` 风格）：

```text
RetrievalHit:
  source_type, source_id, source_version
  retrieval_tier
  score, lexical_score, semantic_score, recency_score,
  authority_score, lifecycle_score, final_score
  title, snippet
  canonical_key, scope_type, scope_id,
  thread_id, epoch_id, segment_id, memory_record_id
  provenance            # 真实可定位：含 source_type/source_id/source_version/turn 范围（如适用）
  retrieval_reason      # exact | lexical | semantic | authority
  token_count           # 必须用真实 TokenCounter 计算
  route
```

要求：
- 每条命中可定位回真实源记录；`snippet` 不替代 `provenance`。
- 不允许模型编造 source id；score 必须可解释（各分量可见）。
- 相同 source 不重复返回（按 `source_type+source_id` 去重）。
- 同一事实近重复命中：去重/聚类（见 K 节 dedup 规则）。
- `token_count` 由真实 `TokenCounter.count_messages()` 计算（不使用 `len//4`）。

---

## J. auto / search / deep 模式

| 模式 | 用途 | 默认范围 | 原始 Turn/Event | archived | sleeping |
|---|---|---|---|---|---|
| `auto` | `ContextAssembler` 自动召回 | 仅 warm；active 优先；sleeping 仅高相关 | ❌ 禁止 | ❌ 禁止 | 仅 exact/高相关 route |
| `search` | `memory_search`/`history_search` 工具 | warm + cold Summary；sleeping 可 | 默认不加载大量 | 仅 `include_archived=True` | 可 |
| `deep` | 显式深度回溯 | 先 Summary/Checkpoint 检索，再对命中 Summary 的 source Turn 范围有界展开 | ✅ 有界（segment/时间/数量/token 上限） | 仅显式参数 | 可 |

- 不通过关键词 if/else 判断用户是否需要 deep；由调用方显式传入 `mode` 或主 Agent 选择工具参数（符合「禁止关键词硬编码路由」）。
- `auto` 候选数与 token 严格受限（复用 `RecallConfig` 新增 `retrieval_*` 预算）。

---

## K. 混合检索与评分

统一流水线（最终代码为准，参考 `recall_fusion.fuse_and_pack`）：
```
1. Query normalization   → 复用 _tokenize_cjk_aware
2. Structured filters    → scope_type/scope_id/source_type/time_from/time_to
3. Exact key/scope lookup→ MemoryRecord.canonical_key / scope_id 精确
4. Lexical retrieval     → search_text ILIKE/trigram（可移植）
5. Optional semantic      → v1 关闭（embedding 列保留位）
6. Score fusion           → RRF + 归一化加权（不量纲混加）
7. Lifecycle/tier filter  → fail-closed（见 M）
8. Deduplication         → 同 source 去重；近重复聚类
9. Diversity selection    → 同 Segment/事实优先权威源
10. Token-budget packing  → 真实 TokenCounter
11. Optional raw expansion→ 仅 deep（见 L）
12. Access tracking       → 仅最终注入/工具返回（见 N）
```

**评分（确定性加权，配置化）**：
```
final = w_rrf*RRF + w_lexical*lexical + w_exact*exact_boost
      + w_scope*scope + w_recency*recency + w_authority*authority
      + w_lifecycle*lifecycle
```
- 权重在 `RecallConfig` 新增 `retrieval_route_weights`（复用既有 `route_weights` 风格）。
- `pinned` ≠ 无限 score；受 query relevance + token budget 约束。
- `archived` 在 auto 模式**直接过滤**（非仅降分）；`sleeping` 在 auto 仅经 allowed route（exact/fts 高相关）。
- 原始 Event 不应压过其对应 Summary（raw 是 Summary 的二阶段补充）。
- tie-breaker 稳定：`(final_score DESC, source_type, source_id)`。

**去重优先级**（同文档）：
- 同一 Segment：SegmentSummary 优先于 raw Turn。
- 同一长期事实：有效 active MemoryRecord 优先于旧 SegmentSummary 中的事实描述。
- 同一 Epoch：最新 version EpochCheckpoint 优先于旧 version（靠 `is_current`）。
- 同一来源多个索引版本：仅 `is_current=True` 参与。

---

## L. 二阶段原始历史回溯（二阶段）

v1 **不索引原始 Turn/Event**；原始历史只通过 Summary provenance 有界加载：
- 复用 P3 字段：`SegmentSummary.source_turn_ids` / `source_event_ids` / `source_turn_range` / `source_hash`。
- 流程：
  1. 第一阶段检索 MemoryRecord / SegmentSummary / EpochCheckpoint。
  2. 第二阶段仅对选中的 Summary 命中（deep 模式）按 `source_turn_range` / `source_turn_ids` 有界加载 `Event`。
- 加载后校验：source id 属于命中 Summary；`source_hash` 仍有效；数量 ≤ `deep_max_turns`；token ≤ `deep_history_raw` 分区预算；Event 顺序稳定（`turn_id, turn_event_index`）；tool_call/tool_result 配对（复用 `context_assembler._dicts_to_chat_messages` 的配对逻辑）；不跨 Segment 无界扩展。

---

## M. 生命周期过滤（fail-closed）

遵守 P4 语义（`memory_lifecycle_service.MemoryLifecycleState`）：
- active：默认可召回。
- sleeping：默认自动召回受限；exact/高相关或显式 search/deep 可召回；实际注入后触发 AccessTracker + best-effort wake。
- archived：auto 禁止；显式 `include_archived` 才允许；不自动 wake。
- forgotten：所有模式禁止。

**fail-closed 保证**（即使索引 entry 显示 active）：
- 索引 entry 存 `lifecycle_state/validity_state` 快照，但每次源生命周期变更都会触发 `retrieval_index_refresh`（见 O）。
- **auto 模式额外对 top 候选做一次有界实时校验**：`SELECT id, lifecycle_state, validity_state FROM memory_records WHERE id IN (...)`（单查询，非长事务），过滤掉 `archived/forgotten/superseded/expired`。
- 对 SegmentSummary/EpochCheckpoint：若源记录不存在或 `source_hash` 不一致 → 跳过该命中（陈旧索引不泄漏）。

---

## N. AccessTracker 接入

- 复用 `aiive.memory.memory_access_tracker.MemoryAccessTracker.touch(ids)`（仅 touch 实际注入/工具最终返回的 id；初筛候选不 touch；sleeping 注入后 best-effort wake）。
- 仅 touch `MemoryRecord` 命中（与现有行为一致）。
- **SegmentSummary / EpochCheckpoint 是否需要 access tracking：审计结论——P4 生命周期服务只对 MemoryRecord 维护，Summary/Checkpoint 无 active/sleeping 语义，因此 v1 不对其做 AccessTracker.touch**（不套用 MemoryRecord 逻辑）。如需未来支持 Summary 热度，再加。
- `memory_search` 工具返回的记忆已在 `_handle_memory_search` 中 touch；Phase 5 统一后改为在工具最终返回 `RetrievalHit` 时 touch 其 `memory_record_id`。

---

## O. Outbox 索引更新链

复用现有 Outbox 范式（`ENABLED_OUTBOX_JOB_TYPES` + `HandlerRegistry` + `OutboxWorker`）：

新增 job_type（加入 `recall_config.ENABLED_OUTBOX_JOB_TYPES`）：
- `retrieval_index_refresh`：单条源索引 upsert/tombstone。
- `retrieval_index_rebuild`：批量重建（新 `index_version`）。

operation_id（幂等）：
```
retrieval_refresh:{source_type}:{source_id}:{source_version}
retrieval_index_rebuild:{index_version}
retrieval_index_rebuild:bootstrap:{index_version}
```
（refresh 的 operation_id 含 `source_version`：每次源版本变更产生独立 job，已完成的旧版本
不会阻塞新版本刷新，避免「首次刷新完成后后续变更（含 forget）被永久丢弃」（revision 7 隐私
承诺）；同版本内多次入队幂等合并。`rebuild` 的 `:index_version` 避免跨版本重入冲突；
`bootstrap` 前缀用于启动时历史 backfill，通过 `operation_id` UNIQUE 约束保证仅入队一次）

**触发点（同一事务内入队，不阻塞主写入）**：
- SegmentSummary sealed（`handle_segment_sealing` Phase C 写 Summary 后）。
- EpochCheckpoint sealed（`handle_epoch_checkpoint` Phase C）。
- MemoryRecord create/revise/reinforce/promote/sleep/wake/archive/supersede（`memory_mutation.enqueue_projection` 内新增；复用现有同一事务入队位置）。
- Forget / forgotten 状态变化（`memory_write_service.forget()` → `lifecycle_state='forgotten'`）：入队 tombstone（`is_searchable=False`）。
- 索引版本升级 / 管理员重建：`retrieval_index_rebuild`。

**Handler 纪律**：
- Handler **不直接 finalize OutboxJob**（OutboxWorker 负责）。
- 提交前再校验 source version（fencing）；旧版本 Job 不覆盖新数据。
- 索引失败 → 仅该 Job retry/deadletter，**不影响已成功的 Memory/Segment/Epoch 主事务**（fail-open）。
- `forgotten` 必须 fail-closed：即使索引未更新，检索时按 M 节实时校验源状态/ tombstone，确保不泄漏。

---

## P. 索引重建与版本切换

新增表 `retrieval_index_runs`（概念 `RetrievalIndexRun`）：
```text
id, trace_id, operation_id, status, execution_token, attempt_count,
index_version, source_type, source_id, source_version,
batch_cursor, indexed_count, skipped_stale_count, error_message,
created_at, started_at, completed_at
```
重建要求：
- 新 `index_version` 与旧版本**并行构建**（新版本全部 `is_current=False`）。
- 构建期间线上继续用旧 `is_current` 版本（不中断）。
- 新版本完整可用后**原子切换**：单条 `UPDATE retrieval_index_entries SET is_current=(index_version=:new)`。
- 支持中断/接管/续跑（复用 MaintenanceRun 的 claim/lease/fencing 范式）。
- **重建开始时不清空旧索引**；失败不影响旧版本服务；切换完成后旧版本异步保留审计期。
- 不使用「先清空索引表再全量重建」破坏性方案。

---

## Q. ContextAssembler 接入

- 不扩大 P1/P3 已有 hard limit（`ContextBudget`）。
- 新增分区（在 `ContextBudget` 加两项，需同步 `all_partitions`/`validate`）：
  - `retrieved_history_summary`（warm/cold Summary+Checkpoint 命中，独立 token budget）。
  - `deep_history_raw`（仅 deep 模式；auto 模式不使用，默认 0/极小）。
- `auto` 模式：调用 `UnifiedRetriever.retrieve(mode='auto')`，结果落入 `retrieved_memory` + `retrieved_history_summary`；不使用 `deep_history_raw`。
- Summary 命中优先；按 token budget 打包；超预算稳定截断（`(final_score DESC, source_type, source_id)`）。
- `Current User Message` 与 `WorkingState` 不能被检索结果挤掉（其在 assemble 中的插入位置早于检索结果，且各自分区独立预算）。
- 最终请求继续过 `TokenCounter` hard gate。
- **检索失败降级**：捕获 `UnifiedRetriever` 异常 → 回退到当前 P3/P4 上下文（即不加载检索增强分区），不中断对话（与 `_load_sealing_bridge` 的 degraded 风格一致）。

---

## R. Migration（基于真实 Alembic head）

- **新增表**：`retrieval_index_entries`、`retrieval_index_runs`。
- **新增列**：源表**不新增列**（tier 存投影，不修改 MemoryRecord/SegmentSummary/EpochCheckpoint）。
- **新增索引**：
  - `retrieval_index_entries`：`UNIQUE(source_type, source_id, source_version, index_version)`、`ix_retrieval_current(source_type, is_current)`、`ix_retrieval_search(search_text)`（SQLite 用普通索引；Postgres 可选 GIN/trigram 后续迁移）、`ix_retrieval_thread(thread_id)`、`ix_retrieval_scope(scope_type, scope_id)`、`ix_retrieval_tier(tier)`。
  - `retrieval_index_runs`：`ix_retrieval_run_status(status)`、`uq_retrieval_run_op(operation_id)`。
- **FK/CHECK**：`source_type` CHECK IN (memory_record, segment_summary, epoch_checkpoint)；`retrieval_tier` CHECK IN (hot, warm, cold)；`is_current`/`is_searchable` Boolean。
- **PostgreSQL / SQLite 兼容**：v1 不使用 `tsvector`/GIN/`pg_trgm` 等 SQLite 不支持的类型；`search_text` 用普通 `Text` + Python 词汇检索，两库通用。
- **upgrade/downgrade**：双写；downgrade 丢弃两表（`drop_table`）。
- **历史索引初始化（backfill）**：新增**一次性 Outbox 任务**或 `scripts/backfill_retrieval_index.py`，有界分批扫描现有 MemoryRecord / SegmentSummary / EpochCheckpoint，写入 `retrieval_index_entries`（index_version=1）。**不在 migration 中执行**（migration 只建表）；backfill 由 `retrieval_index_rebuild:1` Job 完成。
- **大表建索引锁表风险**：新表初始无数据 → 建索引无锁；后续对大表加普通 B-tree 索引可用 `CREATE INDEX CONCURRENTLY`（Postgres，在独立迁移/script，非 initial）。
- **分批 backfill**：复用 MaintenanceRun 的 batch 续跑范式，按 `updated_at`/`created_at` keyset 分批。
- **migration 中禁止**调用 embedding model、禁止不可恢复索引清空。

---

## S. 精确修改文件

**已存在（修改）**：
- `db/models.py`：新增 `RetrievalIndexEntry`、`RetrievalIndexRun` 模型。
- `alembic/versions/*`：新增一个 migration（建表 + 索引）。
- `memory/recall_config.py`：新增 `retrieval_*` 预算与 `retrieval_route_weights`；`ENABLED_OUTBOX_JOB_TYPES` 增加 `retrieval_index_refresh`/`retrieval_index_rebuild`。
- `runtime/context_budget.py`：新增 `retrieved_history_summary` / `deep_history_raw` 分区。
- `runtime/context_assembler.py`：`assemble` 中接入 `UnifiedRetriever`（auto 模式），失败时降级；新增 `_load_retrieved_history` 辅助。
- `memory/memory_mutation.py`：`enqueue_projection` 内（同一事务）增加 `retrieval_index_refresh` 入队（替代原 gated 但未接线的 vector job）。
- `memory/memory_access_tracker.py`：touch 接口不变，仅调用侧改为传 `RetrievalHit.memory_record_id`。
- `worker/outbox_handlers.py`：新增 `handle_retrieval_index_refresh` / `handle_retrieval_index_rebuild`；`register_all` 注册。
- `worker/outbox_dto.py`：无需改（复用 `HandlerResult`）。
- `worker/outbox_worker.py`：无需改（allowlist 已涵盖）。
- `tools/builtin_tools.py`：增强 `memory_search`（统一为 `UnifiedRetriever`，返回 `RetrievalHit`）；新增 `history_search`（search/deep 模式，统一返回）；保留 `memory_event_log` 作为 deep raw 的轻量别名或迁移到 deep。
- `api/routes_*.py`：如前端需展示检索命中，在 `routes_memories.py` / 新增 `routes_retrieval.py` 暴露检索（可选，v1 以工具为主）。
- `memory/automatic_recall.py`：保留（MemoryRecord 路由）；其 FTS tokenize 复用于 `UnifiedRetriever`。

**新增（遵循现有 `memory/`、`worker/` 风格；新建 `retrieval/` 包）**：
- `retrieval/retrieval_types.py`：`RetrievalHit` / `RetrievalRequest` / `RetrievalSourceVersion` 等 Pydantic 模型。
- `retrieval/retrieval_index.py`：索引 entry 的读写/upsert/tombstone/is_current 切换。
- `retrieval/retrieval_store.py`：源 → `search_text`/`title`/`snippet`/`tier` 的投影构造（每个 source_type 一个 builder）。
- `retrieval/retrieval_retriever.py`（即 `hybrid_retriever`）：统一流水线（J 节 1–12）。
- `retrieval/raw_history_expander.py`：二阶段原始回溯（L 节）。
- `retrieval/indexing_service.py`：Handler 逻辑（fencing、source version 校验、实时入队）。
- `retrieval/index_rebuild.py`：批量重建 + `RetrievalIndexRun` 范式。
- `retrieval/source_version.py`：H 节真实 source_version 提取器。
- `scripts/backfill_retrieval_index.py`：历史初始化（有界分批）。

**不重复创建**：不新建与 `knowledge/`（文档 Qdrant）重复的检索模块；`memory_retriever.py` 文档提及但**项目中不存在**，Phase 5 用 `retrieval/` 包实现，不凭空引用不存在的模块。

---

## T. 测试矩阵（覆盖要求 #1–#45）

新增 `tests/test_phase5_retrieval.py`（及必要的 indexing/rebuild 测试），至少覆盖：
1. MemoryRecord 可被统一检索；2. SegmentSummary 可被统一检索；3. EpochCheckpoint 可被统一检索；4. active 默认可召回；5. sleeping 只在 allowed 模式/route；6. archived 不进 auto；7. archived 仅 `include_archived` 返回；8. forgotten 所有模式禁止；9. pinned 不挤满上下文；10. exact 命中；11. lexical 命中；12. 语义（v1 关闭，留回归位）；13. hybrid score 确定；14. tie-breaker 稳定；15. 同 source 不重复；16. Memory 与 Summary 近重复去重；17. raw Turn/Event 不进 auto；18. deep 先 Summary 再有界展开 raw；19. raw 不跨 Segment；20. tool_call/result 顺序正确；21. provenance 完整；22. token_count 用真实 TokenCounter；23. 检索不挤掉 WorkingState/Current Message；24. 超预算稳定截断；25. refresh operation_id 幂等；26. 旧 source_version 不覆盖新索引；27. 生命周期变化后陈旧索引不泄漏 archived/forgotten；28. 索引失败不阻断主写入；29. 索引服务不可用自动降级；30. Summary sealed 触发 refresh；31. Checkpoint sealed 触发 refresh；32. Memory 生命周期变化触发 refresh；33. sleeping 注入后 touch + wake；34. 初筛未注入候选不 touch；35. memory_search 返回 MemoryRecord 被 touch；36. source hash 不一致索引提交失败/跳过；37. rebuild 中旧索引继续服务；38. rebuild 完成后原子切换；39. rebuild 失败不清空旧索引；40. 1k/10k/100k 索引规模查询有界；41. 无无界 raw scan；42. P0.5A–P4 全量回归；43. Postgres migration upgrade/downgrade；44. 单机重启后索引任务可接管恢复；45. ContextAssembler 检索失败降级正常。

---

## U. 回滚与降级方案

- **索引不可用时降级顺序**（不阻断主对话）：
  1. exact DB lookup（MemoryRecord.canonical_key/scope）→
  2. 现有 `AutomaticRecallEngine`（MemoryRecord 路由）→
  3. 最近 Summary/Checkpoint（`_load_segment_summaries` / `_load_epoch_checkpoint`）→
  4. 无检索扩展（纯 P3/P4 上下文）。
- **Source newer than index**：auto 跳过陈旧命中；search 标记 stale 并按需回源；enqueue refresh；不允许旧索引覆盖新数据。
- **Source lifecycle changed**：按 M 节 fail-closed 过滤（即使索引 entry 仍显示 active）。
- **Partial rebuild**：旧 `index_version` 保持 `is_current` 直到新版本完整可用。
- **索引失败不回滚主事务**：refresh/rebuild Job 失败仅 retry/deadletter，Memory/Segment/Epoch 主写入已成功。
- **forgotten**：检索时实时校验，确保即使索引未刷新也不泄漏。
- **回滚**：migration downgrade 删除两新表即可；索引为派生投影，删除后可从源表 `retrieval_index_rebuild` 重建（源表是唯一真相源，未被 Phase 5 修改）。

---

## 已确认决策（用户批准后直接进入实施）

1. **检索技术 v1** = 真实有索引的可移植倒排表 `retrieval_index_tokens`（CJK 二元 token + 英文规范 token，SQL 按 token 精确查 posting 聚合命中数）。`search_text` 仅用于 snippet/debug，不作为 10 万规模主检索路径。
2. **全局 generation**：`RetrievalIndexGeneration` 以唯一 active generation 的 `index_version` 作为查询真相源；每条 Entry 的 `is_current` 仅用于「同 source 仅保留一个当前版本」，不充当全局真相。
3. **rebuild 与实时 refresh 并发闭合**：`active=v1 / building=v2`；来源变化同时为 active+building 两个 generation 入队刷新；operation_id 含 `index_version`；切换前完成 backfill、消化 building refresh backlog、再次校验源版本，然后原子激活。
4. **单条 refresh 不走 Run**：单条 `retrieval_index_refresh` 直接 upsert/tombstone；全量 `retrieval_index_rebuild` 才用 `RetrievalIndexRun`（分批游标在 `batch_cursor` JSON 列，复用 `HandlerOutcome.CONTINUE` 分页，不计入失败重试）。
5. **自动召回单一编排入口**：`ContextAssembler → UnifiedRetriever → {AutomaticRecallEngine(Memory 路由适配器) + Summary/Checkpoint lexical 路由} → 统一 RetrievalHit/去重/预算`。禁止 ContextAssembler 同时独立调用两者。
6. **Deep 原始回溯**：`ORDER BY TurnRecord.turn_sequence, Event.turn_event_index`；读取 P3 `CompactionInput` manifest，逐条校验 source IDs、event content hash、source hash；校验失败 degraded 不返回未验证内容。
7. **Forgotten tombstone 清理派生内容**：删除 `retrieval_index_tokens`、清空 `title/search_text/snippet/敏感 metadata`、`is_searchable=false`；检索仍实时校验源 lifecycle 防异步空窗泄漏；source version 仅精确 equality，不做跨类型字符串比较；`EpochCheckpoint.source_hashes` 先 canonical JSON 再 SHA-256。
8. **v1 不增加未使用的 embedding 列**：仅保留代码接口，等真实 provider/pgvector 技术确定后再迁移。
9. **v1 去重仅做确定性去重**：同 source / 同 Segment provenance / 同 Memory canonical_key+scope+value hash / 同 Epoch 最新版本；无 embedding 时不自动删除「语义近重复」。

### 确认的参数

- lexical index：`retrieval_index_tokens`
- raw Turn/Event：不直接索引（仅经 Summary provenance 二阶段回溯）
- `retrieved_history_summary` token 预算：**800**（auto/search 用）
- `deep_history_raw` token 预算：**1500**，auto 模式为 **0**
- backfill：经 `retrieval_index_rebuild:1` Outbox Job 有界分批

---

## Phase 5 v1 修订明细（对应上面 9 点）

### 修订 1 — 可移植倒排表 `retrieval_index_tokens`

新增表：

```text
retrieval_index_tokens:
  id
  entry_id      FK -> retrieval_index_entries.id
  index_version int
  token         str
  token_kind    str   # cjk_bigram | word
  term_frequency int
  UNIQUE(entry_id, token)
  INDEX(index_version, token, entry_id)
  INDEX(entry_id)
```

复用 `automatic_recall._tokenize_cjk_aware` 生成中文二元 token 与英文规范 token；`token_kind` 区分 `cjk_bigram`/`word`。检索时：

```sql
SELECT entry_id, COUNT(*) AS hits
FROM retrieval_index_tokens
WHERE index_version = :active AND token IN (:query_tokens)
GROUP BY entry_id
ORDER BY hits DESC
LIMIT :k
```

再 JOIN `retrieval_index_entries`（按 active generation + `is_current` + lifecycle/tier 过滤）得到候选。`search_text` 只用于 snippet 生成与调试，不作为 10 万级主路径。

### 修订 2 — 全局 `RetrievalIndexGeneration`

```text
retrieval_index_generations:
  id
  index_version   int  UNIQUE
  status          str   # building | active | retired | failed
  build_started_at
  build_completed_at
  activated_at
  source_cutoff
  policy_version
  backfill_done   bool  # 启动引导：标记历史 backfill 是否已完成
```

查询统一使用唯一 `status='active'` 的 `index_version`（启动时校验唯一性）。每条 Entry 仍保留 `is_current`（同 source 仅一个当前版本），但**全局真相源是 active generation**，不是 `is_current`。

### 修订 3 — rebuild 与 refresh 并发闭合

- 并发窗口：`active=v1`（线上服务）、`building=v2`（并行构建）。
- 来源写路径（Memory 生命周期变更 / Summary sealed / Checkpoint sealed）调用 `enqueue_retrieval_refresh`，当存在 `building` generation 时，由 handler 对同一 refresh Job 同时作用于 `active` 与 `building` 两代（operation_id 含 `source_version`，不含 `index_version`）。
- operation_id 格式：
  ```text
  retrieval_refresh:{source_type}:{source_id}:{source_version}
  ```
- 切换条件（原子激活前校验）：① building generation 的 backfill 完成；② building refresh backlog 已处理（无 pending/running 的 building refresh Job）；③ 再次校验源版本未被并发写超越；满足后 `UPDATE ... SET status='active' WHERE index_version=:new` 并把旧 active 置 `retired`。

### 修订 4 — 单条 refresh vs 全量 rebuild

- 单条 `retrieval_index_refresh`：直接 upsert/tombstone 一个 Entry + token posting，**不创建 Run**。
- 全量 `retrieval_index_rebuild`：创建 building generation → 用 `RetrievalIndexRun`(UNIQUE `outbox_job_id` NOT NULL) 经 `HandlerOutcome.CONTINUE` 分页分批扫描所有源（分批游标存在 `batch_cursor` JSON 列，无独立 batch 表）→ 完成后原子切换。
- `RetrievalIndexRun` 字段：`outbox_job_id FK UNIQUE NOT NULL`、`operation_id`、`status`、`execution_token`、`attempt_count`、`index_version`、`batch_cursor(JSON)`、`indexed_count`、`skipped_stale_count`、`error_message`、时间戳。
- 正常分页用 `HandlerOutcome.CONTINUE`，**不计入** `failure_attempt_count`/retry（`failure_attempt_count` 当前为预留占位列，尚未写入）。

### 修订 5 — 自动召回单一编排入口

`ContextAssembler._load_agent_context` 改为只调用 `UnifiedRetriever.retrieve(mode='auto')`；其内部分派：
- `AutomaticRecallEngine` 作为 **Memory 路由适配器**（复用 exact/fts/episode 路由与评分）；
- Summary/Checkpoint 走 **lexical 路由**（查 `retrieval_index_tokens`）；
- 统一为 `RetrievalHit` → 去重 → 预算打包。
禁止 ContextAssembler 再独立调用 `AutomaticRecallEngine`。

### 修订 6 — Deep 原始回溯

`raw_history_expander`：
- `ORDER BY TurnRecord.turn_sequence, Event.turn_event_index`（**不按 turn_id 排序**）；
- 读取 P3 `CompactionInput.event_manifest` + `source_hashes/source_hash`；
- 逐条校验：① event id 属于命中的 Summary；② `event_content_hash` 与 manifest 一致；③ Summary `source_hash` 仍有效；
- 校验失败 → 该 Summary 标记 degraded，不返回未验证 raw 内容；
- 有界：segment / 时间范围 / `deep_max_turns` / `deep_history_raw` token 上限；不跨 Segment 无界扩展；tool_call/tool_result 配对沿用 `_dicts_to_chat_messages` 逻辑。

### 修订 7 — Forgotten tombstone 清理派生内容

`tombstone_entry`：
- `DELETE FROM retrieval_index_tokens WHERE entry_id=:id`；
- `title='' / search_text='' / snippet='' / metadata={}（清空敏感 metadata）`；
- `is_searchable=False`；
- 实时校验：检索时对 top 候选回源校验 `lifecycle_state`（MemoryRecord）与 `source_hash`（Summary/Checkpoint），fail-closed 过滤 archived/forgotten/superseded/expired；异步 tombstone 空窗不泄漏。
- source version 仅精确 equality，不跨类型字符串比较；`EpochCheckpoint.source_hashes` 先 `json.dumps(..., sort_keys=True, ensure_ascii=False)` 再 SHA-256。

### 修订 8 — 不新增 embedding 列

`RetrievalIndexEntry` **不定义** `embedding/embedding_model/embedding_dimension/embedding_version` 列；仅代码层保留 `SemanticAdapter` 接口占位（默认返回空），待真实 provider/pgvector 技术确定后再迁移。

### 修订 9 — 确定性去重

仅以下确定性去重，无 embedding 时不自动删「语义近重复」：
- 同 `source_type+source_id`；
- 同 Segment provenance（Summary 命中优先于其 raw Turn）；
- 同 Memory `canonical_key+scope_type+scope_id+content_hash`；
- 同 Epoch 最新 `version` Checkpoint（靠 `is_current`）。

### 新增测试（叠加在原 #1–#45）

- 10 万 Entry 查询走 token 索引而非全表 ILIKE（用 `EXPLAIN`/查询 plan 或计数断言）；
- active generation 唯一；
- rebuild 期间来源更新同时进入 active/building generation；
- 切换后不丢 rebuild 期间的变化；
- CONTINUE 不增加失败重试计数；
- 自动召回不重复执行 Memory 路由；
- deep raw 按 `turn_sequence` 排序；
- Event hash 不一致时拒绝 raw expansion；
- forgotten tombstone 清空索引文本与 token；
- 旧 source_version Job 不覆盖新版本；
- 无 embedding 列仍可完成 v1 全链路。

---

## 实施状态（代码与测试）

已实现并落地的组件：

- **检索包 `aiive/retrieval/`**：`retrieval_types`（统一 `RetrievalHit`/请求/结果）、
  `source_version`（deterministic version 比较、EpochCheckpoint source_hashes SHA-256）、
  `index_tokenizer`（CJK 二元 + 英文规范 token）、`retrieval_index`
  （`RetrievalIndexManager`：generation / upsert / tombstone / 倒排 posting / rebuild 游标）、
  `retrieval_store`（`build_entry_fields` 三类源投影）、`indexing_service`
  （`refresh_source`：active+building 双 generation、forgotten tombstone、
  source_version fencing；archived 保持索引不 tombstone）、`raw_history_expander`
  （DEEP 二阶段原始回溯，CompactionInput manifest/hash 校验，verification_status 标注）、
  `index_rebuild`（`retrieval_index_rebuild` 三阶段有界分批 Handler，CONTINUE 分页，
  `_insert_conflict_do_nothing` SQLite SAVEPOINT 保护）、`retrieval_bootstrap`
  （启动时 `main.lifespan` 调用 `ensure_retrieval_backfill()`，operation_id UNIQUE
  约束幂等，`backfill_done` 标记）。
- **统一编排 `UnifiedRetriever`**：AUTO/SEARCH/DEEP 三模式；memory 路由复用
  `AutomaticRecallEngine` 适配器，summary/checkpoint 走 lexical 倒排；统一去重 + 预算；
  两路异常均隔离降级（revision 5 / #45）。
- **接线**：`MemoryMutationExecutor._enqueue_retrieval_refresh`（operation_id 同 source
  幂等去重，避免 `outbox_jobs.operation_id` 唯一冲突污染外层事务）；`outbox_handlers`
  注册 `retrieval_index_refresh` 与 `retrieval_index_rebuild`，并在 segment sealing /
  epoch checkpoint 阶段 C 入队 refresh；`ContextAssembler._load_agent_context` 改为经
  `UnifiedRetriever` 自动召回（失败降级原 `AutomaticRecallEngine`，revision 5）；
  `builtin_tools` 新增 `history_search` 工具。
- **迁移**：`p5a0b1c2d3e4f_retrieval_index.py`（五张新表：generations / entries / tokens /
  runs / batches，含约束与索引，PG 用 JSONB、其余 JSON；downgrade 全量回退）；
  `p5c1d2e3f4a5b_retrieval_ops.py`（添加 `retrieval_index_generations.backfill_done`
  Boolean，移除 `retrieval_index_runs` 未使用的 `source_type`/`source_id`/`source_version`
  列；downgrade 可逆）。
- **启动引导 `main._ensure_retrieval_generation`**：幂等确保存在唯一 active generation
  （全新部署创建 v1），否则 lexical 路由空转、统一检索失效。

测试：`tests/test_phase5_retrieval.py`（**45 例**）全面覆盖——

**基础索引与检索**：索引构建与倒排查询（#11）、exact 命中（#10）、三类源 refresh
upsert/tombstone、AUTO 排除 sleeping（#5）、生命周期过滤与 tombstone（#8/#27）、
archived 保持索引可检索但受 `include_archived` 控制。

**版本控制**：旧 source_version 不覆盖新索引（#26）、同 source 单 current（#15）、
精确版本重复 refresh 不违反 UNIQUE、迟到旧版本不重新成为 current。

**UnifiedRetriever 编排**：SEARCH 模式经记忆路由/lexical 路由召回（#1/#2/#3/#11）、
AUTO 单一编排入口（#5）、token 预算截断（#24）、确定性去重（#15）、
索引路由降级隔离（#45）、`memory_search` 统一走 UnifiedRetriever、
`token_count` 使用真实 `LiteLLMTokenCounter.count_text`（非 `len//4`）、
fail-closed 有界回源校验不 N+1。

**rebuild 端到端**：全量回填 + 原子切换（`active`→`retired`，新 gen 上线）、
rebuild 期间并发更新写双代、切换后不丢失新内容——均经 CONTINUE 分页驱动完整覆盖。

**新增回归**（12 例）：include_archived=false 不返回 archived（#1）；include_archived=true
返回 archived（#2）；AUTO 永远过滤 archived（#3）；archived 不触发 wake/touch（#4）；
forgotten 所有模式禁止且索引文本已清理（#5）；旧数据库启动自动且仅一次入队 bootstrap
rebuild（#6）；backfill_done 后不再入队（#7）；EpochCheckpoint version 相同但
source_hashes 不同则过滤（#8）；DEEP Event 不在 manifest 中拒绝展开（#9）；
DEEP Event hash 变化拒绝展开（#10）；Hot Summary 与 UnifiedRetriever 去重不重复（#11）；
SQLite 冲突仅回滚单条插入不破坏整个事务（#12）。

架构数据流详情：

- **bootstrap backfill**：`main.lifespan` 中调用 `ensure_retrieval_backfill()`，检测
  active generation 是否已完成 backfill（`backfill_done` 字段）；未完成时入队
  `retrieval_index_rebuild:bootstrap:{index_version}` OutboxJob，通过 `operation_id`
  UNIQUE 约束保证幂等（仅一次）。空数据库直接标记 `backfill_done=True` 跳过。
- **archived 处理**：archived 源保持在索引中（`is_searchable=True`，不入 tombstone），
  文本/token 均保留；在检索时由 `include_archived` 参数控制是否返回：SEARCH/DEEP 中
  `include_archived=True` 可返回，AUTO 永远过滤；不会自动 wake/touch。
- **forgotten 处理**：所有模式禁止返回；`retrieval_index_rebuild` 对 forgotten 源执行
  `tombstone_by_source` 清理索引文本和 token；`refresh_source` 对其 tombstone
  （`is_searchable=False`，清空文本）。
- **token_count**：使用真实 `LiteLLMTokenCounter.count_text()` 计算（含 fallback），
  非 `len//4` 启发式。
- **EpochCheckpoint source_hashes 校验**：检索时重算 `epoch_checkpoint_source_hashes_hash`
  （canonical JSON → SHA-256），与索引命中时的 `source_hash` 比对，不一致则过滤加
  best-effort stale refresh 入队。
- **DEEP manifest/hash 验证**：读取 P3 `CompactionInput` 不可变快照的 `event_manifest`
  与 `SegmentSummary.source_event_ids`；Event 必须属于 source_event_ids 集合；
  `event_content_hash` 与 manifest 逐条比对；`CompactionInput.source_hash` 与
  `SegmentSummary.source_hash` 整体比对；任意失败 → 父 Summary 标注
  `verification_status=degraded|stale`，不返回对应 raw 内容。
- **AUTO 摘要去重**：`ContextAssembler._collect_hot_history_ids` 收集最近 sealed
  SegmentSummary 和最晚 EpochCheckpoint 的 source_id，通过
  `RetrievalRequest.exclude_source_ids` 传入 `UnifiedRetriever`，避免与 legacy
  热分区重复加载。
- **SQLite 幂等插入**：`_insert_conflict_do_nothing` 使用 `begin_nested()` SAVEPOINT
  包裹单条插入，冲突时仅回滚该 SAVEPOINT，不中止整个 rebuild 事务（替代原 `db.rollback()`）。

已知后续项：

- 语义检索 v1 关闭，仅保留 `SemanticAdapter` 接口占位；待真实 embedding provider 接入。
- sleeping 仅经 memory 路由按 `include_sleeping` 过滤；summary/checkpoint 恒为 valid。
- **K 节评分融合（RRF + 归一化加权）已实现**：`UnifiedRetriever._fuse_scores` 按三路
  （exact / memory / lexical）原生排名计算 RRF 分量 `k/(k+rank)`，与已归一化到 [0,1] 的
  确定性分量（lexical / exact / recency / authority / scope）按 `RetrievalConfig.
  retrieval_route_weights` 加权求和（权重和归一，保证 `final_score ∈ [0,1]`）。`rrf_k` 与
  `retrieval_route_weights` 现已接入；`_dedup_and_budget` 已用稳定 tie-breaker
  `(-final_score, source_type, source_id)`。`scope` 信号当前未接到 `RetrievalHit`（以中性
  0.5 占位，仅贡献常数基线、不影响相对排序）；文档公式中的 `lifecycle` 分量因 `RetrievalConfig`
  无对应权重而暂不计入。`retrieval_relevance_threshold` 仍为零引用（属候选过滤阈值，非融合项）。
- `RetrievalIndexRun.failure_attempt_count` 已接线上报：在 `index_rebuild._mark_run_failed`
  的持久化 UPDATE 中 `failure_attempt_count += 1`（每次真正失败落地一次）。CONTINUE 分页
  不调用 `_mark_run_failed`，故不计入；与 `attempt_count`（仅计接管/重试获取次数）语义区分
  清晰，符合 `RetrievalIndexRun` 模型注释约定。

