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
Worker 提议，并通过 Capability Broker 执行。Main Conversation 只接收任务受理、
审批、补充信息和终态报告等高层事件，不保存低层工具 trace。

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
- Task budget 限制 Run、Action、单 Run 步数、Token 和 deadline。

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

