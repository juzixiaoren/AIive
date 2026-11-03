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

## 5. V60.1 部署与故障恢复修复

V60 上线后暴露出三处「只在真实部署故障下才显形」的问题，本次只做修复，不改检索架构。

### 5.1 BM25 SQL 失败会污染上层事务

* 现象：BM25 查询抛错后 PostgreSQL 事务进入 aborted，同一事务里随后的词法降级召回与
  ForgetVisibilityService 过滤全部抛 `InFailedSqlTransaction`，还可能回滚上层业务事务。
* 修复：`Bm25SearchService.search` 与所有可用性探测 / 索引维护语句都包进独立 SAVEPOINT
  （`db.begin_nested()`）；失败只回滚该 SAVEPOINT，置 status=error 并返回 None。
* 证据：`test_bm25_sql_failure_rolls_back_only_its_savepoint` 注入真实 SQL 故障（调用不存在
  的函数），断言降级为 lexical、`SELECT 1` 可执行、未提交写入仍在。把 SAVEPOINT 去掉后该
  用例失败（复现原始 bug），恢复后通过。

### 5.2 迁移已记录完成、索引却建不出来

* 现象：扩展未装时迁移 WARNING 跳过并仍被记为已执行，`alembic upgrade head` 不会重跑；
  扩展后装、索引被误删、索引结构被改坏都无法自愈。
* 修复：新增 `ensure_memory_bm25_index()`（幂等：扩展补装 / 索引补建 / 结构不兼容重建 /
  结构正确时零 DDL），应用启动时 `main._ensure_bm25_index()` 自动调用一次（失败不阻断
  启动），并提供 `python -m aiive.memory.bm25_maintenance status|ensure` 显式入口。
* 证据：`test_ensure_creates_missing_index`、`test_ensure_is_idempotent`（索引 OID 不变）、
  `test_ensure_rebuilds_incompatible_index`、`test_ensure_installs_extension_in_late_enabled_database`
  （新建库只有 pgvector，ensure 后扩展与 bm25 索引齐备）。

### 5.3 旧数据卷升级后 pg_search 加载不了

* 现象：镜像把 pg_search 写进 postgresql.conf.sample，只对 initdb 生成的新卷生效；已有
  PGDATA 的 postgresql.conf 没有这一行，`CREATE EXTENSION pg_search` 直接失败。
* 修复：新增 `deploy/pg/docker-entrypoint-bm25.sh` + `deploy/pg/ensure-preload.sh`，容器启动
  前幂等补齐 `shared_preload_libraries`（只追加配置、不动数据；保留已有预加载库；重复启动
  不再追加）。Dockerfile 接管 ENTRYPOINT，并对 .sh 做 CRLF 防御。
* 证据（真实容器）：用 `pgvector/pgvector:pg16` 初始化数据卷（预加载为空）→ 换新镜像启动 →
  日志 `aiive: 已更新 ... shared_preload_libraries = 'pg_search'`，
  `SHOW shared_preload_libraries` = pg_search，`CREATE EXTENSION pg_search` 成功；
  `docker restart` 后配置行数不变（幂等），数据卷未删除。

### 5.4 验证结果

* 全量：`pytest -q` → **1489 passed / 49 skipped / 0 failed**（PG、TEI 专项按环境跳过）。
* 真实 PG + pg_search：`AIIVE_PG_TESTS=1 pytest tests/integration/pg/test_pg_bm25_recall.py -q`
  → **26 passed**（原 21 + 新增 5）。
* 离线单测：`tests/unit/backend/test_bm25_index_maintenance.py` → **17 passed**（含真实执行
  `ensure-preload.sh` 的 5 个用例）。
* PG 契约：`test_pg_contracts.py` → 10 passed。
* 迁移：空库 `alembic upgrade head` 成功（含 c3f7b2a9d1e4），`alembic check` →
  「No new upgrade operations detected」。
* 维护命令：`status` / `ensure` 在真实库上输出 ready，索引定义与预期一致。
* 静态检查：改动文件 ruff All checks passed；basedpyright 0 errors。

## 6. 遗留风险

1. 本机 `.env` 未改动：需手动加 `AIIVE_MEMORY_BM25_ENABLED=true` 并重建 postgres
   镜像才真正启用 BM25；否则走词法兜底（route=lexical，pack.degraded=true）。
2. BM25 索引为普通 CREATE INDEX（迁移事务内不能 CONCURRENTLY），大表需停机窗口。
3. 单字中文查询在 jieba 下不命中（两字以上正常）。
4. pg_search 版本与 PostgreSQL 主版本强绑定，升级需同步改 Dockerfile 变量。
5. ensure 重建索引是普通 CREATE INDEX，结构不兼容时会在启动/维护命令执行期间短暂阻塞
   写入；存量极大时放到停机窗口执行。
6. 入口脚本只处理文件形式的 postgresql.conf；若改用 ALTER SYSTEM 或 `-c` 指定预加载库，
   脚本不覆盖，以运维配置为准（用 maintenance status 确认）。
