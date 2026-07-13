# AIive project_plan_v17.md：可解释性 UI：Events、Context、Retrieval、Tools

> 上位文档：`final_project_plan_v7.md`  
> 编码约束：`编码规范.md`  
> 阶段编号：V17  
> 本阶段定位：让用户能看懂 AIive 为什么这么答、记了什么、检索了什么、用了什么工具。

---

## 1. 从上一阶段到本阶段的过渡

从 V16 到 V17：后端已有 events、context snapshots、retrieval runs、tools 和 capabilities，本阶段把可解释性集中做成 UI。

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

用户能从 Chat Page 点击 trace_id，进入 Event Timeline / Context Inspector / Retrieval Inspector。

AIive 的差异化要求：本阶段实现不能退化成普通 chatbot / 普通 RAG / 普通工具调用器。所有新增能力都必须服务于长期个人管家目标：理解用户、维护节奏、维护软件身体、给自己加能力。

---

## 3. 必须实现

- 实现 EventTimeline.tsx。
- 实现 ContextInspector.tsx。
- 实现 RetrievalInspector.tsx。
- 实现 Tools/Capabilities 页面基础版。
- Chat Page trace_id 链接到对应 inspector。
- 后端提供 GET /api/events、/api/context-runs/{trace_id}、/api/retrieval-runs/{id}。


---

## 4. 明确不得实现

- 复杂图可视化。
- 编辑 memory 高级功能。
- 主动任务 UI。


---

## 5. 后端实现细节

- 所有 inspector API 返回结构化 JSON。
- 敏感字段保留 sensitivity 标记。


---

## 6. 前端实现细节

- 页面先可读，不追求复杂美观。
- 错误和空状态必须明确显示。


---

## 7. 数据库 / 存储变更

- 无新核心表，复用已有。


---

## 8. API 契约

- GET /api/events
- GET /api/context-runs/{trace_id}
- GET /api/retrieval-runs/{run_id}
- GET /api/tools


---

## 9. 可观测结果

用户可验证：

```text
事件流可见。
```

AI 可验证：

```text
pytest tests/unit/backend/test_inspector_api.py -q；python scripts/smoke/smoke_trace_inspector.py；手动验证 Chat -> Context Inspector 跳转
```

---

## 10. 验收标准 Done Definition

- 事件流可见。
- 上下文注入可见。
- 检索候选可见。
- 工具安全声明可见。


---

## 11. 测试要求

只允许运行本阶段相关测试：

- pytest tests/unit/backend/test_inspector_api.py -q


Smoke 脚本：

- python scripts/smoke/smoke_trace_inspector.py
- 手动验证 Chat -> Context Inspector 跳转


禁止默认运行：

```bash
pytest
pytest tests
npm test
pnpm test --all
```

---

## 12. 测试清理要求

- UI smoke 不写数据库；如写测试 trace，带 test_run_id 清理。


所有清理必须由测试脚本、fixture 或 cleanup helper 自动完成，不允许编码 AI 测试后手动删除。

---

## 13. 阶段完成后必须更新

```text
docs/current_phase.md
tests/artifacts/v17/<run_id>/summary.json
tests/artifacts/v17/<run_id>/commands.txt
tests/artifacts/v17/<run_id>/cleanup_report.json
```

`docs/current_phase.md` 必须记录：

```text
Phase: V17 可解释性 UI：Events、Context、Retrieval、Tools
Status: completed
Implemented: ...
Not Implemented: ...
Observable Result: ...
Tests Run: ...
Cleanup Result: ...
Known Gaps: ...
Next Phase: V18 Forget、Memory Maintenance 与 Projection 同步
```


