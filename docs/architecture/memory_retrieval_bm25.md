# AIive 记忆检索架构：Exact + Dense + Sparse(BM25) + Unified Ranking

> 版本：2026-11-03
> 范围：backend/aiive/memory/automatic_recall.py、recall_fusion.py、recall_ranking.py、
> bm25_search.py、vector_projection.py、forget/visibility_service.py、
> backend/alembic/versions/c3f7b2a9d1e4_add_memory_bm25_index.py、deploy/pg/Dockerfile
> 关联：docs/CODE_MAP.md、docs/architecture/phase5.md、docs/architecture/memory_v2_architecture.md

## 1. 结论与定位

记忆召回由「Exact Lookup + Dense Vector + Sparse BM25 + Unified Ranking」四段构成：

```text
AutomaticRecallEngine
├── Exact Lookup        canonical_key / scope_id 精确匹配（确定性候选）
├── Dense Retrieval     pgvector HNSW（MemoryVectorProjectionService，512 维）
├── Sparse Retrieval    ParadeDB pg_search BM25（索引建在 memory_records 上）
└── Unified Ranking     RRF + Relevance + Recency + Importance + Trust + Scope
```

三条硬性约束：

1. **真 BM25**：使用 pg_search 的 bm25 访问方法索引 + paradedb.score()，不是 ts_rank、
   ILIKE 或 token overlap 的近似。
2. **单一事实源**：索引直接建在 memory_records 上，不复制记忆内容；索引与表数据在同一
   事务内维护，无需同步任务。
3. **安全不旁路**：所有路由的候选在融合前统一经过 ForgetVisibilityService 批量过滤，
   BM25 不可能成为遗忘后内容的新入口。

## 2. 索引 DDL 与 Tokenizer

由 Alembic 迁移 backend/alembic/versions/c3f7b2a9d1e4_add_memory_bm25_index.py 创建：

```sql
CREATE EXTENSION IF NOT EXISTS pg_search CASCADE;  -- CASCADE 同时启用 vector

CREATE INDEX IF NOT EXISTS ix_memory_records_bm25 ON memory_records USING bm25 (
    id,
    (content::pdb.jieba),
    (canonical_key::pdb.literal_normalized),
    ((keywords)::text::pdb.jieba)
);
```

字段与 tokenizer 选择：

| 字段 | Tokenizer | 理由 |
|---|---|---|
| id | 主键（key field） | bm25 索引需要唯一行标识；paradedb.score(id) 以它取分 |
| content | pdb.jieba | 中文分词 + 英文词元，适配中英混排；搜索态使用 jieba search_mode |
| canonical_key | pdb.literal_normalized | 整串作为一个词元，保留 user.preference.editor_theme 这类标识符的精确匹配能力 |
| keywords | ((keywords)::text)::pdb.jieba | 显式同义词/上位词（JSON 数组）参与 BM25，保持既有「奶茶 → 霸王茶姬」召回能力 |

版本锁定：pg_search 0.26.4 + PostgreSQL 16.15 + pgvector 0.8.7。pg_search 的包名与
PostgreSQL 主版本、发行版代号绑定，升级必须同时改 deploy/pg/Dockerfile 的
PARADEDB_VERSION 与 PG_MAJOR，并重跑集成测试。

## 3. 查询 SQL 与语义

backend/aiive/memory/bm25_search.py::Bm25SearchService.search 发出的真实 SQL：

```sql
SELECT m.id AS memory_id, paradedb.score(m.id) AS score
FROM memory_records m
WHERE m.id ``` paradedb.boolean(should => ARRAY[
        paradedb.match_disjunction('content', :query),
        paradedb.match_disjunction('canonical_key', :query),
        paradedb.match_disjunction('keywords', :query)
      ])
  AND m.lifecycle_state IN :lifecycle_states
  AND m.validity_state IN :validity_states
  AND ((m.scope_type = :st_0 AND m.scope_id IS NULL)
       OR (m.scope_type = :st_1 AND m.scope_id = :si_1) ...)
ORDER BY score DESC, m.id ASC
LIMIT :limit
```

为什么用 match_disjunction 而不是 paradedb.parse：

* parse 对不含空格的整句中文会退化成**短语查询**（要求词序相邻）。实测
  「最近聊到的手冲咖啡」在 parse 下零命中，而 match_disjunction 会先用索引上的
  jieba 分词再按 should/OR 匹配，语义等价于「任一查询词命中」，与旧词法路由一致。
* match_disjunction 不做查询语法解析，用户输入中的冒号、引号、AND 不会被解释成
  查询 DSL，避免语法错误与查询注入。

分数口径：paradedb.score 返回真 BM25 分（受 k1/b、文档长度、IDF 影响），量纲不是 0..1。
因此路由内部按**批内最高分**归一化（recall_ranking.normalize_scores），原始分另存于
MemoryRecallItem.raw_route_score 与 RecallCandidateTrace.route_raw_score 供诊断。

回源验证：BM25 只返回 (memory_id, score, rank)。引擎随后按 ID 回查 memory_records，
重新校验 lifecycle / validity / scope（fail-closed），再走统一 Forget 过滤。索引里即使
残留旧行，也不会直接进入上下文。

## 4. 事务一致性

pg_search 的 bm25 索引是事务性索引（随表 DML 在同一事务内更新），因此：

| 操作 | 索引行为 | 验证用例 |
|---|---|---|
| INSERT | 同事务可见 | test_bm25_recalls_chinese_memory |
| UPDATE content（含 forget 的 scrub 为 [forgotten]） | 旧词立即不可检索 | test_bm25_index_follows_update_and_scrub |
| DELETE | 同事务不可见 | test_bm25_index_respects_transaction_rollback |
| ROLLBACK | 回到回滚前状态，不残留脏索引项 | 同上 |
| Supersede / merge（更新 validity/内容） | 由 lifecycle/validity 过滤兜底 | test_forgotten_and_archived_are_not_returned |

不存在「索引同步任务」：遗忘时 phase_a_shield 把 lifecycle 置 forgotten、purge 阶段把
content 覆盖为 [forgotten]，索引自动跟随。

## 5. 统一排序公式

配置：RecallConfig.signal_weights（和为 1.0）与 RecallConfig.route_multipliers。

```text
fused = 0.40 * rrf_norm
      + 0.20 * relevance
      + 0.20 * recency
      + 0.10 * importance
      + 0.05 * trust_weight
      + 0.05 * scope_score

rrf_raw  = Σ_r  w_r / (k + rank_r)          k = 60（RecallConfig.rrf_k）
rrf_norm = min(1, rrf_raw * (k + 1) / Σ_r w_r)
relevance = 路由内归一化分（dense=cosine；bm25=score/批内最高分；lexical=词重叠比例）
trust_weight = trusted 1.0 / semi_trusted 0.7 / untrusted 0.3 / untrusted_derived 0.2 / 未知 0.5
scope_score = 1 - index(scope)/len(chain)   （thread 最高，global 最低）
```

要点：

* **不直接相加原始分**：cosine 与 BM25 分只通过各自的归一化 relevance 与 RRF 排名进入
  公式；RRF 只用排名，天然消除量纲差异。
* **Exact 不被丢弃**：route 含 exact 的候选跳过 relevance 阈值判断（确定性候选），只受
  token 预算约束。RRF 中 exact 通常 rank=1，因此 fused 天然靠前。
* **确定性**：同分按 memory_id 升序；所有信号都是纯函数（recall_ranking.py），
  无随机、无 LLM、无 cross-encoder。
* **缺失值**：importance 缺省 0.5；trust 未知 0.5；时间戳缺失 recency=0；relevance 缺失 0。
* **去重**：按 memory_id 合并，保留最大 relevance / 最大原始分，route 用 | 连接
  （如 exact|bm25），RRF 分桶在去重之前完成，避免排名漂移。

## 6. Recency：类型感知 + 意图感知

```text
recency = 0.5 ** (age_days / half_life(memory_type)) * intent_factor(memory_type, query)
```

| memory_type | half_life |
|---|---|
| episodic / event | 3 天 |
| task | 7 天 |
| knowledge | 180 天 |
| user_profile / agent_self / preference | 365 天 |
| 其他 | 90 天 |

intent_factor 只作用于情节记忆：

* 查询含显式时间意图（最近/上次/刚才/今天/昨天/上周/recent/last time/yesterday/...）→ 1.0
* 普通查询 → 0.35（折减）

效果：新近情节记忆在时间敏感查询里能排到最前；在普通查询里不会无条件压过长期偏好与
重要事实（test_recency_is_type_aware_and_intent_aware 同时验证这两种行为）。

## 7. Recent Episode 的去向

旧实现有独立的 episode 召回路由（scope 内按 observed_at 倒序 + 词重叠打分）。现在：

* 情节记忆与其它记忆同表，由 BM25 / Dense 正常召回（不再有独立路由）；
* 时间特征通过上节的 recency 信号进入统一排序；
* 时间敏感查询的能力由「意图系数 + 短半衰期」保留，而不是「无条件注入最近事件」。

## 8. 安全链路（硬性约束）

```text
候选产生 → 路由内 SQL 过滤（scope_type/scope_id、lifecycle_state、validity_state）
        → 引擎统一 ForgetVisibilityService.filter_memory_ids（Shield 选择器级 + Tombstone 实体级，fail-closed）
        → 策略渲染 render_content（sensitivity → LLM_CONTEXT）
        → fuse_and_pack 阈值 / 预算
        → 上下文注入
```

* 过滤异常时丢弃本轮全部候选（fail-closed），不做「先放行再告警」。
* Scope：ScopeContext.chain() 逐级（thread → project → workspace → capability → environment → global），
  SQL 内按 (scope_type, scope_id) 精确匹配，回源后再校验一次。
* lifecycle：默认 active；include_sleeping / include_archived 显式放宽；forgotten 永不进入。
* validity：默认 valid；include_archived 时放宽为 (valid, expired)；superseded 恒排除。
* 证据：tests/integration/pg/test_pg_bm25_recall.py 中
  test_forget_shield_blocks_bm25_hits / test_forget_tombstone_blocks_bm25_hits
  先证明「未屏蔽时 BM25 能召回」，再加 Shield/Tombstone 证明「被屏蔽后召不回来」。

## 9. 降级与可观测性

| 情形 | Bm25SearchService.status | 行为 | pack.degraded |
|---|---|---|---|
| 正常 | ready | route=bm25 | false |
| 非 PostgreSQL（SQLite 开发库） | unsupported_dialect | 词法兜底 route=lexical | true |
| 扩展未安装 | extension_missing | 词法兜底 | true |
| 索引缺失 | index_missing | 词法兜底 | true |
| 同名索引但访问方法不是 bm25 | index_incompatible | 词法兜底 | true |
| SQL 执行失败 | error | 词法兜底 | true |
| 配置显式关闭 BM25 | —（不探测） | 词法兜底 route=lexical | false（配置选择，不是降级） |

degraded_reason 记录具体原因（如 bm25_index_missing），供前端/Inspector/日志显式展示。
路由名即事实来源：bm25 表示真 BM25，lexical 表示词法兜底，二者不会混淆。

降级在聊天链路中的传播（不丢失）：

@@@text
MemoryRecallPack.degraded / degraded_reason        （引擎出口）
→ UnifiedRetriever._memory_route 返回 (hits, degraded_reason)
→ result.notes 追加 "memory_recall_degraded:<reason>"
→ RetrievalResult.degraded = True（该 note 前缀参与判定）
→ ContextAssembler._unified_recall 把 notes 回填进 MemoryRecallPack
→ 检索诊断表 routes_executed 持久化，前端「检索」页可见
@@@

## 10. 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| AIIVE_MEMORY_BM25_ENABLED | false（.env.example 与 compose 默认 true） | 是否启用 BM25 稀疏召回；非 PostgreSQL 下开启不阻断启动，运行时降级为词法并标记 degraded |
| AIIVE_MEMORY_BM25_TOP_K | 16 | BM25 有界 Top-K；与 RecallConfig.bm25_top_k 取较小值 |
| AIIVE_MEMORY_VECTOR_ENABLED | false | Dense 召回（保持原状） |
| AIIVE_MEMORY_VECTOR_TOP_K | 16 | Dense 有界 Top-K |

两路可独立启停：关 Dense 仍走 BM25；关 BM25 走词法；两路都关只剩 Exact（并有降级标记）。

## 11. 迁移与部署

* 迁移：alembic upgrade head 执行 c3f7b2a9d1e4；CREATE EXTENSION 与 CREATE INDEX 都包在
  savepoint 内，扩展缺失时打印 WARNING 并跳过，不阻断升级（运行时显式 degraded）。
* 幂等：CREATE EXTENSION IF NOT EXISTS / CREATE INDEX IF NOT EXISTS，可重复执行。
* downgrade 只 DROP INDEX，不删扩展（同库可能有其它对象依赖）。
* 扩展索引不写入 Base.metadata（bm25 AM 无法用 SQLAlchemy Index 表达，SQLite create_all
  也不应创建），因此 backend/alembic/env.py 增加 include_object 忽略该索引名，避免
  alembic check 在已装 pg_search 的库上误报「多余索引」。
* 部署镜像：deploy/pg/Dockerfile（FROM pgvector/pgvector:pg16 + ParadeDB 官方 .deb），
  docker-compose.yml 的 postgres 服务改为 build 该 Dockerfile，并把 pg_search 追加到
  postgresql.conf.sample 的 shared_preload_libraries（缺失会导致 CREATE EXTENSION 失败）。

```bash
# 构建 / 更新镜像
docker compose build postgres

# 应用迁移（容器内或宿主机）
alembic upgrade head

# 验证索引存在且访问方法为 bm25
psql -c "SELECT am.amname FROM pg_class c JOIN pg_am am ON am.oid = c.relam
         WHERE c.oid = to_regclass('ix_memory_records_bm25');"
```

## 12. 上下文注入链路

```text
Turn 执行
→ ContextAssembler._load_agent_context
   → ContextAssembler._unified_recall (context_assembler.py)
      → UnifiedRetriever.retrieve (retrieval/unified_retriever.py)
         → UnifiedRetriever._memory_route
            → AutomaticRecallEngine.recall (memory/automatic_recall.py)
               → _route_exact / _route_bm25 / _route_vector
               → _filter_forgotten (forget/visibility_service.py)
               → fuse_and_pack (memory/recall_fusion.py → memory/recall_ranking.py)
      → memory_hits → MemoryRecallPack
   → recall_messages（system 消息，受 retrieved_memory 预算约束）
   → MemoryAccessTracker.touch（记录 last_accessed_at）
→ LLM 请求
```

诊断：ContextAssembler._persist_retrieval_diagnostics 把 routes_executed、
memory_recall_candidates.route / raw_score / fused_score 写入检索运行表，前端「检索」页
据此展示每条记忆来自 bm25 还是 vector。

## 13. 测试与验证命令

```bash
# 纯数学单测（RRF / Recency / 归一化 / 权重）
pytest tests/unit/backend/test_recall_ranking.py -q

# 记忆召回单测（SQLite，验证降级路径与融合语义）
pytest tests/unit/backend/test_v2_recall.py tests/integration/backend/test_phase4_recall_sleeping_and_access.py -q

# 真实 PostgreSQL + pg_search（18 个用例；无 AIIVE_PG_BM25_URL 时自动起容器）
AIIVE_PG_TESTS=1 pytest tests/integration/pg/test_pg_bm25_recall.py -q
```

## 14. 已知限制

* 单字中文查询（如「咖」）在 jieba 分词下不产生词元，BM25 不命中；两字以上正常。
  旧词法路由能命中单字，但那是子串匹配而非词元匹配，属于有意取舍。
* BM25 索引构建是普通 CREATE INDEX（非 CONCURRENTLY，因为后者不能在迁移事务内执行），
  存量数据量大时会在建索引期间阻塞写入，需要停机窗口或手工 CONCURRENTLY 预建。
* bm25 索引不参与 SQLAlchemy metadata，索引结构变更必须新增迁移，不能靠 autogenerate。
* Recency 的意图判定是关键词表，不含语义理解；新增语言需要补 _TIME_SENSITIVE_MARKERS。
