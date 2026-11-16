# Reminder 与共享聊天契约收尾审查

日期：2026-11-17。基线：main `6c7a6ba`，实施前工作区干净；远端 fetch 确认同一基线。

## 现有问题确认与行为变化

- P0：原 `TaskManager.cancel()` 虽然只迁移 pending/dispatching Task，却无条件改写关联 reminder_created Event，completed/failed 也被标成 cancelled；cancelled 重试还重复改 Event。工具 handler 丢失真实状态并在 ok 时固定返回 cancelled=true。现按锁定后的状态先判断：pending/dispatching 在调用方事务中迁移 Task、取消未领取的 pending Outbox，并将关联 Event 标成 cancelled/dismissed；completed/failed 返回失败且三表不变；cancelled 返回幂等成功且 changed=false、events_cancelled=0；不存在返回 not_found。所有分支均返回 ok/task_cancelled/changed/status/events_cancelled。
- DELETE 通知保持独立的隐藏结果：终态任务不能取消，但指定 Event 可以 dismissed=true，Event 原状态及其他关联 Event 不变，不误报取消。隐藏赋值移至任务取消之后，并从最新 payload 合并，避免覆盖取消刚写入的事件状态，也避免先写 Event 再领取 Task 锁。
- P1：安全描述原先只在 LangChain Adapter 追加，ContextAssembler 的 Function Definition 缺少相同描述。统一在 function_definition 追加 side_effect 与 high/critical risk；两条导出路径消费同一定义。Canonical Schema、注册、校验及执行授权代码未改变。
- P2：新增共享 ExecutionStatus 与 ApprovalResponse，完整保留 Web 字段并覆盖 Persistent Task 的 task_id/action_id；trace_id/error 按服务端允许 null。两端 API 直接 import 并沿原路径导出，保留平台 UI 模型。
- P3：test:chat 直接转译加载真实 shared/chat/stream.ts，补齐单事件、连续事件、跨 Chunk CRLF、跨 Chunk UTF-8、无效 JSON、空 data、无尾换行、回调异常、无 body 共九项测试。解析器按完整行去掉 CR，并在结束时 flush TextDecoder；异常仍传播并释放 reader。

## 修改文件

- `backend/aiive/runtime/task_manager.py`：状态门控、稳定结果契约、幂等。
- `backend/aiive/tools/builtin_reminders.py`：透传真实结果，保留 cancelled/events_updated 兼容字段。
- `backend/aiive/api/routes_notifications.py`：隐藏与取消分离，合并最新 Event payload。
- `backend/aiive/core/function_calling.py`、`backend/aiive/tools/langchain_adapter.py`：安全描述唯一生成点。
- `shared/chat/session.ts`、`shared/chat/approval.ts`：完整共享 API DTO。
- `frontend/src/api/chat.ts`、`apps/android/src/api/chat.ts`：引用共享 DTO 并保留 API 导出路径。
- `frontend/src/pages/ChatPage.tsx`、`apps/android/src/App.tsx`：在 UI 模型赋值处将 nullable error 转为 undefined。
- `shared/chat/stream.ts`、`frontend/scripts/test-chat-model.mjs`：解析器边界处理与真实实现测试。
- `tests/unit/backend/test_notification_api.py`：六种状态的 Manager/工具双入口、三表一致性、幂等重试及 savepoint 回滚；已领取/终态 Outbox 保持不变；终态任务只隐藏指定通知。
- `tests/unit/backend/test_function_calling.py`：high/critical 外部副作用工具，真实 ContextAssembler 装配和 LangChain 转换结果全等、Schema 不变。
- `docs/CODE_MAP.md` 与本报告。

## 测试证据

- 专项：`pytest tests/unit/backend/test_notification_api.py tests/unit/backend/test_function_calling.py tests/unit/backend/test_tasks.py tests/unit/backend/test_retired_task_types.py tests/flow/test_reminder_end_to_end.py -q`：67 passed，7.95s。
- `ruff check backend/aiive --select F,E9`：通过。
- `basedpyright`：0 errors、0 warnings、0 notes。
- Web `npm run test:chat`：15 passed；直接测试真实解析器的九项新用例。
- Web `npm run test:work`：20 passed。
- Web `npm run build`、Android `npm run build`：通过。
- `git diff --check`：通过。

日志：工作区外 `E:/AIive/artifacts/contracts-focused-final.txt`、`contracts-typecheck-final.txt`、`chat-tests-final.txt`、`web-build-final.txt`、`android-build-final.txt`。后端全量回归日志为 `contracts-backend-final.txt`；GitHub CI 随本提交触发，最终状态以该提交 Actions 检查及交付回复为准。

## 附带发现与修复、剩余技术债

- 附带修复：原两端 UI 在审批 error 为 null 时直接写入只接受 string/undefined 的模型。共享 DTO 如实声明可空后，构建检查暴露此问题；在两处赋值边界归一化，未收窄服务端字段或合并平台 UI 模型。
- SQLAlchemy bulk Outbox 更新沿用原有 synchronize_session=False；测试显式 refresh/expire 再检查数据库值和事务回滚，避免把 identity map 的旧值当成持久化结果。
- LangChain 的 OpenAI 转换会展开嵌套 $ref；保留原转换行为。现有嵌套 Canonical Schema 测试继续覆盖 StructuredTool.args_schema，新增全等测试使用无引用 Schema，实际绑定描述与 ContextAssembler 全等。
- 本轮离线测试不调用在线模型或真实外部工具；本地 SQLite 不提供 PostgreSQL 行锁并发证据。已领取的 Outbox 不强行撤回，继续遵守现有 worker 生命周期；取消结果不能证明已发生的外部效果可撤销。
- 没有修改数据库模型、Schema 或 Alembic Migration，没有新增依赖。
