# AIive project_plan_v20.md：Personal Rhythm Manager：生活节奏维护与软话题切换

> 上位文档：`final_project_plan_v7.md`  
> 编码约束：`编码规范.md`  
> 阶段编号：V20  
> 本阶段定位：让 AIive 不只是提醒器，而是逐步理解用户一天/一周的节奏，并在合适时机低打扰地维护。

---

## 1. 从上一阶段到本阶段的过渡

从 V19 到 V20：已有主动任务，本阶段把 routine、attention、recent topics、短时间话题切换合起来，形成个人管家的节奏感。

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

用户短时间在代码和论文之间切换，AIive 不硬切上下文；半天后新目标才倾向新边界。

AIive 的差异化要求：本阶段实现不能退化成普通 chatbot / 普通 RAG / 普通工具调用器。所有新增能力都必须服务于长期个人管家目标：理解用户、维护节奏、维护软件身体、给自己加能力。

---

## 3. 必须实现

- 创建 attention_states 表。
- 实现 attention_manager.py：continue/switch/suspend/resume soft decision。
- 实现 rhythm_manager.py：daily/weekly routine summary。
- 短时间话题切换保留同一 conversation working set。
- 长时间间隔 + 新目标时创建新 focus/thread 建议。


---

## 4. 明确不得实现

- 强制切换会话。
- 自动删除旧上下文。
- 高打扰主动建议。


---

## 5. 后端实现细节

- soft switch 只影响 Context Builder 的 working set 权重，不硬丢近期内容。
- 半天阈值配置化。
- 主动建议分 silent/badge/notification，不默认 voice/urgent。


---

## 6. 前端实现细节

- Chat Page 显示 current_focus/recent_topics 可选。
- Personal Rhythm 页面简单展示 routine summary。


---

## 7. 数据库 / 存储变更

- attention_states
- working_summaries 可选


---

## 8. API 契约

- GET /api/attention/current
- POST /api/attention/recompute


---

## 9. 可观测结果

用户可验证：

```text
短时间代码->论文->代码能保留连续性。
```

AI 可验证：

```text
pytest tests/unit/backend/test_attention_manager.py -q；pytest tests/unit/backend/test_rhythm_manager.py -q；python scripts/smoke/smoke_soft_topic_switch.py
```

---

## 10. 验收标准 Done Definition

- 短时间代码->论文->代码能保留连续性。
- 长时间新话题倾向新 focus。
- Context Snapshot 展示 attention decision。
- 生活节奏 summary 可见。


---

## 11. 测试要求

只允许运行本阶段相关测试：

- pytest tests/unit/backend/test_attention_manager.py -q
- pytest tests/unit/backend/test_rhythm_manager.py -q


Smoke 脚本：

- python scripts/smoke/smoke_soft_topic_switch.py


禁止默认运行：

```bash
pytest
pytest tests
npm test
pnpm test --all
```

---

## 12. 测试清理要求

- 测试 attention/thread 数据自动清理。


所有清理必须由测试脚本、fixture 或 cleanup helper 自动完成，不允许编码 AI 测试后手动删除。

---

## 13. 阶段完成后必须更新

```text
docs/current_phase.md
tests/artifacts/v20/<run_id>/summary.json
tests/artifacts/v20/<run_id>/commands.txt
tests/artifacts/v20/<run_id>/cleanup_report.json
```

`docs/current_phase.md` 必须记录：

```text
Phase: V20 Personal Rhythm Manager：生活节奏维护与软话题切换
Status: completed
Implemented: ...
Not Implemented: ...
Observable Result: ...
Tests Run: ...
Cleanup Result: ...
Known Gaps: ...
Next Phase: V21 MCP Self-Bootstrap v1：按目标搜索、评估、测试、启用 MCP
```


