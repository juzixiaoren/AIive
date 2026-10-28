# V58 主动提问与用户回复关联闭环 — 交付报告

日期：2026-10-28
范围：`backend/aiive/task_runtime/`、`backend/aiive/runtime/`、`tests/unit/backend/`、`docs/`

## 1. 目标与结论

V50 的 `ask_user` / `wait_user` 只把步骤置 `waiting_user` 并写一条主动消息，**问题本身没有生命周期**：
用户随后的自然语言回复只是一条普通聊天消息，系统无法把它关联回“哪个目标、哪一步、哪个问题”，
闭环停在“提问 → 等待”。

结论：闭环已补齐，且**没有新增表、没有新增消息系统、没有新增 Agent、没有为每条用户消息多调一次模型**：

- 问题生命周期复用 `agent_task_events`（`goal_question_asked` / `goal_question_resolved` / `goal_question_expired`）；
- 关联判定由**本轮对话模型在同一次调用里**完成（上下文注入 + 一个按需绑定的工具）；
- 计划变更全部复用 `GoalService`（`send_input` / `modify` / 步骤状态机），审批仍走原审批机制；
- 提醒与沉默沿用 V50 的 `scan_due_reminders`，**没有新增调度器**。

## 2. 待回答问题的持久化

新增 `task_runtime/goal_questions.py`，唯一读写入口：

| 事件 | 写入时机 | 幂等键 | 关键字段 |
| --- | --- | --- | --- |
| `goal_question_asked` | `GoalDecisionEngine.apply` 的 ask_user / wait_user 分支（与主动消息同一事务） | `goal_question:<id>` | task_id / step_id / decision_id / trigger_key / message_event_id / asked_at / expires_at |
| `goal_question_resolved` | 用户回复被成功应用后（与计划变更同一事务） | `goal_question_resolved:<id>` | user_event_id / answer / intent / outcome / resolved_at |
| `goal_question_expired` | 访问到已过期问题时补记（审计用） | `goal_question_expired:<id>` | expired_at |

- **稳定问题 ID**：`question_id = uuid5(NAMESPACE_URL, "goal_question:<task>:<step>:<trigger>")`，与 V57 的
  `decision_id` 同源；Outbox 重投、决策重放都不会开第二个问题（测试断言事件数与消息数均为 1）。
- **状态是推导值**：有 resolved 事件 → `resolved`；`now >= expires_at` → `expired`；任务已终结 → `stale`；
  其余 → `open`。因此过期**不需要独立调度器**，也不会因为进程没跑而“忘记过期”。
- **有效期**：`QUESTION_TTL_HOURS = 72`，写进 payload 的 `expires_at`；过期后任何路径都不修改目标。
- 查询按线程：`list_open_questions(thread_id)` 用 `agent_task_events.created_at >= now - TTL` 收窄，再一次性批量
  读取 resolved/expired 键，避免 N+1。

## 3. 用户回复的关联（不额外调用模型）

| 环节 | 位置 | 行为 |
| --- | --- | --- |
| 上下文注入 | `runtime/context_assembler.py::assemble` | 仅当 `_source` 是真实用户消息时，把 `render_questions_block`（≤1200 字符，含 question_id 与四条处理规则）作为 system 消息**紧邻当前消息**注入；同时写入 `agent_ctx["open_questions"]` |
| 工具绑定 | `runtime/agent_graph.py` | 只有 `open_questions` 非空时，把 `answer_goal_question` 注册到**本轮** registry 快照（与 V54 `notify_user` 同一模式），并把 schema 追加到本轮 `tool_schemas` |
| 判定 | 本轮对话模型（既有的一次调用） | 回答 / 修改计划 / 拒绝 / 普通聊天；无法判断时按上下文规则先澄清 |
| 普通聊天 | —— | 没有待回答问题时不注入、不绑定工具；即使模型误调用，服务端返回 `no_pending_question` 且不改任何状态 |

> 关键点：**没有新增 LLM 请求**。V50 的“一次触发一次模型调用”与 V54 的“事件注入不新增请求”都保持成立。

## 4. 执行、校验与幂等

新增 `runtime/question_tool.py::answer_goal_question(answer, question_id?, intent, plan_changes?)`：

| 场景 | 服务端行为 |
| --- | --- |
| 无待回答问题 | `no_pending_question`，不改计划 |
| 多个候选且未给 question_id | `clarification_required` + 候选列表，**绝不猜测绑定** |
| 已关闭（已回答） | `already_answered`，不改计划 |
| 已过期 | `question_expired` + 写过期标记，不改计划 |
| 任务已终结 / 步骤已完成 | `rejected`（`task_not_found` / `step_already_finished` / `goal_not_active`） |
| 审批步骤 | `step_requires_approval`，问题保持打开，必须走原审批接口 |
| intent=answer | `GoalService.answer_step_question`：`send_input` 记录回答 → `waiting_user → ready` → 必要时入队 |
| intent=modify_plan | 复用 `GoalService.modify`（字段级校验、V50 触发状态保留规则不变） |
| intent=reject | 步骤置 `skipped`，写 `goal_step_answered(result=skipped)`，问题关闭 |

幂等：解析事件按 question_id 唯一；重复消息（同 `user_event_id`）或 Outbox 重投只回报 `already_answered`。
拒绝路径用**保存点**回滚（`db.begin_nested()`），不会把调用方事务整体回滚。

## 5. 恢复与沉默

- 问题与回答都在数据库里：进程重启后 `list_open_questions` 仍能读到（测试用 `expire_all + rollback` 模拟重启）。
- 回答内容进入 `task_state.inputs`（复用 `send_input`），下一次 Run 的上下文能读到。
- 问题过期后：不修改目标，只写审计标记；提醒继续由 V50 的 `scan_due_reminders` 按 `wake_at` 驱动。
- Trace：`goal_question_resolved` 保留问题、用户回答、模型判断（intent）与最终变更（outcome + `goal_step_answered`）。

## 6. 测试

新增 `tests/unit/backend/test_goal_question_answer.py`（11 例，全离线，无外部 API）：

| # | 用例 | 覆盖点 |
| --- | --- | --- |
| 1 | `test_single_pending_question_answer_resumes_step` | 单问题 → 解除等待 + 入队 + 回答进 inputs + Trace 完整 |
| 2 | `test_answer_requires_explicit_binding_when_multiple_questions` | 多候选未指明 → 澄清；显式绑定只影响对应步骤 |
| 3 | `test_plain_chat_does_not_link_anything` | 无问题 / 模型不调用 → 计划不变 |
| 4 | `test_modify_plan_intent_reuses_goal_service` | modify_plan 复用 GoalService，revision 递增，问题关闭 |
| 5 | `test_reject_intent_records_refusal_and_skips_step` | 拒绝 → skipped + 记录拒绝 |
| 6 | `test_approval_step_cannot_be_answered_in_chat` | 审批边界：拒绝且问题保持打开 |
| 7 | `test_expired_question_never_modifies_goal` | 过期 → 只标记，不改目标 |
| 8 | `test_duplicate_user_event_does_not_reapply` | 同 user_event_id 重投 → 只应用一次 |
| 9 | `test_replayed_decision_does_not_open_second_question` | 决策重放 → 一个问题、一条消息 |
| 10 | `test_question_survives_restart_and_replay` | 重启后仍可关联，且不可重复应用 |
| 11 | `test_context_injects_only_open_questions` | 只注入 open 问题；系统来源不注入；无问题不注入 |

相关既有套件（goal_triggers / goal_decision_serialization / context_assembler）38 passed。
全量回归：**1444 passed / 23 skipped / 0 failed**（1433 基线 + 11 新增；跳过项为 PG 与本地 Embedding 专项）。
`ruff --select F,E9` 全通过；`basedpyright` 改动源文件 0 errors / 0 warnings。

## 7. 改动清单

| 文件 | 类型 | 说明 |
| --- | --- | --- |
| `backend/aiive/task_runtime/goal_questions.py` | 新增 | 待回答问题生命周期（事件表达、状态推导、幂等解析） |
| `backend/aiive/runtime/question_tool.py` | 新增 | `answer_goal_question` 工具 + 单轮条件注册 |
| `backend/aiive/task_runtime/goal_decision.py` | 修改 | ask_user / wait_user 应用时登记问题，返回 `question_id` |
| `backend/aiive/task_runtime/goal_service.py` | 修改 | `answer_step_question`：解除等待 / 修改计划 / 记录拒绝（审批边界拒绝） |
| `backend/aiive/runtime/context_assembler.py` | 修改 | 仅真实用户消息注入问题块 + `agent_ctx["open_questions"]` |
| `backend/aiive/runtime/agent_graph.py` | 修改 | 按轮次条件绑定工具与 schema |
| `tests/unit/backend/test_goal_question_answer.py` | 新增 | 11 例离线验收 |
| `docs/CODE_MAP.md` / `docs/plans/current_phase.md` | 修改 | 图谱与阶段记录 |

未改动：V50 提醒/沉默策略、V54 事件注入与 notify_user 授权、V55 流式标记过滤、V56 收件箱溢出、
V57 线程级占用与决策幂等。

## 8. 遗留风险

1. 关联依赖模型在本轮主动调用工具：模型不调用时问题保持打开（用户可再发一条，或去任务中心操作）。
2. 同线程多个问题且用户表述含糊时要求用户再说一句确认（刻意不做猜测绑定）。
3. 问题生命周期存放在 `agent_task_events`，按线程查询依赖 `(task_id, created_at)` 索引；规模随线程任务数增长。
4. `wait_user` 的提问文本取 `decision.reason` 或步骤 `note`；若两者都为空则退化为“等待你的输入”，
   语义弱于 `ask_user`（后者有真实消息正文）。
