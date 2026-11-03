# V61 知识库结构感知递归切分（交付报告）

日期：2026-11-03
范围：文档解析 → Block → Chunk → Embedding → 向量索引 → 检索上下文，端到端升级。
不涉及：Parent-Child 切块、LLM 切块、摘要、BM25 / Memory Recall 重构。

## 1. 背景

原实现用 `chunk_text()` 按 500 字符窗口切分纯文本：

* 标题、列表、表格、代码块全部退化为普通文本，Chunk 丢失结构；
* 章节被字符窗口随机截断，检索命中后无法说明"来自哪一节、哪一页"；
* `content_hash` 只哈希正文、`embedding_model` 只比较模型名，
  切分规则或 Embedding 模板升级后旧向量不会失效，库里新旧分块混用；
* `len(text) // 4` 估算 token，中文严重低估，700 token 上限形同虚设。

## 2. 交付清单

### 新增

| 文件 | 作用 |
|---|---|
| `backend/aiive/knowledge/tokenization.py` | 确定性 token 计数（CJK 1 字 1 token，非 CJK 每 4 字符 1 token） |
| `backend/aiive/knowledge/blocks.py` | 结构化 Block 解析（Markdown/DOCX/PDF/HTML/CSV/JSON/源码/纯文本） |
| `backend/aiive/knowledge/embedding_input.py` | Embedding 输入模板（文档/章节/正文）与输入哈希 |
| `backend/alembic/versions/b8d2e6f4a1c7_knowledge_chunk_structure_metadata.py` | 结构元数据迁移（revises `c3f7b2a9d1e4`） |
| `docs/architecture/knowledge_structure_chunking.md` | 架构说明 |
| `tests/unit/backend/test_structure_chunking.py` | 33 例：解析 / 切分 / Embedding / 版本重建 / 邻居扩展 |
| `tests/unit/backend/test_structure_chunking_migration.py` | 2 例：空库升级、存量库补列与降级回滚 |
| `tests/integration/pg/test_pg_structure_chunking.py` | 4 例：真实 PG + pgvector 契约 |

### 修改

| 文件 | 变更 |
|---|---|
| `knowledge/chunker.py` | 重写为 `chunk_blocks()` 递归切分（`chunk_text` 保留为遗留接口） |
| `knowledge/ingestor.py` | 解析→切分→结构元数据落库；切分版本落后自动重建；命中带结构定位；邻居扩展 |
| `knowledge/chunk_vectors.py` | 新鲜度改为「输入哈希 + 模板版本 + 模型身份」，Embedding 输入改用标题上下文 |
| `knowledge/maintenance.py` | 新增 `stale_chunking` 问题码；重建合并「缺失分块」与「版本落后」 |
| `db/models.py` | Chunk 9 个结构列、Document 2 个版本列、KnowledgeChunkVector `input_version` |
| `config.py` / `.env.example` | 邻居扩展三项配置 |
| `docs/CODE_MAP.md`、`docs/architecture/knowledge_维护.md`、`README.md` | 图谱与说明同步 |

## 3. 调用链

```
ingest(path)
  └─ put_content_addressed → 持久原文（对象存储，内容寻址）
  └─ _replace_chunks(document, data, filename)
       ├─ purge_chunk_vectors + 删除旧分块（不新旧混合）
       ├─ parse_document_blocks(data, filename)   → list[Block]
       ├─ chunk_blocks(blocks)                    → list[ChunkDraft]
       ├─ 逐 Chunk 写入结构元数据 + embedding_input_hash
       ├─ 回写 document.parser_version / chunker_version
       └─ _refresh_chunk_vectors → ChunkVectorService.refresh_document

search_chunks(query)
  └─ _lexical_chunk_hits + _vector_chunk_hits → _fuse_chunk_hits（RRF k=60）
       └─ _expand_neighbor_hits（同 Section ±N、去重、token 预算）
```

`reindex(document_id)` 与维护重建都先 `read_source()` 取持久原文，再走同一
`_replace_chunks`，因此"重新导入 / 手工重索引 / 规则升级重建"完全同源。

## 4. Chunk 示例（实测输出）

输入 `notes.md`：

````markdown
# Redis 技术笔记

Redis 是一个内存数据库。

## 持久化

通过记录写命令实现持久化。

### AOF

AOF 追加写命令到文件。

- 先写缓冲区
- 再刷盘
  - 子项说明

| 模式 | 优点 | 缺点 |
|---|---|---|
| RDB | 文件小 | 可能丢数据 |

```python
def append(command):
    return command
```
````

切分结果：

| # | block_type | heading_path | section_id | lines | tokens |
|---|---|---|---|---|---|
| 0 | paragraph | `Redis 技术笔记` | `s0b5487049f04a038` | 2-2 | 11 |
| 1 | paragraph | `Redis 技术笔记 > 持久化` | `s7799f78d4a0905c4` | 6-6 | 13 |
| 2 | mixed | `Redis 技术笔记 > 持久化 > AOF` | `sb5e0b35ddaebd2f1` | 10-14 | 25 |
| 3 | table | `Redis 技术笔记 > 持久化 > AOF` | `sb5e0b35ddaebd2f1` | 16-18 | 27 |
| 4 | code | `Redis 技术笔记 > 持久化 > AOF` | `sb5e0b35ddaebd2f1` | 21-22 | 9 |

* 标题只进入 `heading_path`，不产生只有标题的空 Chunk；
* 列表保留缩进子项；表格与代码各自成块（原子块，不参与普通 Overlap）；
* `section_id` 由标题路径派生，重建后不变。

## 5. Embedding 输入

Chunk 2 的原文 `AOF 追加写命令到文件。…` 对应的 Embedding 输入：

```
文档：notes.md
章节：Redis 技术笔记 > 持久化
正文：通过记录写命令实现持久化。
```

* 展示与关键词检索仍用 `chunks.content`，标题前缀不会出现在给用户的内容里；
* `chunks.embedding_input_hash = sha256(完整 Embedding 输入)`，
  标题或章节变化同样让旧向量失效；
* 模板版本 `EMBEDDING_INPUT_VERSION = title_context_v1` 参与投影新鲜度。

## 6. 重建机制

| 触发 | 判定 | 动作 |
|---|---|---|
| 正文变化 | `documents.content_hash` 新值 | 新文档版本，旧版本 superseded |
| 标题/章节变化 | `embedding_input_hash` 不一致 | 重新 Embedding 该 Chunk |
| 模板升级 | `input_version != title_context_v1` | 全量重新 Embedding |
| 模型/revision 变化 | `embedding_model != identity` | 全量重新 Embedding |
| 解析/切分规则升级 | `document.parser_version/chunker_version` 落后 | 从持久原文重新解析、切分、刷新向量 |

* 摄取去重命中时也会检查 `_chunking_stale`，原文未变同样重建；
* 维护扫描新增 `stale_chunking`，`apply_report` 走 `reindex(mark_failure=False)`；
* 失败保留 `index_failed` / `source_unavailable`，不写半套数据；
* 同内容同版本重复执行幂等（模型调用次数不变）；
* 检索侧同样按 `input_version` 过滤：模板升级后旧向量不参与召回，
  避免新旧 Embedding 混合排序（等维护回填补齐）。

## 7. 测试与验证（实际结果）

| 命令 | 结果 |
|---|---|
| `.venv/Scripts/python.exe -m pytest -q` | **1524 passed, 53 skipped**（EXIT=0，213.9s） |
| `pytest tests/unit/backend/test_structure_chunking.py -q` | 33 passed |
| `pytest tests/unit/backend/test_structure_chunking_migration.py -q` | 2 passed |
| `AIIVE_PG_TESTS=1 pytest tests/integration/pg/test_pg_structure_chunking.py -q` | 4 passed |
| `AIIVE_PG_TESTS=1 pytest tests/integration/pg -q` | 39 passed / 13 skipped（1 例 Windows 文件锁 flaky，单跑 10 passed） |
| `ruff check backend/aiive --select F,E9` | All checks passed |
| `basedpyright backend` | 0 errors, 0 warnings |
| PG 空库 `alembic upgrade head` + `alembic check` | 升级成功；No new upgrade operations detected |
| SQLite 空库 `alembic upgrade head` | 升级成功（`alembic check` 仅剩既有 VECTOR/FLOAT 方言差异） |

PG 验证使用一次性 `pgvector/pgvector:pg16` 容器；向量由确定性假实现生成，
不依赖本地 TEI 服务。

## 8. 未完成事项与风险

1. **历史库需要一次重建**：升级后存量分块版本为空，会被 `stale_chunking` 识别；
   但重建依赖对象存储中的原文与可用的 Embedding 服务，未做"升级即自动全量重建"，
   由维护任务按批推进（`AIIVE_KNOWLEDGE_VECTOR_BACKFILL_BATCH` 控制节奏）。
2. **PDF 不重建标题层级**：pypdf 不提供样式信息，只按页给 Block，
   `heading_path` 为空、`page_start/page_end` 有值。需要标题时请在源文件中使用 DOCX/Markdown。
3. **邻居扩展只作用于检索入口**：`search_chunks` 返回的上下文块带 `expanded_from`，
   但上下文装配（`context_assembler`）仍按原预算消费命中结果，未额外改写。
4. **`chunk_text` 保留**：作为遗留接口，仓库内已无调用方；后续可删除。
5. **并发契约用例的 Windows flaky**：`test_concurrent_ingest_same_path_yields_single_active_version`
   在文件被占用时依赖 `os.replace` 重试，与本次改动无关（单跑稳定通过）。
6. **token 计数是估算**：与具体 BPE 词表存在偏差，口径偏保守（宁少切不多切）；
   若更换模型需要重新标定 `target/max`。
