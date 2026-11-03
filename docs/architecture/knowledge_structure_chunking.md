# 结构感知解析与递归切分（V61）

知识库的分块从「固定字符窗口」升级为「结构感知递归切分」：解析阶段把文档还原成带层级
的 Block，切分阶段按 Section → Block → 句子 → 标点 → 字符窗口逐级降级，最终 Chunk
携带标题路径、块类型与来源定位，Embedding 输入额外拼接标题上下文。

本文描述实现契约；迁移与运维细节见 `knowledge_维护.md`，原文持久化见
`knowledge_durable_source.md`。

## 1. 数据流

```
原始字节（对象存储持久原文）
  └─ parse_document_blocks()        knowledge/blocks.py      → list[Block]
       └─ chunk_blocks()            knowledge/chunker.py     → list[ChunkDraft]
            └─ Chunk 落库           knowledge/ingestor.py    → chunks 表（含结构元数据）
                 └─ build_embedding_input()  knowledge/embedding_input.py
                      └─ ChunkVectorService.refresh_document()  → knowledge_chunk_vectors
```

关键约束：

* 解析与切分**只从持久原文出发**（`read_source(document_id)`），不依赖用户本地文件，
  因此重新导入、reindex 与维护重建走同一条链路；
* 向量是 `chunks` 的派生投影，删除/重建时同步清理，检索命中后回源读取 `chunks`；
* 解析失败（格式可读但结构解析异常）退回通用文本 Block，**不静默丢内容**；
  格式本身不可读（后缀不支持、无文本层、超限）仍抛 `DocumentReadError`，由 API 报错。

## 2. Block：统一的结构单元

`knowledge/blocks.py` 定义 `Block(content, block_type, heading_path, block_index,
line_start, line_end, page_start, page_end, section_id)`。

| block_type | 来源 | 说明 |
|---|---|---|
| `heading` | Markdown ATX / DOCX 标题样式或 outlineLvl / HTML h1-h6 | 只提供层级上下文，不进入正文 Chunk |
| `paragraph` | 段落、HTML p | 默认文本单元 |
| `list_item` | Markdown 列表（含嵌套缩进）、HTML li | 保留原始缩进，父子关系靠缩进体现 |
| `table` | Markdown 表格、DOCX tbl、HTML table、CSV/TSV 整表 | 原子块，不参与普通 Overlap |
| `code` | 围栏代码块、源码文件（.py/.js/.ts/…）、JSON | 原子块，保留缩进 |
| `quote` | Markdown 引用、HTML blockquote | 去掉引用前缀，保留正文 |
| `text` | 纯文本、解析失败兜底 | 按空行分段，任何非空行都保留 |

格式覆盖：

* **Markdown**：H1-H6、段落、列表（含续行与嵌套）、表格、引用、围栏代码；
* **DOCX**：`w:pStyle`（Heading1/标题 1）与 `w:outlineLvl` 推标题层级，`w:tbl` → 表格；
* **PDF**：按页解析，`page_start/page_end` 记录真实页码，**不伪造标题层级**；
* **HTML**：h1-h6 / p / li / pre / blockquote / table；
* **CSV/TSV**：首行作表头，整表作为一个表格 Block；
* **源码 / JSON**：按顶层 `def/class/function/...` 边界切块，无法识别时按行；
* **其他文本**：按空行分段。

`heading_path` 由标题栈推导（`A > B > C`），`section_id` = `s` + `sha1(heading_path)`
前 16 位：同一路径在重建后保持不变，检索侧可据此定位与扩展邻居。

`PARSER_VERSION = "structure_v1"` 随解析规则变化递增。

## 3. 递归切分

`knowledge/chunker.py`，预算默认 `target 450 / max 700 / min 100 / overlap 60`（token 口径）。

优先级：**结构边界优先于长度边界**

1. Section：不同 `section_id` 的 Block 永不进入同一个 Chunk；
2. Block：段落 / 列表项按块装箱；
3. 句子：块内超长时按 `。！？；.!?;` 拆；
4. 标点：仍超长时按 `，,、：:` 拆；
5. 字符窗口：按 token 预算硬切（二分找最长可行前缀），保证绝不超上限。

特殊结构：

* **代码块**：优先完整保留；超长时先按顶层定义边界拆，再按行，最后字符窗口；
  代码块不参与普通文本 Overlap，也不与段落合并；
* **表格**：超长时按行分组，**每组复制表头**（Markdown 的 `|---|` 分隔行一并复制）；
* **列表**：同一 Section 内连续装箱，父项与子项尽量留在同一 Chunk；
* **空 Block**：丢弃，不产生空 Chunk；**短尾块**：不足 `min` 时与上一块合并（不超 `max`）；
* **Overlap**：只在同一 Section 的相邻文本 Chunk 之间、且只在句子边界取尾部；
  代码与表格前后不做 Overlap。

`CHUNKER_VERSION = "structure_recursive_v1"` 随切分规则变化递增。

### Token 口径

`knowledge/tokenization.py` 给出确定性估算（`TOKENIZER_VERSION = "cjk_char_latin4_v1"`）：

* CJK（汉字/假名/谚文/全角标点/CJK 标点）：1 字符 = 1 token；
* 连续非 CJK、非空白字符：每 4 字符 1 token，向上取整、至少 1；
* 空白不计数。

不使用 `len(text) // 4`：中文会被严重低估，700 上限形同虚设。该口径对上界友好，
同一文本在任何机器上得到同一个数字，便于幂等重建与测试。

## 4. Chunk 元数据

`chunks` 新增列（迁移 `b8d2e6f4a1c7`，全部带 server_default）：

| 列 | 含义 |
|---|---|
| `heading_path` | 标题路径 `A > B > C`（展示与 Embedding 上下文） |
| `section_id` | 标题路径派生标识，重建稳定 |
| `block_type` | 主块类型；混合内容记 `mixed` |
| `source_type` | 文档类型（markdown/docx/pdf/text/code/…） |
| `line_start` / `line_end` | 来源行号 |
| `page_start` / `page_end` | 来源页码（仅 PDF） |
| `chunk_index` | 文档内连续序号 |
| `token_estimate` | 切分时的 token 估算 |
| `parser_version` / `chunker_version` | 生成该分块的解析/切分版本 |
| `embedding_input_hash` | 完整 Embedding 输入的 sha256 |

`documents` 记录最近一次解析/切分的 `parser_version` / `chunker_version`，用于判断
「原文没变但规则升级」；`knowledge_chunk_vectors.input_version` 记录 Embedding 输入模板版本。

## 5. Chunk 原文与 Embedding 输入

两者显式分离（`knowledge/embedding_input.py`）：

```
文档：{title}
章节：{A > B > C}
正文：{chunk.content}
```

* **Chunk 原文**（`chunks.content`）：展示、关键词检索、回源校验，保持文档原貌；
* **Embedding 输入**：标题 + 章节 + 正文，让「AOF」这类短片段在向量空间带上主题；
* 标题前缀只参与 Embedding，不进入展示正文；
* `embedding_input_hash` 覆盖**完整输入**（含标题前缀），标题/章节变化同样让旧向量失效；
* `EMBEDDING_INPUT_VERSION = "title_context_v1"` 随模板措辞变化递增。

不引入 Parent-Child 切块、LLM 切块或摘要：Chunk 即检索与展示单元。

## 6. 索引更新与重建

`ChunkVectorService.refresh_document` 的新鲜度判定由三项共同决定：

1. `input_version == EMBEDDING_INPUT_VERSION`（模板变化 → 全部重算）；
2. `content_hash == 期望的 Embedding 输入哈希`（正文/标题/章节任一变化 → 重算）；
3. `embedding_model == embedding_model_identity()`（模型或 revision 变化 → 重算）。

三者一致时跳过模型调用，重建幂等。检索侧（`ChunkVectorService.search`）同样要求
`input_version` 与当前模板一致：模板升级后旧向量不参与召回，避免新旧 Embedding
混合排序，直到维护回填完成。

`missing_vector_document_ids` 同样把「投影缺失、模型不一致、模板版本不一致、
分块未记录输入哈希、投影哈希与分块哈希不一致」都视为待回填，因此历史库升级后
会被维护任务按批补齐。

**版本触发的重建**：

* `KnowledgeIngestor._chunking_stale(document)`：`parser_version` / `chunker_version`
  与当前常量不一致即视为落后；
* 去重命中（原文未变）时，若分块缺失或版本落后，仍从持久原文重新解析、切分、刷新向量；
* `maintenance.scan_knowledge_base` 新增 `stale_chunking` 问题码，
  `apply_report` 通过 `_rebuild_missing_chunks` 合并「缺失分块」与「版本落后」一起重建；
* 重建失败保留 `index_failed` / `source_unavailable`，不吞异常、不写半套数据；
* 同内容同版本重复执行幂等；删除 / Supersede / 重新导入语义不变。

## 7. 检索

保留现有架构（词法 + 向量 RRF 融合，k=60，各 0.5），只增强命中信息：

* 命中返回 `heading_path`、`section_id`、`block_type`、`source_type`、
  `line_start/line_end`、`page_start/page_end`、`chunk_index`、`token_estimate`；
* 允许按 `section_id + chunk_index` 扩展前后各 N 个相邻 Chunk
  （`aiive_knowledge_neighbor_expansion`，默认 1；`..._seed_limit` 默认 3；
  `..._budget_tokens` 默认 600）：
  * 只在同一文档、同一 Section 内扩展，不跨主题拼接；
  * 去重、遵守 token 预算，超预算的邻居直接跳过；
  * 扩展结果附 `expanded_from`（种子 chunk_id），前端可区分「命中」与「上下文」；
* 不重构 BM25 / Memory Recall。

## 8. 配置

| 配置 | 默认 | 说明 |
|---|---|---|
| `aiive_knowledge_neighbor_expansion` | 1 | 前后扩展的相邻 Chunk 数，0 关闭 |
| `aiive_knowledge_neighbor_seed_limit` | 3 | 参与扩展的种子命中数 |
| `aiive_knowledge_neighbor_budget_tokens` | 600 | 单次检索用于扩展的 token 预算 |

## 9. 测试

* `tests/unit/backend/test_structure_chunking.py`：Token 计数、各格式 Block 解析、
  递归切分（Section 隔离 / Overlap / 代码 / 表格 / 短尾）、Embedding 输入与哈希、
  版本触发重建、投影新鲜度、检索结构定位与邻居扩展；
* `tests/unit/backend/test_structure_chunking_migration.py`：空库升级、存量库补列与
  降级回滚（数据不丢、默认值正确）；
* `tests/integration/pg/test_pg_structure_chunking.py`：真实 PG + pgvector 上的
  结构元数据、投影版本、维护重建、损坏投影回填与邻居预算。
