# V50 目标步骤触发规则与触发决策闭环 — 交付报告

日期：2026-10-27
范围：`backend/aiive/task_runtime/`、`backend/aiive/runtime/proactive_message.py`、`backend/aiive/worker/`、`backend/aiive/config.py`
约束：不新建表、不新建任务引擎；复用既有 Outbox / Event / TurnRecord / Trace；不改既有审批与安全内核语义。

## 1. 先还原真实调用链（改造前）

读源码确认的既有链路（V47/V48 已落地）：

```
scheduler_daemon._agent_task_scanner_job (10s)
  ├ recover_stale_tasks / reconcile_queued_tasks
  ├ WatcherService.poll_due → GoalService.on_watch_due
  └ GoalService.scan_due_reminders()           # wake_at <= now，确定性，不调 LLM

OutboxJob(agent_task_run) → handle_agent_task_run
  → PersistentTaskRuntime.run
      → GoalService.on_run_started / on_step_finished / on_step_awaiting_user
      → _advance：下一步 ready → repo.enqueue_run(agent_task_run)
```

关键事实：

- 目标就是一条 `AgentTask(task_type="long_term_goal")`，计划存 `task_state["goal"]`，**没有独立表**。
- 状态机由 `state_machine.ensure_transition` 强制，非法跃迁抛 `InvalidStateTransition`。
- 时间触发只支持 `AgentTaskWatch`（time/file/node 三类），`schedule` 只有 once/weekly 两种。
- Outbox 已具备 `operation_id` 唯一键、claim token、`HandlerRegistry` 版本校验；`AgentTaskEvent` 已有 `dedupe_key` 唯一约束。
- 聊天时间线由 `TurnRecord`（`turn_sequence` 排序、租约字段）+ `Event` 构成，`ThreadState.list_thread_messages_page` 只放行
  `user_message / tool_call / tool_result / llm_response / maintenance_report_terminal`。
- Trace 由 `ContextSnapshot`（完整 context_items）+ `LLMCall` + `Event(trace_id)` 组成，与聊天读模型完全分离。

结论：触发规则与决策闭环可以**全部挂在既有链路上**，不需要新引擎、新表或新的调度器。

## 2. 触发规则（最小扩展）

`task_runtime/goal_plan.py`：

- 新增 `TriggerKind`（once_at / recurring / window_once / event_condition / opportunistic）与 `TriggerRule`
  （at / start / end / frequency / weekday / time / timezone / until / event_types / match /
  cooldown_hours=24 / max_per_day=2 / max_per_week=5 / evaluate_on_step_done），`validate_shape` 逐 kind 校验必填项。
- `GoalStep` 增补：`trigger`、`trigger_count`、`last_trigger_key`、`last_trigger_at`、`armed_at`、`expired_at`、
  `cooldown_until`、`opportunistic_hits`、`last_message_key`。
- `trigger_from_schedule` / `trigger_of`：把历史 `schedule`（once/weekly）**归一化**为 `TriggerRule`，
  旧计划、旧 API、27 个 V47/V48 用例零改动。
- `next_time_trigger` 计算下一次日历落点（时区感知，支持 until）；`next_runnable_step` 改为按 `trigger_of` 判定。

`task_runtime/goal_triggers.py`（新，232 行，纯函数、无 DB）：

- `arm_rule`（首次装载 armed_at）、`match_event`（事件类型 + payload 子集）、`_within_caps`（日/周上限）、
  `_latest_occurrence`（时区感知最近落点）、`evaluate_rule`、`expiry_of`。
- 每次命中返回**稳定幂等键**：`once_at:<iso>` / `recurring:<iso>` / `window_once:<start>:<event_id>` /
  `event_condition:<event_id>` / `opportunistic:<event_id>`。
- `opportunistic` 只有在**收到相关事件**且通过冷却/上限时才命中；没有任何“定时无条件调 LLM”的路径。

## 3. 确定性扫描 → Outbox

`task_runtime/goal_trigger_service.py`（新，277 行）：`GoalTriggerService.scan(now, limit)`

```
ACTIVE 目标（QUEUED/BLOCKED_USER/BLOCKED_NODE/BLOCKED_APPROVAL，跳过运行中）
  → 逐步骤：trigger_of → 已完成/运行中/依赖未满足 跳过
  → arm_rule → expiry_of（window_once 到期 → MISSED + expired_at）
  → evaluate_rule → 命中
      → AgentTaskEvent(goal_trigger_fired, dedupe_key=trigger:<operation_id>)
      → OutboxJob(job_type=goal_trigger_decision,
                  operation_id=goal_trigger:<task>:<step>:<sha1(key)[:16]>,
                  trace_id=uuid5(operation_id))
```

事件来源：同线程 `Event`（排除自触发类型）+ `AgentTaskEvent(goal_step_completed)`；
`scheduler_daemon._agent_task_scanner_job` 在 `scan_due_reminders()` 之后追加一次 `scan()`（10s 周期，纯 DB 匹配）。
`worker/job_types.py` 的 allowlist 与 `outbox_handlers.register_all` 注册新 job（schema_version=1）。

## 4. 决策闭环

`task_runtime/goal_decision.py`（新，443 行）：

| 阶段 | 实现 |
|---|---|
| 上下文 | `context()`：目标/计划/当前步骤/依赖/触发原因与事件/执行证据/相关记忆/近期对话/当前时间；超长按字符预算截断 |
| 调用 | `messages()` → `chat(json_mode)`（复用 `default_llm_client`，可被测试替换为 Fake LLM） |
| 解析 | `GoalDecision`（`extra="forbid"`，7 种 action：no_action / run_step / wait_user / modify_plan / ask_user / send_message / complete_goal），`json_repair` 兜底 |
| 服务端校验 | `validate()`：action 白名单、目标 ACTIVE、步骤存在且未完成/未运行、依赖满足、审批态、证据要求；LLM 不直接写库 |
| 执行 | `apply()`：先校验 `step.last_trigger_key == trigger_key`（否则 `stale_trigger`），再调 `GoalService` 的 `begin_step_from_trigger` / `mark_step_waiting_user` / `save_plan` / `complete_goal` |
| 审计 | `_record()` 写 `goal_decision` 事件（dedupe_key 幂等）；`write_trace()` 写 `ContextSnapshot`（完整 system + 历史 + 触发事件）+ `LLMCall` + `goal_trigger_decision` 事件 |

`GoalService` 新增四个入口（`goal_service.py`）：

- `save_plan`：决策改计划后回写并递增 revision。
- `begin_step_from_trigger`：校验 ACTIVE / 依赖 / 审批 → `ready` → `queued` → `repo.enqueue_run` → checkpoint `goal_step_triggered`。
- `mark_step_waiting_user`：只把该步骤置 `waiting_user`，**独立步骤不受影响**。
- `complete_goal`：校验 `plan_complete` + 证据齐全后，按状态机强制走到 `SUCCEEDED`。
- `_advance` 增补 `triggered` 分支：还有步骤在等触发规则时保持目标 ACTIVE，不误判完成。

## 5. 主动消息与 Trace

`runtime/proactive_message.py`（新，124 行）：

- `post()` 写 `TurnRecord(source="runtime_event", status="completed", turn_sequence=peek_next_turn_sequence)` +
  `Event(event_type="llm_response", payload={content, message_source, proactive, trigger})`，
  与用户消息共用唯一时间线与同一序号空间，前端无需特殊分支即可渲染。
- 线程内存在**活跃租约的 running Turn** 时抛 `ProactiveBlocked`；Handler 返回 `RETRY_LATER`（15s 后重试），
  由 Outbox 串行补发，避免并发写入导致乱序或丢失。
- 同一 `turn_id`（`uuid5(trace_id + 文本前缀)`）重复调用返回既有事件，重试不产生重复消息。

可见性边界：

- 聊天读模型（`ThreadState.list_thread_messages_page`）只渲染用户可见事件；system 提示词、内部决策指令、工具上下文**不进聊天**。
- Trace 保留完整实际输入：`ContextSnapshot.context_items[0].role == "system"`，含系统提示词、历史消息、触发事件；
  `LLMCall.input_preview` 同步留存；`/api/debug/traces/{id}` 与 `/api/inspector/context-runs/{id}` 可查看。

## 6. Handler 事务边界

`worker/handlers_goal_triggers.py`（新，204 行）`handle_goal_trigger_decision`：

1. 短事务：读任务/计划/步骤/触发事件；复核 ACTIVE、步骤未完成、`last_trigger_key` 未过期、任务不忙（DISPATCHING/RUNNING/VERIFYING/RECONCILING → `RETRY_LATER`）、当日决策数未超上限。
2. 事务外调用模型。
3. 独立短事务写主动消息；`ProactiveBlocked` → `RETRY_LATER("foreground_turn_active")`。
4. 独立短事务执行决策 + 写 Trace；提交后 `ws_manager.broadcast_to_thread_sync("new_message")`（广播失败只记日志，不影响落库）。

## 7. 测试

`tests/unit/backend/test_goal_triggers.py`（新，632 行，16 例，Fake LLM，全离线）：

| # | 用例 | 断言要点 |
|---|---|---|
| 1 | once_at 幂等 + 重启恢复 | 连续扫描只入队 1 次；新服务实例扫描仍为 0 |
| 2 | recurring 时区 | 上海时区落点正确，`next_trigger_at` 指向下一场 |
| 3 | window_once 首次命中 | 窗内第一个匹配事件触发，之后不再触发 |
| 4 | window_once 到期 | 未满足 → 步骤 MISSED + `expired_at`，不入队 |
| 5 | event_condition | 事件类型 + payload 子集匹配才触发 |
| 6 | opportunistic | 冷却期内不触发、超日上限不触发、**无事件不调 LLM** |
| 7 | 运行中任务 | RUNNING 时 scan 跳过，回到 BLOCKED_USER 后可触发 |
| 8 | no_action | 计划 revision 不变，写 `goal_decision` 审计 + 完整 Trace，聊天无消息 |
| 9 | run_step | 只入队一次 `agent_task_run`，服务端复核依赖 |
| 10 | modify_plan | revision 递增、计划内容落库 |
| 11 | complete_goal | 证据不全被拒；证据齐才 SUCCEEDED |
| 12 | ask_user | 真实助手消息进聊天页（`llm_response`），序号连续 |
| 13 | 聊天 vs Trace | 聊天页不含系统提示词，`ContextSnapshot` 保留完整 system |
| 14 | 前台 Turn 占用 | 首次 `RETRY_LATER`，租约释放后补发 `turn_sequence=2`，重试不重复 |
| 15 | 用户沉默 | 只阻塞依赖该步骤的下游，独立步骤照常触发 |
| 16 | 旧 schedule | once/weekly 归一化为 `TriggerRule`，行为不变 |

回归：

- `pytest tests/unit/backend/test_goal_triggers.py tests/unit/backend/test_long_term_goal.py -q` → 43 passed
- `pytest tests -q` → **1325 passed, 23 skipped**（PG 契约 10 例 + 本地 Embedding 13 例按环境跳过）
- `ruff check backend/aiive --select F,E9` → All checks passed
- `basedpyright backend/aiive` → 0 errors, 0 warnings, 0 notes

## 8. 修改文件

| 文件 | 变更 |
|---|---|
| `task_runtime/goal_plan.py` | 新增 `TriggerKind`/`TriggerRule`/归一化与排期函数；`GoalStep` 增补触发状态字段 |
| `task_runtime/goal_triggers.py` | 新增：纯函数规则求值 + 幂等键 + 过期判定 |
| `task_runtime/goal_trigger_service.py` | 新增：确定性扫描 → 事件 + Outbox |
| `task_runtime/goal_decision.py` | 新增：上下文 / LLM / 校验 / 执行 / Trace |
| `task_runtime/goal_service.py` | 新增 4 个决策入口；`_advance` 增补 triggered 分支；`attach_plan` 写 `armed_at` |
| `runtime/proactive_message.py` | 新增：主动消息写入唯一时间线 + 前台 Turn 冲突保护 |
| `worker/handlers_goal_triggers.py` | 新增：`goal_trigger_decision` Handler（三段短事务） |
| `worker/job_types.py` / `worker/outbox_handlers.py` | allowlist + HandlerRegistry 注册 |
| `worker/scheduler_daemon.py` | `_agent_task_scanner_job` 追加 `GoalTriggerService.scan()` |
| `config.py` | `aiive_goal_trigger_max_decisions_per_day`（默认 12，>0 校验） |
| `docs/CODE_MAP.md` / `docs/plans/current_phase.md` | 图谱与阶段文档同步 |

数据库：**无表结构变更、无迁移**（复用 `agent_tasks.task_state`、`agent_task_events`、`outbox_jobs`、`turn_records`、`events`、`context_snapshots`、`llm_calls`）。

## 9. 遗留风险

1. 事件源范围：`event_condition`/`opportunistic` 目前只消费同线程 `Event` 与 `goal_step_completed` 任务事件；
   文件变更、外部 Webhook 等需显式接入（可复用 `TriggerEvent` 结构）。
2. `recurring` 只支持 daily/weekly（`frequency` + `weekday` + `time` + `timezone` + `until`），不支持 cron 表达式；
   复杂日历需扩展 `TriggerRule`。
3. `window_once` 过期只把步骤置 MISSED 并通知，不会自动改计划；是否改计划仍由用户/决策链路决定。
4. 主动消息与前台 Turn 的冲突用 Outbox 重试（15s）解决；未实现用户级“稍后再说/免打扰”抑制。
5. 每日决策上限是**任务无关的全局上限**（`decisions_today` 按 task 统计），多目标并发时单目标可能被其他目标挤占额度。
6. 主动消息广播依赖 `ws_manager` 已绑定主事件循环；未绑定时只落库不推送（前端下次拉取可见），与既有通知行为一致。
7. `test_goal_triggers.py` 依赖测试夹具的 `SessionLocal` 分发语义：Handler 内不得对“无写入”的路径调用
   `rollback()`（会回滚外层事务，吞掉夹具数据）。当前 `ProactiveBlocked` 分支已改为只 `close()`。

## 10. 运维注意

- 新配置项：`AIIVE_GOAL_TRIGGER_MAX_DECISIONS_PER_DAY`（默认 12）。调低可限制主动打扰频率，调 0 会在启动校验被拒。
- 观察点：`AgentTaskEvent(goal_trigger_fired)` 与 `goal_decision` 的成对出现率；`goal_trigger_decision` 的死信率；
  `ProactiveBlocked` 重试次数（持续偏高说明前台 Turn 长时间占用）。
- 回滚：删除 allowlist 中的 `goal_trigger_decision` 即可停用触发决策，计划与历史数据不受影响（无迁移）。
