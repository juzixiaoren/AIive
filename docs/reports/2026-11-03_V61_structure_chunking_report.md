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
| 0 | paragraph | `Redis 技术笔记` | `s67aaa9db6ff61513` | 2-2 | 11 |
| 1 | paragraph | `Redis 技术笔记 > 持久化` | `s3219b294bdbcc718` | 6-6 | 13 |
| 2 | mixed | `Redis 技术笔记 > 持久化 > AOF` | `s3ee01f85d7bad115` | 10-14 | 25 |
| 3 | table | `Redis 技术笔记 > 持久化 > AOF` | `s3ee01f85d7bad115` | 16-18 | 27 |
| 4 | code | `Redis 技术笔记 > 持久化 > AOF` | `s3ee01f85d7bad115` | 21-22 | 9 |

* 标题只进入 `heading_path`，不产生只有标题的空 Chunk；
* 列表保留缩进子项；表格与代码各自成块（原子块，不参与普通 Overlap）；
* `section_id` 由「标题路径 + 出现序号」派生，重建后不变（V61.1 起同名章节互不相同）。

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


## 9. V61.1 正确性修复（2026-11-03 追加）

V61 上线后审计发现四类「不报错、但结果不对」的缺陷。V61.1 只修这些缺陷，不引入
Parent-Child / LLM 切块，也不改检索架构。

### 9.1 缺陷与修复

| # | 缺陷 | 原实现的问题 | 修复 |
|---|---|---|---|
| 1 | 超长表格行 | `_split_table` 只在行与行之间分组，单行超过 `max_chunk_tokens` 时被整行塞进一个 Chunk，输出直接突破上限 | 新增 `_split_row`：按单元格边界（`_cell_spans`）拆行，`_row_fragment` 为续片段补回前导竖线保持列语义；单个单元格仍超限时对该单元格递归 `_split_text`；每段继续复制表头；表头自身超限或剩余预算小于 `min_chunk_tokens` 时整表退化为普通文本拆分 |
| 2 | 重复标题 Section ID 冲突 | `section_id = sha1(heading_path)`，两个同名「## 概述」共用同一个 id，导致 Chunk 合并、Overlap 与邻居扩展跨章节 | `section_id_for(heading_path, occurrence)` 把「该路径在本文档第几次出现」一起哈希；`_finalize` 按文档顺序维护 `occurrences` 计数；无标题文档用空路径第 0 次。纯文档顺序推导，无 UUID / 自增 |
| 3 | 递归切分后来源定位漂移 | 拆分函数返回 `list[str]`，拆分后各段沿用整个 Block 的行号，一个 Block 拆成多块时每块都声称覆盖全部行 | 拆分函数统一返回 `_Segment(text, start, end)`（相对 Block 原文的字符区间），`_line_at` / `_piece_of` 由区间换算行号；Overlap 前缀与重复表头标记 `context=True`，只累加 `context_tokens`，不参与 `line_start/line_end` 汇总 |
| 4 | 无竖线表格漏识别 | `_table_extent` 要求行内含竖线且有分隔行，省略首尾竖线的写法与空格对齐的列式文本被当成普通段落 | `_table_extent` 接受「表头行 + 分隔行」的任意竖线写法；新增 `_columnar_extent` / `_column_starts` 识别空格对齐列式文本（连续 ≥3 行、字段起始列一致、行长 ≤120、无句读标点、数据行含数字），普通段落不误判 |
| 5 | Token 上限不统一 | 各分支各自判断，Overlap 与短尾合并后仍可能超限 | 新增 `_enforce_budget` 在 `chunk_blocks` 末尾统一兜底拆分，任何超限 draft 都不会落库；文档同步澄清「估算 ≠ 模型 BPE」 |

### 9.2 修改文件

| 文件 | 变更 |
|---|---|
| `backend/aiive/knowledge/chunker.py` | 全量重写：`_Segment` 携带字符区间，新增 `_split_text` / `_pack_segments` / `_char_windows` / `_split_row` / `_cell_spans` / `_row_fragment` / `_enforce_budget`；`CHUNKER_VERSION = structure_recursive_v2` |
| `backend/aiive/knowledge/blocks.py` | `section_id_for(heading_path, occurrence)` + `_finalize` 出现序号；新增 `_table_extent` / `_columnar_extent` / `_column_starts`；`PARSER_VERSION = structure_v2` |
| `tests/unit/backend/test_chunker_correctness.py` | 新增 22 例回归（超长表格、同名章节、来源定位、无竖线表格、预算不变量） |
| `tests/unit/backend/test_structure_chunking.py` | 版本重建用例的 monkeypatch 目标改为 `structure_recursive_v9`（原目标与真实常量撞车） |
| `docs/architecture/knowledge_structure_chunking.md` | 补 section_id 出现序号、表格拆分细则、来源定位小节、Token 口径澄清、V61.1 版本影响 |
| `docs/reports/2026-11-03_V61_structure_chunking_report.md` | 本节 + 示例 section_id 更新为 V61.1 实际值 |

未改动：`ingestor.py` / `chunk_vectors.py` / `maintenance.py` / `db/models.py` / Alembic
迁移 / 检索侧代码。`context_tokens` 只是切分期字段，不落库。

### 9.3 实测输出（真实运行）

| 场景 | 输入 | 输出 | 校验 |
|---|---|---|---|
| 超长表格行 | 单行 1084 tokens（3 个长单元格） | 4 块：375 / 375 / 375 / 21，均以表头 + 分隔行开头，`context_tokens=13` | 每个单元格标记都出现在某块中；行号落在数据行（2-2 / 2-3） |
| 超长单元格 | 单元格 1600 tokens | 5 块：456 / 456 / 456 / 265 / 11 | `超长单元格内容。` 出现次数合计 200，与输入一致 |
| 超长表头 | 表头 724 tokens | 2 块：700 / 35（退化为文本拆分，不再复制表头） | 表头文本 240 个片段全部保留 |
| 超长代码块 | 1280 tokens / 320 行 | 3 块：448 / 448 / 384，行号 3-114 / 115-226 / 227-322 | 首块从 `def f0(x):` 开始，末块以 `return x + 159` 结束，无内容丢失 |
| 同名章节 | 两个 `## 概述` | 两个不同 `section_id`，各自 Chunk 不合并 | `search_chunks` 邻居扩展只在同一 `section_id` 内 |
| 无竖线表格 | 省略首尾竖线的表头 + 分隔行 + 数据行 | 解析为单个 `table` Block | 同目录的散文用例仍为 `paragraph` |

### 9.4 版本与存量数据影响

* `PARSER_VERSION`: `structure_v1` → `structure_v2`；
* `CHUNKER_VERSION`: `structure_recursive_v1` → `structure_recursive_v2`；
* `section_id` 推导加入出现序号，同一文档的 id 全部变化。

因此存量文档的 Chunk 会被 `_chunking_stale` 全部判为落后，维护任务按批从持久原文
重新解析、切分并刷新向量。**数据库 schema 无变化，不需要新的 Alembic 迁移**，
`b8d2e6f4a1c7` 保持原样；重建失败仍保留 `index_failed` / `source_unavailable`，
不写半套数据。

### 9.5 验证命令与结果

| 命令 | 结果 |
|---|---|
| `pytest tests/unit/backend/test_chunker_correctness.py -q` | 22 passed |
| `pytest tests/unit/backend/test_structure_chunking.py -q` | 33 passed |
| `pytest -q`（全量） | **1546 passed, 53 skipped, 52 warnings**（227.83s，EXIT=0） |
| `ruff check backend/aiive --select F,E9` | All checks passed |
| `basedpyright backend` | 0 errors, 0 warnings, 0 notes |
| `AIIVE_PG_TESTS=1 pytest tests/integration/pg -q` | 39 passed / 13 skipped / 1 failed（`test_concurrent_ingest_same_path_yields_single_active_version`，Windows 文件锁既有 flaky，单跑 11.05s 通过） |

### 9.6 剩余风险

1. **表头退化的可读性**：表头本身超 700 tokens 时整表按文本拆，续块没有列名，
   但内容完整、不超限；这是刻意取舍（优先保证上限与不丢内容）。
2. **列式文本识别的边界**：靠「列起点一致 + 数据行含数字」区分表格与散文，
   极端情况下（多行等宽对齐且含数字的散文）仍可能被识别为表格；已加句读与行长
   约束降低概率，未引入打分模型。
3. **行内片段的字符区间**：表格续片段会补一个前导竖线，片段的字符区间按原始行计算，
   行号不受影响（表格行内无换行），但逐字符偏移与原文存在 2 字符偏差。
4. **token 上限仍按估算口径**：`count_tokens` 是启发式估算，模型侧真实 token 数可能
   不同；需要严格长度保证时应在 Embedding 客户端按模型 tokenizer 处理。


## 10. V61.2 边界正确性修复（2026-11-03 追加）

V61.1 之后继续审计，发现三处只在边界条件下出现的缺陷：不报错，但来源定位或列结构
会静默变错。V61.2 只修这三处，不改架构、不动 Section ID / Overlap / 检索 / 向量机制。

### 10.1 缺陷与修复

| # | 缺陷 | 原实现的问题 | 修复 |
|---|---|---|---|
| 1 | 代码超长行的来源偏移重复累加 | `_split_code_by_lines` 的超长单行分支在生成 `_Segment` 时已经算成 `base + offset + ...`，函数返回时又统一加一次 `base`。`base` 是函数在代码块中的字符偏移，第二次累加会把片段推到后面的行上，甚至越过块尾被 clamp | 统一坐标语义：`_Segment.start/end` 一律相对「当前函数入参文本」。`_split_code_by_lines` 只返回局部偏移，由 `_split_code` 的函数边界分支 rebase 一次；普通代码行、函数边界拆分、字符窗口拆分共用同一套换算 |
| 2 | 兜底拆分丢失来源映射 | `_enforce_budget` 把最终 `content` 当成原文重新 `_split_text`，用「draft.line_start + 内容内换行数」推行号。但 content 可能以 Overlap 前缀或重复表头开头，行号被整体前移；同时 `context_tokens` 被清零，上下文被冒充成本 Chunk 的来源 | `ChunkDraft` 新增 `parts: tuple[ChunkPart, ...]`（正文 / 行范围 / 是否上下文），`_assemble` 汇总时填充；`_enforce_budget` 按片段拆分、按片段换算行号、重新装箱，上下文片段只累加 `context_tokens`。没有 parts 的历史 draft 退化为「整块一个来源片段」，行为与旧实现一致 |
| 3 | 超长表格行拆分丢空单元格 | `_cell_spans` 过滤掉空串与纯竖线片段，「张三 + 两个空列 + 超长值」这类行进入单元格拆分路径后两个空列消失，列数与列序都变了 | 保留全部单元格（行首竖线并入首格），`_row_fragment` 的续片段连带前一单元格的结尾竖线，片段文本始终是原始行的连续子串——列结构完整，顺带消除了 V61.1 遗留的「人工补竖线造成 2 字符偏移漂移」 |

### 10.2 修改文件

| 文件 | 变更 |
|---|---|
| `backend/aiive/knowledge/chunker.py` | 新增 `ChunkPart`、`ChunkDraft.parts`、`_Piece.parts`、`_piece_parts`、`_split_draft`、`_line_in_part`、`_pack_parts`、`_draft_from_parts`；`_split_code_by_lines` 去掉 `base` 参数只返回局部偏移；`_cell_spans` / `_row_fragment` 保留空单元格与精确偏移；`CHUNKER_VERSION = structure_recursive_v3` |
| `tests/unit/backend/test_chunker_boundaries.py` | 新增 13 例回归（代码行号 4 例、兜底拆分 4 例、空单元格与列结构 4 例、混合文档预算 1 例） |
| `docs/architecture/knowledge_structure_chunking.md` | 补坐标语义、兜底拆分的来源片段、空单元格保留、V61.2 版本影响 |
| `docs/CODE_MAP.md` | 图谱同步 `ChunkPart` / `_enforce_budget` / 新测试文件 |
| `docs/reports/2026-11-03_V61_structure_chunking_report.md` | 本节 |

未改动：`blocks.py`（`PARSER_VERSION` 仍为 `structure_v2`）、`ingestor.py`、
`chunk_vectors.py`、`maintenance.py`、`db/models.py`、Alembic 迁移、检索侧代码。

### 10.3 实测输出（真实运行）

| 场景 | 输入 | 输出 | 校验 |
|---|---|---|---|
| 后续函数中的超长代码行 | 代码块 1674 tokens，超长行在第 67 行 | 4 块：697（3-53）/ 159（54-66）/ 700（67-67）/ 118（67-71） | 超长行的两块都指向 67 行；第三个函数的 `return value * two` 仍落在 71 行 |
| 兜底拆分（Overlap） | Overlap 8 tokens + 12 行正文（约 1700 tokens） | 4 块：440（20-22，`context_tokens=8`）/ 432（23-25）/ 432（26-28）/ 432（29-31） | 首块保留上下文长度，行号从 20 起，不回到 Overlap 的 10 行 |
| 兜底拆分（重复表头） | 表头 17 tokens + 10 行数据 | 多块，首块 `context_tokens=17`，其余 0 | 行号全部落在 7-16 的数据行，表头不计入来源 |
| 超长表格行 + 连续空列 | 含两个空列、单个 900 tokens 单元格的数据行 | 3 块：23 / 700 / 235 | 首块保留「张三 + 两个空列」的行片段，空列不丢，`context_tokens=17` |

### 10.4 版本与存量数据影响

`CHUNKER_VERSION` 由 `structure_recursive_v2` 升到 `structure_recursive_v3`
（`PARSER_VERSION` 不变）。触发原因是切分结果本身变化：表格空列不再丢失、续片段不再
人工补竖线、代码行号被修正。存量文档会被 `_chunking_stale` 判为落后并按批重建；
数据库 schema 与 Alembic 迁移均无变化，不需要新迁移。重建完成前，旧 Chunk 的内容与
行号仍是旧规则产物，检索可用但结构定位不可全信。

### 10.5 验证命令与结果

| 命令 | 结果 |
|---|---|
| `pytest tests/unit/backend/test_chunker_boundaries.py -q` | 13 passed |
| `pytest tests/unit/backend/test_chunker_correctness.py tests/unit/backend/test_structure_chunking.py -q` | 55 passed（V61.1 全部保持通过） |
| `pytest -q`（全量） | **1559 passed, 53 skipped, 52 warnings**（279.31s，EXIT=0） |
| `ruff check backend/aiive --select F,E9` | All checks passed |
| `basedpyright backend` | 0 errors, 0 warnings, 0 notes |
| `AIIVE_PG_TESTS=1 pytest tests/integration/pg -q` | 40 passed / 13 skipped（211.37s，真实 PG + pgvector 容器） |

**回归有效性（变异验证）**：把三处修复分别改回旧行为后，对应测试均失败——代码偏移
重复累加 → `TestCodeSourceOffsets` 2 例失败；`_cell_spans` 恢复过滤 → `TestEmptyTableCells`
2 例失败；`_enforce_budget` 忽略 parts → `TestEnforceBudgetProvenance` 2 例失败。
说明新测试确实覆盖缺陷，而不是只跟着新实现走。

### 10.6 剩余风险

1. **兜底拆分不重复表头**：`_enforce_budget` 只把表头放进第一个新块（后续块没有列名）。
   正常路径不会触发——`_split_table` 已保证输出 `<= max`；该路径只在未来新增切分
   分支时兜底。
2. **续片段重复一个竖线**：表格续片段连带前一单元格的结尾竖线，字符上多一个 `|`，
   这是保持列语义的必要代价，不影响列数与内容完整性。
3. **代码块仍会跨函数合并短行**：`_pack_segments` 会把后续函数的短行并入同一 Chunk
   （V61 既有行为），V61.2 只保证行号准确，不改变切分粒度。
4. **token 仍是估算口径**：`count_tokens` 是启发式估算，模型侧真实 token 数可能不同。


