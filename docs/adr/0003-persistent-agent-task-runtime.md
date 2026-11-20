# ADR-0003：持久化个人 Agent Task Runtime

- 状态：已采纳
- 日期：2026-08-11
- 范围：P0–P8 持久任务、控制面、Desktop 执行、观察面与受控自改进

## 决策

AIive 将长期对话与具体执行拆成彼此独立的两个运行世界：

```text
Conversation Plane                Task Plane
Epoch / Segment / Turn            Task / AgentRun / Action
人格、关系、历史与记忆             状态、事件、证据与产物
Main Agent                        Worker Agent
4 个 Task Meta Tools              Task-scoped Capabilities
```

Main Agent 固定只暴露 `delegate_task`、`get_task_status`、`cancel_task`、
`send_task_input`。Desktop、文件、Shell 和 Self-Improvement 能力只能由 Task
Worker 提议，并通过 Capability Broker 执行。Main Conversation 通过
`agent_task_created` 卡片关联委派工作。任务终态可以唤醒 Main Agent 验收结果，最终答复进入对话时间线。工作中心展示执行过程、证据和产物。

## 持久化事实模型

`agent_tasks` 是跨请求、模型调用、服务重启和节点离线长期存在的业务实体；
`agent_runs` 是一次有租约、有预算的认知执行；`agent_actions` 是最小可审计副作用。
三者关系明确为 `Task != AgentRun != Action`。

每个 Task 还拥有：

- 单 Task 单调递增、追加式的 `agent_task_events`；
- 不复制 messages 的 `agent_task_checkpoints`；
- 只进入 Worker Prompt 的结构化 `TaskBrief`、`TaskState` 与压缩 Evidence；
- 内容寻址的 `task_evidence`、独立 `task_artifacts` 和结构化 `TaskReport`；
- AgentRun、Action、审批、Watcher 和资源锁的完整审计关联。

Task 采用确定性状态机：

```text
queued -> dispatching -> running
running -> blocked_approval | blocked_user | blocked_node | reconciling
running -> verifying -> succeeded | partial | failed
any non-terminal -> cancelled
```

每次 wake 都创建新的 AgentRun。审批或用户输入只把 Task 重新入队，不恢复旧 LLM
session。Worker 从 TaskBrief、TaskState、Checkpoint、关键事件、Evidence 摘要、Artifact
引用和当前 Task-scoped capability schema 重新组装上下文。

## 控制面和审批

唯一副作用路径是：

```text
Worker proposal -> Capability Broker -> deterministic scope/policy
                -> frozen Action -> Approval (if required) -> Executor
```

Broker 强制 capability allowlist、路径/网络/secret scope、服务端权威 capability
descriptor、risk policy、资源锁和执行前 precondition。审批冻结 Action、arguments、
descriptor、preconditions、effects、checkpoint、有效期与 approval hash。批准不会直接执行；
它重新验证冻结内容和可观察前置条件，然后将 Action 置为 `ready` 并创建新的 AgentRun。
如果资源在批准后变化，Action 进入 `invalidated`，旧审批不可复用。

## Desktop Protocol v2

Desktop Node 使用协议版本和共享认证 token 建立连接；服务端只接受内置权威 capability
定义，不信任 Node 自报的未知工具。每个下发请求携带稳定 `task_id`、`run_id`、
`action_id`、`idempotency_key`、arguments hash、scope、preconditions 和 fencing token。

Node 在副作用前按原子文件持久化 Journal：

```text
received -> started -> committed | failed
```

相同 Action 的终态结果直接重放；`received/started` 的未知动作拒绝重复执行。Node 在紧邻
副作用处再次检查 scope/preconditions，并持有本地 path/workspace 或 foreground GUI lease。
断线导致服务端无法确认时，Action 为 `unknown`、Task 为 `reconciling`；重连后服务端查询
Journal，以 result hash 对账，再恢复 Task，绝不盲目重试未知副作用。

## 恢复、并发和观察

- Outbox 的 `agent_task_run` 是唯一 Worker 唤醒入口；operation id 保证入队幂等。
- Startup/周期 scanner 回收过期 AgentRun；已成功 Action 不重放，未知 Action 等待对账。
- 数据库资源锁以排序后的资源键获取并带租约；GUI 使用每 Node 唯一 foreground key。
- 代码 Task 只有显式声明 `isolation_mode=git_worktree` 时才创建独立 worktree，并将
  Task scope 收紧到该 workspace。
- File、time 和 node Watcher 只产生事件和 Task wake，不调用 LLM 轮询。

### Main Agent 跟进任务终态

`TaskRepository.transition_task` 将 `succeeded`、`partial`、`failed` 与 `main_agent_task_terminal:<task_id>:<status>` Outbox 作业同事务写入。载荷包含任务、线程 ID 和状态。用户主动取消由取消入口确认，Forget 取消也无需唤醒。原有 Goal 订阅仍由 `goal_event_dispatch` 匹配，Goal 自身的终态由 Main Agent 跟进。

Worker 校验 claim、线程和 Forget 来源，通过稳定键将同线程到期作业批量写入 `TurnEventInbox`。前台 Turn 执行时，从工具结果已配对的边界进入下一次模型调用，Outbox 等待收口。空闲时，`TurnExecutionService` 以 `runtime_event` 来源走正常租约、`ContextAssembler`、`AgentGraph` 和 `TurnRecord`。一次唤醒最多领取五个任务终态，剩余作业继续排队。

Main Agent 通过 `get_task_status` 查看原始目标、验收标准、报告、预算和用量，再按需续读分页或 Evidence，判断是否反馈、继续委托或无需行动。Graph 在成功状态工具结果返回后记录本轮读取时间。正常收口与最终答复同事务，显式独立提醒与其 Inbox 回执同事务。空闲唤醒批次无法全部收口时暂缓批次答复，由各作业重试；需要另行处理的事件仍有自己的 Outbox 作业。成功答复写入后经 `new_message` 送达在线客户端，离线客户端通过会话历史取得。

线程繁忙或事件仍在排队时延迟重试。中断或租约过期后，旧 Inbox 领取失效，从 Outbox 重领并刷新读取回执。重复投递遇到已处理事件直接结束。模型或读取持续失败时按 Outbox 退避，三次失败后保留 `deadletter` 与诊断原因，修复后可运维重排原作业。来源屏蔽在投递、注入、工具读取和收口时校验。

### 新任务的预算

`TaskBudget` 从 `AIIVE_WORKER_DEFAULT_*` 取得默认值。省略、空对象或字段为 `null` 时使用有效默认值。创建时保存完整有限预算，之后配置变更适用于新任务。既有任务保留原先保存的上限与空缺语义。显式值可在 Schema 范围内调高或调低默认值。

| 字段 | 出厂默认 | 新任务硬上限 | 计量 |
|---|---:|---:|---|
| `max_runs` | 8 | 128 | 累计执行 Run，独立成稿 Run 单独处理 |
| `max_model_calls` | 32 | 512 | Worker 决策调用，包含发现与纠错 |
| `max_actions` | 24 | 256 | 累计创建的 Action，包含已进入审批或被策略拦截的 Action |
| `max_actions_per_run` | 8 | 32 | 单 Run 批大小，能力发现也占用批位置 |
| `max_tool_failures` | 6 | 64 | 已计入的工具执行失败，审批、取消、失效和未知结果不计 |
| `max_total_tokens` | 512,000 | 8,000,000 | Worker 决策的累计输入和输出。缺少供应商用量时按预估保守计入。成稿用量在 `report.report_usage` |
| `max_wall_time_seconds` | 7,776,000（90 天） | 31,536,000（365 天） | 从创建起计时，含排队、等待审批和用户输入。在 Worker 执行检查点生效 |
| `max_cost_usd` | 无 | 1,000,000 | 仅按配置好的输入和输出 Token 价格估算 Worker 决策费用。缺少价格时，带美元上限的决策以 `cost_pricing_unavailable` 停止 |
| `deadline_at` | 无 | — | ISO 8601 绝对时间，无时区按 UTC，和经过时间上限同时生效 |

默认保留现有的八步 Run 批大小，24 个 Action 配合 32 次决策为发现、纠错和收尾留出空间，八次 Run 容纳多批执行和等待后恢复。累计 Token 默认取配置的模型窗口两倍，出厂窗口为 256,000，适用于重复发送上下文的 Worker 决策循环。90 天限制任务生命周期，涵盖跨周、跨月计划和等待。常规执行资源由 Run、Action、调用次数、Token 等预算控制。美元估算使用服务端配置的模型单价，独立成稿记录单独的 Token 用量。

## Evidence、UI 与 API

大型结果不进入 Prompt：超过阈值后完整字节写入内容寻址 Object Store，数据库只保存
摘要、hash 和引用；REST 支持上限明确的 range read。Task Center 展示 Task、AgentRun、
Action timeline、审批、Evidence、Artifact、错误和 TaskReport，并通过 Task WebSocket
接收实时事件，REST 负责断线补齐。旧 `/api/tasks` 提醒接口保持不变，新的持久任务使用
`/api/agent-tasks`。

## Lifecycle / Trusted Core

Self-Improvement 也必须作为 Task 运行，并遵循 Candidate -> targeted tests/eval ->
isolated health check -> promote -> rollback。普通 Main/Worker 都不能直接写 Trusted Core。
服务端在规划和应用两处阻止修改 authentication、Capability Broker、Policy、Approval、
Release/Rollback、数据库迁移和 Task Runtime 等受保护路径；promotion 还要求已有通过的
目标测试 Evidence。旧 Selfdev 直写 API 需要独立 Trusted Core 管理 token，force promote
被禁用。

## 兼容性与运维要求

- 原 Turn-aware Approval 路径继续工作；统一 Approval 表通过 owner XOR 约束区分 Turn/Task。
- 生产环境必须配置非默认 `AIIVE_DESKTOP_NODE_AUTH_TOKEN` 和
  `AIIVE_TRUSTED_CORE_ADMIN_TOKEN`，并通过 TLS/WSS 传输。
- Schema 只由 Alembic 迁移推进；启动失败时不得绕过迁移继续运行。
- Prompt 全部通过 `backend/aiive/prompts/manifest.toml` 版本化，禁止在运行代码中内联。

## 验收映射

自动化覆盖 Main 工具隔离、Conversation/Task Context 隔离、简单与多 Action Task、
数小时审批后新 AgentRun、重启恢复、Desktop unknown Journal 对账、TOCTOU invalidation、
GUI/path lease、Watcher 唤醒、大输出 Evidence range read，以及 Trusted Core 不可变性。



## P1 用户工作契约（2026-11-12）

用户视角由 WorkReadModel 唯一投影。Web 与 Android 保留独立 React 界面，共享
`shared/work` 的纯 TypeScript 契约、写操作分流、同步控制器和文件算法；Desktop 使用 Web。
普通 Task 不展示推测百分比，Goal 使用 `GoalPlan.progress()`。Action 摘要复用有界且
经过 Forget 可见性过滤的查询，不暴露原始 arguments 或 TaskState。

Attention 的 allowed_actions 仅提示 UI；服务端写接口继续独立授权。Action Approval、
Goal Step Approval、Task Input 与 Goal Question 使用各自入口。Question 的 reject 跳过
当前步骤，Goal reject 拒绝整个目标。内容修改失效当前及依赖步骤；API 对已有步骤按 ID
保留未编辑字段，避免安全视图省略的 trigger.match 被编辑提交意外擦除。执行机制仍由
现有 GoalService 和 Task Runtime 负责。

Thread `/ws/{thread_id}` 是工作同步入口。work_changed 在事务提交后发出，包含
 task_id、task_version、event_sequence，仅通知数据失效。客户端刷新 REST Inbox/WorkView，
使用 Timeline 原始高水位补齐；重连、前台和网络恢复后重新对齐。快照替换旧缓存以遵守
Forget。聊天保持挂载，与工作页互斥订阅 Thread；没有 Task WebSocket 客户端和业务轮询。

文件读取单次最多 1 MiB；原生 Android 逐段缓存和计算 SHA256，通过后复制 Documents
或分享 Cache URI；Web 使用有界 Blob。文件名和 MIME 白名单由公共模块处理。
详见 [Android 流程](../guides/ANDROID_APP.md) 和 [P1 验收报告](../reports/2026-11-12_P1_work_center.md)。
