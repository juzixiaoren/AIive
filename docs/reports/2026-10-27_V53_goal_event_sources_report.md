# V53 通用计划事件源扩展 — 交付报告

日期：2026-10-27
范围：`backend/aiive/task_runtime/`、`backend/aiive/worker/`、`backend/aiive/selfdev/`、`tests/unit/backend/`、`docs/`

## 1. 目标与结论

让长期目标（`long_term_goal`）能响应**任务执行、知识维护、自进化修复**三类真实运行事件，
并在命中后走既有"确定性匹配 → Outbox → LLM 结构化决策 → 服务端校验 → 主动消息"闭环。

结论：事件源已接入，复用既有 `OutboxJob` / `AgentTaskEvent` / `TriggerRule` / `GoalDecisionEngine` /
`ProactiveMessageService`，**未新增事件总线、未新增任务引擎、未新增数据表、未新增迁移**。

## 2. 事件源盘点（按真实生产链路）

| 事件类型 | 生产点（唯一收口） | 触发时机 | 关联范围 |
| --- | --- | --- | --- |
| `task_completed` | `task_runtime/repository.py::transition_task` → `publish_task_terminal_event` | 任务进入 succeeded / partial / failed 终态 | 线程级（`task.thread_id`） |
| `task_cancelled` | 同上 | 任务进入 cancelled | 线程级 |
| `knowledge_maintenance_completed` | `worker/handlers_knowledge.py::_publish` | 维护结束且 `healthy` 或"无自动可修复进展"（partial） | 全局 |
| `knowledge_maintenance_failed` | 同上 | 仅最后一次重试（`retry_count + 1 >= max_retries`）仍异常 | 全局 |
| `selfdev_repair_completed` | `selfdev/repair_loop.py::persist_repair_run` | 修复记录落库且 `ok=True` | 全局 |
| `selfdev_repair_failed` | 同上 | 修复记录落库且 `ok=False` | 全局 |

不接入的事件：记忆生命周期（`memory.*`）、改进候选发现（`improvement_discovery`）、通知类事件。
原因：前两者已有独立闭环且缺少"目标可响应"的明确使用场景，按"不为凑齐类型虚构事件"处理；
`memory.*` 仍可经既有线程 `Event` 扫描路径被 `event_condition` 命中（原路径未变）。

目标自身任务（`task_type == long_term_goal`）的终态**不发布**事件，避免"目标完成 → 事件 → 再唤醒自己"的自激。

## 3. 投递与匹配

```
生产者（与业务写入同一事务）
  → goal_events.publish_goal_event(...)
       ├ has_active_goal_subscribers 为假 → 直接丢弃（不制造 Outbox 噪声）
       └ OutboxJob(job_type=goal_event_dispatch,
                   operation_id=goal_event:<source>:<event_id>,
                   payload={event_id,event_type,source,thread_id,occurred_at,event_payload})
worker/handlers_goal_events.py::handle_goal_event_dispatch
  → GoalTriggerService.dispatch_event(event)
       ├ 取活动目标：task_type=long_term_goal 且状态 queued/blocked_*
       ├ 线程级事件只匹配同线程目标；全局事件匹配所有活动目标
       └ 逐步骤：trigger_of → 仅 event_condition/window_once/opportunistic
                 → arm_rule → match_event(类型 + payload 子集) → evaluate_rule
                 → 命中则 _fire（AgentTaskEvent + OutboxJob goal_trigger_decision）
```

- **无匹配不调 LLM**：`dispatch_event` 只做确定性判断，返回 `goal_event_matched:0` 时不会产生决策作业。
- **事件只带最小元数据**：`_bounded_payload` 把载荷封顶 2000 字符，超限只保留 id/status/title 等定位字段；
  目标计划、Evidence、近期对话由 `GoalDecisionEngine.context` 按需读取。
- **目标关联**：任务事件带 `thread_id`，只唤醒同线程目标；知识与自进化事件是全局事件，`thread_id=None`。
- **未知事件类型**：`publish_goal_event` 直接抛 `ValueError`，避免拼错类型的事件静默失效。

## 4. 触发 LLM 决策

匹配成功后仍走既有 `goal_trigger_decision` 作业：

- 幂等键：`event_condition:<event_id>` / `window_once:<start>:<event_id>` / `opportunistic:<event_id>`，
  `operation_id = goal_trigger:<task>:<step>:<sha1(key)[:16]>` 唯一，重复匹配不产生第二条决策。
- 决策作业额外携带 `trigger_event_type` 与 `trigger_event_payload`（外部事件没有聊天 `Event` 行）；
  `handle_goal_trigger_decision` 优先使用载荷，缺失时才回查 `Event`（原路径不变）。
- 服务端校验完全复用：`GoalDecisionEngine.validate`（action/步骤状态/依赖/审批）、
  `GoalService.begin_step_from_trigger`（依赖/审批/状态机）、`decisions_today` 每日上限、
  `CapabilityBroker`（真正执行时的权限/预算/审批）。
- 主动消息：`ProactiveMessageService.post`（`TurnRecord(source=runtime_event)` + `Event(llm_response)`），
  前台 Turn 持租约时 `ProactiveBlocked` → `RETRY_LATER`。

## 5. 窗口过期

`window_once` 到期未满足条件时：

1. `expiry_of` 返回 `TriggerExpiry(reason="window_expired", key="window_once_expired:<start>")`；
2. `_scan_task` 仍先 `_mark_expired`（`expired_at` + `status=missed` + `goal_trigger_expired` 事件）；
3. 随后用同一个 `_fire` 路径入队**一次**决策作业（`reason=window_expired`），LLM 可 `no_action` /
   `send_message` / `ask_user` / `wait_user` / `modify_plan` / `complete_goal`；
4. 幂等：`expired_at` + `last_trigger_key` 双重保证重复扫描、进程重启都不再唤醒；
5. 已暂停/结束/完成的目标：`plan.status != ACTIVE` 或任务不在活动状态集合内，扫描与事件匹配都会跳过。

## 6. 可靠性与成本

- 事件投递与业务写入同事务：任务终态、维护结果、修复记录要么一起落库，要么一起回滚。
- 消费失败 → `RETRYABLE_ERROR`，由既有 Outbox 重试/恢复（claim token + lease + dead letter）接管。
- 重复事件：`operation_id` 唯一键（`goal_event:<source>:<event_id>`）；
  同一事件对同一规则：`step.last_trigger_key`；同一触发对同一作业：`goal_trigger:<...>`。
- 冷却与频率：`opportunistic` 的 `cooldown_hours` / `max_per_day` / `max_per_week` 未改动；
  Handler 侧 `aiive_goal_trigger_max_decisions_per_day`（默认 12）仍是硬上限。
- 无周期性 LLM 轮询：事件型规则只在事件到达或窗口到期时入队。

## 7. 修改文件

新增：

- `backend/aiive/task_runtime/goal_events.py`：`GoalEvent`、`publish_goal_event`、`has_active_goal_subscribers`、
  `event_from_job_payload`、`publish_task_terminal_event`、事件类型常量与载荷裁剪。
- `backend/aiive/worker/handlers_goal_events.py`：`handle_goal_event_dispatch`。
- `tests/unit/backend/test_goal_event_sources.py`：13 例离线验收。

修改：

- `task_runtime/goal_triggers.py`：`TriggerExpiry` 增加稳定幂等键；`expiry_of` 给出 `window_once_expired:<start>`。
- `task_runtime/goal_trigger_service.py`：新增 `dispatch_event` / `_dispatch_task`；过期也触发一次决策；
  `window_once` 触发后清空 `next_trigger_at`；决策作业携带事件类型与载荷。
- `task_runtime/repository.py`：`transition_task` 终态发布事件（`_publish_terminal_event`，失败不阻断落库）。
- `worker/handlers_knowledge.py`：维护结果发布 completed/failed 事件（`_publish`）。
- `selfdev/repair_loop.py`：`persist_repair_run` 发布修复结果事件。
- `worker/handlers_goal_triggers.py`：优先使用载荷里的 `trigger_event_type` / `trigger_event_payload`。
- `worker/job_types.py` / `worker/outbox_handlers.py`：注册 `goal_event_dispatch`（allowlist + HandlerRegistry）。
- `tests/unit/backend/test_goal_triggers.py`：窗口过期用例按新契约更新（见第 9 节）。
- `docs/CODE_MAP.md`、`docs/plans/current_phase.md`：图谱与阶段记录。

## 8. 测试结果

| 命令 | 结果 |
| --- | --- |
| `pytest tests/unit/backend/test_goal_event_sources.py -q` | 14 passed |
| `pytest tests/unit/backend/test_goal_triggers.py -q` | 19 passed |
| `pytest test_long_term_goal + test_knowledge_maintenance + test_selfdev_repair + test_outbox_worker` | 114 passed（含前两组） |
| 全量 `pytest -q --tb=line -ra`（`AIIVE_MEMORY_VECTOR_ENABLED=false`） | 1383 passed / 23 skipped / 0 failed |
| basedpyright（8 个改动后端文件） | 0 errors, 0 warnings |
| ruff（新增文件） | 仅剩仓库既有的 `UP017`/`FURB162` 风格基线，无新增逻辑问题 |

## 9. 对既有用例的唯一改动

`tests/unit/backend/test_goal_triggers.py::test_window_once_expires_without_condition` 原本断言
"过期不产生决策作业"（`_jobs(...) == []`）。V53 的需求明确要求"window_once 到期未满足条件时……还应按规则
触发一次决策"，两者直接冲突，因此该用例按新契约更新为：

- 首次扫描返回 1（过期触发一次决策），步骤仍为 `missed` 且 `expired_at` 已写；
- 作业 `reason == "window_expired"`、`last_trigger_key` 以 `window_once_expired:` 开头；
- 再次扫描返回 0，作业数仍为 1。

除此之外未修改任何既有断言。

## 10. 验证记录

- `tests/unit/backend/test_goal_event_sources.py`：13 passed
- `tests/unit/backend/test_goal_triggers.py`：19 passed
- `test_long_term_goal.py` + `test_knowledge_maintenance.py` + `test_selfdev_repair.py` + `test_outbox_worker.py`：
  114 passed（含上面两组）
- 全量回归（`AIIVE_MEMORY_VECTOR_ENABLED=false .venv/Scripts/python.exe -m pytest -q --tb=line -ra`）：
  **1383 passed / 23 skipped / 0 failed**（257.09s；skip 为 PG 专项与本地 Embedding 专项）
- 静态检查：basedpyright（新增/改动 8 个后端文件）0 errors / 0 warnings

## 11. 遗留风险

1. 任务事件只唤醒**同线程**目标；跨线程的"任务完成"不会广播到其他线程的目标（避免事件风暴）。
2. 知识与自进化是全局事件：所有订阅该类型的活跃目标都会各决策一次，成本随目标数线性增长（受每日上限约束）。
3. Outbox 死信后事件不重放：维护任务本身需要人工重跑才能再次产生事件。
4. `task_cancelled` 事件已生产，但没有专用 `match` 语义；订阅方需显式写 `event_types: ["task_cancelled"]`。
5. `window_once` 过期决策与"用户结束目标"之间没有额外确认：LLM 仍只能走既有 action 集合，
   `run_step` 会重新校验依赖/审批，不会绕过计划顺序。
