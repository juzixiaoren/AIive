# AIive project_plan_v22.md：Schema Migration：Expand-Contract 与 A/B 兼容

> 上位文档：`final_project_plan_v7.md`  
> 编码约束：`编码规范.md`  
> 阶段编号：V22  
> 本阶段定位：避免 B 版本迁移数据库后 A 无法回滚，是 A/B 自我进化安全性的关键。

---

## 1. 从上一阶段到本阶段的过渡

从 V21 到 V22：AIive 已能自举工具和修改 inactive slot；若未来 self-dev 修改数据库，必须先有 schema 安全机制。

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

用户能看到一次 schema 变更计划是否兼容 A/B，失败时不会破坏当前版本。

AIive 的差异化要求：本阶段实现不能退化成普通 chatbot / 普通 RAG / 普通工具调用器。所有新增能力都必须服务于长期个人管家目标：理解用户、维护节奏、维护软件身体、给自己加能力。

---

## 3. 必须实现

- 创建 schema_version 表。
- 实现 migration_planner.py。
- 支持 migration dry run。
- 标记 migration phase：expand/contract。
- self-dev 涉及 schema 时必须生成 compatibility note。
- contract 不允许和同次 promote 合并执行。


---

## 4. 明确不得实现

- B 直接删除 A 依赖字段。
- 一次 migration 同时 expand+contract。
- 对生产库做不可逆测试。


---

## 5. 后端实现细节

- expand 后 A 仍可运行。
- contract 只能在单独维护阶段、确认旧版本不依赖后执行。
- migration 测试使用临时数据库或 dry run。


---

## 6. 前端实现细节

- Self-Dev Dashboard 展示 schema compatibility。


---

## 7. 数据库 / 存储变更

- schema_version
- migration_plans


---

## 8. API 契约

- POST /api/schema/dry-run
- GET /api/schema/version


---

## 9. 可观测结果

用户可验证：

```text
expand-only 变更可通过。
```

AI 可验证：

```text
pytest tests/unit/backend/test_migration_planner.py -q；pytest tests/unit/backend/test_expand_contract_policy.py -q；python scripts/smoke/smoke_migration_dry_run.py
```

---

## 10. 验收标准 Done Definition

- expand-only 变更可通过。
- contract 同阶段被拒绝。
- A/B 兼容性失败禁止 promote。
- 生产库不被测试污染。


---

## 11. 测试要求

只允许运行本阶段相关测试：

- pytest tests/unit/backend/test_migration_planner.py -q
- pytest tests/unit/backend/test_expand_contract_policy.py -q


Smoke 脚本：

- python scripts/smoke/smoke_migration_dry_run.py


禁止默认运行：

```bash
pytest
pytest tests
npm test
pnpm test --all
```

---

## 12. 测试清理要求

- migration 测试使用临时 DB，测试结束销毁。


所有清理必须由测试脚本、fixture 或 cleanup helper 自动完成，不允许编码 AI 测试后手动删除。

---

## 13. 阶段完成后必须更新

```text
docs/current_phase.md
tests/artifacts/v22/<run_id>/summary.json
tests/artifacts/v22/<run_id>/commands.txt
tests/artifacts/v22/<run_id>/cleanup_report.json
```

`docs/current_phase.md` 必须记录：

```text
Phase: V22 Schema Migration：Expand-Contract 与 A/B 兼容
Status: completed
Implemented: ...
Not Implemented: ...
Observable Result: ...
Tests Run: ...
Cleanup Result: ...
Known Gaps: ...
Next Phase: V23 Self-Evolution Loop：失败复盘、Lesson Memory 与再次修复
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

