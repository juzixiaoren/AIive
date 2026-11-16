# Agent 工具契约与有界恢复

ToolRegistry 是有效工具契约的唯一入口。注册时生成并保存 Canonical JSON Schema；明确提供的 `input_schema`（包括 MCP/Desktop 原始定义）具有最高优先级，不再叠加另一份 parameters 校验。没有显式 Schema 时，复用现有严格 Pydantic 参数模型：handler 类型注解、默认值及 parameters 元数据共同生成定义；嵌套 `schema` 元数据可直接复用领域模型的 `model_json_schema()`。嵌套模型的 `$defs` 提升到根节点，引用保持有效。

`delegate_task.budgets` 使用 TaskBudget 的完整 Schema，保留 extra forbid、数值边界和默认值。`max_steps` 不存在，也没有别名转换。goal_plan、deterministic_action 同样复用已有领域契约。任意 JSON 数据字段（例如 Action arguments、用户扩展 payload）保留显式开放对象语义；固定结构字段声明 properties/items。

注册时 descriptor_hash 总是重新计算，覆盖完整有效 Schema、描述、安全属性和执行节点。传入的旧指纹不能遮蔽契约变化。Registry 对外完整定义中的 input_schema 与 parameters properties 来自同一契约；catalog 继续只返回轻量目录。

Main Chat 继续 Function Calling。LangChain StructuredTool 直接接收 Canonical JSON Schema 字典，不另造参数模型；适配器单独传递真实 ToolCall ID，不把身份注入业务 Schema。LangChain 供应商转换可以去除 title 这类展示注释，但必须保留所有有效约束。AgentGraph 保留现有三次总尝试的本地校验和 ToolMessage 反馈。执行结果继续携带 error_type、execution_status、operation_id，额外保留 retryable/issues；执行失败不会自动进入参数纠错重放。

统一的 validate_tool_params 使用 JSON Schema Draft 2020-12 校验，支持嵌套对象、数组、组合类型、本地引用、枚举与约束。integer 使用严格 JSON/Python 整数检查，不转换字符串或把 bool/float 当成整数。失败只反馈路径、约束、错误类别和实际值类型，不复制实际敏感内容。default 是 Schema 注释，省略参数仍由 handler/领域模型应用既有默认值。

Worker System Prompt 注入 ActionProposal.model_json_schema()。JSON mode 只确保语法；JSON Repair 后仍执行 Pydantic strict=True 校验。解析/结构错误统一为 AgentOutputValidationError，与 TaskNotRunnableError、TaskTerminalError、TaskNotFoundError 分离。

Worker 决策流程：

1. 检查任务预算；每次模型调用前持久化 model_calls 并续租。
2. 解析 ActionProposal；保存成功或最终失败响应的 Token Usage。
3. action 决策查找当前完整注册定义，先 validate_tool_params。
4. 参数正确才进入 Broker Scope/Policy、创建 AgentAction、审批/READY 和 Dispatch。
5. execute_brokered 再检查定义指纹和参数，然后才调用 handler。

结构错误和参数错误共享连续三次总尝试（初次 + 两次纠正）；纠错计数写入 Task state，跨基础设施重试保留；每次反馈真实结构化错误，下一次重新生成 Proposal，不创建失败参数对应的 Action。max_model_calls、Token、时限预算可以更早终止。有效决策清除旧反馈。Scope 拒绝不属于参数纠错，不会扩大权限。纠错耗尽时，无进度任务 FAILED，已有成功 Action 的任务 PARTIAL，当前 Run FAILED 且清除 Lease。

工具注入按完整定义控制字符预算。超限删除整个工具定义并切换 discovery，记录 omitted_capability_ids；TaskContextAssembler 不再对工具 JSON 做字符截断。单个工具定义超过预算时也不会注入残缺 Schema。

Outbox Handler 对有效 current_run、RUNNING 状态和未过期 Lease 安全去重；过期运行复用 recover_stale_tasks，生成恢复事件/Checkpoint 和下一次唤醒。在调用后续模型/派发前，续租重新读取 Run 与 current_run，避免旧 Worker 继续使用被替换的运行。

意外异常回滚后重新锁定可见 Task，并确认 current_run 仍是本次创建的 Run（旧 Worker 不得回收新 owner）：Run FAILED；无在途 Action 时 Task QUEUED，写 run_failed、Checkpoint 并提交，由已有 Outbox 有界重试处理。存在 dispatched/running/unknown Action 时改为 UNKNOWN + RECONCILING，禁止自动重放。LLM 超时/限流保留基础设施重试语义。Forget 的 TaskContentUnavailable 分支与执行授权保持原有边界，不读取或恢复已隐藏任务。未增加数据库字段或迁移。

验收包含真实 handle_agent_task_run（真实数据库事务、有效 Outbox claim，只有 LLM 边界离线替换），覆盖纠错、预算、Token、恢复、重投、Scope 和未知副作用。SQLite 验证逻辑状态收敛；生产 PostgreSQL 的并发锁保证仍由现有专项测试负责。
