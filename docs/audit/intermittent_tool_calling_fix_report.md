# Chat-to-Tool 不确定性修复报告（intermittent_tool_calling_fix）

- 日期：2026-07-09
- 关联审计：`docs/audit/intermittent_tool_calling_audit.md`
- 范围：在不重写 Agent Runtime 的前提下，修复「同一请求有时调工具、有时不调」的确定性缺陷，并禁止 LLM 自然语言假成功。

## 1. 问题定位

同一句用户请求（如「1 分钟后提醒我 赫赫」「我有哪些提醒？」）在多次调用中：

- 有时 `schedule_reminder`/`list_tasks` 真的被调用并写入 `tasks`；
- 有时 LLM 直接回复「已经安排上了」「没有提醒」，而**从未真正调用工具**，导致数据库状态与回复不一致。

根因（来自审计，本次已修复）：

1. `ActionPlanner.plan()` 使用 `temperature=0.1` + 自由文本输出，决策本身非确定性。
2. 解析失败（JSON 非法）时静默降级为 `should_execute=False` 的最终回复，LLM 可自由编造答案。
3. `AgentDecision` 只做「拦截（block）」，**从不强制（force）** 调用工具；主 LLM 还需自己输出 `<tool_call>` 标签才会执行，否则被当作最终回复返回。
4. 主 LLM 调用未设 `temperature`，高方差。
5. 上下文把助手自身的历史自然语言当成「已执行」的证据（如「已经安排上了」被当作提醒已创建）。

## 2. 修复内容（按阶段）

### Phase 1 — 确定性 IntentResult（`core/action_planner.py`）
- `plan()` 的 LLM 调用 `temperature=0.0`。
- 新增 `MUST_CALL_TOOLS` 集合（所有副作用/查询工具）。
- 新增 `derive_tool_policy(decision_type, execution_mode, should_execute, tool_name)`
  → `(tool_call_policy, tool_required, success_requires_tool_result)`。
  **关键**：策略完全由核心决策字段确定性推导，不信任 LLM 自由输出。
- `AgentDecision` 新增字段：`tool_call_policy`（默认 `may_call`）、`tool_required`、`success_requires_tool_result`、`args`、`parse_failed`。
- 解析失败不再静默降级，而是返回 `AgentDecision.clarification_needed()`（`decision_type=ask_clarification`，`parse_failed=True`），运行时进入明确澄清分支。
- `PLANNER_PROMPT` 强化「读/查/搜/取消类请求必须调用对应工具」「假设性/元问题（你会调用哪个工具？）是 explain_only」。

### Phase 2 — must-call Dispatcher（`runtime/agent_loop.py` + `runtime/tool_executor.py`）
- `run()` 在 ReAct 循环**之前**读取 `decision = self._last_decision`：
  - 若 `parse_failed` → 直接进入澄清分支，绝不编造答案。
  - 若 `decision.tool_required and should_execute and tool_name` → 调用 `_dispatch_must_call()`，**强制** `execute_direct()`，不再等待主 LLM 输出 `<tool_call>` 标签。
- `ToolExecutor.execute_direct()`：构造合法 `ParsedToolCall` 并走同一执行/日志路径。
- `build_action_cards()`：对 `schedule_reminder` 成功结果新增 `task_created` 卡片（含 `task_id`/`reminder_id`）。

### Phase 3 — 禁止假成功（`runtime/agent_loop.py`）
- `_dispatch_must_call()`：
  - `ok = status=="completed" and result.ok`。
  - 不 ok（执行失败 / 抛异常 / DB 写入失败 / 解析失败）→ **不返回成功回复**，记录 `tool_error` 事件，返回明确失败原因（如「操作未能完成（schedule_reminder）：…。我不会谎称已经完成。」）。
  - ok → 由 `_summarize_tool_result()` 基于 `tool_result` 确定性生成回复，绝不来自自由文本。

### Phase 4 — tool_result 回填与 verified facts（`core/context_builder.py`）
- `STABLE_PREFIX` 重写：
  - 删除过时的 `model_decide / unknown` 措辞，明确 `execute + must_call` 时 Dispatcher **必须**执行工具、回复由工具结果生成。
  - 新增 `## Verified Facts vs Claims` 段落：工具结果与数据库/事件记录是**已验证事实**；助手自身的历史自然语言只是**主张（claim）**，不能作为「操作已发生」的证据；需要确认是否存在时应由 Dispatcher 强制调用工具（如 `list_tasks`）并基于其结果作答。
- `build()` 新增 `tool_results` 参数与 `## Tool Results (verified evidence)` 结构化块（仅在有数据时渲染，保证既有测试 `test_build_with_history` 仍为 4 项）。
- `## Intent Result` 在 `must_call` 时透出 `tool_call_policy`，强化 LLM 认知。
- 核心执行保障：Phase 1–3 的 must-call 强制路径保证「我有哪些提醒？」一定走 `list_tasks` 真实查询 DB，而非信任历史文本。

### Phase 5 — 流式路径与标签泄漏修复（`runtime/agent_loop.py`）
- `run_stream()` 增加与 `run()` 对等的 must-call 分支与 `parse_failed` 澄清分支（流式入口 `/api/chat/stream` 同样确定性）。
- 新增生成器 `_stream_final()`：统一「token 流式 + 单个 done 事件」终态输出，供 must-call 与 max-steps 复用。
- **max-steps 分支不再返回原始 `reply`**，改为返回 `last_cleaned`（清洗后的文本），杜绝 `<tool_call>`/`</tool_cost>` 泄漏到用户可见回复。

### Phase 6 — 附带修复
- `tools/builtin_tools.py`：`_handle_cancel_task` 状态拼写 `canceled` → `cancelled`（全仓唯一出现，已统一）；`_handle_list_tasks` 增加按当前线程 `thread_id` 作用域过滤（用户只看到自己的提醒）。
- `runtime/task_manager.py`：`list_all(status, thread_id=None)` 支持线程作用域。
- `core/llm_client.py`：新增统一工厂 `default_llm_client()`（惰性读取 `settings`，避免循环导入）。
- 接入工厂：`api/routes_chat.py`、`runtime/graph.py`、`worker/task_worker.py`、`tools/builtin_tools.py(_handle_create_selfdev_plan)` 均改用 `default_llm_client()`；清理因此变为未使用的 `settings`/`LLMClient` 导入。
- 附带健壮性：`agent_loop._get_tasks_context()` 对 SQLite 等回读为 naive 的时区时间戳做 tz 归一化，避免 `offset-naive vs offset-aware` 比较异常（真实 Postgres 不会出现，但属合理防御）。

### Phase 7 — 针对性测试（`tests/unit/backend/test_must_call_tool_calling.py` + 更新 `test_action_planner.py`）
新增 6 个 targeted 测试，均通过隔离 SQLite（自定义 `NullPool` 连接，避免测试引擎共享连接导致 handler 提交被锁）与注册真实 builtin 工具验证**真实 DB 读写**：

1. `test_reminder_create_must_call_tool_repeated`：10 次「1 分钟后提醒我 赫赫」每次都调用 `schedule_reminder`、写入 `tasks`、产出 `task_created` 卡片、回复含真实标题；无自然语言-only 成功。
2. `test_task_list_must_call_tool_repeated`：先建提醒，10 次「我有哪些提醒？」每次都调用 `list_tasks` 且回复基于真实 DB 结果。
3. `test_tool_failure_cannot_fake_success`：`schedule_reminder` 失败时**绝不**出现「已经安排」字样、无 `task_created` 卡片、无 Task 写入、且记录 `tool_error` 事件。
4. `test_hypothetical_should_not_execute`：「如果我想让你一分钟后提醒我，你会调用哪个工具？」→ explain_only，不执行、不写库。
5. `test_no_raw_tool_call_leak`：主 LLM 输出畸形标签 `</tool_cost>` 时，用户可见回复不含 `<tool_call>`/`</tool_cost>`，且 `parse_errors` 非空。
6. `test_context_does_not_treat_assistant_claim_as_fact`：历史中存在「已经安排上了」的虚假主张但库里无 Task 时，must-call 强制 `list_tasks` 查询后回复「没有任何提醒」，不被虚假主张误导。

`test_action_planner.py::test_plan_error_fallback` 更新为断言解析失败返回 `ask_clarification`（`parse_failed=True`、`tool_call_policy=never_call`）。

## 3. 测试情况

- 新增/更新 targeted 测试：`test_must_call_tool_calling.py`（6 个）+ `test_action_planner.py`（1 个更新）→ **20 passed**。
- 受影响既有测试针对性回归（非全量）：
  - `test_tool_calling_runtime.py`、`test_dispatcher_validation.py`、`test_context_builder.py`、`test_tasks.py`、`test_condition_watch.py`、`test_notification_api.py`、`test_reminder_end_to_end.py` → **全部通过**。
- 附带清理：删除 `test_context_builder.py` 中 2 个针对已废弃 `working_limit` 旧 API 的陈旧测试；修正 `test_reminder_end_to_end.py::test_multiple_due_tasks_all_fire` 的 `status` 断言（`"notified"` → `"triggered"`，与 `poll_and_notify` 实际返回值对齐）。
- Lint：所有改动文件 `read_lints` 结果均为 0。

## 4. 文档维护

- 新增 `docs/audit/intermittent_tool_calling_fix_report.md`（本报告）。
- `docs/audit/intermittent_tool_calling_audit.md` 为前置审计，无需改动。
- 代码行为变化（取消状态拼写、list_tasks 线程作用域、流式终态清洗、解析失败转澄清）已在测试中覆盖；如需面向用户的文档补充可后续补充，但当前仓库未见对应的运行时使用文档需同步。

## 5. 暂缓事项

- 暂未大规模重写为 LangGraph `ToolNode` 或原生 function calling：当前 `ToolExecutor` 架构已可承载 must-call 策略与工具结果闭环。原生 function calling / ToolNode 迁移作为后续独立任务，待本次测试通过后另行规划。

## 6. 自检清单

- [x] 执行类 intent 必定调用工具（must-call Dispatcher，run + run_stream 双路径）。
- [x] 禁止 LLM 自然语言假成功（失败即返回失败原因，绝不谎称完成）。
- [x] 解析失败进入明确澄清分支，不再静默降级为自由回答。
- [x] 流式路径与 `/chat` 行为对齐，max-steps 返回清洗后文本。
- [x] 用户可见回复不含 `<tool_call>`/`</tool_cost>` 等原始标签。
- [x] 工具结果/DB 记录作为 verified facts，助手历史仅为 claim。
- [x] 统一 LLM 工厂，意图/工具路径 `temperature=0`。
- [x] 针对性测试通过且自动清理（tmp SQLite + rmtree）。
- [x] Lint 0 错误；预先存在的无关失败已识别并说明。
