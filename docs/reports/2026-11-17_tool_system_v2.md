# Tool System V2 交付验证

## 实现

BaseTool / DatabaseTool 统一可执行能力；业务代码迁入类方法，未保留 Callable
注册或通用函数包装工具。ToolDefinition、ToolExecutionContext、ToolError /
ToolResult、CancellationPolicy / CancelResult 位于 tools/base.py 与 registry.py。
普通、审批与 Task 调用共用 ToolExecutor；保留 Outbox、Broker、Task Action、
Desktop Journal 与 Forget 授权交接边界。Worker 控制契约独立为 FunctionSpec。

详细接口、状态与部署顺序见 [设计文档](../architecture/tool-system-v2.md)。

## 验证结果

| 验证 | 证据 |
| --- | --- |
| 全量后端 | `python -m pytest -q --disable-warnings`：2,098 passed、151 skipped；跳过项主要是默认不启用的 PG 专项、Embedding 服务与 Windows 下的 POSIX 部署脚本 |
| PostgreSQL 关键回归 | contracts、forget_exec_auth、forget_v6261、tool_v2_cancellation：37 passed；独立临时 pgvector/pg16，真实连接和事务 |
| 最终定向回归 | Persistent Task、ToolOperation、取消、Agent Contract、Desktop：78 passed；随后 Desktop 声明兼容、Task 与取消定向回归：42 passed |
| Desktop Node | 12 passed；包括真实 Node Host 入口启动/终止进程、身份 fencing、Journal UNKNOWN 重启后不重放 |
| Web | TypeScript + Vite build；chat 15 passed、work 20 passed |
| Android | TypeScript + Vite build |
| Desktop | Electron Forge Windows x64 package |
| 静态检查 | Ruff `--select F,E9` 通过；basedpyright 0 errors |
| 数据库 | PostgreSQL 从旧 head 升级；3 条旧 Operation（queued/committed/execution_unknown）状态保留，取消时间为 NULL；`alembic check` 无差异；downgrade / upgrade 回环通过 |

PG 取消测试证明：取消可以在执行事务持有 Operation 行锁时提交；业务写入和
receipt 一同回滚；Task 已派发 Action 收到取消信号并收敛真实终态。未把 Task
取消、连接断开或 Python 线程等待超时视为工具已停止。

## 迁移与能力边界

迁移为 `e6f90123b405`，仅增加可空取消意图字段并扩展取消终态约束。旧审批
指纹因完整定义更新而失效，需重新审批。先迁移并更新后端，再更新 Desktop。
旧 Desktop 不声明 remote 支持时保持 none；MCP 当前不暴露可确认停止的句柄，
同样声明 none。部分外部副作用保留 UNKNOWN，不能自动重试或自动补偿。

工作区开始时干净，基于与 origin/main 一致的 fa5c1b9 开发。提交前检查
`git diff --check` 与完整文件列表；没有用户已有修改被覆盖。
