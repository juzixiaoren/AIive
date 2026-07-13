# AIive project_plan_v12.md：Self-Dev Patch Proposal：只生成补丁计划

> 上位文档：`final_project_plan_v7.md`  
> 编码约束：`编码规范.md`  
> 阶段编号：V12  
> 本阶段定位：尽早验证“自己给自己加能力/改能力”的规划能力，同时不引入代码破坏风险。

---

## 1. 从上一阶段到本阶段的过渡

从 V11 到 V12：有了 A/B 外壳，但还不能改代码。本阶段只让 LLM 读需求并生成 patch proposal，作为可解释计划，不应用。

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

用户说“给你加一个 X 能力”，AIive 能生成将修改哪些文件、怎样测试的计划。

AIive 的差异化要求：本阶段实现不能退化成普通 chatbot / 普通 RAG / 普通工具调用器。所有新增能力都必须服务于长期个人管家目标：理解用户、维护节奏、维护软件身体、给自己加能力。

---

## 3. 必须实现

- 创建 selfdev_requests、patch_operations proposal 结构。
- 实现 selfdev/planner.py。
- LLM 输出必须是结构化 patch plan。
- Self-Dev Dashboard v0 或 API 展示 proposal。
- proposal 记录 source_event_id 和 trace_id。


---

## 4. 明确不得实现

- 应用 patch。
- 修改 inactive slot。
- 修改 active slot。
- 运行测试。


---

## 5. 后端实现细节

- PatchPlan 包含 target_files、reason、operations_summary、test_plan、risk_notes、requires_schema_change。
- 若涉及删除，必须声明 safe_delete scope。


---

## 6. 前端实现细节

- Self-Dev Dashboard 展示 patch plan。


---

## 7. 数据库 / 存储变更

- selfdev_requests
- patch_operations


---

## 8. API 契约

- POST /api/selfdev/plan
- GET /api/selfdev/requests/{id}


---

## 9. 可观测结果

用户可验证：

```text
用户需求能生成 patch proposal。
```

AI 可验证：

```text
pytest tests/unit/backend/test_selfdev_planner.py -q；python scripts/smoke/smoke_selfdev_plan.py
```

---

## 10. 验收标准 Done Definition

- 用户需求能生成 patch proposal。
- proposal 可解释、可追溯。
- 没有任何代码文件被修改。
- 涉及未来阶段的操作被标记 not_allowed_yet。


---

## 11. 测试要求

只允许运行本阶段相关测试：

- pytest tests/unit/backend/test_selfdev_planner.py -q


Smoke 脚本：

- python scripts/smoke/smoke_selfdev_plan.py


禁止默认运行：

```bash
pytest
pytest tests
npm test
pnpm test --all
```

---

## 12. 测试清理要求

- selfdev 测试请求带 test_run_id 自动清理。


所有清理必须由测试脚本、fixture 或 cleanup helper 自动完成，不允许编码 AI 测试后手动删除。

---

## 13. 阶段完成后必须更新

```text
docs/current_phase.md
tests/artifacts/v12/<run_id>/summary.json
tests/artifacts/v12/<run_id>/commands.txt
tests/artifacts/v12/<run_id>/cleanup_report.json
```

`docs/current_phase.md` 必须记录：

```text
Phase: V12 Self-Dev Patch Proposal：只生成补丁计划
Status: completed
Implemented: ...
Not Implemented: ...
Observable Result: ...
Tests Run: ...
Cleanup Result: ...
Known Gaps: ...
Next Phase: V13 Inactive Slot Patch、定向测试与 Promote/Rollback
```


