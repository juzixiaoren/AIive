# V46 交付报告：轻量 Embedding 接入与真实混合检索 RAG

> 日期：2026-10-26　范围：长期记忆 RAG、知识库混合检索、检索效果评测、本地 2 GiB 内存实测
> 结论：真实模型 + 真实 PostgreSQL/pgvector + 完整 RAG 链路已在**单 cgroup 2 GiB、关闭 swap** 下通过验证。

## 0. 结论摘要

| 项 | 结果 |
|---|---|
| 模型 | `BAAI/bge-small-zh-v1.5` @ `534b6bfaaf500e70bcb9f3771cebc940c23b219d`（512 维，CLS pooling，L2 归一化，TEI `cpu-1.9` 托管） |
| 记忆 RAG | 写入 → Outbox 投影 → pgvector 召回 → 统一检索 → 上下文装配，全链路真实跑通（含归档/遗忘删除派生向量、scope 隔离、历史回填） |
| 知识库 RAG | 新增 `knowledge_chunk_vectors` 派生投影 + 关键词/向量 RRF 混合召回；删除/版本取代/重建/维护回填全部覆盖 |
| 检索效果（27 条标注查询，Top-5） | 纯关键词 Recall@5 0.550 / MRR@5 0.550；纯向量 1.000 / 0.8875；**混合 1.000 / 0.9500**；负例通过率三模式均 100% |
| 2 GiB 实测（基准负载） | 峰值 **629.0 MiB**，余量 1419.0 MiB，OOM 0，swap 0 |
| 2 GiB 实测（压力负载） | 峰值 **525.4 MiB**，余量 1522.6 MiB，OOM 0，swap 0 |
| 回归 | 离线全量 1248 passed / 23 skipped（skip 全为 PG 专项）；真实 PG+TEI 专项 13 passed；`alembic check` 无漂移 |

---

## 1. 最终模型、版本与实测内存

### 1.1 选型

| 维度 | 取值 | 说明 |
|---|---|---|
| 模型 | `BAAI/bge-small-zh-v1.5` | 中文短文本检索强、CPU 推理快、体积小 |
| Revision | `534b6bfaaf500e70bcb9f3771cebc940c23b219d` | 固定提交，写入 `embedding_model` 身份，避免换版本后新旧向量混用 |
| 维度 | 512 | 与既有 `memory_vector_projections` schema 一致，**无需改列宽**；知识库新表沿用同一维度 |
| Pooling / 归一化 | CLS + L2（实测范数 1.000000） | TEI `/info` 报告 `pooling: cls`，实测向量单位长度 |
| 查询前缀 | `为这个句子生成表示以用于检索相关文章：` | BGE 非对称检索要求，仅用于 query 侧 |
| 部署形态 | 独立 TEI 服务（`ghcr.io/huggingface/text-embeddings-inference:cpu-1.9`） | 模型只在 TEI 进程加载一次；后端进程**不加载模型**，多 worker 不会重复占内存 |
| 模型缓存 | `./.data/models/embeddings`（持久卷） | 正常启动不重复下载；实测容器启动到 TEI 就绪 **7s** |

选择理由：现有 schema 已是 512 维、`docker-compose.yml` 已内置 TEI profile、配置项（`AIIVE_EMBEDDING_*`）已存在。因此本 Goal 走"接线 + 补缺"，而不是引入新推理栈；也避免了 ONNX 自建服务带来的额外内存与依赖风险。**实测不需要 INT8 ONNX 备选方案**（见第 4 节）。

### 1.2 实测内存（单进程 RSS，取自 2 GiB 容器内）

| 进程 | 基准负载 | 压力负载 | 说明 |
|---|---|---|---|
| `postgres` | 209.4 MiB | 279.1 MiB | shared_buffers 128MB、max_connections 40 |
| `uvicorn`（backend，单 worker） | 175.1 MiB | 176.8 MiB | 不加载模型 |
| `text-embeddings-router` | 149.9 MiB | 153.6 MiB | 模型常驻，压力负载仅 +3.7 MiB |
| 负载客户端 `python` | 102.2 MiB | 114.0 MiB | **仅实测时存在**，非生产进程 |
| `supervisord` | 29.7 MiB | 30.0 MiB | 进程托管 |

---

## 2. 修改文件与真实调用链

### 2.1 新增

| 文件 | 作用 |
|---|---|
| `backend/aiive/knowledge/chunk_vectors.py` | `ChunkVectorService`：批量生成/刷新/删除分块向量、余弦检索、按文档批次回填；`missing_vector_document_ids`/`orphan_vector_count`/`purge_orphan_vectors` 供维护使用 |
| `backend/alembic/versions/d5c1a7b9e4f2_add_knowledge_chunk_vectors.py` | 新建 `knowledge_chunk_vectors`（512 维、HNSW + `vector_cosine_ops`、FK 级联） |
| `scripts/rag_eval/corpus.json` / `queries.json` / `run_eval.py` | 12 篇语料 + 2 篇失效语料、27 条人工标注查询、三模式评测器 |
| `scripts/memtest/`（Dockerfile / supervisord.conf / entrypoint.sh / start_backend.sh / load_test.py / run_memtest.sh） | 单 cgroup 2 GiB 实测环境与负载脚本 |
| `tests/integration/pg/test_pg_vector_rag.py` | 真实 PG+TEI 的 13 个链路用例 |

### 2.2 修改

| 文件 | 改动 |
|---|---|
| `backend/aiive/memory/embedding_provider.py` | 新增 `embed_batch`（按 `aiive_embedding_batch_size` 分批、按 `index` 还原顺序、逐条校验维度）；单条 `embed` 的请求载荷保持原样（字符串 input） |
| `backend/aiive/config.py` | 新增 `aiive_embedding_batch_size`、`aiive_knowledge_vector_enabled`、`aiive_knowledge_vector_top_k`、`aiive_knowledge_vector_min_similarity`、`aiive_knowledge_vector_backfill_batch` 及校验 |
| `backend/aiive/db/models.py` | 新增 `KnowledgeChunkVector`（派生投影，SQLite 下退化为 JSON 列） |
| `backend/aiive/knowledge/ingestor.py` | `_replace_chunks`/`purge_chunks` 接入向量刷新与清理（保存点隔离、失败只告警）；`search_chunks` 改为词法 + 向量 RRF 融合（各取 4×TopK 候选后截断），向量故障自动退化 |
| `backend/aiive/knowledge/maintenance.py` | 新增 `missing_chunk_vectors` / `orphan_chunk_vectors` 扫描与幂等修复（回填 + 清理），并计入 `changed` 判据 |
| `backend/aiive/api/routes_knowledge.py` | 检索端点文档更新（混合召回、可降级） |
| `.env.example` / `.env` / `README.md` / `docs/CODE_MAP.md` / `docs/guides/技术原理与选型详解.md` | 配置项、部署说明、内存口径、代码图谱与"已知短板"同步 |

### 2.3 真实调用链

**记忆写入（异步投影）**
```
MemoryWriteService / MemoryMutationExecutor
  → outbox_jobs(job_type=memory_vector_refresh)
  → worker/outbox_handlers.handle_memory_vector_refresh
  → MemoryVectorProjectionService.refresh
  → OpenAIEmbeddingProvider.embed(input_type=document)  [TEI: /v1/embeddings]
  → memory_vector_projections(embedding vector(512), embedding_model=model@revision)
```

**记忆召回（每轮进入 Agent 上下文）**
```
ContextAssembler._unified_recall
  → UnifiedRetriever.retrieve(mode=auto)
  → AutomaticRecallEngine.recall
      ├ exact / lexical / recent_episode
      └ vector: MemoryVectorProjectionService.search（SQL 过滤 scope + lifecycle + validity + 模型身份，再经遗忘可见性复核）
  → recall_fusion.fuse_and_pack（RRF + relevance/scope/recency/importance/trust）
  → retrieved_memory 分区 → messages
```

**知识库导入与检索**
```
KnowledgeIngestor.ingest / reindex
  → chunk_text（500 字符按行分块）
  → _refresh_chunk_vectors（保存点内，失败降级）
      → OpenAIEmbeddingProvider.embed_batch(batch_size=4)
      → knowledge_chunk_vectors（FK chunk_id → chunks.id ON DELETE CASCADE）

search_chunks(query, limit)                      ← /api/knowledge/search、search_knowledge 工具
  ├ _lexical_chunk_hits(depth=4×K)   ILIKE 多 token + 覆盖度排序
  ├ _vector_chunk_hits(depth=4×K)    cosine 距离 + 相似度下限 + 模型身份过滤
  └ _fuse_chunk_hits                 RRF(k=60, 权重各 0.5) 去重 → TopK
```

**清理与回填**
- 文档更新（同路径新内容）：旧版本 `superseded` → `purge_chunks` 先删向量再删分块；
- 删除文档：`purge_chunks` 清向量，墓碑记录保留；
- 重建（reindex）：先清后建，向量按新分块重算（内容与模型身份未变则跳过重算）；
- 维护任务：`missing_chunk_vectors` 按 `AIIVE_KNOWLEDGE_VECTOR_BACKFILL_BATCH`（默认 200 篇/轮）回填，`orphan_chunk_vectors` 幂等清理。

---

## 3. 检索效果对比（真实模型 + 真实 pgvector）

数据：12 篇活跃文档 + 2 篇失效文档（1 篇 superseded、1 篇 deleted），13 个分块、13 个向量。
查询：27 条人工标注（语义改写 5、精确术语 6、中英混合 4、相似实体 5、无关 4、权限过滤 3）。
条件：同一份数据、同一过滤（只检索活跃文档）、同一 Top-5。

| 模式 | Recall@5 | MRR@5 | 负例通过率 | 平均延迟 | P95 延迟 |
|---|---|---|---|---|---|
| 纯关键词 | 0.5500 | 0.5500 | 1.0000 | 3.03 ms | 5.38 ms |
| 纯向量 | 1.0000 | 0.8875 | 1.0000 | 96.78 ms | 118.61 ms |
| 混合 | 1.0000 | **0.9500** | 1.0000 | 110.79 ms | 131.34 ms |

分类别（Recall@5 / 负例通过率）：

| 类别 | 关键词 | 向量 | 混合 |
|---|---|---|---|
| semantic（语义改写） | 0.000 | 1.000 | 1.000 |
| exact_term（精确术语） | 1.000 | 1.000 | 1.000 |
| mixed（中英混合） | 1.000 | 1.000 | 1.000 |
| similar（相似实体） | 0.200 | 1.000 | 1.000 |
| unrelated（无关查询，负例） | 1.000 | 1.000 | 1.000 |
| permission（失效文档，负例） | 1.000 | 1.000 | 1.000 |

**结论与解读（不预设混合更好）**
- 纯关键词在"问法与文档用词不一致"时完全失效（语义改写 0.000、相似实体 0.200），这是中文无空格 + 子串匹配的固有短板。
- 纯向量的 Recall@5 已经满分，但 **MRR 低于混合**：可混淆的精确标识符（如 `AIIVE_KNOWLEDGE_VECTOR_MIN_SIMILARITY` vs 其他 `AIIVE_*` 变量）会被语义近邻排到第 4，词法把它拉回第 1（见下表 t6）。这正是 RRF 融合的价值，也是"候选深度 4×TopK"必须存在的原因。
- 混合的代价是延迟：比纯向量多约 14 ms（多跑一路词法 + 融合），相对 100 ms 级的向量推理可接受。
- 负例（无关查询、已删除/已取代文档）三模式均 100% 通过：相似度下限 0.30 挡住了无关语义近邻，失效文档因分块与向量都被清理而不可召回。

典型查询 Top-5（混合）：

| 查询 | 关键词 | 向量 | 混合 |
|---|---|---|---|
| 上线前要做哪些验证 | [] | ci-pipeline, **deploy-rollout**, ops-backup… | 同向量（词法无命中） |
| `AIIVE_KNOWLEDGE_VECTOR_MIN_SIMILARITY` 的作用 | **knowledge-vectors** | knowledge-vectors(4) | **knowledge-vectors(1)** |
| `memory.current` 和 `memory.peak` 在哪里看 | **ops-monitoring** | ops-monitoring(2) | **ops-monitoring(1)** |
| 知识库文档删除之后旧分块怎么处理 | [] | **knowledge-chunks** | 同向量 |
| 旧报销政策要求纸质发票原件吗 | [] | 无失效文档 | 无失效文档 |

---

## 4. 本地 2 GiB 内存实测

### 4.1 环境与方法

- **不访问任何目标服务器**，全部在开发者本机完成。
- 用 Docker Desktop 的 Linux VM 承载一个**单容器**，容器内同时运行 PostgreSQL 16 + pgvector、TEI（Embedding）、Backend（uvicorn 单 worker）与 supervisord。
- 只有把三件事放进同一个 cgroup，`--memory=2g` 才是**整套服务**的上限（而不是"每个容器各 2 GiB"）。容器内 `/sys/fs/cgroup` 即该 cgroup 根，读到的 `memory.current` / `memory.peak` / `memory.events` 就是整套服务的真实用量。
- `--memory-swap=2g`（= memory），即**关闭容器 swap**；实测 `memory.swap.max=0`、`memory.swap.current=0`。
- 模型来自宿主机持久缓存 `.data/models/embeddings`（不重新下载）。
- 复现：`bash scripts/memtest/run_memtest.sh`（压力场景见 4.4）。

### 4.2 场景与结果

| 场景 | 数据规模 | 并发 | 查询数 | 平均延迟 | P95 | 峰值内存 | 余量 | OOM | Swap |
|---|---|---|---|---|---|---|---|---|---|
| 基准（TEI 并发 4 / batch 2048） | 14 篇文档、13 分块、30 条记忆 | 4 | 108 | 173.94 ms | 225.82 ms | **629.0 MiB** | **1419.0 MiB** | 0 | 0 |
| 压力（TEI 并发 32 / batch 16384） | 112 篇文档、104 分块、120 条记忆 | 8 | 216 | 419.79 ms | 609.06 ms | **525.4 MiB** | **1522.6 MiB** | 0 | 0 |

两轮峰值差异来自**页缓存**（`file` 217.3 MiB vs 76.6 MiB）：匿名内存（真实进程占用）分别是 388.1 MiB 与 407.4 MiB，都很稳定。页缓存可回收，不构成 OOM 风险。

分阶段峰值（基准 / 压力，MiB）：启动 576.2 / 433.9 → 空闲 579.7 / 434.5 → 知识重建+记忆写入 608.7 / 478.3 → 检索 628.6 / 523.5。

启动耗时：TEI 就绪 7s、backend 就绪 10s（含 `alembic upgrade head`）。首次推理（预热查询）约 102 ms。

### 4.3 cgroup 证据（基准场景原文）

```
== memory.max ==        2147483648        (2 GiB)
== memory.peak ==       659550208         (629.0 MiB)
== memory.swap.max ==   0
== memory.swap.current == 0
== memory.events ==
low 0   high 0   max 0   oom 0   oom_kill 0   oom_group_kill 0
== memory.stat ==
anon 388.1 MiB   file 217.3 MiB
```

压力场景：`memory.peak=551677952`（525.4 MiB）、`oom_kill 0`、`memory.swap.current=0`。

### 4.4 验收对照

| 验收标准 | 结果 |
|---|---|
| 完整服务在 2 GiB 限制下启动并完成真实检索 | ✅ 两轮均完成（含模型加载、迁移、真实向量检索） |
| 无 OOM、无异常重启、无 swap 依赖 | ✅ `oom/oom_kill/max` 全 0，`swap.current/peak` 全 0，`health` 前后均 200 |
| 峰值至少保留约 200 MiB | ✅ 余量 1419 / 1522 MiB |
| TEI 不达标时先优化再验证 INT8 ONNX | 未触发：TEI 方案余量充足，不需要 INT8 备选 |

压力场景复现命令：

```bash
SKIP_BUILD=1 TEI_MAX_CONCURRENCY=32 TEI_MAX_BATCH_TOKENS=16384 \
MEMTEST_DOC_VARIANTS=8 MEMTEST_MEMORIES=120 \
MEMTEST_QUERY_ROUNDS=8 MEMTEST_CONCURRENCY=8 \
bash scripts/memtest/run_memtest.sh
```

---

## 5. 测试与命令

| 命令 | 结果 |
|---|---|
| `.venv/Scripts/python.exe -m pytest tests -x -q` | **1248 passed, 23 skipped**（23 个 skip 均为 PG 专项，默认离线跳过） |
| `AIIVE_PG_TESTS=1 AIIVE_PG_TEST_URL=.../aiive_pgtest pytest tests/integration/pg/test_pg_vector_rag.py -q` | **13 passed**（真实 PG + 真实 TEI） |
| `.venv/Scripts/python.exe scripts/rag_eval/run_eval.py` | 三模式指标（第 3 节），结果写入 `scripts/rag_eval/last_run.json` |
| `bash scripts/memtest/run_memtest.sh` | 2 GiB 实测（第 4 节），证据写入 `.data/memtest/` |
| `.venv/Scripts/python.exe -m alembic check` | `No new upgrade operations detected.` |
| `basedpyright`（改动文件） | `0 errors, 0 warnings, 0 notes` |
| `ruff check`（改动文件） | 无新增告警：12 条均为改动前既有（`routes_knowledge` 9×B008 + 1×I001，`ingestor` 1×I001 + 1×UP017），与 `HEAD` 版本逐条一致 |

真实 PG 用例覆盖矩阵（`test_pg_vector_rag.py`）：

| 用例 | 覆盖点 |
|---|---|
| `test_ingest_persists_real_vectors_for_every_chunk` | 每个分块都有真实向量；库侧 `vector_dims=512`；模型身份；单位范数 |
| `test_semantic_query_recalls_chunk_without_lexical_overlap` | 无字面重叠时纯词法为空、混合召回目标 |
| `test_hybrid_keeps_exact_term_ranking` | 精确术语仍排第一，且带 `hybrid_score` |
| `test_vectors_from_other_model_are_never_used` | 异模型/异版本向量不参与检索 |
| `test_document_update_purges_superseded_vectors` | 版本取代后旧分块与旧向量都不可召回 |
| `test_delete_purges_vectors_and_blocks_recall` | 删除后向量清空、任何通道都不召回 |
| `test_reindex_rebuilds_vectors_idempotently` | 重建补齐向量且无重复投影 |
| `test_embedding_outage_degrades_to_lexical_without_corruption` | 模型服务不可用时摄取仍成功、检索退化为关键词 |
| `test_maintenance_reports_missing_vectors_and_backfills` | 缺失扫描 + 批次回填 + 复查归零 |
| `test_memory_vector_recall_reaches_agent_context` | Outbox 投影 → 向量召回 → `UnifiedRetriever` 命中进入上下文 |
| `test_memory_archive_and_forget_remove_derived_vectors` | 归档/遗忘删除派生向量，且不再召回 |
| `test_memory_backfill_bootstraps_only_missing_projections` | 历史回填只入队缺失项、幂等、归档不入队 |
| `test_memory_vector_query_respects_scope` | 向量召回沿用 scope 约束 |

---

## 6. 遗留风险

1. **2 GiB 环境是 Docker Desktop 的 Linux VM + cgroup v2，不是独立 Linux 虚拟机**。内存限制、swap 关闭、cgroup 计数都是真实的（`memory.max`/`peak`/`events` 原文见 4.3），但宿主 VM 的 CPU 调度与真机不同；生产部署仍需在目标机复核一次。
2. **知识库没有用户级作用域**：当前只按文档生命周期（active/superseded/deleted）过滤。单用户产品够用，多租户场景需要补 scope 列与过滤。
3. **无 Reranker**：混合检索靠 RRF，未做精排；对长文档、近义实体密集的场景仍有提升空间（本 Goal 明确不做）。
4. **HNSW 索引维护**：向量表增长后需要关注索引重建成本与 `maintenance_work_mem`；当前规模（百级分块）无感。
5. **TEI 单点**：Embedding 服务不可用时降级为关键词检索（已验证），但没有第二模型兜底，也没有队列重试补齐失败向量（依赖维护任务回填）。
6. **记忆历史回填一次性入队**：`ensure_memory_vector_backfill` 仍按现有设计把缺失项全部入队，限流靠 Outbox worker 并发与单条任务粒度；超大记忆库首次开启时需要观察 worker 压力。
7. **评测集规模小**（27 条查询、12 篇文档），结论只对本仓库语料有效；不做大型 Benchmark 是本 Goal 的约束，不能外推为通用 SOTA 结论。
8. **相似度下限 0.30 是经验值**，按本地 bge-small-zh 的分数分布标定；换模型必须重新标定（模型身份变化本身会让旧向量失效，不会静默混用）。

---

## 7. 复现步骤

```bash
# 1. 启动真实依赖（PostgreSQL/pgvector + 本地 TEI）
docker compose --profile local-embedding up -d postgres embedding
# 2. 迁移
.venv/Scripts/python.exe -m alembic upgrade head
# 3. 真实链路测试（需提供独立测试库，避免污染开发库）
AIIVE_PG_TESTS=1 AIIVE_PG_TEST_URL='postgresql+psycopg://aiive:aiive_dev@127.0.0.1:5432/aiive_pgtest' \
  .venv/Scripts/python.exe -m pytest tests/integration/pg/test_pg_vector_rag.py -q
# 4. 检索效果评测
.venv/Scripts/python.exe scripts/rag_eval/run_eval.py
# 5. 2 GiB 内存实测（自动建镜像 + 单 cgroup 容器）
bash scripts/memtest/run_memtest.sh
```
