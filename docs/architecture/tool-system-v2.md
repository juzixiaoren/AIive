# Tool System V2

更新：2026-11-17。本文描述类化工具、执行协调、结果契约与取消恢复的实际实现。

## 审查与决策

原注册表同时保存 Callable、生成 Schema、检查来源、分配线程、处理超时，
execute / execute_approved 重复实现入队和验证。db_handler 通过隐式属性暴露
原函数，Operation 执行器据此注入事务。MCP、Desktop 使用动态闭包；Worker
把决策控制函数伪装成永远不能执行的工具。

统一类化与执行协调合理，但不合并既有的持久化记录。ToolOperation、
AgentAction、Desktop Journal 分别对应不同事务、安全边界与恢复依据。

## 工具接口与注册

`tools/base.py` 定义 BaseTool、DatabaseTool、ToolExecutionContext、
CancellationPolicy、CancellationToken、CancelResult、ToolError、ToolResult。

工具业务实现为类的 `_run` 方法。BaseTool.execute(context, arguments) 统一
注入 ctx / db / _execution，业务方法类型签名与参数元数据生成 Pydantic
参数模型；MCP/Desktop 使用完整 JSON Schema。工具可覆盖 execute、
normalize_error、request_cancel、reconcile、compensate。

DatabaseTool 的 db 由执行适配器在实际执行线程内创建。共享工具实例仅保存
定义或固定远端标识，取消令牌、事务、截止时间与授权数据属于单次 Context。
ToolRegistry.register 只接受 BaseTool 实例；没有 FunctionTool、Callable
注册方式或 db_handler 隐式属性。全部 builtin、Attachment、Task 元工具、
Forget、notify_user、answer_goal_question、MCP、Desktop 均采用类实现。

ToolDefinition 包含安全配置、描述、参数契约、执行节点、取消策略、幂等与
对账声明。descriptor_hash 覆盖有效 Schema、安全属性、实际超时、节点与
取消/幂等/对账声明。注册快照共享静态工具定义，不共享单次执行状态。

Worker 控制协议由 core/function_spec.py 的 FunctionSpec / FunctionSpecs
单独承载。控制 Spec 没有执行方法，也不进入 ToolRegistry。

## 执行边界

- ToolExecutor：统一工具查找、来源/指纹/Schema 校验、执行前取消检查、
  适配器选择、执行 Context、错误/超时响应。Registry 的三个执行方法仅转发。
- DirectAdapter：调用工具并**先归一化再决定事务**；DatabaseTool 的业务
  事务在实际工作线程创建、提交/回滚与关闭（详见下节）。
- TurnOperationAdapter：创建或复用 ToolOperation / Outbox，按 operation_id
  有限等待。Operation 的事务执行继续将业务副作用和 committed receipt
  原子提交；非重复外部操作的 UNKNOWN 不重放。
- Task：CapabilityBroker 继续负责 Scope、来源可见性、指纹、审批流程与资源
  锁；完成授权后进入同一 ToolExecutor。AgentAction 仍是 Task 的幂等单元。
- DesktopAdapter：消费 Broker 的 Action envelope，通过 WebSocket 发送
  action_id / request_id / fencing_token；Journal 是断线与重启对账依据。

Attachment 的 Vision 前后事务、Forget Phase A 的授权交接，以及运行期元工具
自身服务事务仍保留各自领域边界，不把 Session 搬进跨线程 Context。

## 统一执行结果契约

`tools/base.py` 的 `coerce_tool_result` 是工具返回值进入调用方之前的唯一
归一化入口，`ToolExecutor.execute_direct` 是唯一转换点。其它层不再各自解析
`ok` / `allowed`。

- 失败识别只看业务字典**顶层**标记（`ok is False` / `allowed is False`），
  绝不递归扫描嵌套业务数据；`ToolError`、`ToolResult(error=...)`、
  `ToolResult(value={"ok": false})` 同样判为失败。
- 失败时原业务字典保留在 `ToolResult.value` 供诊断，业务字段进入
  `ToolError.details`；`payload()` 永远优先输出错误封套。
- `execution_unknown` 与 `UNKNOWN_OUTCOME_CODES`（Desktop 超时/断连）统一
  改写为 `execution_unknown` 并强制 `outcome_known=False`；只有结果确定时才
  可能判为 `cancelled`。

事务判断与结果判断必须同源：

- DirectAdapter 先 `coerce_tool_result`，`error is not None` → `rollback`
  后返回规范化结果；无错误才 `commit`。
- `operation_executor._execute_db_transactional` 使用同一份判定；业务失败时
  整个事务（含业务写入）回滚，再以独立短事务记录 `failed`。
- 失败/未知在 `ToolOperation.effect_receipt` 中持久化
  `{"outcome_known": ..., "side_effect_started": ...}`，`operation_response`
  原样透出，晚到的查询不会丢失"已经开始产生外部影响"这一事实。

`outcome_known` 与 `side_effect_started` 相互独立：确定性失败也可能已经写了
半个文件（`True/True`），未知结果也可能确认尚未开始（`False/False`）。
未显式声明时，UNKNOWN 默认 `side_effect_started=True`，确定性失败/取消默认
`False`；显式值优先。终态只由契约决定，`side_effect_started` 不参与推导
成功/失败/UNKNOWN。

## Desktop 发送边界

`DesktopDispatchError.phase`（`desktop/protocol.py::DesktopDispatchPhase`）
以"请求是否已写往 Node"为界：

| 场景 | phase | 工具结果 |
| --- | --- | --- |
| 主事件循环不可用 / Node 离线 | NOT_SUBMITTED | `execution_failed`，确定未执行 |
| 本地信封参数校验失败 | NOT_SUBMITTED | `execution_failed` |
| 写 socket 过程中异常 | SUBMITTED | `execution_unknown` |
| 已发送后等待超时 / 断连 | SUBMITTED | `execution_unknown` |
| 同一 Action 已有在途请求 | SUBMITTED | `execution_unknown` |
| Node 执行前拒绝 | TERMINAL | 按 `classify_pending_error` 映射 |

写 socket 失败按 UNKNOWN 处理：本机没有 ACK 不能证明对端没有收到请求。取消
投递失败不改变本次派发结论；UNKNOWN 保留资源锁并等待 Journal 对账。
Task 路径（`DesktopAdapter`）与聊天路径（`DesktopTool._run`）共用
`desktop_dispatch_result` 的阶段映射。

## 取消与结果

取消策略区分 none、cooperative、remote、transactional。数据库内置工具声明
transactional；本机 Shell 声明 cooperative；desktop_exec 声明 remote；
Desktop 节点必须在 capability 中显式声明 remote，旧节点默认 none。
目前 MCP Runtime 没有暴露可确认停止的执行句柄，明确声明 none。

1. 启动前检测取消，阻止业务调用。
2. Turn.cancel_requested_at / AgentAction.cancel_requested_at 保存持久取消意图。
3. Turn 取消时，queued Operation 可立即收敛 cancelled；运行中事务的
   Operation 行锁用 SKIP LOCKED 跳过，避免阻塞 Turn 取消意图提交。
4. 数据库 Operation 在业务执行前、提交前重读 Turn 取消，取消则回滚整个
   事务后以 fencing token 记录 cancelled。
5. 执行线程按 Context 传递取消令牌；只有实际停止才能返回 cancelled。
6. 不支持中断的工具等待真实结果。先完成的成功回执保留成功。
7. Shell 进程树可终止，但已经产生的外部效果不能撤销：报告 UNKNOWN，
   保留日志与对账需求。补偿不自动执行。
8. Desktop 取消同时校验三项执行身份；启动后的文件操作返回 not_cancellable。
   断线/超时不代表取消成功。Node 终态写 Journal，再通过原结果通道回传。

Outbox 的外部执行在前台等待超时后仍继续保存真实终态；不会因前台 Turn
取消或有限等待结束而丢弃晚到回执。

Task 取消后的崩溃恢复（`recover_cancelled_execution`）：Task=cancelled 且
Run=running、租约过期时，Run→interrupted，dispatched/running 的 Action→
unknown，Task 保持 cancelled，不重放、不重新入队；重复执行是空操作。Worker
异常分支在取消任务上收敛成功并**提交事务后**返回 COMPLETED（否则仍返回
RETRYABLE_ERROR），避免下一次投递只读到同一个终态的空转。Forget 已进入 Phase A 时等待其确定终态，
共享执行授权继续由实际线程独立获取、释放与恢复。

ToolError 包含 code、message、outcome_known、side_effect_started、retry_hint、
details。未知外部结果不标记 retryable；参数纠错仍保留 Function Calling 的
argument_validation_error / issues / retryable 契约。

## 迁移与部署

迁移 `e6f90123b405` 为 tool_operations、agent_actions 增加可空
cancel_requested_at，并允许 ToolOperation 与 Desktop receipt 的 cancelled
状态。旧记录保持 NULL，不凭空推断历史操作已取消。旧幂等键、执行 token、
Outbox 租约和 Journal 身份保持不变。

先升级数据库，再重启后端及 Desktop Node。新的完整定义指纹会使升级前的
待审批工具定义失效；重新审批而非沿用旧授权。旧运行中外部记录恢复时继续
进入 UNKNOWN / 对账，不重放。回滚数据库前将新增 cancelled 状态映射为
failed，保留审计原因；降级不会恢复被取消的工作。

## 验证入口

- 全量 `python -m pytest`；PG 通过 AIIVE_PG_TESTS / AIIVE_PG_TEST_URL 显式启用。
- `test_tool_execution_cancellation.py`：真实线程并发、隔离、取消确认与 none 策略。
- `test_pg_tool_v2_cancellation.py`：真实 PostgreSQL + Outbox，排队取消、
  行锁竞争与提交前回滚，证明取消意图可在工具持有事务锁时提交。
- `test_tool_operation_execution.py`：幂等、事务 receipt、有限等待与最终提交。
- `test_tool_result_contract.py`：结果契约、DatabaseTool 六种结果的事务边界、
  outcome_known / side_effect_started 组合、operation receipt 字段。
- `test_tool_result_consumers.py`：LangChain ToolMessage、回执解析、MCP、Desktop。
- `test_task_action_result_contract.py`：Action 终态与事件中的副作用事实。
- `test_desktop_dispatch_phase.py`：真实连接管理器 + 模拟 WebSocket 的发送
  前后失败分类。
- `test_task_cancelled_execution_recovery.py`、`test_agent_contract_recovery.py`：
  取消恢复幂等与 Outbox 返回值。
- `test_tool_and_cancel_recovery_pg.py`：PostgreSQL 上的结构化失败回滚与
  SKIP LOCKED 并发恢复。
- Desktop Node：journal、locks、cancellation 三组测试；Web/Android 构建与共享
  chat/work 契约测试；Ruff、basedpyright、Alembic upgrade/check。
