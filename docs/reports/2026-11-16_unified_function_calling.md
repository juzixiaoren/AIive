# Main / Worker Function Calling 统一改造

日期：2026-11-16。基于本地 main `bfd54b9` 实施；远端 fetch 已成功，检查时 origin/main 无需合并的新提交。

## 结果与关键设计

两个工具 Agent 统一使用 LangChain Function Calling。Main 继续运行既有 Graph；Worker 使用专用提案函数与控制函数，只生成内部 ActionProposal，不执行真实工具。没有新建 HTTP 工具协议或复制完整 AgentGraph，没有增加数据库字段或迁移。

共享层 `core/function_calling.py` 负责模型工厂、Canonical Function Schema、tool_calls/invalid_tool_calls 归一化、参数校验、ToolMessage 反馈、有限纠错策略和 Usage 归一化。它不执行工具、不提交事务、不在内部循环请求模型。Main 与 Worker 分别保持 Graph/Runtime 的生命周期与预算管理。

Worker 从 TaskContext 的同一份选中工具快照生成 `propose_<能力前缀>_<哈希>` 函数。arguments 完整复用原始工具 Schema，preconditions/effects/summary 放在独立包装层，防止与业务参数冲突。本地 `$defs` 引用随包装保持完整。函数名稳定、限定字符及长度，并检查冲突。函数绑定只包含定义字典，不含真实 handler。

控制函数为 task_finish、task_ask_user、task_watch、task_search_capabilities、task_describe_capability；调用校验通过后确定性转换为 ActionProposal，再交 Runtime 执行既有业务流程。ActionProposal 保留内部字段与审计语义，退出模型直接输出协议。

Worker 每次必须且只能产生一个函数调用。纯文本、空调用、多调用、未知函数、损坏参数和领域 DTO 结构错误，都通过统一错误反馈有限纠正。Worker 不再使用 JSON mode、JSON Repair 或文本 JSON 输出回退。调用失败时保存最近一次 AIMessage/ToolMessage 修复交换，严格匹配 tool_call_id；不保留损坏参数原始字符串。纠错次数跨 Outbox 重试保持，受 max_model_calls、Token 及其他预算约束。

Main 共用 Schema、参数校验、反馈和纠错策略，仍允许普通文本回答及原有批量调用。高风险参数错误在创建审批请求前进入纠错。所有真实执行仍走原 Registry/ToolNode 或 Persistent Task Runtime。SDK max_retries 显式设为 0，避免与宿主纠错、Outbox 重试叠加隐藏调用；网络错误使用已有归一化错误与恢复机制。

Runtime 保留参数复核、Scope、Policy、Approval、Resource Lock、Trusted Core、Forget、Lease、Outbox、Evidence、Checkpoint、UNKNOWN 和 Reconciliation。提案成功不会被反馈成工具执行成功。TaskContentUnavailable 的安全分支保留；已执行或结果未知的副作用不进入参数纠错自动重放。

Prompt 不重复注入完整工具 Schema 或 ActionProposal 输出 Schema；原生绑定与上下文元数据使用相同 snapshot。两份 Worker Prompt 版本更新为 3.0.0。

## 修改文件

- 新增 `backend/aiive/core/function_calling.py`：共享 Function Calling 组件。
- 新增 `backend/aiive/task_runtime/function_decisions.py`：Worker 提案/控制定义及 DTO 映射。
- 修改 `backend/aiive/runtime/agent_graph.py`、`backend/aiive/tools/langchain_adapter.py`：Main 接入共享组件。
- 修改 `backend/aiive/task_runtime/agent_executor.py`、`context_assembler.py`、`runtime.py`、`errors.py`、`schemas.py`：Worker 协议迁移、纠错消息与 Runtime 边界。
- 修改 `backend/aiive/prompts/manifest.toml` 与 `task_runtime/worker_system.md`、`task_context.md`：更新协议说明及版本。
- 新增 `tests/unit/backend/test_function_calling.py`：共享机制、提案隔离、Schema、消息配对、Main 高风险纠错测试。
- 修改 `tests/integration/backend/test_agent_contract_recovery.py`：保留原恢复验收场景，迁移为原生函数调用；新增真实 Handler 协议与恢复测试。
- 修改 `tests/unit/backend/test_worker_tool_injection.py`：验证完整 Schema 经原生绑定传递，Prompt 不重复注入。
- 修改 `tests/integration/backend/test_approval_security.py`：修正旧夹具的手写指纹，使用实际有效契约指纹；定义变化测试改为改变真实契约描述。审批生产校验未放宽。
- 更新 `docs/architecture/agent_tool_contract.md` 及本报告。

## 验证证据

1. 后端全量回归：`pytest tests/unit/backend tests/integration/backend/test_agent_contract_recovery.py -q`：**1414 passed、5 skipped，301.37s**。5 项跳过是 Windows 缺少 POSIX bash 的既有脚本测试。
2. 全量本地集成回归：`pytest tests/integration/backend -q`：**673 passed，103.04s**，涵盖 MCP、Desktop、Approval、Forget、Outbox 与 Task 链路。
3. 最终协议/恢复/审批专项：`pytest tests/unit/backend/test_function_calling.py tests/unit/backend/test_agent_graph.py tests/integration/backend/test_agent_contract_recovery.py tests/integration/backend/test_approval_security.py -q`：**77 passed，18.85s**。包含全量回归后补充的 Main 损坏调用、高风险调用、TaskContentUnavailable 边界及最终消息反馈修正。
4. `ruff check backend/aiive --select F,E9`：**通过**。
5. `basedpyright`：**0 errors、0 warnings、0 notes**。
6. `git diff --check`：**通过**。

全量测试中的现有 warnings 主要为 SQLite datetime 适配器弃用和测试事务提示，未屏蔽、未删除测试。测试计数存在重叠，不相加。

日志目录：`E:/AIive/artifacts/function-calling-2026-11-16/`。最终证据为 fc-backend-regression.txt、fc-integration-final.txt、fc-verified-final.txt、fc-typecheck-final.txt。

## 范围与限制

测试离线替换模型边界，但通过真实 handle_agent_task_run、真实数据库事务和有效 Outbox claim 验证恢复。未调用在线 LLM、真实外部 MCP/Desktop 节点，未运行 PostgreSQL 并发专项。部署使用的模型端点需要支持原生 Function Calling；不支持时不会回退旧 Worker JSON 协议。

本次统一 Main 工具调用与 Worker Action 决策，其余领域分类器的 LLM 接口保持现有实现。

用户原有 `docs/plans/current_phase.md`、`docs/reports/2026-11-12_P1_work_center.md` 修改保留，不纳入提交。提交与 origin/main 推送结果在交付回复中记录。
