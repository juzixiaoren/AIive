# AIive project_plan_v5.md：Memory Records、抽取候选与召回注入

> 上位文档：`final_project_plan_v7.md`  
> 编码约束：`编码规范.md`  
> 阶段编号：V5  
> 本阶段定位：尽早让 AIive 具备“越来越懂用户”的基础能力：能记偏好、称呼、项目事实，并在后续对话召回。

---

## 1. 从上一阶段到本阶段的过渡

从 V4 到 V5：Context Builder 已有 Evidence Pack 插槽，本阶段将 memory_records 作为第一个长期证据来源。先做 PostgreSQL 真相源，不接 KG/Qdrant。

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

用户说“以后叫你大李”，之后 AIive 能记住并使用。

AIive 的差异化要求：本阶段实现不能退化成普通 chatbot / 普通 RAG / 普通工具调用器。所有新增能力都必须服务于长期个人管家目标：理解用户、维护节奏、维护软件身体、给自己加能力。

---

## 3. 必须实现

- 创建 memory_records 表。
- 字段包含 memory_id、memory_type、lifecycle_state、content、source_event_id、confidence、lineage、pinned、created_at、updated_at。
- 实现 memory_store.py。
- 实现 memory_extractor.py：LLM 抽取 candidate，单测用 fake LLM。
- 实现 memory_gate.py：明确“记住/以后/从现在起”可 active，弱推断保持 candidate。
- Context Builder Evidence Pack 注入 active memory。


---

## 4. 明确不得实现

- Temporal KG。
- Qdrant memory embedding。
- 复杂自动合并。
- 自动删除 memory。


---

## 5. 后端实现细节

- memory_records 是生命周期唯一真相源。
- KG/Qdrant/Markdown 未来都是派生视图。
- 抽取记忆必须保留 source_event_id。
- 普通闲聊不得乱写 active memory。


---

## 6. 前端实现细节

- Chat Page 可显示“本轮产生了 memory candidate/active”提示。
- Memory Dashboard 只做简单列表可选。


---

## 7. 数据库 / 存储变更

- memory_records


---

## 8. API 契约

- GET /api/memories
- POST /api/memories 手动写入可选


---

## 9. 可观测结果

用户可验证：

```text
明确偏好能进入 active。
```

AI 可验证：

```text
pytest tests/unit/backend/test_memory_store.py -q；pytest tests/unit/backend/test_memory_extractor.py -q；pytest tests/unit/backend/test_memory_gate.py -q；python scripts/smoke/smoke_memory_extraction.py
```

---

## 10. 验收标准 Done Definition

- 明确偏好能进入 active。
- active memory 可注入后续上下文。
- snapshot 可看到 injected_memory_ids。
- 普通闲聊不产生 active memory。


---

## 11. 测试要求

只允许运行本阶段相关测试：

- pytest tests/unit/backend/test_memory_store.py -q
- pytest tests/unit/backend/test_memory_extractor.py -q
- pytest tests/unit/backend/test_memory_gate.py -q


Smoke 脚本：

- python scripts/smoke/smoke_memory_extraction.py


禁止默认运行：

```bash
pytest
pytest tests
npm test
pnpm test --all
```

---

## 12. 测试清理要求

- memory 测试记录带 test_run_id 并自动删除。
- 真实 LLM smoke 使用测试 thread，不写真实长期用户偏好，或写入后自动 forget 测试记录。


所有清理必须由测试脚本、fixture 或 cleanup helper 自动完成，不允许编码 AI 测试后手动删除。

---

## 13. 阶段完成后必须更新

```text
docs/current_phase.md
tests/artifacts/v5/<run_id>/summary.json
tests/artifacts/v5/<run_id>/commands.txt
tests/artifacts/v5/<run_id>/cleanup_report.json
```

`docs/current_phase.md` 必须记录：

```text
Phase: V5 Memory Records、抽取候选与召回注入
Status: completed
Implemented: ...
Not Implemented: ...
Observable Result: ...
Tests Run: ...
Cleanup Result: ...
Known Gaps: ...
Next Phase: V6 Personal Steward Signals：偏好、节奏与提醒雏形
```


