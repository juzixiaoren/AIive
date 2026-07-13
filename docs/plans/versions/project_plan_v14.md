# AIive project_plan_v14.md：PostgreSQL Outbox Worker 与异步派生任务

> 上位文档：`final_project_plan_v7.md`  
> 编码约束：`编码规范.md`  
> 阶段编号：V14  
> 本阶段定位：让聊天主路径保持快，同时记忆抽取、索引、维护等派生任务可靠执行。

---

## 1. 从上一阶段到本阶段的过渡

从 V13 到 V14：已有事件日志、自我修改雏形和 memory 抽取，本阶段把派生任务从同步路径移到可靠 outbox。

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

用户聊天不被记忆抽取阻塞，后台完成后能看到 candidate memory 或任务状态。

AIive 的差异化要求：本阶段实现不能退化成普通 chatbot / 普通 RAG / 普通工具调用器。所有新增能力都必须服务于长期个人管家目标：理解用户、维护节奏、维护软件身体、给自己加能力。

---

## 3. 必须实现

- 创建 outbox_jobs 表。
- 实现 worker/outbox_worker.py。
- 事件和 outbox job 同事务写入。
- 支持 job 状态 pending/running/completed/failed/deadletter。
- 将 memory extraction 改为 outbox 异步。


---

## 4. 明确不得实现

- Celery/Redis/Kafka。
- 多 worker 并发锁。
- 复杂调度平台。


---

## 5. 后端实现细节

- Worker 单进程轮询即可。
- job handler 幂等：operation_id/source_event_id。
- 失败 retry_count 增加，超过阈值 deadletter。


---

## 6. 前端实现细节

- Chat Page 或 Memory 页面显示 memory extraction pending/completed。


---

## 7. 数据库 / 存储变更

- outbox_jobs


---

## 8. API 契约

- GET /api/outbox/jobs?trace_id=... 可选。


---

## 9. 可观测结果

用户可验证：

```text
Chat 不等待 memory extraction。
```

AI 可验证：

```text
pytest tests/unit/backend/test_outbox_worker.py -q；pytest tests/unit/backend/test_async_memory_extraction.py -q；python scripts/smoke/smoke_outbox_memory.py
```

---

## 10. 验收标准 Done Definition

- Chat 不等待 memory extraction。
- Worker 可消费并生成 candidate。
- 失败 job 可 retry/deadletter。
- 无内存队列丢任务问题。


---

## 11. 测试要求

只允许运行本阶段相关测试：

- pytest tests/unit/backend/test_outbox_worker.py -q
- pytest tests/unit/backend/test_async_memory_extraction.py -q


Smoke 脚本：

- python scripts/smoke/smoke_outbox_memory.py


禁止默认运行：

```bash
pytest
pytest tests
npm test
pnpm test --all
```

---

## 12. 测试清理要求

- outbox 测试 job 带 test_run_id 自动清理。


所有清理必须由测试脚本、fixture 或 cleanup helper 自动完成，不允许编码 AI 测试后手动删除。

---

## 13. 阶段完成后必须更新

```text
docs/current_phase.md
tests/artifacts/v14/<run_id>/summary.json
tests/artifacts/v14/<run_id>/commands.txt
tests/artifacts/v14/<run_id>/cleanup_report.json
```

`docs/current_phase.md` 必须记录：

```text
Phase: V14 PostgreSQL Outbox Worker 与异步派生任务
Status: completed
Implemented: ...
Not Implemented: ...
Observable Result: ...
Tests Run: ...
Cleanup Result: ...
Known Gaps: ...
Next Phase: V15 本地知识摄入、Chunk 与 PostgreSQL 文本检索
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

