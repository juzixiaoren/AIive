# V60 记忆检索架构升级：pg_search BM25 + 统一排序

日期：2026-11-03
范围：记忆召回（Automatic Recall）从「Exact + Lexical + Vector + Recent Episode」
升级为「Exact + Dense + Sparse(BM25) + Unified Ranking」。

详细实现说明见 `docs/architecture/memory_retrieval_bm25.md`。

## 1. 问题

原 Lexical 路由是 ILIKE + CJK bigram 词重叠打分：

* 不是 BM25：没有词频饱和（k1/b）、没有 IDF，长文档与常见词会失真；
* 每条候选都要把 content/keywords 全文拉回 Python 再算重叠，候选一多就退化；
* Recent Episode 是独立路由，按 observed_at 倒序 + 词重叠，容易出现「最近事件
  无条件压过长期偏好」。

## 2. 改动

* 新增 ParadeDB pg_search 0.26.4（PostgreSQL 16.15 + pgvector 0.8.7）BM25 索引，
  直接建在权威表 `memory_records` 上：content / canonical_key / keywords。
  Tokenizer：content/keywords 用 `pdb.jieba`（中英混排），canonical_key 用
  `pdb.literal_normalized`（保留标识符整串匹配）。
* 新增 `memory/bm25_search.py`：可用性探测（ready / extension_missing /
  index_missing / index_incompatible / error / unsupported_dialect）、查询 SQL、
  返回 (memory_id, 原始 BM25 分, rank)，由引擎回源读取 MemoryRecord。
* 新增 `memory/recall_ranking.py`：RRF、类型感知 Recency、归一化与缺失值纯函数。
* `automatic_recall.py`：去掉 `_route_recent_episode`；新增 `_route_bm25`；
  `_route_lexical` 降级为 BM25 不可用时的兜底；新增 `_filter_forgotten` 统一
  Forget 过滤（Shield/Tombstone，fail-closed）。
* `recall_fusion.py`：RRF 只用排名，cosine 与 BM25 原始分不直接相加；
  Exact 候选跳过相关性阈值；同分按 memory_id 确定性排序。
* 降级可见性：`MemoryRecallPack.degraded/degraded_reason` →
  `UnifiedRetriever` notes `memory_recall_degraded:<原因>` → `RetrievalResult.degraded`
  → 上下文 pack 回填 → 检索诊断表。
* 部署：`deploy/pg/Dockerfile`（pgvector + pg_search）、compose postgres 改为 build、
  `AIIVE_MEMORY_BM25_ENABLED` / `AIIVE_MEMORY_BM25_TOP_K`。
* 迁移：`c3f7b2a9d1e4_add_memory_bm25_index`（幂等；扩展缺失时 WARNING 跳过、
  不阻断升级）；`alembic/env.py` 增加 include_object 忽略该原生 DDL 索引。

## 3. 安全

* 所有路由候选在融合前统一经过 `ForgetVisibilityService.filter_memory_ids`；
  过滤异常时丢弃全部候选（fail-closed）。
* BM25 只返回 ID/分数，内容与状态一律回源 `memory_records` 复核 lifecycle /
  validity / scope。
* 索引与表同事务维护：forget 的 content scrub、purge 的物理删除、事务回滚都不需要
  额外同步任务。

## 4. 验证

* 全量：`pytest -q` → 1472 passed / 44 skipped（PG、TEI 专项按环境跳过）/ 0 failed。
* 真实 PG + pg_search：`AIIVE_PG_TESTS=1 pytest tests/integration/pg/test_pg_bm25_recall.py -q`
  → 21 passed（含中英混排、标识符匹配、双路去重/RRF、Recency、Scope 隔离、
  forgotten/archived、Shield/Tombstone、UPDATE/DELETE/ROLLBACK、降级、AUTO 全链路）。
* PG 契约（带 bm25 索引）：`test_pg_contracts.py` → 10 passed。
* 迁移：`alembic upgrade head` 成功且 `alembic check` 无漂移；无 pg_search 的库上
  迁移打印 WARNING 并跳过索引。
* 静态检查：改动文件 ruff All checks passed；新模块 basedpyright 0 errors。

## 5. 遗留风险

1. 本机 `.env` 未改动：需手动加 `AIIVE_MEMORY_BM25_ENABLED=true` 并重建 postgres
   镜像才真正启用 BM25；否则走词法兜底（route=lexical，pack.degraded=true）。
2. BM25 索引为普通 CREATE INDEX（迁移事务内不能 CONCURRENTLY），大表需停机窗口。
3. 单字中文查询在 jieba 下不命中（两字以上正常）。
4. pg_search 版本与 PostgreSQL 主版本强绑定，升级需同步改 Dockerfile 变量。
