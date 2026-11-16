# 代码库收敛与重复实现清理

日期：2026-11-16。基于本地 main `c901335` 实施；工作区原有的
`docs/plans/current_phase.md`、`docs/reports/2026-11-12_P1_work_center.md`
修改保留，不纳入本次提交。

## 1. 确认的重复实现与原调用链

| 能力 | 原实现 A | 原实现 B | 原调用链 |
|---|---|---|---|
| 提醒投递 Outbox 入队 | `TaskManager.enqueue_reminder_now`（按 ID FOR UPDATE） | `worker.task_worker.enqueue_due_tasks`（SKIP LOCKED 批量） | `POST /api/tasks/{id}/check-now`、scheduler `_poll_job` → `reminder_delivery` → `handle_reminder_delivery` |
| 到期任务查询 | `TaskManager.get_due` | `enqueue_due_tasks` 内联筛选 | `get_due` 无生产调用者；扫描入口自带同一筛选 |
| 提醒取消 | `TaskManager.cancel`（含 pending Outbox 取消） | `builtin_reminders.handle_cancel_task` 自行改 Task/Event | 工具 `cancel_task` 与 `POST /tasks/{id}/cancel`、`DELETE /notifications/{id}` |
| 模型可见工具 Schema | `context_assembler._build_tools_schema_list`（LangChain 对象反推 + 手工剔参数） | `agent_graph._notify_user_schema` / `_answer_question_schema`（手写副本，预算外追加） | 上下文装配 → `AssembledContext.tools_schema` → 图绑定；Token 预算只覆盖前者 |
| 单 Turn 工具注册时机 | `ContextAssembler`（构建 schema） | `AgentGraph._prepare_execution`（快照 + 再注册 notify/answer） | 每个聊天 Turn |
| Offline 上下文导出工具面 | `context_inspector` 渲染 `ToolRegistry` 全量（Worker 能力面） | 运行时 Main Agent 只有元工具 + 附件 + notify_user | `scripts/dump_agent_context.py` |
| 系统契约兼容方法 | `AgentGraph._build_stable_contract`（仅委托） | `runtime/system_contract.build_stable_contract` | 无生产调用者 |
| 知识文档状态常量 | `knowledge/ingestor.py` 的 `as X as X` 再导出 | `knowledge/document_status.py` | 全部消费方已直接导入 `document_status` |
| SSE 帧解析 | `frontend/src/api/chat.ts` 内联循环 | `apps/android/src/api/chat.ts` 内联 `handleLine` | 两端聊天流 |
| WorkCenter Hook | `frontend/src/hooks/useWorkCenter.ts` | `apps/android/src/work/useWorkCenter.ts` | 两端工作中心页 |
| 构建产物入库 | `frontend/dist/index.html`、`backend/aiive.egg-info/*` | 构建/安装过程可重建 | Docker 前端构建、`pip install -e .` |

## 2. 权威实现与选择理由

| 能力 | 权威实现 | 理由 |
|---|---|---|
| 提醒投递身份/载荷/幂等/终态 | `runtime/reminder_outbox.py` | 两个入口只在「如何选中行」不同；operation_id、payload、终态映射原本已出现三份（手动、批量、handler 兜底），集中到无状态纯函数后不会形成第二套业务规则 |
| 到期提醒查询 | `reminder_outbox.due_reminder_tasks` | 与扫描器同一筛选/排序；`get_due` 退化为只读兼容视图 |
| 提醒取消 | `TaskManager.cancel` | 唯一同时取消 pending Outbox 与 reminder_created 事件的实现；工具层不再自行改状态 |
| 工具 Schema 导出 | `core/function_calling.function_definitions` + `tools/tool_validation.canonical_tool_schema` | 延续 2026-11-16 的统一 Tool Contract：Canonical 契约已是校验、指纹、绑定的唯一真相源；导出只是遍历注册项 |
| 单 Turn 工具面 | `runtime/turn_tool_surface.build_turn_tool_registry` | 谁需要 Schema/预算，谁就必须看到全部实际绑定工具；注册时机只能有一个 |
| 系统契约 | `runtime/system_contract` | 兼容静态方法无调用者，删除 |
| 文档状态常量 | `knowledge/document_status` | 再导出无任何消费者 |
| SSE 帧解析 | `shared/chat/stream.ts` | 纯协议逻辑，与平台 UI 无关；两端原本在 CRLF 归一、空 data、JSON 容错上行为不一致 |
| WorkCenter Hook | `shared/work/useWorkCenter.ts` | 只有传输层（client 工厂/URL/Token/文案）不同，其余生命周期完全一致 |
| 生成产物 | 构建过程 | `.gitignore` 早已覆盖，仅历史入库 |

## 3. 实际合并、迁移与删除

### 新增（单一入口）

- `backend/aiive/runtime/reminder_outbox.py`：`reminder_operation_id`、`due_reminder_tasks`、`build_reminder_delivery_job`、`claim_reminder_delivery`。
- `backend/aiive/runtime/turn_tool_surface.py`：`build_turn_tool_registry`。
- `shared/chat/stream.ts`：`readChatEventStream`。
- `shared/work/useWorkCenter.ts`：共享 Hook 实现。

### 迁移与删除

- `runtime/task_manager.py`：`enqueue_reminder_now` 改为调用 `claim_reminder_delivery`；`get_due` 改为委托 `due_reminder_tasks`；`next_due_at` 只统计可自动投递的 reminder 且排除空时间；`cancel` 返回 `events_cancelled` 计数。
- `worker/task_worker.py`：删除内联 Outbox 构造/终态映射，改用共享入口与共享到期查询。
- `worker/outbox_handlers.py`：operation_id 兜底改用 `reminder_operation_id`。
- `tools/builtin_reminders.py`：`handle_cancel_task` 改为委托 `TaskManager.cancel`（行为差异：不再把 completed 任务改回 cancelled）。
- `api/routes_tasks.py`：`POST /api/tasks` 只接受 `reminder`，其余返回 422；消除「创建后永不调度」的静默死路。
- `runtime/agent_graph.py`：删除 `_notify_user_schema`、`_answer_question_schema`、`_build_stable_contract`；`_prepare_execution` 直接消费装配层注册表，不再重复注册/手工补 Schema。
- `runtime/context_assembler.py`：删除 `_INJECTED_TOOL_PARAMS`/`_strip_injected_params`；`_build_tools_schema_list` 改为 `function_definitions` 导出；工具面含 notify_user 与按需 answer_goal_question，全部进入预算。
- `runtime/context_inspector.py`：工具分区改用 Main Agent 实际工具面；删除独立的手工 Schema 清洗副本。
- `tools/registry.py`：新增只读 `registrations()`。
- `tools/langchain_adapter.py`：直接遍历注册项，不再 `list_all()` + `get()` 往返。
- `core/function_calling.py`：新增 `function_definitions`。
- `knowledge/ingestor.py`：删除 `ACTIVE/RECOVERABLE/INACTIVE/DOCUMENT_STATUSES` 再导出；`db/models.py` 文档引用改指 `document_status`。
- `frontend/src/api/chat.ts`、`apps/android/src/api/chat.ts`：SSE 循环替换为 `readChatEventStream`，仅保留各自事件语义映射。
- `frontend/src/hooks/useWorkCenter.ts`、`apps/android/src/work/useWorkCenter.ts`：退化为模块级传输配置 + 共享 Hook。
- 版本管理移除：`backend/aiive.egg-info/*`（5 个）、`frontend/dist/index.html`。

## 4. 保留的重复入口及具体理由

| 入口 | 保留理由 |
|---|---|
| `TaskManager.check_now` 的 condition_watch / routine 分支 | 生产入口已不能创建这两类 Task（`POST /api/tasks` 只接受 reminder），调度职责由 `task_runtime/watchers.py` 与 Goal Trigger 承担。剩余分支仅服务历史行与既有单元测试（`test_tasks.py::TestConditionWatch`、`test_condition_watch.py`、`test_notification_api.py::test_condition_not_met_no_notification`）。仓库测试归用户所有，本次不改测试；删除分支等于删除这些用例，需用户明确授权后进行（最小改动：删除 `check_now` 中 `condition_watch`/`routine` 两段与 `condition` 列读取，并删除上述用例）。 |
| `TaskManager` 与 `PersistentTaskRuntime` | 目标文档第 4 节明确要求保留的职责边界：前者是提醒投递行锁与 Outbox 前置，后者是跨会话执行/审批/证据运行时。 |
| `ContextBudget.from_env` | 已是 `default()` 的薄委托（仅额外提示废弃环境变量），无独立业务逻辑；`main.py` 启动校验、assembler 默认值、turn 缓存均依赖它。删除只增加调用点改动，不产生收敛收益。 |
| `agent_graph._build_runtime_identity`（模块级别名） | 零逻辑别名，指向 `system_contract.build_runtime_identity`；两个既有测试按该导入路径断言身份文本。 |
| `context_assembler._build_tools_schema_list` | 方法本身已只剩一次导出调用，但保留为装配层可替换点：5 个集成测试用 `lambda *a, **k` 打桩工具预算。保留签名不构成第二份 Schema 逻辑。 |
| Web `ApiError` 与 Android `ChatApiError` | 字段集合不同（Web 有 `retryAfterSeconds`，Android 无），且被各自 UI 错误处理与文案直接消费；共享解析器只负责帧，错误对象属于平台契约。 |
| `ActionCard` → 审批/工作卡片映射（Android）与工具卡片（Web） | 平台 UI 能力差异（远程审批、原生分享），按目标文档要求保留。 |

## 5. 验证证据

- 后端全量：`.venv/Scripts/python.exe -m pytest tests -q` → **2075 passed、143 skipped、0 failed，402.97s**（skip 为既有 PG/Embedding 专项与 Windows 缺 POSIX bash；未新增跳过项、未删除用例）。日志：`E:/AIive/artifacts/convergence-2026-11-16/pytest-full.log`。
- 提醒/任务专项复跑（最终代码）：`test_reminder_end_to_end.py`、`test_notification_api.py`、`test_tasks.py`、`test_agent_graph.py` → **47 passed**。
- 工具面/上下文专项：`test_agent_graph.py`、`test_function_calling.py`、`test_context_assembler.py`、`test_turn_event_injection.py`、`test_identity_memory.py` → **68 passed**。
- 前端：`npm run build`（tsc + vite）通过；`npm run test:chat` **4 passed**；`npm run test:work` **20 passed**。
- Android：`npm run build`（tsc + vite）通过。
- 共享 SSE 解析器：临时 Node 脚本验证「跨 chunk 的 CRLF 边界、JSON 容错、事件顺序」通过（脚本已删除，未入库）。
- `ruff check backend/aiive --select F,E9`：通过。
- `basedpyright`：**0 errors, 0 warnings, 0 notes**。

## 6. 尚未解决的重复实现

1. `TaskManager.check_now` 的 condition_watch / routine 分支（见第 4 节）：唯一阻塞是测试所有权约束，不是技术不可合并。
2. `runtime/thread_state.py::_dicts_to_chat_messages` 与 `context_assembler._dicts_to_chat_messages` 仍各有一份「历史事件 → chat messages」转换（前者仅供 Token 估算）。两者在附件、guard、tool_call 配对细节上已有差异，合并会改变 Token 估算口径，本次未纳入；如需收敛，应让 `thread_state` 复用装配层实现并接受估算口径微调。
3. Web 与 Android 的消息模型（`ActionCard`、`ApprovalItem`、`WorkCard`）仍各自声明；这是平台 UI 契约差异，不属于纯协议重复。

## 7. Git Commit

代码与本文档同一提交，哈希见最终交付回复。
