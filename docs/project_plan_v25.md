# AIive project_plan_v25.md：Final Convergence：终局缺口审计与下一轮自进化计划

> 上位文档：`final_project_plan_v7.md`  
> 编码约束：`编码规范.md`  
> 阶段编号：V25  
> 本阶段定位：对照 final_project_plan_v7.md 检查终局目标完成度，形成下一轮自进化计划。

---

## 1. 从上一阶段到本阶段的过渡

从 V24 到 V25：核心闭环已覆盖真实交互、记忆、个人节奏、工具/MCP、自我修改、维护、KG。本阶段不新增大功能，专门做终局对照和缺口收敛。

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

用户能看到 AIive 目前完成了什么、哪些未完成、下一步它建议如何改自己。

AIive 的差异化要求：本阶段实现不能退化成普通 chatbot / 普通 RAG / 普通工具调用器。所有新增能力都必须服务于长期个人管家目标：理解用户、维护节奏、维护软件身体、给自己加能力。

---

## 3. 必须实现

- 生成 docs/final_gap_report.md。
- 实现 gap_checker.py：对照终局模块清单。
- 跑指定 smoke summary，不跑全量回归。
- 输出 implemented / partial / missing / risk / self_evolution_suggestions。
- 确保所有核心 trace 链路可查。


---

## 4. 明确不得实现

- 临时大重构。
- 为了完成报告而伪造实现状态。
- 运行全量 pytest。


---

## 5. 后端实现细节

- gap_checker 读取 capabilities、events、memory、selfdev、tasks、retrieval 状态。
- 每个 missing item 必须有 suggested_phase 或 selfdev_request proposal。


---

## 6. 前端实现细节

- Final Dashboard 可选；至少报告文件可读。


---

## 7. 数据库 / 存储变更

- 无新核心表。


---

## 8. API 契约

- GET /api/final-gap-report 可选。


---

## 9. 可观测结果

用户可验证：

```text
final_gap_report.md 生成。
```

AI 可验证：

```text
pytest tests/unit/backend/test_gap_checker.py -q；python scripts/smoke/smoke_final_observability.py
```

---

## 10. 验收标准 Done Definition

- final_gap_report.md 生成。
- 核心链路 user_message -> context -> llm_call -> retrieval/tool -> response -> memory/outbox 可追踪。
- 下一轮自进化建议可执行。
- 没有虚假完成项。


---

## 11. 测试要求

只允许运行本阶段相关测试：

- pytest tests/unit/backend/test_gap_checker.py -q


Smoke 脚本：

- python scripts/smoke/smoke_final_observability.py


禁止默认运行：

```bash
pytest
pytest tests
npm test
pnpm test --all
```

---

## 12. 测试清理要求

- 报告保留；smoke 临时数据自动清理。


所有清理必须由测试脚本、fixture 或 cleanup helper 自动完成，不允许编码 AI 测试后手动删除。

---

## 13. 阶段完成后必须更新

```text
docs/current_phase.md
tests/artifacts/v25/<run_id>/summary.json
tests/artifacts/v25/<run_id>/commands.txt
tests/artifacts/v25/<run_id>/cleanup_report.json
```

`docs/current_phase.md` 必须记录：

```text
Phase: V25 Final Convergence：终局缺口审计与下一轮自进化计划
Status: completed
Implemented: ...
Not Implemented: ...
Observable Result: ...
Tests Run: ...
Cleanup Result: ...
Known Gaps: ...
Next Phase: 无，进入下一轮自进化规划
```


