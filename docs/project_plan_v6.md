# AIive project_plan_v6.md：Personal Steward Signals：偏好、节奏与提醒雏形

> 上位文档：`final_project_plan_v7.md`  
> 编码约束：`编码规范.md`  
> 阶段编号：V6  
> 本阶段定位：建立 AIive 与普通 Agent 的产品差异：它不仅回答问题，还开始积累偏好、routine、注意力、节奏线索。

---

## 1. 从上一阶段到本阶段的过渡

从 V5 到 V6：已有长期记忆，本阶段不做复杂主动任务，而是先定义个人管家所需的信号结构，让 AIive 从“记事实”走向“理解用户生活节奏”。

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

用户可以告诉 AIive 自己的作息、提醒偏好、工作节奏，系统能结构化保存并在聊天中体现。

AIive 的差异化要求：本阶段实现不能退化成普通 chatbot / 普通 RAG / 普通工具调用器。所有新增能力都必须服务于长期个人管家目标：理解用户、维护节奏、维护软件身体、给自己加能力。

---

## 3. 必须实现

- 扩展 memory_type：user_profile、preference、routine、project、agent_self、policy、environment。
- 新增 personal_signals 表或使用 memory_records 类型化保存。
- 实现 steward_signal_extractor.py：从用户显式表达中识别 routine/preference。
- Context Builder 对 personal_signals 做轻量注入。
- Chat 中能回答“你记得我的工作节奏吗”。


---

## 4. 明确不得实现

- 真正定时主动通知。
- 复杂日历集成。
- 语音主动打断。
- 自动外发消息。


---

## 5. 后端实现细节

- routine 记忆必须含 schedule_text、confidence、source_event_id。
- 偏好记忆必须区分长期偏好和当前任务约束。
- 短时间话题切换不硬切 thread，只更新 attention hints。


---

## 6. 前端实现细节

- Chat Page 可展示“识别到个人节奏信号”。
- 简单 signals debug API 可见。


---

## 7. 数据库 / 存储变更

- memory_records 扩展类型或 personal_signals


---

## 8. API 契约

- GET /api/personal-signals 可选。


---

## 9. 可观测结果

用户可验证：

```text
明确 routine 能被保存。
```

AI 可验证：

```text
pytest tests/unit/backend/test_steward_signal_extractor.py -q；pytest tests/unit/backend/test_memory_types.py -q；python scripts/smoke/smoke_personal_signal.py
```

---

## 10. 验收标准 Done Definition

- 明确 routine 能被保存。
- 后续聊天能召回 routine。
- 不产生真实主动通知。
- Personal steward 方向有第一个可验证结果。


---

## 11. 测试要求

只允许运行本阶段相关测试：

- pytest tests/unit/backend/test_steward_signal_extractor.py -q
- pytest tests/unit/backend/test_memory_types.py -q


Smoke 脚本：

- python scripts/smoke/smoke_personal_signal.py


禁止默认运行：

```bash
pytest
pytest tests
npm test
pnpm test --all
```

---

## 12. 测试清理要求

- 测试 routine/preference 必须自动删除。


所有清理必须由测试脚本、fixture 或 cleanup helper 自动完成，不允许编码 AI 测试后手动删除。

---

## 13. 阶段完成后必须更新

```text
docs/current_phase.md
tests/artifacts/v6/<run_id>/summary.json
tests/artifacts/v6/<run_id>/commands.txt
tests/artifacts/v6/<run_id>/cleanup_report.json
```

`docs/current_phase.md` 必须记录：

```text
Phase: V6 Personal Steward Signals：偏好、节奏与提醒雏形
Status: completed
Implemented: ...
Not Implemented: ...
Observable Result: ...
Tests Run: ...
Cleanup Result: ...
Known Gaps: ...
Next Phase: V7 Tool Registry、Capability Safety 与 Permission Manager
```


