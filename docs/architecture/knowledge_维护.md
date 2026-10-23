# 知识库维护闭环

> 范围：文档导入 / 内容更新 / 删除 / 重索引 / 失败恢复，以及分块、原文对象与派生索引的一致性清理。
> 不涉及 Reranker、Retrieval Planner、Temporal Graph、Model Router。

## 1. 为什么需要维护闭环

知识库早期实现只有"导入"和"重建分块"两个动作，长期运行会暴露三类问题：

1. **同一文件改内容后，新旧两份文档同时存在**。`documents.content_hash` 唯一，内容变化必然新建一行，
   旧行与旧分块留在库里，检索时新旧内容一起被召回。
2. **删除没有语义**。没有删除入口，也没有"已删除"状态，用户无法撤回一条知识。
3. **派生数据没有归属**。分块、原文对象、对象存储里的残留文件没有对账机制，
   中断或异常会留下孤儿分块、孤儿对象，且没有任何入口能看见。

本轮补齐的是一套**可观测、可重复执行、可中断恢复**的维护闭环，而不是新知识系统。

## 2. 文档生命周期

`Document.status` 五态（`backend/aiive/db/models.py`，约束 `ck_documents_status`）：

| 状态 | 含义 | 参与检索 | 分块 | 原文对象 |
|---|---|---|---|---|
| `indexed` | 正常可用 | 是 | 有 | 保留 |
| `index_failed` | 分块构建失败，等待重试 | 是（保留上次可用分块） | 可能为空 | 保留 |
| `source_unavailable` | 原文对象已丢失 | 是（分块仍可用） | 有 | 已丢失 |
| `superseded` | 同路径被新版本取代 | **否** | **必须为空** | 保留（回滚窗口） |
| `deleted` | 用户删除（墓碑） | **否** | **必须为空** | 已释放 |

状态集合在 `knowledge/ingestor.py` 中集中定义，代码里不要再写裸字符串：

- `ACTIVE_STATUSES = {indexed, index_failed, source_unavailable}` —— 参与检索
- `RECOVERABLE_STATUSES = {indexed, index_failed}` —— 原文应可读，允许自动重建
- `INACTIVE_STATUSES = {superseded, deleted}` —— 永不参与检索，分块必须为空

`search_chunks` 的唯一过滤条件是 `Document.status NOT IN INACTIVE_STATUSES`。
这一条就保证了"更新/删除后不会被错误召回"，不需要额外清理检索索引。

## 3. 内容更新（版本取代）

`KnowledgeIngestor.ingest(path)` 的顺序：

1. 读取文件 → 提取文本 → `put_content_addressed` 落原文对象（内容寻址，天然幂等）。
2. 按 `content_hash` 查已有文档：
   - **命中**：说明内容与某条历史记录一致。若该记录处于失效态（被取代/已删除），复活它
     （`content_hash` 唯一，无法另建一行）；否则走去重分支。两种情况都会把同路径的其他活跃版本置为 `superseded`。
   - **未命中**：新建 `Document`，`flush` 拿到 id 后，把同 `source_path` 的其他活跃版本置为 `superseded`
     并写入 `superseded_by` 指向新文档。
3. `_replace_chunks` 重建分块。

`_supersede` 是唯一写 `superseded` 的地方，它同时**立即删除旧分块**。
因此"版本切换"与"旧内容不可召回"在同一次事务内完成，不存在新旧并存的窗口。

写序约束（配合部分唯一索引 `ix_documents_active_source_path`，见 §9）：
`ingest` 必须先置失效同路径的其他活跃版本并 flush，再写入新文档或复活历史记录；
新文档 id 在构造时预生成，使旧版本的 `superseded_by` 在置失效时即可指向新文档。

版本链通过 `documents.superseded_by` 保留，可用于审计，但当前不提供"回滚到旧版本"的 API
（原文对象仍在，属于后续可选能力）。

## 4. 删除与所有权边界

`DELETE /api/knowledge/{document_id}` → `KnowledgeIngestor.delete`：

- 删除该文档的全部分块（派生数据，可由原文重建）；
- 释放原文对象：仅当没有其他文档引用同一 `(bucket, key)` 时执行；
- `status='deleted'`、写 `deleted_at`、保留墓碑行（可审计、可幂等重复删除）；
- **绝不删除 `source_path` 指向的用户原始文件**，响应里显式返回 `source_file_removed: false`。

对象释放是 best-effort：失败只记 warning，文档仍然进入 `deleted`。
遗留的对象会被维护任务按"无任何文档引用"识别为孤儿对象回收 —— 这是崩溃恢复路径，
而不是失败兜底逻辑。

重复删除返回 200 + `already_deleted: true`（幂等）；不存在的 id 返回 404。

## 5. 重索引与失败恢复

`reindex(document_id, mark_failure=True)`：

1. 失效态文档直接拒绝（`ValueError` → 409），避免把旧版本内容复活；
2. 从对象存储 `get_verified` 读原文（SHA-256 校验，损坏即失败）；
3. 重新提取并整体替换分块；
4. 失败时用**独立短会话**写 `index_failed`（外层事务回滚不会抹掉这个事实）。

维护任务调用时传 `mark_failure=False`：它在自己的 SAVEPOINT 里调用 `reindex`，
回滚该篇后由维护任务写 `index_failed`，避免再开第二个会话（单篇失败不影响整批）。

失败不会被标记成成功：`index_failed` 文档保留在报告里，下一轮维护会再试一次。

## 6. 维护任务

`knowledge/maintenance.py` 分两步，职责分离：

### 6.1 `scan_knowledge_base(db)` —— 只读

返回 `KnowledgeMaintenanceReport`：状态计数、分块总数、7 类问题、最近一次维护完成时间。

| issue code | 含义 | 修复动作 |
|---|---|---|
| `orphan_chunks` | 分块没有所属文档 | 删除 |
| `duplicate_chunks` | 同 `(document_id, chunk_index)` 多条 | 保留最早一条 |
| `stale_document_chunks` | 失效文档残留分块 | 删除 |
| `missing_chunks` | 可修复文档没有分块 | 从原文重建 |
| `missing_object` | 原文对象文件不存在 | 标记 `source_unavailable` |
| `orphan_objects` | 桶内对象无人引用 | 移入回收站 |
| `expired_superseded_objects` | 超过保留期的旧版本原文 | 释放对象并清空引用 |

扫描本身不写库，可以随时调用（`GET /api/knowledge/maintenance`）。

### 6.2 `apply_report(db, report, limit)` —— 幂等清理

按上表逐类执行，返回动作计数。关键约束：

- 每类最多处理 `limit` 条，可中断、可续跑；
- `documents_rebuild_failed` **不计入"有进展"**：否则持续失败的文档会让任务无限空转；
- 调用方负责 `commit`，单批失败只回滚该批。

对象类清理有严格范围限制：只处理 `sha256/xx/<64 位十六进制>` 形态的内容寻址键，
跳过隐藏目录（`.trash` 等）与临时文件，避免误删其他布局的数据。
同时只回收写入超过 `MIN_ORPHAN_OBJECT_AGE_SECONDS`（300 秒）的对象：
并发摄取"已写对象、尚未提交"的窗口里，文件看起来就是孤儿，加年龄门槛可避免误删。

## 7. 调度与 Outbox 接入

复用现有 Outbox / APScheduler，不新建任务框架：

- job_type：`knowledge_maintenance`（`worker/job_types.py::KNOWLEDGE_JOB_TYPES` 并入 allowlist）
- handler：`worker/handlers_knowledge.py::handle_knowledge_maintenance`
  - 最多 `MAX_MAINTENANCE_BATCHES = 5` 轮"扫描→清理"；未清完返回 `CONTINUE`
  - 扫描已健康 → `COMPLETED`
  - 一轮下来没有任何进展 → `COMPLETED` + `terminal_reason=knowledge_maintenance_partial`（保留问题，交给下一次）
- 调度：`scheduler_daemon._knowledge_maintenance_scanner_job`（每小时）
  - 窗口内已有 pending/running 任务（含 `#n` 补跑）→ 跳过，避免重复执行
  - DB 侧存在不一致（失效分块 / 孤儿分块 / 缺失分块 / `index_failed`）→ 立即入队
  - 否则每天至少入队一次，用于对象级对账（孤儿对象、原文丢失）
  - `operation_id = knowledge_maintenance:all:1:{yyyy-mm-dd}`：窗口内首个任务使用基础
    id，UNIQUE 保证同窗口幂等；基础 id 已被终态任务（completed / deadletter）占用时，
    补跑任务分配 `#2`、`#3`… 后缀，保证"当天新出现的不一致仍能即时入队"，
    不会被当天早些时候已结束的任务堵住（终态任务不阻塞在途判定只认 pending/running）

## 8. API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/knowledge/ingest` | 导入；同路径内容变化即版本取代 |
| GET | `/api/knowledge` | 文档列表（含 status / superseded_by / 分块数 / 时间戳）；默认只列活跃文档，`include_inactive=true` 才含墓碑 |
| GET | `/api/knowledge/maintenance` | 维护报告（只读扫描） |
| POST | `/api/knowledge/maintenance` | `{apply: bool, limit: int}`；apply=true 时执行清理并返回前后报告 |
| GET | `/api/knowledge/{id}/source` | 读取校验过的原文 |
| POST | `/api/knowledge/{id}/reindex` | 重建分块（失效文档 409） |
| DELETE | `/api/knowledge/{id}` | 软删除，幂等 |
| GET | `/api/knowledge/search`、`/api/search` | 分块检索（只返回活跃文档） |

## 9. 一致性与并发

- **数据库是唯一事实来源**：分块与状态在同一事务内变更；原文对象释放是事务外的 best-effort，
  失败由维护任务兜底。
- **重复分块由 schema 保证**：`chunks(document_id, chunk_index)` 唯一约束（迁移
  `c7e3a1f5b9d2`，迁移前先清理历史重复行）。
- **并发导入同一路径**：`documents` 上的部分唯一索引
  `ix_documents_active_source_path`（`WHERE status IN ('indexed','index_failed',
  'source_unavailable')`）从 schema 层面保证同一 `source_path` 同时只有一条活跃文档。
  两个事务并发导入同路径时，后插入方阻塞在该索引上，先提交方落库后收到
  `IntegrityError`（API 映射 409，客户端重试即可收敛，`ingest` 幂等）；
  内容相同的并发则由 `content_hash` UNIQUE 拦截。失效态不占用唯一位，
  版本切换与历史版本复活不受影响。ingest 写序因此固定为
  "先置失效旧版本并 flush，再写入/激活新版本"。
- **中断恢复**：摄取在"写对象后、写分块前"崩溃 → 对象成为孤儿（维护回收）或文档缺分块（维护重建）；
  删除在"删分块后、删对象前"崩溃 → 对象成为孤儿（维护回收）。

## 10. 测试

`tests/unit/backend/test_knowledge_maintenance.py`（35 例，全离线）：

- 文档生命周期：版本取代、内容回退时复活旧记录、重复导入幂等、删除不误删原文件、
  重复删除、失效文档拒绝重索引、分块唯一约束
- 一致性扫描：健康基线、孤儿分块、失效分块、缺失分块重建、原文损坏保留 `index_failed`、
  原文丢失标记 `source_unavailable`、孤儿对象回收、新写入对象不被回收（防并发误删）、
  过期旧版本对象回收
- Handler：allowlist、缺 `operation_id`、健康直接完成、修复可重复、无进展时停止空转
- 调度：同窗口入队幂等、终态任务后分配 `#n` 补跑（同日新问题不被堵住）、
  在途任务不重复入队、`knowledge_needs_maintenance` 判定
- 路由：导入 / 报告 / 清理 / 删除 / 404 / 列表默认过滤墓碑 /
  原文损坏与丢失返回 409 / 并发导入冲突返回 409 / OpenAPI schema 可构建
- source_path 活跃唯一性：同路径第二条活跃文档被 schema 拦截、失效态不占活跃位

迁移契约在 `tests/unit/backend/test_schema_migrations.py`：
`test_knowledge_lifecycle_migration_dedups_and_roundtrips` 覆盖历史重复分块清理
（保留最早一条）、新列/索引/唯一约束/状态约束、`updated_at` 回填非空，
以及 downgrade → upgrade 往返；
`test_active_source_path_unique_migration_dedups_and_roundtrips` 覆盖历史重复活跃
文档去重（保留最新一条、旧版分块清空）、部分唯一索引生效与降级移除。

PostgreSQL 并发语义在 `tests/integration/pg/test_pg_contracts.py`（需
`AIIVE_PG_TESTS=1`）：部分唯一索引对第二条活跃文档的拦截（阻塞后 IntegrityError）、
并发导入同路径最终只剩一条活跃文档。

对象存储根目录在测试中被隔离到 `tmp_path`，不需要网络或 API Key。

## 11. 已知边界

- **不提供"回滚到旧版本"**：`superseded` 记录保留原文对象 30 天
  （`SUPERSEDED_OBJECT_RETENTION_DAYS`），但重建旧版本需要手动改状态，没有 API。
- **不做原文哈希全量巡检**：对象完整性在读取路径（`get_verified`）校验，
  扫描只做"文件是否存在"，避免每次维护都全量读盘。
- **对象级问题每天对账一次**：孤儿对象/原文丢失不会触发即时入队（需要遍历文件系统，代价高）。
- **墓碑行不自动清理**：`deleted` 文档行保留用于审计，体量很小。
- **检索索引不含知识库**：`retrieval/index_rebuild.py` 的 lexical 索引只覆盖
  memory_record / segment_summary / epoch_checkpoint，知识库检索走 `search_chunks`，
  因此本轮"派生索引"等价于 `chunks` 表。
