# AIive project_plan_v18.md：Forget、Memory Maintenance 与 Projection 同步

> 上位文档：`final_project_plan_v7.md`  
> 编码约束：`编码规范.md`  
> 阶段编号：V18  
> 本阶段定位：让 AIive 不是 append-only，开始具备代谢能力：忘记、sleep、archive、维护。

---

## 1. 从上一阶段到本阶段的过渡

从 V17 到 V18：用户已经能看到记忆和上下文，本阶段允许用户要求忘记和整理。删除自由但走语义 forget、tombstone、projection 同步，不直接乱 hard delete。

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

用户可以让 AIive 忘记某条记忆，之后不再召回；也能看到维护报告。

AIive 的差异化要求：本阶段实现不能退化成普通 chatbot / 普通 RAG / 普通工具调用器。所有新增能力都必须服务于长期个人管家目标：理解用户、维护节奏、维护软件身体、给自己加能力。

---

## 3. 必须实现

- 创建 forget_requests、maintenance_plans、maintenance_actions 表。
- 实现 POST /api/memories/{id}/forget。
- forgotten memory 不再检索/注入。
- 写 forget tombstone event。
- 实现 manual memory scan：sleep/archive 候选。
- 生成 Markdown/JSON projection v0。


---

## 4. 明确不得实现

- 自动 hard delete 用户长期偏好。
- 复杂 Saga UI。
- 大规模自动清理。


---

## 5. 后端实现细节

- memory_records lifecycle_state 为准。
- pinned memory 不参与 auto sleep。
- forget 后 Qdrant 派生索引创建同步 job；失败标记 partially_completed。


---

## 6. 前端实现细节

- Memory 页面支持 forget/sleep/archive 手动按钮。
- Maintenance report 简单展示。


---

## 7. 数据库 / 存储变更

- forget_requests
- maintenance_plans
- maintenance_actions
- operation_transactions 最小版


---

## 8. API 契约

- POST /api/memories/{id}/forget
- POST /api/maintenance/memory-scan


---

## 9. 可观测结果

用户可验证：

```text
forgotten memory 不再注入。
```

AI 可验证：

```text
pytest tests/unit/backend/test_forget_memory.py -q；pytest tests/unit/backend/test_memory_maintenance.py -q；python scripts/smoke/smoke_forget_memory.py
```

---

## 10. 验收标准 Done Definition

- forgotten memory 不再注入。
- tombstone 可见且不含原文。
- maintenance report 可见。
- pinned memory 不被 sleep。


---

## 11. 测试要求

只允许运行本阶段相关测试：

- pytest tests/unit/backend/test_forget_memory.py -q
- pytest tests/unit/backend/test_memory_maintenance.py -q


Smoke 脚本：

- python scripts/smoke/smoke_forget_memory.py


禁止默认运行：

```bash
pytest
pytest tests
npm test
pnpm test --all
```

---

## 12. 测试清理要求

- forget 测试只对测试 memory 执行。
- projection 测试写入 test prefix 并自动清理。


所有清理必须由测试脚本、fixture 或 cleanup helper 自动完成，不允许编码 AI 测试后手动删除。

---

## 13. 阶段完成后必须更新

```text
docs/current_phase.md
tests/artifacts/v18/<run_id>/summary.json
tests/artifacts/v18/<run_id>/commands.txt
tests/artifacts/v18/<run_id>/cleanup_report.json
```

`docs/current_phase.md` 必须记录：

```text
Phase: V18 Forget、Memory Maintenance 与 Projection 同步
Status: completed
Implemented: ...
Not Implemented: ...
Observable Result: ...
Tests Run: ...
Cleanup Result: ...
Known Gaps: ...
Next Phase: V19 Proactive Task：Reminder、Routine、Condition Watch
```


