# Tool System V2 收尾：事务、执行阶段与取消恢复

日期：2026-11-17。基线 `9866614`（上一轮 Tool System V2 契约与取消恢复）。
本报告只记录本轮四个缺陷的根因、修改与验证。

## 1. 根因

### 1.1 DatabaseTool 事务判断不完整（P0）

`DirectAdapter.invoke` 用 `business_result_failed(result)` 判断是否回滚，而该
函数只接受 `Mapping`。数据库工具若返回 `ToolError`、`ToolResult(error=...)`
或 `ToolResult(value={"ok": false})`，判定为"非失败"，业务事务照常 `commit`，
随后 `ToolExecutor` 归一化出错误并向上报告失败——**副作用已提交却报告失败**。

### 1.2 side_effect_started 被 outcome 吞掉（P1）

`ToolError.from_business_result` 直接写 `side_effect_started=unknown`。于是
`{"ok": false, "error_type": "execution_failed", "outcome_known": true,
"side_effect_started": true}`（确定性失败但已写了一半文件）被压成
`outcome_known=True, side_effect_started=False`，丢失"已经产生外部影响"的事实；
Operation 回执同样只在 `unknown` 时写 True。

### 1.3 Desktop 不区分发送前后（P1）

`DesktopAdapter` 把所有 `DesktopDispatchError` 映射为 `execution_unknown`。
Node 离线、主事件循环不可用、本地信封校验失败都发生在请求写出之前，请求一定
没有到达 Node，却被报成"外部副作用未知"，任务进入 RECONCILING 且保留资源锁。
反向的同类问题也存在：聊天路径（`DesktopTool._run` → `dispatch_sync`）把
这些异常统一交给 `normalize_error`，同样一律 UNKNOWN。

### 1.4 取消恢复成功后仍要求重试（P2）

`handle_agent_task_run` 的异常分支在取消任务上成功收敛并 `commit` 后，仍然
返回 `RETRYABLE_ERROR`。下一次投递只会读到 `cancelled` 终态并直接
`COMPLETED`，产生一次没有实际工作的重复投递。

## 2. 修改

| 文件 | 内容 |
| --- | --- |
| `tools/base.py` | `coerce_tool_result` 保留原始业务值；`ToolResult(value={"ok": false})` 也归一化为失败；`from_business_result` 让 `outcome_known` 与 `side_effect_started` 互相独立（显式值优先，未显式时 UNKNOWN→True、确定性失败/取消→False）；`payload()` 对直接构造的失败字典兜底 |
| `tools/executor.py` | `DirectAdapter` 先 `coerce_tool_result` 再决定提交/回滚，失败时返回规范化结果；新增 `desktop_dispatch_result` 统一 Desktop 阶段映射；`execute_direct` 的通用异常分支对 desktop 工具识别 `DesktopDispatchError` |
| `tools/operation_executor.py` | 失败/未知回执持久化 `{"outcome_known", "side_effect_started"}`；`operation_response` 透出两个字段（committed 默认 `side_effect_started=True`） |
| `desktop/protocol.py` | 新增 `DesktopDispatchPhase`（NOT_SUBMITTED / SUBMITTED / TERMINAL） |
| `desktop/connection_manager.py` | `DesktopDispatchError(phase=...)`；信封校验前置于登记（参数错误判 NOT_SUBMITTED）；`_send` 无 socket→NOT_SUBMITTED、写 socket 失败→SUBMITTED；`_submit_to_loop` 处理循环不可用；等待超时→SUBMITTED；Node 终态→TERMINAL；取消投递失败不再中断本次派发 |
| `worker/handlers_agent_tasks.py` | 取消收敛并提交后返回 COMPLETED，事务失败仍 RETRYABLE_ERROR |
| `task_runtime/runtime.py` | `action_failed` 事件 payload 保留 `outcome_known` / `side_effect_started` |
| `tests/tool_helpers.py` | 适配 DirectAdapter 新的数据库工具返回值（解包 ToolResult，断言不变） |
| 文档 | `docs/CODE_MAP.md`、`docs/architecture/tool-system-v2.md` |

关键调用链（失败判断唯一化）：

```
tool._run() ─┬─ DatabaseTool ──> DirectAdapter: coerce → error? rollback : commit
             └─ 其它 ─────────> execute_direct: coerce_tool_result(...).payload()
operation_executor._execute_db_transactional: coerce → error? 整事务回滚 + failed
operation_executor._execute_external: execute_direct → outcome_is_unknown ? unknown : failed
DesktopAdapter / execute_direct: DesktopDispatchError.phase → NOT_SUBMITTED 失败 / 其余 UNKNOWN
```

## 3. 修复前后

| 场景 | 修复前 | 修复后 |
| --- | --- | --- |
| DatabaseTool 返回 `ToolError` | 业务行已提交，调用方收到失败 | 回滚，业务行不存在 |
| DatabaseTool 返回 `ToolResult(value={"ok": false})` | 已提交 | 回滚 |
| 确定性失败 + 已产生副作用 | `side_effect_started=False` | `True`（原始错误码与 details 保留） |
| UNKNOWN + 显式 `side_effect_started=False` | 被强制 True | 保持 False，`outcome_known=False` |
| Node 离线 / 信封非法 / 循环不可用 | UNKNOWN + 保留锁 + RECONCILING | 确定性失败，立即释放锁 |
| 写 socket 失败 / 等待超时 | UNKNOWN | UNKNOWN（不变，语义明确） |
| 取消任务收敛成功 | Outbox 再投递一次 | 返回 COMPLETED，无重复投递 |
| 取消收敛事务失败 | RETRYABLE_ERROR（但状态已写） | RETRYABLE_ERROR 且状态整体回滚 |

## 4. 新增测试

- `tests/unit/backend/test_tool_result_contract.py`：真实 SQLite 事务下的
  六类结果（`ok=false`、`allowed=false`、`ToolError`、`ToolResult(error)`、
  `ToolResult(value=ok:false)`、成功），每种失败都断言业务行不存在；
  `outcome_known` × `side_effect_started` 四组合；operation receipt 字段。
- `tests/unit/backend/test_tool_result_consumers.py`：LangChain 内容保留两个
  标志与业务字段。
- `tests/unit/backend/test_task_action_result_contract.py`：`action_failed`
  事件保留副作用事实。
- `tests/unit/backend/test_tool_operation_execution.py`：外部工具确定性失败
  但已产生副作用时，`effect_receipt` 持久化 `True/True`。
- `tests/unit/backend/test_desktop_dispatch_phase.py`：真实
  `DesktopConnectionManager` + 运行中的事件循环 + 模拟 WebSocket，覆盖未发送
  （循环/离线/信封）、发送中失败、发送后超时、Node 终态、两条适配路径。
- `tests/integration/backend/test_agent_contract_recovery.py`：取消恢复返回
  COMPLETED 且不重复记录事件；恢复事务失败返回 RETRYABLE_ERROR 且状态回滚。
- `tests/integration/pg/test_tool_and_cancel_recovery_pg.py`：PostgreSQL 上
  五类结构化失败均回滚（真实事务），以及 SKIP LOCKED 并发恢复。

## 5. 验证结果

| 验证 | 结果 |
| --- | --- |
| 后端单元 | 1474 passed |
| 集成 + 流程 | 685 passed |
| PostgreSQL 专项（临时 pgvector/pg16 容器） | 9 passed |
| Desktop Node | 12 passed |
| Web 构建 / Android 构建 | 均成功 |
| Web 契约测试 | chat 15 passed、work 20 passed |
| Ruff `--select F,E9` | 通过 |
| basedpyright | 0 errors, 0 warnings |
| 迁移 | 无模型变更，无新迁移；`alembic` job 照常验证 |

## 6. 边界与未改动项

- 取消恢复、UNKNOWN 不重放、资源锁保留、deadletter / fencing / 重试语义均未改。
- `run_shell` 自身超时仍返回 `error_type="timeout"`（确定性失败）：只读命令
  超时是可重试的常见场景，执行器级超时对写外部世界的工具仍判 UNKNOWN。
- 非取消任务的基础设施异常仍返回 RETRYABLE_ERROR，行为不变。


## 7. 补充：Shell 超时副作用与 Desktop pending 生命周期

### 7.1 Shell 超时

`run_bash` 超时后终止进程树并返回 `ok=False, error_type="timeout",
timed_out=True`，但最初没有声明副作用状态，归一化后默认 `side_effect_started=False`。
超时前命令可能已经写成文件或发出请求，这个事实被丢掉了。

现在超时分支显式返回 `outcome_known=True` 与 `side_effect_started=True`；
`error_type` 保持 `timeout`（不改成 execution_unknown），stdout/stderr、
exit_code、timed_out 与进程树终止行为不变。启动前失败（`_failure`：bash 缺失、
cwd 不存在）显式声明 `outcome_known=True, side_effect_started=False`；普通非零
退出不声明副作用（确定性结果，按既有契约默认 False）。

验证：真实 bash 执行 `echo "partial" > partial.txt; sleep 30`（1s 超时）后
`partial.txt` 内容正确，且返回/归一化/Operation 回执三层都保留
`True/True`。

### 7.2 Desktop pending 生命周期

`dispatch_sync` 与 `dispatch_action_sync` 原先先写入 `_pending` 再调用
`_submit_to_loop`，而这一步在 try/finally 之外：主循环关闭时抛
`NOT_SUBMITTED`，pending 残留，同一 action_id 的重派被误判为
`desktop_action_already_in_flight`（进而报成 UNKNOWN）。

现在 pending 的创建/提交/发送/等待都在同一个 try/finally 内，收尾用
`_discard_pending` 按对象身份删除：未提交 → 清掉且请求确实没发出（确定性失败，
可安全重派）；已提交但发送失败/等待超时 → 仍然 SUBMITTED / UNKNOWN，清理等待器
不改变阶段；旧请求的收尾不会误删同 request_id 下新建的 pending。action_id、
request_id、fencing_token、Journal 与幂等回放语义未变。

验证：`test_desktop_dispatch_phase.py` 16 例，覆盖无循环、无连接、提交前循环
关闭（两条路径）、发送失败、等待超时、超时后同 id 重派成功、旧收尾不误删新
pending、Node 终态正常返回。

### 7.3 补充：终止未确认的 Shell 超时

超时后 `_terminate_tree` 现在返回"是否已确认进程退出"（kill 后 `wait(5s)` 成功
才为 True）。此前该函数吞掉所有失败，随后无条件 `outcome_known=True`——即使
进程树可能仍在运行并继续产生副作用，也把结果报成已知。

现在超时分支记录 `terminated`：确认终止 → `outcome_known=True`、
`side_effect_started=True`（保留 `error_type="timeout"`）；未能确认 →
`outcome_known=False`，归一化后为 `execution_unknown`，Task Action 进入
UNKNOWN 并保留资源锁，Operation 记为 `execution_unknown` 而不是 failed。
同时修复取消分支：原先取消后 `proc.wait(5)` 抛超时会被外层当作 `timed_out`
（把取消误标成超时），现在统一由 `_terminate_tree` 确认。

验证：`_terminate_tree` 的确认/未确认单测；真实 bash 下强制"终止未确认"后
返回值、归一化、执行器 payload 三层都保持 `outcome_known=False,
side_effect_started=True, details.terminated=False`。
