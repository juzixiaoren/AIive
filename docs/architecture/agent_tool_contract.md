# Agent Function Calling 与持久任务执行边界

Main Chat Agent 与 Persistent Task Worker 共用 LangChain Function Calling。`core/function_calling.py` 提供模型工厂、Function Schema 定义、调用归一化、参数验证、错误 ToolMessage、纠错策略和 Usage 归一化。共享组件不执行工具、不提交数据库事务、不在内部循环调用模型。

## DeepSeek Thinking 协议

`AIIVE_LLM_PROVIDER=auto` 按 API URL 的主机名识别 `api.deepseek.com`。反向代理可以设为 `deepseek`，`openai_compatible` 使用通用 ChatOpenAI。`AIIVE_LLM_DEEPSEEK_THINKING` 默认 `enabled`。

AIive 的 `AIiveChatDeepSeek` 继承 LangChain 官方 ChatDeepSeek。SDK 将非流式响应和 SSE 的推理片段读入 `AIMessage.additional_kwargs.reasoning_content`，流式片段按到达顺序合并。适配器为响应记录实际 `provider` 和 `thinking_mode`。启用 Thinking 且携带工具定义时，适配器在 Chat Completions 请求的每条历史 Assistant 消息上原样写回可用的 `reasoning_content`，也包括没有工具调用的最终答复。工具定义、选择、参数和用量仍由 LangChain 序列化或解析。

Main Agent 将每个模型工具批次的元数据写入首条 `tool_call` Event，同批调用全部写在结果之前。最终模型答复将元数据写入 `llm_response` Event。等待审批、策略拦截和已修正的校验失败保留模型协议记录。后续审批结果按原调用 ID 回填历史；审批执行和前端展示继续使用原有事实。中断时保存已确认的工具结果与对应批次元数据，重建时保留中断提示。Worker 的有界参数纠错在既有 `task_state.validation_messages` 保存修复消息和协议字段。

热历史先通过 Forget 可见性过滤，再按 Turn 和批次恢复 Assistant 及其工具结果，兼容升级前调用和结果交错的事件顺序。预算估算计入推理文本。推理字段仅用于 Assistant 协议回放，聊天历史、诊断接口、SSE、WebSocket、记忆抽取和压缩摘要使用公开正文或工具事实。

DeepSeek 在带工具的请求中遇到缺失推理字段的 Thinking 历史、或来源无法确认的旧 Assistant 历史时，当前 Turn 使用 `thinking.type=disabled`，响应元数据记为 `disabled_legacy_fallback`。已有 LLMCall 可确认来自其他供应商的历史，以及明确标记为关闭 Thinking 的历史可以直接回放。需要兼容的旧消息退出上下文后，新的 Turn 恢复配置的 Thinking 模式。

## 契约来源

ToolRegistry 是有效工具契约的唯一入口。显式 `input_schema`（包括 MCP/Desktop 原始定义）优先；否则从已有 Pydantic 参数模型、handler 类型注解和 parameters 元数据派生 Canonical JSON Schema。领域模型的 `model_json_schema()` 可作为嵌套字段来源，本地 `$defs` 引用随包装提升到根节点。

`delegate_task.budgets` 复用 TaskBudget，保留额外字段禁止、类型、默认值和约束；没有 `max_steps` 别名。注册时保存有效契约并重新计算 descriptor_hash，覆盖完整 Schema、描述、安全属性与执行节点。注入、发现、参数校验使用同一份契约。

统一 `validate_tool_params` 使用 Draft 2020-12；整数严格检查，不转换字符串，也不把 bool/float 当成整数。错误只返回路径、期望约束和实际值类型。default 是 Schema 注释，handler 与领域模型继续应用原有默认值。

## 两个适配器

Main 的 `build_langchain_tools` 从共享 `function_definition` 获取 Schema，生成 StructuredTool。模型返回的有效调用经过本地校验与策略处理后，继续由既有 ToolNode/Registry 执行。普通文本回答合法；批量参数无效时整批不执行。真实 ToolCall ID 与业务参数分离，执行结果保留 error_type、execution_status、operation_id、retryable 和 issues。

Worker 的 `WorkerFunctions` 只从当前 TaskContext 选中的 resident/available/ready-loaded 定义生成提案函数，没有绑定任何真实 handler。所有提案注册项只有拒绝执行的防御 stub；模型只接收原生函数定义字典，Worker 不使用 ToolNode 或 Registry.execute。

每个能力有一个稳定的 `propose_<能力前缀>_<哈希>` 函数，函数描述包含真实 capability_id。其参数为 `{arguments: 原始完整工具Schema, preconditions, effects, summary}`，包装层禁止额外字段。真实参数与决策元数据隔离，不维护第二份工具定义。函数名限定于供应商允许的字符与长度，并检查映射冲突。

Worker 控制函数为 `task_finish`、`task_ask_user`、`task_watch`、`task_search_capabilities`、`task_describe_capability`。控制字段从 ActionProposal 的字段 Schema 派生；watch 具有额外结构契约。函数调用只生成内部 ActionProposal DTO，具体完成、提问、观察与发现操作仍由 Runtime 处理。

ActionProposal 不再是模型输出协议。Worker 不使用 JSON mode、文本 JSON 解析或 JSON Repair，不保留旧 JSON 协议回退。模型必须支持 Function Calling；协议不合法时给出结构化纠错，耗尽后安全收敛。

## 校验与有界纠错

共享 `normalize_calls` 处理 LangChain tool_calls 和 invalid_tool_calls。损坏的参数字符串转为不可执行诊断，原始损坏文本不复制到错误或修复记录。缺失/重复调用 ID 归一化为可配对 ID，同时标记错误，不能进入执行。

共享 `validate_calls` 校验函数、参数和调用数量。Main 允许文本及批量调用；Worker 每次必须且只能有一个有效函数调用。Worker 的纯文本、空调用、多调用、未知函数、损坏参数和 DTO 结构错误均归一化为 AgentOutputValidationError。有效的参数错误反馈仍保留 argument_validation_error 的详细路径与约束。

修复使用原生 AIMessage + 对应 ToolMessage；无函数调用时使用结构化 HumanMessage 说明缺失调用。多调用错误为每个调用返回一个匹配 ID 的 ToolMessage，整批不执行。Worker 只保存最近一次修复交换到既有 task_state，并保留现有 Forget 派生来源边界；TaskContext 不重复注入这些消息。

纠错上限共用 `tool_argument_max_retries`（默认 2，即初次 + 两次修正）。Graph 与 Runtime 分别驱动下一次调用，预算与持久化由宿主负责。Worker 的失败计数跨 Outbox 重试保存；每次调用均经过 max_model_calls、Token、时限等预算检查。LangChain 模型工厂显式 `max_retries=0`，网络重试由现有宿主/Outbox 负责，不叠加隐藏 SDK 重试。

每次响应（包括最终失败的响应）记录模型与 Token Usage；网络异常无响应时不伪造 Token。纠错只针对尚未执行的调用，不自动重放已执行或结果未知的副作用。

## Runtime 与恢复

Worker 执行链：Function Calling → 共享参数校验 → 内部 ActionProposal → Runtime 参数复核 → Broker Scope/Policy → AgentAction → Approval/READY → Dispatch → 执行前指纹/参数复核 → handler → Evidence/Checkpoint。

提案生成不等于 Action 已接受或工具已成功。Runtime 继续管理授权、Approval、资源锁、Trusted Core、Forget、Lease、Action 审计、Evidence、Checkpoint、Outbox、UNKNOWN 与 Reconciliation。纠错耗尽时，无成功 Action 的 Task FAILED，有进度的 Task PARTIAL，当前 Run FAILED。

异常回收只处理本次拥有的 current_run：Run FAILED；无在途 Action 时 Task QUEUED，并提交 run_failed 事件与 Checkpoint。未知副作用转 UNKNOWN/RECONCILING，不自动重放。有效 Lease 的重复投递安全去重；过期运行复用既有恢复机制。旧 Worker 不回收新 owner 的 Run。TaskContentUnavailable 继续走 Forget 安全分支。

## 上下文与发现

工具选择按完整定义进行预算控制，超限省略整个定义并切换 discovery。绑定函数与上下文元数据从同一个 snapshot 构建；完整参数 Schema 只通过 Function Calling 传递，System/User Prompt 不再重复注入 Schema 或 ActionProposal 输出契约。TaskContext 内仍保留 in_scope、指纹、发现状态等选择信息。定义变化或卸载会使旧 loaded 定义失效。

没有新增数据库字段或迁移。对外 Task API、ActionProposal 内部字段、事件和执行审计语义保持兼容。验收采用离线 LangChain AIMessage、真实数据库事务与真实 handle_agent_task_run；生产 PostgreSQL 并发锁及在线模型/MCP/Desktop 执行仍由既有专项验证覆盖。
