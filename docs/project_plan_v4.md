# AIive project_plan_v4.md：Context Assembly v0 与 Context Snapshot

> 上位文档：`final_project_plan_v7.md`  
> 编码约束：`编码规范.md`  
> 阶段编号：V4  
> 本阶段定位：让上下文构建成为显式模块，开始验证固定注入、缓存命中和可解释快照。

---

## 1. 从上一阶段到本阶段的过渡

从 V3 到 V4：复用 events 和 thread state，不改变 Chat API 语义；将“直接拼消息”升级为 Context Builder。先做 Stable Prefix + Working Set，不接复杂检索。

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

用户可通过 trace 查看一次 LLM 调用用了哪些上下文。

AIive 的差异化要求：本阶段实现不能退化成普通 chatbot / 普通 RAG / 普通工具调用器。所有新增能力都必须服务于长期个人管家目标：理解用户、维护节奏、维护软件身体、给自己加能力。

---

## 3. 必须实现

- 实现 core/context_builder.py。
- 定义 Stable Prefix：AIive 身份、低约束原则、trust boundary、LLM 不是执行器。
- 定义 Working Set：当前 thread 最近 N 轮消息。
- 创建 context_snapshots 表。
- 每次 LLM 调用写 context_snapshot metadata。


---

## 4. 明确不得实现

- 长期 memory retrieval。
- Qdrant。
- 复杂摘要压缩。
- 硬编码大量 task profile。


---

## 5. 后端实现细节

- ContextItem：item_id、kind、source、trust_level、content_ref/preview、token_estimate。
- Stable Prefix 计算 stable_prefix_hash，内容稳定时 hash 不变。
- Working Set 超过 N 时记录 truncation event。


---

## 6. 前端实现细节

- Chat Page trace_id 可链接到 debug context JSON，正式 UI 后续做。


---

## 7. 数据库 / 存储变更

- context_snapshots


---

## 8. API 契约

- GET /api/debug/traces/{trace_id} 返回 snapshot metadata。


---

## 9. 可观测结果

用户可验证：

```text
每次 LLM 调用有 snapshot。
```

AI 可验证：

```text
pytest tests/unit/backend/test_context_builder.py -q；pytest tests/unit/backend/test_context_snapshot.py -q；python scripts/smoke/smoke_chat_api.py
```

---

## 10. 验收标准 Done Definition

- 每次 LLM 调用有 snapshot。
- stable_prefix_hash 可见且稳定。
- Working Set 数量可见。
- 外部内容不会进入 Stable Prefix。


---

## 11. 测试要求

只允许运行本阶段相关测试：

- pytest tests/unit/backend/test_context_builder.py -q
- pytest tests/unit/backend/test_context_snapshot.py -q


Smoke 脚本：

- python scripts/smoke/smoke_chat_api.py


禁止默认运行：

```bash
pytest
pytest tests
npm test
pnpm test --all
```

---

## 12. 测试清理要求

- context_snapshots 测试记录自动清理。


所有清理必须由测试脚本、fixture 或 cleanup helper 自动完成，不允许编码 AI 测试后手动删除。

---

## 13. 阶段完成后必须更新

```text
docs/current_phase.md
tests/artifacts/v4/<run_id>/summary.json
tests/artifacts/v4/<run_id>/commands.txt
tests/artifacts/v4/<run_id>/cleanup_report.json
```

`docs/current_phase.md` 必须记录：

```text
Phase: V4 Context Assembly v0 与 Context Snapshot
Status: completed
Implemented: ...
Not Implemented: ...
Observable Result: ...
Tests Run: ...
Cleanup Result: ...
Known Gaps: ...
Next Phase: V5 Memory Records、抽取候选与召回注入
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

