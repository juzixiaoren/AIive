# AIive project_plan_v13.md：Inactive Slot Patch、定向测试与 Promote/Rollback

> 上位文档：`final_project_plan_v7.md`  
> 编码约束：`编码规范.md`  
> 阶段编号：V13  
> 本阶段定位：验证 AIive 能维护自己的软件身体：不改 active，先改 inactive，失败回滚，成功切换。

---

## 1. 从上一阶段到本阶段的过渡

从 V12 到 V13：已有 patch proposal，本阶段才允许复制 manifest 文件到 inactive slot、应用 patch、运行定向测试、健康检查后 promote。

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

用户可以让 AIive 做一个小功能改动，看到 B 测试、失败原因或成功切换。

AIive 的差异化要求：本阶段实现不能退化成普通 chatbot / 普通 RAG / 普通工具调用器。所有新增能力都必须服务于长期个人管家目标：理解用户、维护节奏、维护软件身体、给自己加能力。

---

## 3. 必须实现

- 实现 copy active manifest files -> inactive slot。
- 实现 patch_executor.py。
- 实现 targeted_test_runner.py。
- 实现 promote_candidate_to_active 和 rollback_to_previous。
- 测试报告写入 tests/artifacts/selfdev/<run_id>/。
- 失败报告进入 agent_self memory candidate。


---

## 4. 明确不得实现

- 运行全量 pytest。
- 直接修改 active slot。
- 跳过 health check promote。
- schema contract。


---

## 5. 后端实现细节

- 测试选择基于 changed_files -> test mapping。
- 没有映射时只运行阶段允许的最小 smoke/health，不全量回归。
- B 失败时 A 继续运行。


---

## 6. 前端实现细节

- Self-Dev Dashboard 显示 patch、test、promote、rollback 状态。


---

## 7. 数据库 / 存储变更

- candidates
- test_results
- repair_attempts 可选


---

## 8. API 契约

- POST /api/selfdev/{id}/apply-inactive
- POST /api/selfdev/{id}/promote
- POST /api/selfdev/{id}/rollback


---

## 9. 可观测结果

用户可验证：

```text
inactive slot 被修改，active 不变。
```

AI 可验证：

```text
pytest tests/unit/backend/test_patch_executor.py -q；pytest tests/unit/backend/test_targeted_test_runner.py -q；pytest tests/unit/backend/test_promote_rollback.py -q；python scripts/smoke/smoke_selfdev_inactive_patch.py
```

---

## 10. 验收标准 Done Definition

- inactive slot 被修改，active 不变。
- 只运行定向测试。
- B 健康才 promote。
- B 失败时 A 继续运行并记录失败原因。


---

## 11. 测试要求

只允许运行本阶段相关测试：

- pytest tests/unit/backend/test_patch_executor.py -q
- pytest tests/unit/backend/test_targeted_test_runner.py -q
- pytest tests/unit/backend/test_promote_rollback.py -q


Smoke 脚本：

- python scripts/smoke/smoke_selfdev_inactive_patch.py


禁止默认运行：

```bash
pytest
pytest tests
npm test
pnpm test --all
```

---

## 12. 测试清理要求

- slot 测试用临时 A/B 目录。
- 测试报告保留，临时 slot 自动删除。


所有清理必须由测试脚本、fixture 或 cleanup helper 自动完成，不允许编码 AI 测试后手动删除。

---

## 13. 阶段完成后必须更新

```text
docs/current_phase.md
tests/artifacts/v13/<run_id>/summary.json
tests/artifacts/v13/<run_id>/commands.txt
tests/artifacts/v13/<run_id>/cleanup_report.json
```

`docs/current_phase.md` 必须记录：

```text
Phase: V13 Inactive Slot Patch、定向测试与 Promote/Rollback
Status: completed
Implemented: ...
Not Implemented: ...
Observable Result: ...
Tests Run: ...
Cleanup Result: ...
Known Gaps: ...
Next Phase: V14 PostgreSQL Outbox Worker 与异步派生任务
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

