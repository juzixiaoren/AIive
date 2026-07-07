# AIive project_plan_v9.md：MCP Discovery v0：搜索与候选提案，不安装

> 上位文档：`final_project_plan_v7.md`  
> 编码约束：`编码规范.md`  
> 阶段编号：V9  
> 本阶段定位：让 AIive 能根据用户目标搜索 MCP 生态，形成可解释候选能力提案。

---

## 1. 从上一阶段到本阶段的过渡

从 V8 到 V9：AIive 已有 tool safety 和 safe_delete，开始尽早验证“自己搜索相关 MCP”的核心方向。但 V9 只搜索和生成候选，不安装、不执行。

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

用户说“你需要一个能读 GitHub issue 的能力”，AIive 能搜索 MCP registry/配置源，列出候选和风险。

AIive 的差异化要求：本阶段实现不能退化成普通 chatbot / 普通 RAG / 普通工具调用器。所有新增能力都必须服务于长期个人管家目标：理解用户、维护节奏、维护软件身体、给自己加能力。

---

## 3. 必须实现

- 实现 mcp/discovery.py。
- 支持读取官方/配置的 MCP registry metadata。
- 实现 MCPServerCandidate schema：name、source、version、description、transport、package_ref、declared_tools、risk_notes。
- 实现 search_mcp_candidates(goal)。
- 将候选写入 capabilities candidate 状态。
- UI/API 能展示候选。


---

## 4. 明确不得实现

- 安装 MCP。
- 启动 MCP server。
- 调用 MCP tool。
- 把 MCP 工具描述当系统指令。


---

## 5. 后端实现细节

- 搜索结果必须标注 definition_trust_level=untrusted 或 semi_trusted。
- 记录 registry_source 和 descriptor_hash。
- 外部 registry 内容按 untrusted content 处理。


---

## 6. 前端实现细节

- Tools/Capabilities 页面展示 MCP candidate，或 Chat 返回候选列表。


---

## 7. 数据库 / 存储变更

- capabilities candidate 状态
- mcp_candidates 可选


---

## 8. API 契约

- POST /api/mcp/search
- GET /api/capabilities?state=candidate


---

## 9. 可观测结果

用户可验证：

```text
可按目标搜索 MCP 候选。
```

AI 可验证：

```text
pytest tests/unit/backend/test_mcp_discovery.py -q；python scripts/smoke/smoke_mcp_search.py
```

---

## 10. 验收标准 Done Definition

- 可按目标搜索 MCP 候选。
- 候选可见且有风险说明。
- 没有安装或执行任何 MCP。
- 这是自举能力的第一个可验证阶段。


---

## 11. 测试要求

只允许运行本阶段相关测试：

- pytest tests/unit/backend/test_mcp_discovery.py -q


Smoke 脚本：

- python scripts/smoke/smoke_mcp_search.py


禁止默认运行：

```bash
pytest
pytest tests
npm test
pnpm test --all
```

---

## 12. 测试清理要求

- 单测使用 fixture registry JSON。
- 真实 registry smoke 不安装，不写除 candidate 外的副作用；candidate 带 test_run_id 自动清理。


所有清理必须由测试脚本、fixture 或 cleanup helper 自动完成，不允许编码 AI 测试后手动删除。

---

## 13. 阶段完成后必须更新

```text
docs/current_phase.md
tests/artifacts/v9/<run_id>/summary.json
tests/artifacts/v9/<run_id>/commands.txt
tests/artifacts/v9/<run_id>/cleanup_report.json
```

`docs/current_phase.md` 必须记录：

```text
Phase: V9 MCP Discovery v0：搜索与候选提案，不安装
Status: completed
Implemented: ...
Not Implemented: ...
Observable Result: ...
Tests Run: ...
Cleanup Result: ...
Known Gaps: ...
Next Phase: V10 MCP Sandbox Install v0：测试服务器、只读工具与能力激活
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

