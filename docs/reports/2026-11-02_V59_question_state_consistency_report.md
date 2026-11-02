# V59 待回答问题的状态一致性：交付报告

- 日期：2026-11-02
- 版本：V59（承接 V58 主动提问与用户回复关联闭环）
- 范围：`task_runtime/goal_questions.py`、`task_runtime/goal_service.py`、`runtime/question_tool.py`；
  `runtime/context_assembler.py` 与 `runtime/agent_graph.py` 只核对调用链，未改代码。
- 约束遵守：未新增表、未新增调度器、未新增 LLM 调用、未改动前端、未推送。

## 1. 问题

V58 让「主动提问 → 用户回复」可以关联，但待回答问题的状态只判断三件事：
是否已有 `goal_question_resolved`、是否超过 `expires_at`、任务是否终态。
于是下面这些情况下旧问题仍然被当作「可回答」：

1. 目标被暂停（`GoalService.pause`：plan.status=paused，步骤仍停在 waiting_user）；
2. 目标被结束（plan.status=abandoned，任务可能尚未进入终态的时间窗内）；
3. 步骤被修改或重置（`GoalService.modify` 让执行内容变化的步骤回到 pending/ready，步骤已不再等待用户）；
4. 同一步骤被重新提问（旧 question_id 仍在 72h 有效期内）。

后果：`ContextAssembler.assemble` 会把失效问题渲染进 system 消息，`agent_graph` 也会据此绑定
`answer_goal_question` 工具。用户正常聊天时，模型可能把闲聊当回答；真去回答时又只在服务端被拒，
形成「上下文里说有、操作时又不算」的不一致。

## 2. 根因

- `goal_questions.list_open_questions` 的状态推导只看事件与时间，**没有看目标计划本身**；
- `GoalService.pause/resume/end/modify` 修改的是 `task_state["goal"]`（plan.status / step.status），
  这些变化没有任何「顺手关掉问题」的动作，问题生命周期与计划状态之间缺少一致性检查；
- `question_tool` 拿到已关闭问题时的返回是笼统的（`question_not_open` / `stale`），调用方无法判断失效原因。

## 3. 修复方式（只改读取侧派生判定）

### 3.1 `backend/aiive/task_runtime/goal_questions.py`

| 位置 | 改动 |
|---|---|
| L77 | `PendingQuestion.invalid_reason`：失效原因（派生值，不落库） |
| L185 | `_plan_snapshot(task)`：从既有 `task_state["goal"]` 读 plan.status 与每个 step.status；计划缺失/损坏 → 视为失效 |
| L202 | `_superseded_ids(rows)`：同一 (task, step) 只保留最后一次提问，旧提问标记 superseded |
| L227 | `_invalid_reason(...)`：任务终态 → 计划缺失 → 计划非 active（`goal_<status>`）→ 步骤不存在 → 步骤非 waiting_user → 被取代 |
| L253 | `_to_question(...)` 接入上述三个判定；resolved/expired 优先级不变 |
| L317 | `list_open_questions` 只返回仍可回答的问题（按 task 缓存计划快照） |
| L353 | `load_question` 同样带失效原因（供幂等判定与排障） |

关键点：失效**不写事件、不加调度器**。状态是读取时推导的，所以目标 `resume` 后只要步骤仍在
waiting_user，问题自动重新有效；不需要在暂停时写标记，也不需要撤销标记。

### 3.2 `backend/aiive/task_runtime/goal_service.py`

`answer_step_question`（L520 起）在写计划前增加旧 question_id 复核：

- 问题存在时要求 `question.task_id == task.id` 且 `question.step_id == step.id`，否则 `question_mismatch`；
- 问题不是 open（stale/expired/resolved）→ `GoalError("question_<status>:<reason>")`，不进入 modify / send_input / 状态迁移；
- 原有校验顺序保留：plan 非 active → `goal_not_active`；步骤已完成 → `step_already_finished`；审批步骤 → `step_requires_approval`。

### 3.3 `backend/aiive/runtime/question_tool.py`

- 新增 `_closed_result(db, question)`（L89）：resolved → `already_answered`；expired → `question_expired`（补审计标记）；
  其余 → `question_stale` + `reason`（失效原因）+ 明确 hint。三条路径都不改目标。
- 直接指定 question_id 但不在 open 列表时，统一走 `_closed_result`，不再返回笼统状态。
- `GoalError` 以 `question_` 开头时同样回落到 `_closed_result`（并发暂停/修改的服务端复核结果）。
- `_stale_hint(reason)`：`question_stale` 的 hint 按失效原因分支——`goal_paused` 说明「恢复后原问题重新有效、不需要重新发起」；`goal_<blocked|completed|rejected|abandoned>` 与 `task_terminal` 说明目标已结束；`step_not_found`/`step_not_waiting_user` 说明步骤被修改或重置、等新提问；`superseded` 说明回答最新的那个。避免暂停场景误导用户重建目标。
- 审批步骤仍在 `GoalService` 的 `step_requires_approval` 分支，工具只做提示，审批流程未改变。

### 3.4 未改动但已核对的调用链

- `runtime/context_assembler.py` L234-247：`list_open_questions` → `render_questions_block` → system 消息；
  L425：`agent_ctx["open_questions"]`。修复落在数据源，组装器无需改代码。
- `runtime/agent_graph.py` L1176-1196：`open_questions` 非空才注册/放行 `answer_goal_question`；
  失效问题不再出现在列表里，因此本轮不会绑定该工具。

## 4. 行为契约变化（需要知道的语义）

| 场景 | 修复前 | 修复后 |
|---|---|---|
| 目标暂停，用户普通聊天 | 注入旧问题块 + 绑定回答工具 | 不注入、不绑定 |
| 目标暂停，模型仍拿旧 id 回答 | 服务端 `goal_not_active`，工具回报 rejected | 工具先看到问题已失效 → `question_stale` + `goal_paused`（不改目标） |
| 步骤被 modify 改内容 | 旧问题仍在上下文，回答时被拒 | 旧问题立即失效（`step_not_waiting_user`），同线程其它步骤问题不受影响 |
| 同一步骤重新提问 | 两个问题同时 open | 只保留最新（`superseded`） |
| 目标 resume | 问题仍在（原本就还在） | 问题重新有效（前提：步骤仍 waiting_user） |
| 审批步骤 | `step_requires_approval` | 不变（仍在审批检查之后，未绕过） |
| 过期 / 重复回复 | 不改目标 | 不变（另有新增重复用例） |

## 5. 验收对照

| 目标要求 | 证据 |
|---|---|
| 暂停后普通聊天不再注入旧问题 | `test_paused_goal_hides_question_and_rejects_answer`（断言 system 消息无问题块、`agent_ctx["open_questions"] == []`） |
| 步骤被修改/重置后旧问题失效 | `test_step_content_change_invalidates_only_that_question`、`test_new_question_supersedes_previous_one_for_same_step` |
| 目标恢复后的有效问题仍可回答 | `test_resume_restores_still_valid_question`、`test_resume_does_not_revive_question_for_changed_step` |
| 审批步骤仍受原审批限制 | `test_approval_step_keeps_original_approval_after_resume` + 既有 `test_approval_step_cannot_be_answered_in_chat` |
| 过期与重复回复不修改目标 | `test_repeated_reply_to_stale_question_never_modifies_goal`、`test_expired_question_stays_closed_across_repeats` + 既有过期/重复用例 |
| 旧 question_id 直接调用由服务端校验 | `test_paused_goal_rejects_direct_service_call`（`goal_not_active`）、步骤变化用例内直接调用 `GoalService` 断言 `question_stale:step_not_waiting_user` |
| 不新增表 / 调度器 / LLM 调用 | 无迁移、无 `worker/` 改动、无 `llm` 调用；判定全部来自既有 `agent_task_events` + `task_state["goal"]` |

## 6. 验证结果

- 新增用例：`tests/unit/backend/test_goal_question_state_consistency.py` 10 例全通过。
- 相关回归：`test_goal_question_answer.py`、`test_long_term_goal.py`、`test_goal_triggers.py`、
  `test_goal_event_sources.py`、`test_context_assembler.py`、`test_turn_event_injection.py` 合计 100 passed。
- 全量离线：`1453 passed, 23 skipped, 0 failed`（203.58s；23 skipped 为 PG 契约与 TEI 向量专项按环境跳过）。
- 静态检查：`ruff check backend/aiive --select F,E9` → All checks passed；
  `basedpyright backend` → 0 errors / 0 warnings / 0 notes。

## 7. 文档同步

- `docs/CODE_MAP.md`：头部最近更新改为 V59；§2.5 新增「待回答问题的状态一致性（V59）」定位行；
  §3 `goal_questions.py` / `question_tool.py` 符号说明补 `_plan_snapshot`/`_invalid_reason`/`_superseded_ids`/`invalid_reason`
  与 `question_stale`；§7 问答链路补失效分支。
- `docs/plans/current_phase.md`：版本索引与文末 V59 章节。

## 8. 未改动项（明确保留）

- V50 提醒与沉默策略、V54 事件注入、V55 流式过滤、V56 收件箱溢出、V57 线程级占用与决策幂等；
- `answer_goal_question` 的工具面与绑定条件（仍只在存在 open 问题时绑定）；
- 审批流程（`approve_step` / `step_requires_approval`）与前端任务中心交互；
- 数据库结构（无 Alembic 迁移）。

## 9. 遗留风险

1. 判定依据是 `task_state["goal"]` 的步骤状态：若有人绕过 `GoalService` 直接改写计划，判定会跟随脏数据（与既有架构约束一致）。
2. 同一 (task, step) 的旧问题会被新提问取代：模型若拿着旧 question_id 回答，只会得到 `question_stale`（刻意 fail-closed，不做猜测绑定）。
3. 暂停期间不写失效事件：审计依据是 `goal_paused`/`goal_resumed` 事件与问题状态推导结果，不额外落审计行。
4. 问题列表按线程 72h 窗口查询，规模随线程内目标数增长；本次未改索引。

## 10. 变更文件

- `backend/aiive/task_runtime/goal_questions.py`
- `backend/aiive/task_runtime/goal_service.py`
- `backend/aiive/runtime/question_tool.py`
- `tests/unit/backend/test_goal_question_state_consistency.py`（新增）
- `docs/CODE_MAP.md`、`docs/plans/current_phase.md`、本报告
