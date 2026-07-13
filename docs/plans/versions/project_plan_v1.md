# AIive project_plan_v1.md：真实 LLM Client 与 Smoke 验证

> 上位文档：`final_project_plan_v7.md`  
> 编码约束：`编码规范.md`  
> 阶段编号：V1  
> 本阶段定位：从第一版开始接入真实 OpenAI-compatible LLM，避免长期只做 mock 架构。

---

## 1. 从上一阶段到本阶段的过渡

从 V0 到 V1：保留健康检查和配置结构，新增 LLM client；不得改动项目技术栈，不引入 Agent Loop。V1 的目标是验证真实模型连通性，而不是做智能体。

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

用户可以运行 smoke 脚本看到真实模型回复、延迟和 trace_id。

AIive 的差异化要求：本阶段实现不能退化成普通 chatbot / 普通 RAG / 普通工具调用器。所有新增能力都必须服务于长期个人管家目标：理解用户、维护节奏、维护软件身体、给自己加能力。

---

## 3. 必须实现

- 实现 backend/aiive/core/llm_client.py。
- 定义 LLMResponse：content、model、latency_ms、usage、raw_preview、trace_id。
- 读取 AIIVE_LLM_BASE_URL、AIIVE_LLM_API_KEY、AIIVE_LLM_MODEL、AIIVE_LLM_TIMEOUT_SECONDS。
- 实现 FakeLLMClient 供单元测试使用。
- 新增 scripts/smoke/smoke_llm_ping.py，输出结构化 JSON。


---

## 4. 明确不得实现

- 把真实 LLM 调用放进 pytest。
- 实现 Chat API。
- 实现 memory extraction 或 model router。


---

## 5. 后端实现细节

- LLMClient.chat(messages, model=None, temperature=None, timeout=None)。
- 异常包装为 LLMClientError，不泄漏无关堆栈到用户响应。
- trace_id 由调用方可传入；未传入则生成。


---

## 6. 前端实现细节

- 无


---

## 7. 数据库 / 存储变更

- 不写数据库。


---

## 8. API 契约

- 无新 API。


---

## 9. 可观测结果

用户可验证：

```text
FakeLLM 单测通过。
```

AI 可验证：

```text
pytest tests/unit/backend/test_llm_client.py -q；python scripts/smoke/smoke_llm_ping.py
```

---

## 10. 验收标准 Done Definition

- FakeLLM 单测通过。
- smoke 能返回真实模型回复。
- reply_preview 不为空。
- trace_id 可见。


---

## 11. 测试要求

只允许运行本阶段相关测试：

- pytest tests/unit/backend/test_llm_client.py -q


Smoke 脚本：

- python scripts/smoke/smoke_llm_ping.py


禁止默认运行：

```bash
pytest
pytest tests
npm test
pnpm test --all
```

---

## 12. 测试清理要求

- 单元测试使用 fake client，无外部副作用。
- smoke 只调用 LLM，不写数据库；输出到 tests/artifacts/v1/<run_id>/summary.json。


所有清理必须由测试脚本、fixture 或 cleanup helper 自动完成，不允许编码 AI 测试后手动删除。

---

## 13. 阶段完成后必须更新

```text
docs/current_phase.md
tests/artifacts/v1/<run_id>/summary.json
tests/artifacts/v1/<run_id>/commands.txt
tests/artifacts/v1/<run_id>/cleanup_report.json
```

`docs/current_phase.md` 必须记录：

```text
Phase: V1 真实 LLM Client 与 Smoke 验证
Status: completed
Implemented: ...
Not Implemented: ...
Observable Result: ...
Tests Run: ...
Cleanup Result: ...
Known Gaps: ...
Next Phase: V2 Chat API 与 React Chat Page 最小闭环
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

