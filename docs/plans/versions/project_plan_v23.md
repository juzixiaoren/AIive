# AIive project_plan_v23.md：Self-Evolution Loop：失败复盘、Lesson Memory 与再次修复

> 上位文档：`final_project_plan_v7.md`  
> 编码约束：`编码规范.md`  
> 阶段编号：V23  
> 本阶段定位：让 AIive 不只是能改自己，还能维护自己的工程经验，避免重复犯错。

---

## 1. 从上一阶段到本阶段的过渡

从 V22 到 V23：有了 A/B patch、MCP 自举和 schema 安全，本阶段把失败反馈接回 memory，让 AIive 能从自己升级失败中学习。

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

当 B 版本失败时，A 能解释失败原因、记录 lesson，并生成下一轮修复计划。

AIive 的差异化要求：本阶段实现不能退化成普通 chatbot / 普通 RAG / 普通工具调用器。所有新增能力都必须服务于长期个人管家目标：理解用户、维护节奏、维护软件身体、给自己加能力。

---

## 3. 必须实现

- 实现 self_repair_loop.py。
- 失败 test_result -> agent_self memory candidate。
- 下一轮 selfdev planner 必须召回相关 lesson。
- Capability GC：低使用/高失败能力进入 dormant。
- Self-Dev Dashboard 展示 repair attempts。


---

## 4. 明确不得实现

- 无限自动循环修复。
- 失败后继续 promote。
- 自动删除 active capability。


---

## 5. 后端实现细节

- repair_attempts 有 max_attempts 配置。
- lesson memory 类型为 agent_self，默认不自动 hard delete。
- 失败原因包含 changed_files、test_command、error_summary。


---

## 6. 前端实现细节

- Self-Dev Dashboard 展示 failed -> lesson -> next plan。


---

## 7. 数据库 / 存储变更

- repair_attempts
- capability_lifecycle_events


---

## 8. API 契约

- POST /api/selfdev/{id}/repair-plan
- POST /api/capabilities/{id}/sleep


---

## 9. 可观测结果

用户可验证：

```text
失败产生 lesson candidate。
```

AI 可验证：

```text
pytest tests/unit/backend/test_self_repair_loop.py -q；pytest tests/unit/backend/test_capability_gc.py -q；python scripts/smoke/smoke_selfdev_failure_lesson.py
```

---

## 10. 验收标准 Done Definition

- 失败产生 lesson candidate。
- 下一轮 planner 召回 lesson。
- 不会无限循环。
- 高失败 capability 可 dormant 而非删除。


---

## 11. 测试要求

只允许运行本阶段相关测试：

- pytest tests/unit/backend/test_self_repair_loop.py -q
- pytest tests/unit/backend/test_capability_gc.py -q


Smoke 脚本：

- python scripts/smoke/smoke_selfdev_failure_lesson.py


禁止默认运行：

```bash
pytest
pytest tests
npm test
pnpm test --all
```

---

## 12. 测试清理要求

- 失败样本使用测试 slot，测试 lesson 自动清理或标记 test。


所有清理必须由测试脚本、fixture 或 cleanup helper 自动完成，不允许编码 AI 测试后手动删除。

---

## 13. 阶段完成后必须更新

```text
docs/current_phase.md
tests/artifacts/v23/<run_id>/summary.json
tests/artifacts/v23/<run_id>/commands.txt
tests/artifacts/v23/<run_id>/cleanup_report.json
```

`docs/current_phase.md` 必须记录：

```text
Phase: V23 Self-Evolution Loop：失败复盘、Lesson Memory 与再次修复
Status: completed
Implemented: ...
Not Implemented: ...
Observable Result: ...
Tests Run: ...
Cleanup Result: ...
Known Gaps: ...
Next Phase: V24 Temporal KG 与高级长期关系记忆
```


