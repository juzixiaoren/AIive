# V47 长期目标规划与事件驱动的持续执行 — 交付报告

日期：2026-10-26
范围：后端 `task_runtime` 长期目标能力 + 任务中心展示/控制 + 离线验收测试
约束：不新增任务引擎、不新增数据表、不新增周期扫描 LLM 调用；复用既有 Task/Run/Checkpoint/Watcher/Outbox/通知/审批。

## 1. 结论

长期目标（Goal）作为**一种持久任务类型**落地：一条 `AgentTask(task_type="long_term_goal")` 承载目标，
计划与步骤存在 `task_state["goal"]`，执行仍走 `PersistentTaskRuntime`，触发仍走
`AgentTaskWatch` → Outbox `agent_task_run`，通知仍走 `Event(notification_created)` 读模型。

- 提案与确认：LLM 通过 `delegate_task(goal_plan=...)` 提交结构化计划 → 任务停在 `blocked_user` 等确认，
  **确认前不会产生任何 run**；拒绝、暂停、恢复、修改、结束均可从任务中心或 API 触发。
- 步骤生命周期：`pending/ready/running/waiting_user/waiting_time/done/failed/skipped/missed`，
  稳定 ID、依赖、验收标准、证据、尝试次数、错过次数全部持久化；重启后从 task_state + Checkpoint 恢复。
- 完成判定：只有每个需要证据的步骤都拿到 Evidence 才允许 `completed`；LLM 仅口头宣称成功会被
  判为 `missing_evidence`，步骤失败、目标 `blocked` 并发通知。
- 事件驱动：定时步骤由 Watcher 到期触发；交互步骤到期**只发通知**，用户点击开始才在独立任务上下文执行。
- 沉默策略：未确认计划 24h 提醒一次、7 天后休眠；等待用户的步骤 24h 提醒一次；周期交互步骤错过记
  `missed` 且不重复启动同一场，连续 3 次建议调整（绝不自动改计划）。

## 2. 修改文件

新增：

| 文件 | 作用 |
|---|---|
| `backend/aiive/task_runtime/goal_plan.py` | 纯领域模型：`GoalPlan`/`GoalStep`/`GoalSchedule`、状态集合、`next_occurrence`、`next_runnable_step`、`summarize` |
| `backend/aiive/task_runtime/goal_service.py` | `GoalService`：提案/确认/审批/暂停/恢复/修改/结束、执行钩子、Watcher 回调、沉默扫描、通知去重 |
| `tests/unit/backend/test_long_term_goal.py` | 20 例离线验收测试（Fake LLM，无外部依赖） |
| `docs/reports/2026-10-26_V47_long_term_goal_report.md` | 本报告 |

修改：

| 文件 | 改动 |
|---|---|
| `backend/aiive/task_runtime/runtime.py` | 起跑前调用 `on_run_started`（无可执行步骤则不起 LLM）；循环内检查暂停；`ask_user` 后按钩子决定是否续跑下一步；`_complete` 先结算步骤再进 `verifying` |
| `backend/aiive/task_runtime/watchers.py` | 带 `goal_step_id` 的 time watcher 到期交给 `GoalService.on_watch_due`；`notify_only` 只通知不唤醒 |
| `backend/aiive/task_runtime/tools.py` | `delegate_task` 新增 `goal_plan` 参数；先校验 `GoalPlan` 再落库，返回 `status="proposed"`（主 Agent 工具面仍是 5 个） |
| `backend/aiive/task_runtime/state_machine.py` | 允许 `queued → blocked_user`（目标确认后无立即可执行步骤时停在等待态） |
| `backend/aiive/task_runtime/context_assembler.py` | 任务上下文新增 `goal` 快照 + “长期目标模式”系统段（只给当前步骤，不给整份计划原文） |
| `backend/aiive/api/routes_agent_tasks.py` | `GET /{id}/goal`；`POST /{id}/goal/{confirm,reject,pause,resume,end,modify}`；`POST /{id}/goal/steps/{sid}/{start,approve}` |
| `backend/aiive/worker/scheduler_daemon.py` | `_agent_task_scanner_job` 追加 `GoalService.scan_due_reminders()`（仅 `wake_at <= now` 的确定性扫描，不调用 LLM） |
| `backend/aiive/config.py` | `aiive_goal_proposal_reminder_hours=24`、`aiive_goal_proposal_expire_days=7`、`aiive_goal_wait_reminder_hours=24`、`aiive_goal_missed_streak_threshold=3` |
| `frontend/src/api/agentTasks.ts` | `AgentTaskGoal` 类型 + `getAgentTaskGoal`/`controlAgentTaskGoal`/`approveAgentTaskGoalStep`/`startAgentTaskGoalStep` |
| `frontend/src/pages/TaskCenterPage.tsx` | 任务中心新增“长期目标”面板：进度、当前步骤、步骤状态/下次触发/错过次数/证据数，确认/拒绝/暂停/恢复/结束/审批/开始按钮 |
| `docs/CODE_MAP.md`、`docs/plans/current_phase.md` | 代码图谱与本阶段记录 |

## 3. 持久化方式（无新表）

- 目标本体：`AgentTask.task_type = "long_term_goal"`，`task_state["goal"]` 存 `GoalPlan` 全量 JSON，
  `task_state["plan"]` 存步骤投影（id/title/kind/status），供上下文与前端轻量读取。
- 快照：每次状态推进写 `AgentTaskCheckpoint(reason=goal_proposed|goal_confirmed|goal_modified|goal_paused|goal_resumed|goal_waiting)`，
  `plan` 字段保存计划副本，重启恢复以 `task_state` 为准、Checkpoint 作审计与回放。
- 触发：`AgentTaskWatch`（`watch_type="time"`）`config = {at, goal_step_id, occurrence, notify_only, interval_seconds}`，
  `status` 只允许 `active/paused/fired`；同一 `(goal_step_id, occurrence)` 只建一条。
- 幂等：Outbox `operation_id = agent_task_run:{task_id}:{trigger}:v{task.version}`；通知以
  `goal:{category}:{dedupe}` 在最近 `notification_created` 事件里查重。
- 审批：步骤级 `approval_required` 由 `GoalStep.approved_at` 记录，审批前任务停在 `blocked_user`；
  步骤内工具级审批仍走既有 `TaskApprovalService`（`blocked_approval` + `ApprovalRequest`），两层互不替代。

## 4. 触发与恢复流程

```
delegate_task(goal_plan)                       # LLM 只提案
  → GoalService.attach_plan → blocked_user + Event(goal_proposed) + wake_at=+24h
  → 用户 POST /goal/confirm                     # 唯一启动入口
      → status=active, revision+1
      → 可执行步骤 ready → queued + Outbox agent_task_run(trigger=goal_confirmed)
      → 无立即可执行 → blocked_user + goal_waiting(reason=no_runnable_step)

AgentTaskWatch 到期（scheduler _agent_task_scanner_job → WatcherService.poll_due）
  → GoalService.on_watch_due：上一场未开始 → missed（不完成、不重启同一场）
      autonomous → ready → 入队；interactive/approval_required → waiting_user + 通知（notify_only）
      → 计算 next_occurrence 并重建下一条 watch（幂等键含 occurrence）

Outbox agent_task_run → handle_agent_task_run → PersistentTaskRuntime.run
  → on_run_started：没有 ready/到期步骤 → 直接 blocked_user（不消耗 LLM）
  → decide → Broker 派发 → Evidence
  → _complete → on_step_finished：证据齐 → done；缺证据 → failed + goal blocked + 通知
      → _advance：下一步 ready 则入队；仅剩未来触发则写 wake_at；全部完成且证据齐 → completed
```

重启恢复：`load_plan` 只依赖 `task_state["goal"]`；待触发 watch 由 `schedule_pending_watches`
按 `next_trigger_at` 重建/复用；`scan_due_reminders` 只处理 `wake_at <= now` 的任务。

## 5. 对话隔离

- 一个目标 = 一条任务 = 独立 `AgentRun`/`AgentAction`/`Checkpoint`/`Evidence` 与独立
  `TaskContextAssembler` 快照；快照只注入目标摘要 + 当前步骤，**不含会话 raw history**。
- 后台推进只写任务事件与通知，不向前台对话插入消息；交互步骤到期只写
  `notification_created`，用户点击开始才起跑（`trigger=goal_step_start`）。
- 资源锁、权限、审批、预算、副作用对账全部沿用既有任务链路，未新增旁路。

## 6. 沉默处理策略

| 场景 | 行为 |
|---|---|
| 计划未确认 | 24h 提醒一次；7 天后转休眠（`wake_at=None`），不再打扰，草稿保留 |
| 步骤等待用户输入 | 只阻塞依赖该步骤的步骤；24h 提醒一次，之后不再重复 |
| 交互场次到期未开始 | 记 `missed`（不计完成），不重复启动同一 occurrence，下一条触发照常排期 |
| 连续错过 ≥3 次 | 发“是否调整时间或暂停”的通知 + `goal_adjustment_suggested` 事件，**不自动改计划** |
| 暂停/结束/放弃 | 取消全部 active watch、清 `wake_at`、结束任务，旧触发不再生效 |

## 7. 测试

命令：

```
.venv/Scripts/python.exe -m pytest tests/unit/backend/test_long_term_goal.py -q
.venv/Scripts/python.exe -m pytest tests/unit/backend/test_persistent_task_runtime.py \
    tests/unit/backend/test_condition_watch.py tests/unit/backend/test_outbox_worker.py \
    tests/unit/backend/test_tasks.py tests/unit/backend/test_chat_task_bridge.py -q
.venv/Scripts/python.exe -m pytest tests -q
.venv/Scripts/python.exe -m ruff check --select F,E9 backend/aiive/task_runtime/ ...
/e/python/Scripts/basedpyright backend/aiive
cd frontend && ./node_modules/.bin/tsc --noEmit && ./node_modules/.bin/vite build
```

结果：

- `test_long_term_goal.py`：**20 passed**
- 相关回归（runtime/watcher/outbox/tasks/bridge + goal）：**68 passed**
- 全量离线（`pytest tests -q`）：**1268 passed, 23 skipped**；跳过的 23 例是需 `AIIVE_PG_TESTS=1` 的 PG 专项
- `ruff --select F,E9`：All checks passed
- `basedpyright backend/aiive`：**0 errors, 0 warnings, 0 notes**
- 前端 `tsc --noEmit`：通过；`vite build`：通过

20 例覆盖：确认前不执行且确认只启动一次、步骤完成推进进度、全步骤证据齐才完成、
无证据不得成功、等待用户只阻塞依赖、定时触发只入队一次（重复扫描不重复）、交互到期只通知、
用户开始走独立上下文、重启恢复、暂停/恢复/修改/结束使旧计划失效、预算耗尽不自扩张、
连续错过建议调整、未确认计划提醒一次后休眠、`delegate_task` 保持 5 工具面、
REST 控制面（含 409/404）、Outbox 真实调用链、审批步骤未批不执行且审批后只启动一次、
交互步骤不自动起跑且 24h 只提醒一次、审批接口幂等与类型校验。

## 8. 遗留风险

1. **步骤级审批与工具级审批是两层**：`approval_required` 用 `GoalStep.approved_at` 表达，
   没有复用 `ApprovalRequest` 表（该表要求 `action_id` 非空，强行复用会污染 Action 语义）。
   好处是不新增表；代价是审批列表页看不到目标步骤审批，只能从任务中心/目标 API 处理。
2. **计划修改按 `schedule` 相等判断“未变”**：只改标题/验收标准会保留原状态（预期），
   但如果用户改了 schedule 之外却期望重跑该步骤，需要显式改 schedule 或结束重建。
3. **交互步骤不自动执行**：无 schedule 的 `interactive` 步骤在确认后停在 `waiting_user`，
   必须点击开始；如果提案把本该自动跑的步骤写成 `interactive`，目标会看起来“没动静”，
   目前只能靠通知文案与任务中心状态提示。
4. **提醒只做一次**：等待用户/未确认计划各提醒一次后即静默，避免打扰；
   需要更强提醒时应调整 `aiive_goal_*_reminder_hours` 或由用户主动查看任务中心。
5. **未做真实 PG 集成测试**：目标能力全部走既有表与既有代码路径，离线 SQLite 已覆盖；
   与 PostgreSQL 的差异主要是 JSON 字段读写，风险低但未单独跑 PG 用例。
