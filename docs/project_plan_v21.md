# AIive project_plan_v21.md：MCP Self-Bootstrap v1：按目标搜索、评估、测试、启用 MCP

> 上位文档：`final_project_plan_v7.md`  
> 编码约束：`编码规范.md`  
> 阶段编号：V21  
> 本阶段定位：实现产品最关键差异之一：AIive 能给自己找能力、加能力、验证能力，而不是等待人工编码每个工具。

---

## 1. 从上一阶段到本阶段的过渡

从 V20 到 V21：V9/V10 已验证 MCP 搜索和沙箱安装；本阶段把它接入 Agent 自举流程，让 AIive 可根据目标主动提出需要什么 MCP，并完成候选评估和 sandbox 激活。

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

用户说“你需要能读 GitHub issue”，AIive 能搜索 MCP、解释候选、安装测试低风险能力并激活。

AIive 的差异化要求：本阶段实现不能退化成普通 chatbot / 普通 RAG / 普通工具调用器。所有新增能力都必须服务于长期个人管家目标：理解用户、维护节奏、维护软件身体、给自己加能力。

---

## 3. 必须实现

- 实现 capability_planner.py：从用户目标判断缺失能力。
- 调用 mcp_discovery 搜索候选。
- 对候选做 risk scoring：source、version、permissions、tool metadata、descriptor hash。
- 生成 install_plan。
- sandbox install + smoke call + capability activation。
- 记录 capability lesson 和 rollback path。


---

## 4. 明确不得实现

- 未经候选评估直接安装。
- 安装 critical/high risk MCP 后自动启用。
- 让 MCP 访问 secrets 或删除权限。
- 工具 metadata 成为系统指令。


---

## 5. 后端实现细节

- 高风险 MCP 只能进入 needs_user_review。
- 低风险 read-only 可在用户授权策略下自动 sandbox。
- descriptor_hash/tool_list_hash 变化自动降级 needs_review。


---

## 6. 前端实现细节

- Capabilities 页面展示 self-bootstrap timeline：goal -> candidates -> selected -> smoke -> active。


---

## 7. 数据库 / 存储变更

- capability_plans
- mcp_install_records
- capability_versions


---

## 8. API 契约

- POST /api/capabilities/plan-from-goal
- POST /api/capabilities/{id}/activate


---

## 9. 可观测结果

用户可验证：

```text
AIive 能从目标生成能力缺口。
```

AI 可验证：

```text
pytest tests/unit/backend/test_capability_planner.py -q；pytest tests/unit/backend/test_mcp_bootstrap_policy.py -q；python scripts/smoke/smoke_mcp_self_bootstrap.py
```

---

## 10. 验收标准 Done Definition

- AIive 能从目标生成能力缺口。
- 能搜索 MCP 候选。
- 能 sandbox 测试并激活低风险 MCP。
- 全链路可解释、可回滚。


---

## 11. 测试要求

只允许运行本阶段相关测试：

- pytest tests/unit/backend/test_capability_planner.py -q
- pytest tests/unit/backend/test_mcp_bootstrap_policy.py -q


Smoke 脚本：

- python scripts/smoke/smoke_mcp_self_bootstrap.py


禁止默认运行：

```bash
pytest
pytest tests
npm test
pnpm test --all
```

---

## 12. 测试清理要求

- 真实 registry smoke 不启用高风险工具。
- 测试 MCP 安装记录自动清理，测试进程自动关闭。


所有清理必须由测试脚本、fixture 或 cleanup helper 自动完成，不允许编码 AI 测试后手动删除。

---

## 13. 阶段完成后必须更新

```text
docs/current_phase.md
tests/artifacts/v21/<run_id>/summary.json
tests/artifacts/v21/<run_id>/commands.txt
tests/artifacts/v21/<run_id>/cleanup_report.json
```

`docs/current_phase.md` 必须记录：

```text
Phase: V21 MCP Self-Bootstrap v1：按目标搜索、评估、测试、启用 MCP
Status: completed
Implemented: ...
Not Implemented: ...
Observable Result: ...
Tests Run: ...
Cleanup Result: ...
Known Gaps: ...
Next Phase: V22 Schema Migration：Expand-Contract 与 A/B 兼容
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

