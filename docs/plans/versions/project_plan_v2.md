# AIive project_plan_v2.md：Chat API 与 React Chat Page 最小闭环

> 上位文档：`final_project_plan_v7.md`  
> 编码约束：`编码规范.md`  
> 阶段编号：V2  
> 本阶段定位：让用户通过浏览器和真实 LLM 对话，建立后续所有能力的交互入口。

---

## 1. 从上一阶段到本阶段的过渡

从 V1 到 V2：复用 LLMClient，新增最小 Agent Loop、后端 Chat API 和前端 Chat Page。仍不接数据库，不做长期记忆。

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

用户能在本地网页输入消息、收到真实模型回复，并看到 thread_id / trace_id。

AIive 的差异化要求：本阶段实现不能退化成普通 chatbot / 普通 RAG / 普通工具调用器。所有新增能力都必须服务于长期个人管家目标：理解用户、维护节奏、维护软件身体、给自己加能力。

---

## 3. 必须实现

- 实现 POST /api/chat。
- 实现 runtime/agent_loop.py 的最小单轮调用。
- 实现 frontend/src/pages/ChatPage.tsx 和 src/api/chat.ts。
- Chat Page 展示用户消息、Agent 回复、thread_id、trace_id。
- 新增 scripts/smoke/smoke_chat_api.py。


---

## 4. 明确不得实现

- 长期记忆。
- 工具调用。
- 数据库事件日志。
- 复杂状态管理。


---

## 5. 后端实现细节

- ChatRequest：message、thread_id。
- ChatResponse：reply、thread_id、trace_id。
- thread_id 暂时可由 uuid 生成，不持久化。


---

## 6. 前端实现细节

- 输入框、发送按钮、消息列表、trace_id 小字展示。
- 失败时展示错误，不吞掉异常。


---

## 7. 数据库 / 存储变更

- 不接数据库。


---

## 8. API 契约

- POST /api/chat。


---

## 9. 可观测结果

用户可验证：

```text
Chat API 返回真实回复。
```

AI 可验证：

```text
pytest tests/unit/backend/test_chat_api.py -q；python scripts/smoke/smoke_chat_api.py；手动打开 Chat Page 发送一句话
```

---

## 10. 验收标准 Done Definition

- Chat API 返回真实回复。
- Chat Page 可完成一轮真实对话。
- 单元测试不调用真实 LLM。
- V2 完成后 AIive 已有真实交互入口。


---

## 11. 测试要求

只允许运行本阶段相关测试：

- pytest tests/unit/backend/test_chat_api.py -q


Smoke 脚本：

- python scripts/smoke/smoke_chat_api.py
- 手动打开 Chat Page 发送一句话


禁止默认运行：

```bash
pytest
pytest tests
npm test
pnpm test --all
```

---

## 12. 测试清理要求

- API 单测 mock LLM。
- 前端如产生构建缓存，不纳入 tests/artifacts。


所有清理必须由测试脚本、fixture 或 cleanup helper 自动完成，不允许编码 AI 测试后手动删除。

---

## 13. 阶段完成后必须更新

```text
docs/current_phase.md
tests/artifacts/v2/<run_id>/summary.json
tests/artifacts/v2/<run_id>/commands.txt
tests/artifacts/v2/<run_id>/cleanup_report.json
```

`docs/current_phase.md` 必须记录：

```text
Phase: V2 Chat API 与 React Chat Page 最小闭环
Status: completed
Implemented: ...
Not Implemented: ...
Observable Result: ...
Tests Run: ...
Cleanup Result: ...
Known Gaps: ...
Next Phase: V3 PostgreSQL、Event Log、Trace 与 Thread State
```


