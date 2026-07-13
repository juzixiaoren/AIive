# AIive project_plan_v24.md：Temporal KG 与高级长期关系记忆

> 上位文档：`final_project_plan_v7.md`  
> 编码约束：`编码规范.md`  
> 阶段编号：V24  
> 本阶段定位：增强长期记忆的关系、时间有效性、冲突和覆盖能力。

---

## 1. 从上一阶段到本阶段的过渡

从 V23 到 V24：memory_records 生命周期已稳定，self-dev lessons 也可沉淀，本阶段才引入 Temporal KG 作为派生关系推理层。

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

用户偏好变化时，AIive 能理解“旧说法已被新说法覆盖”，而不是同时混用。

AIive 的差异化要求：本阶段实现不能退化成普通 chatbot / 普通 RAG / 普通工具调用器。所有新增能力都必须服务于长期个人管家目标：理解用户、维护节奏、维护软件身体、给自己加能力。

---

## 3. 必须实现

- 实现 temporal_graph_adapter.py。
- 从 active memory_records 派生 entities/relations。
- 关系包含 valid_from、valid_to、confidence、source_memory_id。
- 冲突检测：new explicit user statement supersedes old。
- KG 可由 memory_records 重建。


---

## 4. 明确不得实现

- 让 KG 成为生命周期真相源。
- 跳过 memory_records 直接写长期事实。
- 复杂图可视化优先。


---

## 5. 后端实现细节

- memory_records -> KG projector。
- forget/superseded/sleep 状态同步到 KG。
- Context Builder 可使用 KG 返回关系摘要。


---

## 6. 前端实现细节

- Memory 页面可显示 related memories/relations 简版。


---

## 7. 数据库 / 存储变更

- graph_entities
- graph_relations


---

## 8. API 契约

- GET /api/memories/{id}/relations


---

## 9. 可观测结果

用户可验证：

```text
新偏好覆盖旧偏好。
```

AI 可验证：

```text
pytest tests/unit/backend/test_temporal_graph_adapter.py -q；pytest tests/unit/backend/test_memory_conflict_resolution.py -q；python scripts/smoke/smoke_memory_conflict_kg.py
```

---

## 10. 验收标准 Done Definition

- 新偏好覆盖旧偏好。
- KG 可重建。
- forget 后 KG 不再返回原关系。
- Context 注入冲突状态可解释。


---

## 11. 测试要求

只允许运行本阶段相关测试：

- pytest tests/unit/backend/test_temporal_graph_adapter.py -q
- pytest tests/unit/backend/test_memory_conflict_resolution.py -q


Smoke 脚本：

- python scripts/smoke/smoke_memory_conflict_kg.py


禁止默认运行：

```bash
pytest
pytest tests
npm test
pnpm test --all
```

---

## 12. 测试清理要求

- KG 测试关系带 test_run_id 自动清理。


所有清理必须由测试脚本、fixture 或 cleanup helper 自动完成，不允许编码 AI 测试后手动删除。

---

## 13. 阶段完成后必须更新

```text
docs/current_phase.md
tests/artifacts/v24/<run_id>/summary.json
tests/artifacts/v24/<run_id>/commands.txt
tests/artifacts/v24/<run_id>/cleanup_report.json
```

`docs/current_phase.md` 必须记录：

```text
Phase: V24 Temporal KG 与高级长期关系记忆
Status: completed
Implemented: ...
Not Implemented: ...
Observable Result: ...
Tests Run: ...
Cleanup Result: ...
Known Gaps: ...
Next Phase: V25 Final Convergence：终局缺口审计与下一轮自进化计划
```


