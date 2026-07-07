# AIive project_plan_v3.md：PostgreSQL、Event Log、Trace 与 Thread State

> 上位文档：`final_project_plan_v7.md`  
> 编码约束：`编码规范.md`  
> 阶段编号：V3  
> 本阶段定位：把一次对话变成可追溯事件，为记忆、上下文、self-dev、维护奠定证据层。

---

## 1. 从上一阶段到本阶段的过渡

从 V2 到 V3：保留 Chat API / Chat Page 行为，新增 PostgreSQL、Alembic、事件日志和持久 thread。API 返回结构不破坏。

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

用户可以多轮对话，后端能保存事件和 trace，后续可解释。

AIive 的差异化要求：本阶段实现不能退化成普通 chatbot / 普通 RAG / 普通工具调用器。所有新增能力都必须服务于长期个人管家目标：理解用户、维护节奏、维护软件身体、给自己加能力。

---

## 3. 必须实现

- docker-compose.yml 接入 PostgreSQL 16+。
- 初始化 Alembic。
- 创建 threads、events、llm_calls 表。
- 实现 event_logger.py 和 trace.py。
- Chat 写入 chat_started/user_message/llm_response events。
- Chat 支持 thread_id 持久续聊。


---

## 4. 明确不得实现

- memory_records。
- outbox worker。
- Qdrant。
- 复杂 Context Inspector UI。


---

## 5. 后端实现细节

- 事件必须包含 event_id、trace_id、thread_id、event_type、payload、created_at。
- llm_calls 记录 model、latency_ms、input_preview/output_preview 或 object ref 占位。
- message reconstruction 可从 events 读取最近消息。


---

## 6. 前端实现细节

- Chat Page 保留当前 thread_id。
- 可显示“当前线程”。


---

## 7. 数据库 / 存储变更

- threads
- events
- llm_calls


---

## 8. API 契约

- POST /api/chat 兼容 V2。
- GET /api/debug/events?trace_id=... 可选 debug API。


---

## 9. 可观测结果

用户可验证：

```text
第二轮能引用第一轮内容。
```

AI 可验证：

```text
pytest tests/unit/backend/test_event_logger.py -q；pytest tests/unit/backend/test_thread_state.py -q；python scripts/smoke/smoke_chat_api.py
```

---

## 10. 验收标准 Done Definition

- 第二轮能引用第一轮内容。
- events 表可看到 user_message/llm_response。
- llm_calls 有 trace_id。
- 测试结束无测试 DB 残留。


---

## 11. 测试要求

只允许运行本阶段相关测试：

- pytest tests/unit/backend/test_event_logger.py -q
- pytest tests/unit/backend/test_thread_state.py -q


Smoke 脚本：

- python scripts/smoke/smoke_chat_api.py


禁止默认运行：

```bash
pytest
pytest tests
npm test
pnpm test --all
```

---

## 12. 测试清理要求

- 数据库测试使用 transaction rollback 或 test_run_id fixture。
- 测试写入 events 必须自动删除。


所有清理必须由测试脚本、fixture 或 cleanup helper 自动完成，不允许编码 AI 测试后手动删除。

---

## 13. 阶段完成后必须更新

```text
docs/current_phase.md
tests/artifacts/v3/<run_id>/summary.json
tests/artifacts/v3/<run_id>/commands.txt
tests/artifacts/v3/<run_id>/cleanup_report.json
```

`docs/current_phase.md` 必须记录：

```text
Phase: V3 PostgreSQL、Event Log、Trace 与 Thread State
Status: completed
Implemented: ...
Not Implemented: ...
Observable Result: ...
Tests Run: ...
Cleanup Result: ...
Known Gaps: ...
Next Phase: V4 Context Assembly v0 与 Context Snapshot
```


