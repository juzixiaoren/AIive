# 代码收敛第二轮：退役旧任务分支与统一跨端契约

日期：2026-11-16。基于本地 main `786678a`（第一轮收敛提交）。工作区原有
`docs/plans/current_phase.md`、`docs/reports/2026-11-12_P1_work_center.md`
修改保留，不纳入本次提交。

## 1. 本轮迁移及删除的旧入口

| 旧入口 | 处理 | 迁移后的调用方 |
|---|---|---|
| `TaskManager.check_now` 的 condition_watch / routine 分支 | 删除执行逻辑；历史类型统一返回 `unsupported_task_type`，不写 last_checked_at、不改状态、不建 Outbox | `api/routes_tasks.py::check_task` 原样返回结构化结果 |
| `TaskManager.create` 接受任意 task_type | 仅允许 `reminder`，其余抛 `ValueError("unsupported_task_type")` | `builtin_reminders`（本就只建 reminder）、`POST /api/tasks` |
| `TaskManager.get_due` | 删除，无生产调用者；到期查询只保留 `reminder_outbox.due_reminder_tasks` | 扫描器与测试改用权威查询/next_due_at |
| `ContextBudget.from_env` | 删除；生产调用者全部迁移到 `ContextBudget.default()` | `context_assembler`、`turn_execution._shared_budget`、`main.py` 启动校验 |
| `agent_graph._build_runtime_identity` 私有别名 | 删除；测试直接导入 `runtime.system_contract.build_runtime_identity` | `test_identity_memory.py`、`test_memory_audit_followup.py` |
| `ContextAssembler._build_tools_schema_list` | 删除（只剩一次转发调用）；装配直接使用 `core/function_calling.function_definitions(registry)` | 9 处测试打桩（6 个文件）迁移到 `aiive.runtime.context_assembler.function_definitions` |
| `ThreadState._dicts_to_chat_messages` | 重命名为 `_dicts_to_token_count_messages` | ThreadState 内部 Token 估算与历史加载 |
| `ContextAssembler._dicts_to_chat_messages` | 重命名为 `_dicts_to_prompt_messages` | 装配、测试与集成断言 |
| Web `ActionCard / PendingOperation / ToolExecution / ThreadHistoryMessage / ThreadHistoryPage / ChatResponse` 本地声明 | 删除，改为 `shared/chat/history.ts` 单一契约；`ChatResponse` 无消费者直接移除 | `frontend/src/api/chat.ts`、`ChatPage`（tool_calls 可空处理）；`features/chat/types.ts` 经 `api/chat` 类型别名继续使用 |
| Android `ThreadHistoryMessage / ThreadHistoryPage / ChatApiError` 本地声明 | 删除，改用共享 DTO 与共享错误类 | `apps/android/src/api/chat.ts`、`App.tsx` |
| Android `chatResponseError` / `responseError` | 删除，统一 `chatApiErrorFromResponse` | 7 个聊天 API 调用点 |

## 2. 新的唯一实现来源

| 能力 | 唯一来源 |
|---|---|
| 提醒创建/取消/立即执行 | `TaskManager`（只允许 reminder；投递构造与终态收敛仍在 `reminder_outbox`） |
| 历史旧任务类型拒绝 | `TaskManager.check_now` 的 `unsupported_task_type` 分支（含 task_id/task_type，便于诊断） |
| 到期调度时间 | `TaskManager.next_due_at`（pending + reminder + 非空） |
| 历史消息传输契约 | `shared/chat/history.ts`（ActionCardDTO / PendingOperationDTO / ToolExecutionDTO / ThreadHistoryMessage / ThreadHistoryPage） |
| 聊天错误契约 | `shared/chat/errors.ts`（`ChatApiError`、`chatApiErrorFromPayload`、`chatApiErrorFromResponse`）；Web 以 `ApiError` 别名导出 |
| Token 计数消息转换 | `ThreadState._dicts_to_token_count_messages`（事件粒度，不做守卫/合批） |
| 模型 prompt 消息转换 | `ContextAssembler._dicts_to_prompt_messages`（tool_calls 合批、配对修复、Tool Output Guard、附件占位） |
| 预算配置 | `ContextBudget.default()`；废弃环境变量只在启动期由 `warn_deprecated_budget_override()` 提示一次 |
| 工具 Schema 导出 | `core/function_calling.function_definitions`（装配、预算、绑定、Inspector 同源） |

## 3. 测试随架构更新

- `tests/unit/backend/test_condition_watch.py` 被 `test_retired_task_types.py` 取代：不再断言伪通知/伪重排，改为验证 HTTP check-now 拒绝历史类型、不改状态、不建 Outbox，且不存在任务仍是 not_found。
- `test_tasks.py` 重写：新增 create 拒绝退役类型、next_due_at 排除历史类型与空时间、历史行可查询/可取消、check_now 无副作用、扫描器忽略历史行；提醒创建/立即执行/终态不复活断言保留。
- `test_notification_api.py`：旧 condition_watch 用例改为直接写入历史行，断言扫描不产生通知与不建 Outbox。
- `test_context_assembler.py`：新增两种转换差异断言（prompt 合批 2 个 tool_calls 为 1 条 assistant；Token 计数保持 2 条逐事件消息），并更新预算测试为 `default()` 与启动期单次警告。
- 9 处装配测试打桩（6 个文件）从方法级迁移到模块级 `function_definitions`。
- `frontend/scripts/test-chat-model.mjs` 新增错误契约用例：结构化字段/草稿恢复透传、嵌套 detail、字符串 detail、非 JSON 响应。

## 4. 有意保留的不同实现及依据

| 保留项 | 依据 |
|---|---|
| `ThreadState._dicts_to_token_count_messages` 与 `ContextAssembler._dicts_to_prompt_messages` | 职责不同：前者为 Token 口径的轻量回放，后者必须合批 tool_calls、修补配对并应用输出守卫；合并会改变 Token 估算或破坏模型协议。测试显式锁定差异。 |
| Web `Message` / `ToolCallItem`、Android `ChatMessage` / `ApprovalItem` | 平台 UI/交互模型，不是服务端契约；共享层只提供 DTO。 |
| Web `ApiError` 名称 | 仅为现有 UI 引用保留的导出别名，类型与实现都来自 `ChatApiError`，不是第二套错误类。 |
| `TaskManager` 与 `PersistentTaskRuntime` | 提醒投递编排 与 跨会话执行/审批/证据运行时的职责边界，按目标约束保留。 |
| 历史 `Task` 行 | 数据保留：可查询、可取消，但不可执行；未做迁移删除。 |

## 5. 验证证据

- 后端全量：`.venv/Scripts/python.exe -m pytest tests -q` → **2079 passed、143 skipped、0 failed，396.46s**。跳过数与上轮一致（PG/Embedding 专项、Windows 缺 POSIX bash），未新增跳过、未删除绕过。日志：`E:/AIive/artifacts/convergence2-2026-11-16/pytest-full.log`。
- 改动面专项：任务/提醒/恢复/装配/身份/附件/目标问题 16 个文件 → **153 passed**。
- 前端：`npx tsc --noEmit`、`npm run build` 通过；`npm run test:chat` **6 passed**（含新增错误契约）；`npm run test:work` **20 passed**。
- Android：`npx tsc --noEmit`、`npm run build` 通过。
- `ruff check backend/aiive --select F,E9`：通过；`basedpyright`：**0 errors / 0 warnings / 0 notes**。

## 6. 剩余技术债

1. `frontend/src/api/chat.ts` 仍自行声明 `ExecutionStatus`、`ApprovalResponse`，Android 也有一份字段更少的 `ApprovalResponse`；本轮 DTO 清单未包含它们，属于下一轮可收敛项。
2. 后端 `api/routes_threads.py` 在返回前补齐 attachments / 审批状态，属于路由层对读模型的增强；若未来有第二个消费方，应下沉到 `ThreadState` 读模型，避免复制补齐逻辑。
3. 历史 `Task` 行没有后台清理策略（按目标要求保留），后续如需归档应通过独立数据迁移而不是恢复执行分支。

## 7. Git Commit

代码与本文档同一提交，哈希见最终交付回复。
