# V57 前后台 LLM 串行化与目标决策幂等 — 交付报告

日期：2026-10-28
范围：`backend/aiive/runtime/`、`backend/aiive/task_runtime/`、`backend/aiive/worker/`、`backend/aiive/db/models.py`、
`backend/alembic/versions/`、`tests/unit/backend/`、`docs/`

## 1. 目标与结论

修复前台聊天与后台目标决策之间的两个真实缺陷：

1. **并发窗口**：决策作业用"查有没有活跃 Turn → 调模型"的**读检查**做互斥。检查通过到真正调用 LLM 之间，前台
   Turn 可能刚好启动，同一线程出现两个 LLM 调用（V54/V56 的 `active_turn_for_thread` 只是快照）。
2. **重投不幂等**：Outbox 是 at-least-once。作业在 apply 提交后、finalize 前崩溃时，重投会**再调一次模型**；
   `send_message` 类决策二次发言，`modify_plan` 把 revision 推两次（V56 遗留风险已登记此项）。

结论：两处都已消除，且**只新增一张最小必要的占用表**（`thread_execution_leases` + 一个迁移）；
决策持久化复用既有 `AgentTaskEvent`（dedupe_key 幂等），未新增决策表、未新增事件总线、未新增任务引擎。
V55 流式过滤与 V56 事件恢复语义未改动。

## 2. 线程级互斥（前后台同一把锁）

新增 `runtime/thread_lease.py` + `db/models.py::ThreadExecutionLease` + 迁移 `a9d3e5f7b1c4`：

| 列 | 含义 |
| --- | --- |
| `thread_id`（主键） | 一个线程一行，天然串行化 |
| `owner_kind` / `owner_id` | `turn`（turn_records.id）/ `goal_decision`（稳定决策标识） |
| `fencing_token` | 单调递增；旧持有者的续租/释放被拒绝 |
| `lease_expires_at` | 租约到期即可被接管（进程崩溃无需人工清理） |

- **抢占是条件更新**：租约已过期或同 owner 重入才成功，返回新的 `fencing_token`；
- **每次抢占/续租/释放都是独立短事务**：函数返回后不持有任何事务，**绝不跨 LLM 调用持有数据库事务**；
- **释放不删行**，只把 `lease_expires_at` 置为过期：否则 token 每次归零，fencing 失去意义。

接入点：

| 侧 | 位置 | 行为 |
| --- | --- | --- |
| 前台 | `turn_execution.py::_resolve_and_preempt` | TurnRecord 置 running **之前**抢占；后台持有时轮询等待 `FOREGROUND_WAIT_SECONDS=35s`，超时返回既有 `turn_in_progress`（HTTP 202 可重试） |
| 前台 | `TurnHeartbeat` | 与 TurnRecord 租约一起续租线程占用；续租失败即 `lease_lost`（长 Turn 不会因占用先过期被后台抢走） |
| 前台 | `_finalize_turn` / `_mark_interrupted` / `_persist_interrupted_tool_facts` | 在各自事务内释放，与状态变更同一提交 |
| 后台 | `worker/handlers_goal_triggers.py` | 守卫（V56）通过后**先抢占**才允许调模型；失败 `RETRY_LATER(thread_lease_busy)`；持有到 apply + 主动消息 + Trace 提交完成 |

**用户输入不丢失**：TurnRecord(`not_started`) 与 `user_message` 事件在更早的短事务里已提交；同一 turn_id 重试走
`attempt_no += 1` 续跑，不会产生第二条用户消息（测试用例直接断言）。

**兼容旧状态**：拿到占用后再用 `active_turn_for_thread` 兜底——升级前启动、没有占用行的旧 Turn 仍然让路。

## 3. 决策持久化与幂等

稳定决策标识：`decision_id_for(task_id, step_id, trigger_key) = uuid5(NAMESPACE_URL, "goal_decision:<task>:<step>:<trigger>")`。

| 步骤 | 写入 | 幂等方式 |
| --- | --- | --- |
| 模型返回后 | `AgentTaskEvent(goal_trigger_decision_recorded)`，payload 含结构化决策 + meta | `dedupe_key = goal_decision:<id>` |
| 应用成功后 | `AgentTaskEvent(goal_trigger_decision_applied)`，payload 含 outcome | `dedupe_key = goal_decision_applied:<id>` |
| 重投（已应用） | 不再读模型、不再改计划 | 直接 `COMPLETED(decision_already_applied)` |
| 重投（已决策未应用） | 复用持久化决策 | **不再调用模型** |

事务一致性：计划变更、`goal_decision` 审计事件、主动消息、已应用标记、Trace 在**同一个短事务**提交；apply 内部沿用
`ProactiveBlocked` 的"整体回滚 + 交给 Outbox 重试"语义。apply 提交后崩溃，重投只能看到已应用标记。

主动消息唯一性：`ProactiveMessageService.post(dedupe_key="goal_decision:<id>")` 的 turn_id 与 `Event.id` 都由
`uuid5(thread_id + dedupe_key)` 派生——**不依赖生成文案**；同一决策重放先按 `Event.id` 幂等返回 `created=False`。

## 4. 并发时序（真实链路）

```
worker: handler
  ├ 短事务：读目标/步骤/触发；读已持久化决策（已应用 → 直接完成）
  ├ acquire_thread_lease(owner=goal_decision)   ← 抢不到 → RETRY_LATER(thread_lease_busy)
  ├ 兜底：active_turn_for_thread（旧 Turn）      ← 有 → RETRY_LATER，释放占用
  ├ LLM（无事务、持占用）→ persist_decision（短事务）
  ├ apply + mark_decision_applied + write_trace（同一短事务提交）
  └ release_thread_lease → ws broadcast

api: /chat（同一线程）
  ├ 短事务：TurnRecord(not_started) + user_message 事件提交   ← 用户输入先落库
  ├ acquire_thread_lease(owner=turn, wait=35s)               ← 后台持有时等待
  │    └ 超时 → turn_in_progress(202)，前端重试同一 turn_id 续跑
  └ 原子抢占 TurnRecord(not_started → running) → 心跳续租 → finalize 释放
```

## 5. 验收测试（`tests/unit/backend/test_goal_decision_serialization.py`，8 例，Fake LLM 全离线）

| 用例 | 断言 |
| --- | --- |
| 占用互斥/fencing | 第二个持有者抢占失败；错误 token 续租与释放均失败 |
| 崩溃恢复 | ttl=0 的占用立即失效，接管者 token +1，旧 token 续租失败 |
| 后台让路前台（检查后 Turn 启动的竞态） | `RETRY_LATER(thread_lease_busy)`，**零 LLM 调用**，无决策事件、无消息 |
| 用户输入不丢失 | 前台超时返回 `turn_in_progress`，user_message 已落库且只有一条；释放占用后同一 turn_id 续跑为 running |
| 决策持久化后崩溃 | 模型只被调用 1 次；重投复用决策并应用成功 |
| 应用后重复投递 | 3 次重投全部 `decision_already_applied`；revision 只涨一次；apply 标记只有一条 |
| 主动消息唯一性 | Event.id == uuid5(thread + 决策标识)；换文案调用 `post` 返回 `created=False` 且不新增消息 |
| 前台收尾释放占用 | `execute_turn` 完成后 `lease_holder` 为 None，后台可立即接管 |

已持久化决策不会再次调用模型：`assert len(fake.calls) == 1`（跨"崩溃 → 重投 → 应用成功"全流程）。

## 6. 回归与静态检查

- 全量：`python -m pytest -q -p no:randomly` → **1433 passed / 23 skipped / 0 failed**（1425 基线 + 8 新增；
  跳过项为 PG 与本地 Embedding 专项）。
- 相关套件（goal_triggers / goal_event_sources / turn_event_injection / turn_event_overflow /
  decision_marker_stream / chat_api / flow turn_execution / outbox_worker）→ **102 passed**。
- `ruff check --select F,E9` 改动文件全通过；`basedpyright` 改动源文件 **0 errors / 0 warnings**
  （新增测试文件 0 errors，剩余 warning 与既有测试文件同为"fixture 未标注类型"）。

## 7. 改动清单

| 文件 | 变更 |
| --- | --- |
| `backend/aiive/runtime/thread_lease.py` | 新增：线程级占用（acquire/renew/release/holder + 常量） |
| `backend/aiive/db/models.py` | 新增 `ThreadExecutionLease` |
| `backend/alembic/versions/a9d3e5f7b1c4_add_thread_execution_leases.py` | 新增迁移（down_revision=f7a1c3d5e9b2） |
| `backend/aiive/runtime/turn_execution.py` | 前台抢占/续租/释放；`_resolve_and_preempt` 返回 fencing token |
| `backend/aiive/task_runtime/goal_decision.py` | `decision_id_for` / `load_persisted_decision` / `persist_decision` / `mark_decision_applied`；`apply(decision_id=)` |
| `backend/aiive/runtime/proactive_message.py` | `post(dedupe_key=)`：turn_id 与 Event.id 由决策标识派生 |
| `backend/aiive/worker/handlers_goal_triggers.py` | 先抢占再调模型；复用/收尾已持久化决策 |
| `tests/unit/backend/test_goal_decision_serialization.py` | 新增 8 例验收 |
| `docs/CODE_MAP.md` / `docs/plans/current_phase.md` | 定位、版本索引、V57 章节 |

## 8. 已知取舍与残留风险

1. **前台最多等待 35s**：后台决策正在调用模型时，前台请求会让路；超时返回 202，由前端重试。用户输入已落库，不会丢失，
   但用户会看到一次"稍后再试"。35s 覆盖单次模型 30s 超时 + 应用余量。
2. **线程级串行**：同一线程上的多个目标决策会排队（设计如此）。低频触发场景下代价可接受；若未来需要并行，只能按
   "同一线程仍串行"拆分，不能放开同线程并发 LLM。
3. **SQLite 语义**：`with_for_update` 在 SQLite 下不生效，跨进程抢占依赖单写者 + 条件更新（`fencing_token` 匹配）；
   Postgres 路径由行锁 + 条件更新共同保证。
4. **租约行常驻**：释放不删行，表规模 = 线程数（每线程一行），无需清理任务。
