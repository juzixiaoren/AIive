# AIive project_plan_v16.md：Qdrant 与 Hybrid Retrieval

> 上位文档：`final_project_plan_v7.md`  
> 编码约束：`编码规范.md`  
> 阶段编号：V16  
> 本阶段定位：提升知识检索质量，支持向量召回和混合检索。

---

## 1. 从上一阶段到本阶段的过渡

从 V15 到 V16：已有 PostgreSQL chunks 和文本检索，本阶段将 Qdrant 作为派生索引加入。PostgreSQL 仍是真相源，Qdrant 可重建。

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

用户问语义相近的问题，也能召回已摄入文档。

AIive 的差异化要求：本阶段实现不能退化成普通 chatbot / 普通 RAG / 普通工具调用器。所有新增能力都必须服务于长期个人管家目标：理解用户、维护节奏、维护软件身体、给自己加能力。

---

## 3. 必须实现

- 接入 Qdrant client。
- 创建 kb_docs collection。
- 实现 embedding_client.py。
- 实现 reindex script。
- 实现 retrieval_planner.py，支持 dense + text 或 dense + sparse RRF。
- 记录 retrieval_candidates。


---

## 4. 明确不得实现

- 复杂学习 reranker。
- 人工评测集。
- 把 Qdrant 当真相源。
- Graphiti。


---

## 5. 后端实现细节

- Qdrant vector payload 包含 chunk_id/document_id/source_hash。
- chunks 可重建 Qdrant。
- 检索结果经 gate 后进入 Context Builder。


---

## 6. 前端实现细节

- Retrieval debug JSON 可看候选来源和分数。


---

## 7. 数据库 / 存储变更

- retrieval_runs
- retrieval_candidates


---

## 8. API 契约

- GET /api/retrieval-runs/{run_id}


---

## 9. 可观测结果

用户可验证：

```text
Qdrant 可由 chunks 重建。
```

AI 可验证：

```text
pytest tests/unit/backend/test_qdrant_indexer.py -q；pytest tests/unit/backend/test_hybrid_retrieval.py -q；python scripts/smoke/smoke_qdrant_retrieval.py
```

---

## 10. 验收标准 Done Definition

- Qdrant 可由 chunks 重建。
- 语义检索能召回相关 chunk。
- Retrieval candidates 可追踪。
- 不依赖 Qdrant 存活维持 source 数据。


---

## 11. 测试要求

只允许运行本阶段相关测试：

- pytest tests/unit/backend/test_qdrant_indexer.py -q
- pytest tests/unit/backend/test_hybrid_retrieval.py -q


Smoke 脚本：

- python scripts/smoke/smoke_qdrant_retrieval.py


禁止默认运行：

```bash
pytest
pytest tests
npm test
pnpm test --all
```

---

## 12. 测试清理要求

- 测试使用 test collection，结束删除 collection。
- DB 测试记录自动清理。


所有清理必须由测试脚本、fixture 或 cleanup helper 自动完成，不允许编码 AI 测试后手动删除。

---

## 13. 阶段完成后必须更新

```text
docs/current_phase.md
tests/artifacts/v16/<run_id>/summary.json
tests/artifacts/v16/<run_id>/commands.txt
tests/artifacts/v16/<run_id>/cleanup_report.json
```

`docs/current_phase.md` 必须记录：

```text
Phase: V16 Qdrant 与 Hybrid Retrieval
Status: completed
Implemented: ...
Not Implemented: ...
Observable Result: ...
Tests Run: ...
Cleanup Result: ...
Known Gaps: ...
Next Phase: V17 可解释性 UI：Events、Context、Retrieval、Tools
```


---

## 成熟实践参照

本阶段设计遵循以下成熟实践，但不直接把复杂框架整体搬进来：

```text
LangGraph / LlamaIndex：短期线程记忆 + 长期记忆 + token 限制 + 检索注入。
OpenAI Agents Guardrails：工具调用前后进行机械 guardrail，而不是只相信 LLM。
MCP 官方 Registry / MCP Security：MCP 先发现、评估、沙箱验证，再启用；防 tool poisoning / rug pull。
Blue-Green / Rolling Update：A/B slot、健康检查、失败回滚。
Transactional Outbox：业务状态、事件、异步任务同事务写入，由 worker 可靠消费。
Expand-Contract Migration：先兼容扩展，再切换，最后收缩旧结构。
```

