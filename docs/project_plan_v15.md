# AIive project_plan_v15.md：本地知识摄入、Chunk 与 PostgreSQL 文本检索

> 上位文档：`final_project_plan_v7.md`  
> 编码约束：`编码规范.md`  
> 阶段编号：V15  
> 本阶段定位：让 AIive 不仅记住用户，还能读取用户提供的资料、项目文档和代码片段。

---

## 1. 从上一阶段到本阶段的过渡

从 V14 到 V15：已有 outbox，本阶段引入外部知识库的最小版本。先用 Markdown/TXT/代码文本和 PostgreSQL 检索，不接 Qdrant。

过渡规则：

```text
1. 必须保留上一阶段已通过的用户可观察结果。
2. 必须优先新增模块、表、API，不做大范围删除。
3. 旧 API 如需扩展，只能兼容扩展，不能破坏返回结构。
4. 本阶段完成前，不得实现后续阶段功能。
5. 如果需要迁移数据，必须提供可重复执行的脚本或 Alembic migration，并在测试库验证。
```

---

## 2. 本阶段产品意义

用户摄入一个 md/txt 文件后，聊天能引用其中内容。

AIive 的差异化要求：本阶段实现不能退化成普通 chatbot / 普通 RAG / 普通工具调用器。所有新增能力都必须服务于长期个人管家目标：理解用户、维护节奏、维护软件身体、给自己加能力。

---

## 3. 必须实现

- 创建 documents、chunks 表。
- 实现 knowledge/ingestor.py、chunker.py。
- 支持 Markdown/TXT/代码文本基础 chunk。
- 保存 source_path、hash、line_start、line_end。
- 实现 /api/knowledge/ingest 和 /api/search 简单检索。
- Context Builder Evidence Pack 注入 top chunks。


---

## 4. 明确不得实现

- PDF。
- 网页抓取。
- Qdrant embedding。
- 复杂 code symbol graph。


---

## 5. 后端实现细节

- 重复 hash 不重复摄入。
- chunk metadata 是 PostgreSQL 真相源。
- 检索结果必须记录 retrieval_run_id 可选。


---

## 6. 前端实现细节

- 简单 Knowledge 页面可选；Chat 能展示引用 chunk ids。


---

## 7. 数据库 / 存储变更

- documents
- chunks
- retrieval_runs 可选


---

## 8. API 契约

- POST /api/knowledge/ingest
- GET /api/search


---

## 9. 可观测结果

用户可验证：

```text
同文件重复摄入去重。
```

AI 可验证：

```text
pytest tests/unit/backend/test_ingestor.py -q；pytest tests/unit/backend/test_text_retrieval.py -q；python scripts/smoke/smoke_ingest_md_search.py
```

---

## 10. 验收标准 Done Definition

- 同文件重复摄入去重。
- 检索返回 chunk_id。
- Chat 可注入文档片段。
- snapshot 记录 injected_chunk_ids。


---

## 11. 测试要求

只允许运行本阶段相关测试：

- pytest tests/unit/backend/test_ingestor.py -q
- pytest tests/unit/backend/test_text_retrieval.py -q


Smoke 脚本：

- python scripts/smoke/smoke_ingest_md_search.py


禁止默认运行：

```bash
pytest
pytest tests
npm test
pnpm test --all
```

---

## 12. 测试清理要求

- 摄入测试文件来自 tests/fixtures/files。
- 测试 documents/chunks 带 test_run_id 自动清理。


所有清理必须由测试脚本、fixture 或 cleanup helper 自动完成，不允许编码 AI 测试后手动删除。

---

## 13. 阶段完成后必须更新

```text
docs/current_phase.md
tests/artifacts/v15/<run_id>/summary.json
tests/artifacts/v15/<run_id>/commands.txt
tests/artifacts/v15/<run_id>/cleanup_report.json
```

`docs/current_phase.md` 必须记录：

```text
Phase: V15 本地知识摄入、Chunk 与 PostgreSQL 文本检索
Status: completed
Implemented: ...
Not Implemented: ...
Observable Result: ...
Tests Run: ...
Cleanup Result: ...
Known Gaps: ...
Next Phase: V16 Qdrant 与 Hybrid Retrieval
```


