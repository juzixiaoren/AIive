# Agent 工具契约与可靠性改造交付记录

日期：2026-11-16

## 基线与范围

基于本地 main / 缓存 origin/main 的 `7ea542a`。已检查相关最新本地历史，包含附件授权隔离与存储根目录改动，按当前代码实现而非旧方案覆盖。两次远端 fetch 分别因连接重置、传输超时失败，因此未能确认 GitHub 上是否存在更新提交。

用户原有 `docs/plans/current_phase.md` 和 `docs/reports/2026-11-12_P1_work_center.md` 的修改保留，不纳入本次提交。没有数据库字段或迁移变更，没有改动 CapabilityBroker、Approval、Resource Lock、Trusted Core 或 Reconciliation 的授权规则。

## 修改文件与设计

| 文件 | 变更 |
| --- | --- |
| backend/aiive/tools/tool_validation.py | 从原严格参数模型派生 Canonical Schema；显式 input_schema 优先；复用领域模型 Schema、合并本地引用；以 jsonschema 取代不完整手写校验，严格整数、不做类型转换，输出安全路径与期望约束 |
| backend/aiive/tools/registry.py | 注册时保存有效完整契约并重新生成 descriptor_hash；发现视图、管理视图均从同一契约导出 |
| backend/aiive/tools/langchain_adapter.py | Function Calling 直接使用完整 Schema 字典；ToolCall ID 独立注入；错误反馈保留执行状态与详细问题 |
| backend/aiive/tools/builtin_tools.py | 补全补丁 operations 数组及 Working State payload 的嵌套字段；动态扩展字段保持原有语义 |
| backend/aiive/task_runtime/tools.py | budgets 复用 TaskBudget；goal_plan 复用 GoalPlan；确定性 Action 复用 ActionProposal 字段定义，保留可选 null 与默认值 |
| backend/aiive/task_runtime/context_assembler.py | Worker 注入 ActionProposal.model_json_schema；移除对工具定义的字符截断 |
| backend/aiive/task_runtime/tool_injection.py | 超预算按整个定义裁减并切换 discovery，记录被省略的 capability ID |
| backend/aiive/task_runtime/errors.py | 明确任务状态异常与 AgentOutputValidationError |
| backend/aiive/task_runtime/agent_executor.py | Repair 后 Pydantic strict 校验；结构化错误与 Usage 一起返回运行时；下一调用附真实纠错反馈 |
| backend/aiive/task_runtime/runtime.py | 参数校验在 Broker/Action 创建前；初次 + 最多两次纠正，共享预算并跨基础设施重试保存计数；失败响应计入 Token；耗尽按进度 FAILED/PARTIAL、Run FAILED；记录本次拥有的 Run ID |
| backend/aiive/task_runtime/repository.py | 明确状态异常；续租重读 Run/current_run，拒绝旧 owner 继续运行 |
| backend/aiive/worker/handlers_agent_tasks.py | 移除宽泛 ValueError 状态分支；有效 Lease 去重、过期运行复用恢复；异常事务提交 Run FAILED、Task QUEUED、事件与 Checkpoint；在途未知 Action 转 Reconciliation；旧 Worker 不回收新 Run |
| pyproject.toml | 将 jsonschema 声明为直接依赖 |
| tests/integration/backend/test_agent_contract_recovery.py | 22 项确定性验收：真实 Handler + 数据库事务 + 有效 Outbox claim，仅离线替换模型边界 |
| tests/unit/backend/test_identity_memory.py | 保留原身份指导断言，改检验最终 Function Calling Schema，适配标准 JSON Schema args_schema |
| docs/architecture/agent_tool_contract.md | 记录契约来源、优先级、注入、校验、纠错、预算与异常恢复流程 |

## 验证证据

- `python -m pytest tests/unit/backend tests/integration/backend/test_agent_contract_recovery.py -q`：**1392 passed, 5 skipped, 29 warnings，279.83s**。5 项为 Windows 下需要 POSIX bash 的既有测试；warnings 为现有测试事务/依赖警告。
- 相关集成回归：`test_forget_v625_outbox.py`、`test_phase3_handlers.py`、三组 `test_context_assembler_*`：**28 passed，12.69s**。
- 最终异常恢复专项（新 Handler 验收 + Persistent Task Runtime）：**52 passed**。
- CI 同口径 Lint：`ruff check backend/aiive --select F,E9`：**通过**。
- `basedpyright`：**0 errors, 0 warnings, 0 notes**。
- `git diff --check`：**通过**。

完整最终日志保存在 `E:/AIive/artifacts/agent-contract-2026-11-16/`，主要为 `regression-tool-contract-verified.txt`、`integration-tool-contract-final.txt`、`recovery-contract-final.txt` 和 `typecheck-tool-contract-final.txt`。

验收包含：完整 budgets、max_steps 精确拒绝、max_actions 通过、内置/MCP/Desktop 统一契约、嵌套约束与指纹变化、严格解析与 Repair、结构/参数纠错、连续三次停止、无 Action/副作用、Scope 不扩张、UNKNOWN 不重放、已有进度 PARTIAL、超时/限流恢复、Outbox 重投有效/过期 Lease、Usage 跨失败保留、旧 Worker 与新 owner 隔离、跨基础设施重试的纠错次数上限、完整定义预算裁减。

## 限制与后续注意

- 验证为离线 SQLite；未运行真实 PostgreSQL 并发锁、真实模型、真实 MCP 或 Desktop 节点。现有 PostgreSQL/外部执行机制保持原有实现，本次未削弱安全边界。
- 外部工具的原始 input_schema 若声明开放对象，其开放语义保留；无法从工具方未声明的任意数据结构推断额外字段契约。
- 单个工具定义本身超过注入预算时，整个定义会省略并保留 discovery 语义，不注入损坏 Schema。
- 远端 main 最新状态因网络不可达未验证；本地提交基于已检查的 main。未推送远端。

Git commit 在最终交付回复中列出，提交包含本记录。
