# AIive project_plan_v19.md：Proactive Task：Reminder、Routine、Condition Watch

> 上位文档：`final_project_plan_v7.md`  
> 编码约束：`编码规范.md`  
> 阶段编号：V19  
> 本阶段定位：让 AIive 开始维护用户生活节奏，但先以低风险提醒和条件检查为主。

---

## 1. 从上一阶段到本阶段的过渡

从 V18 到 V19：AIive 已有个人信号、记忆和 outbox，本阶段才引入主动任务。主动任务不是独立复杂平台，而是 tasks 表 + outbox worker。

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

用户可以说“明早提醒我带伞”，到时间后 AIive 产生本地 notification event，不外发。

AIive 的差异化要求：本阶段实现不能退化成普通 chatbot / 普通 RAG / 普通工具调用器。所有新增能力都必须服务于长期个人管家目标：理解用户、维护节奏、维护软件身体、给自己加能力。

---

## 3. 必须实现

- 创建 tasks 表。
- 实现 reminder、routine、condition_watch 三类最小任务。
- next_check_at、status、last_checked_at。
- worker 到期触发 notification event。
- 发消息/邮件/下单仍需 confirmation。


---

## 4. 明确不得实现

- 位置触发。
- 语音打断。
- 自动对外发消息。
- 复杂日历双向同步。


---

## 5. 后端实现细节

- Condition evaluator 先支持手动 check 和简单本地条件。
- 低置信任务不主动打扰，进入 suggestion。
- 任务创建必须关联 source_event_id。


---

## 6. 前端实现细节

- Tasks 页面或 Chat 中显示 pending reminder。


---

## 7. 数据库 / 存储变更

- tasks
- notifications 可用 events 表表示


---

## 8. API 契约

- POST /api/tasks
- GET /api/tasks
- POST /api/tasks/{id}/check-now


---

## 9. 可观测结果

用户可验证：

```text
能创建 reminder。
```

AI 可验证：

```text
pytest tests/unit/backend/test_tasks.py -q；pytest tests/unit/backend/test_condition_watch.py -q；python scripts/smoke/smoke_reminder_task.py
```

---

## 10. 验收标准 Done Definition

- 能创建 reminder。
- 到期产生 notification event。
- condition 不满足不打扰。
- 外发动作仍需确认。


---

## 11. 测试要求

只允许运行本阶段相关测试：

- pytest tests/unit/backend/test_tasks.py -q
- pytest tests/unit/backend/test_condition_watch.py -q


Smoke 脚本：

- python scripts/smoke/smoke_reminder_task.py


禁止默认运行：

```bash
pytest
pytest tests
npm test
pnpm test --all
```

---

## 12. 测试清理要求

- 任务测试使用短时间 test task，执行后自动删除。


所有清理必须由测试脚本、fixture 或 cleanup helper 自动完成，不允许编码 AI 测试后手动删除。

---

## 13. 阶段完成后必须更新

```text
docs/current_phase.md
tests/artifacts/v19/<run_id>/summary.json
tests/artifacts/v19/<run_id>/commands.txt
tests/artifacts/v19/<run_id>/cleanup_report.json
```

`docs/current_phase.md` 必须记录：

```text
Phase: V19 Proactive Task：Reminder、Routine、Condition Watch
Status: completed
Implemented: ...
Not Implemented: ...
Observable Result: ...
Tests Run: ...
Cleanup Result: ...
Known Gaps: ...
Next Phase: V20 Personal Rhythm Manager：生活节奏维护与软话题切换
```


