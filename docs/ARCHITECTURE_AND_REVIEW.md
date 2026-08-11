# AIive 项目架构说明与代码审查报告

> 版本：2026-07-17
> 范围：`backend/aiive`（api / core / runtime / memory / tools / worker / retrieval / forget / retention / knowledge / mcp / selfdev / db）与 `frontend/src`
> 说明：本文档分两部分——第一部分说明**当前项目架构与技术栈**，第二部分给出**审查发现与开发/修复建议**。所有结论均给出文件位置。

---

## 第一部分：项目架构与技术栈

### 1. 项目定位

AIive 是一个面向个人用户的长期运行 Agent（"个人管家"）。核心特征：

- **长期记忆**：记忆有独立的生命周期（candidate/active/sleeping/archived/forgotten）与有效性（valid/superseded/contradicted/expired）两个维度，支持证据链、谱系、冲突解析、定期维护（"每日梦境"）与遗忘 Saga。
- **有界上下文**：每轮对话在 token 预算内组装上下文，超预算硬失败（不静默截断）。
- **原生工具调用**：意图识别与工具分派完全交给 LLM 的原生 `tool_calls`，配合基于工具元数据的策略引擎做安全校验。
- **自进化与能力扩展**：支持 MCP 能力发现/安装、代码补丁槽位管理。
- **异步可靠执行**：所有需要幂等、重试、崩溃恢复的后台工作走 Outbox 模式 + 租约/心跳。

### 2. 技术栈

| 层 | 技术 |
|---|---|
| 语言 | Python 3.12+ |
| Web 框架 | FastAPI（含 lifespan、全局异常中间件、CORS） |
| 数据校验/配置 | Pydantic v2 / pydantic-settings + `.env` |
| Agent 编排 | LangChain + LangGraph（`StateGraph` + `ToolNode`） |
| LLM 接入 | OpenAI 兼容 Chat Completions（默认 DeepSeek），自研 `LLMClient` + `ChatOpenAI` 双通道 |
| Token 计量 | LiteLLM token counter |
| ORM/数据库 | SQLAlchemy 2.0 Declarative / PostgreSQL 16（psycopg 3） |
| 迁移 | Alembic（同时存在运行时 `create_all` + 手工 `ALTER`，见问题 2.1） |
| 向量检索 | Qdrant（知识库嵌入） |
| 定时/后台 | APScheduler（BackgroundScheduler）+ Outbox Worker + 心跳租约 |
| 对象存储 | 本地文件系统实现（接口兼容 S3/MinIO），删除统一走 `safe_delete` |
| 前端 | React + TypeScript + Vite + TailwindCSS + WebSocket |
| 测试 | pytest |

### 3. 分层结构

```
用户请求 (/api/chat[/stream|/system])
        │
        ▼
TurnExecutionService  ── Turn 生命周期唯一入口
        │  · 幂等指纹 + 原子抢占（not_started→running）
        │  · 递增 turn_sequence + epoch/segment 归属（Thread 行锁）
        │  · TurnHeartbeat 租约续约 + fencing 校验
        │  · 崩溃恢复（recover_orphaned_tools）
        ▼
ContextAssembler  ── 有界上下文硬门（超预算抛 ContextBudgetExceededError）
        │  · 稳定契约(system) + 自动召回 + 历史 + 工具 schema 子集
        ▼
AgentGraph (LangGraph)
        │  START → assistant → policy_check → [tools → assistant]* → END
        │  · assistant：ChatOpenAI.bind_tools 生成原生 tool_calls
        │  · policy_check：普通已注册工具 allow；高危/删除工具 confirm；未注册工具 block
        │  · tools：ToolNode 执行 + 工具结果规范化（大结果→Artifact 引用）+ WorkingState 生命周期维护
        ▼
_finalize_turn  ── 事务化落库
        │  · TurnRecord.completed（fencing 校验 affected==1）
        │  · Events（tool_call/tool_result/llm_response/chat_ended）
        │  · ContextSnapshot 轮转（current/previous/audit≤5）
        │  · 若记忆信号=EXTRACT_ASYNC → 写 memory_extraction OutboxJob
        ▼
回复返回前端；后台异步：记忆提取 / 段落密封 / Epoch checkpoint / 记忆维护 / 检索重建 / 遗忘 / 保留清理
```

### 4. 核心子系统

**运行时（runtime/）**
- `turn_execution.py`：Turn 生命周期唯一编排点，含幂等、租约、fencing、快照轮转。
- `agent_graph.py`：LangGraph 图编排，取代已废弃的 `agent_loop.py`；结构化工具记录写入 Graph state，同步与流式最终结果使用同一事实源。`message_normalizer.py` 统一新旧工具消息的 name/params/result/tool_call_id 解析；ContextAssembler、ThreadState token counting 与 LangChain 消息转换复用同一规则，畸形 arguments 保留原文并 fail-closed，不使整轮历史重建失败。
- `context_assembler.py` / `context_budget.py` / `token_counter.py`：有界上下文与 token 预算。同步 Chat 的预算超限返回 HTTP 413；SSE 建连后保持 HTTP 200，并通过统一 `error` 事件携带 `status=413`、trace、hard limit 和 partition reports。同步与流式共用同一错误载荷契约。
- `attention_manager.py`：按当前回合之前最近一条用户消息计算 4/12 小时注意力切换；首回合及决策/焦点转换时持久化 `AttentionState`，连续普通回合不重复写库。`ContextAssembler` 将结果作为独立系统消息注入整体 token hard gate，并明确其中焦点是用户来源数据而非指令；强类型快照标记为 `untrusted`。解析失败通过嵌套事务 fail-open，不污染 WorkingState 或阻断聊天。
- `policy_engine.py`：机械化工具安全校验（不做关键词匹配）。
- `epoch_manager.py` / `compaction.py` / `working_state.py` / `thread_state.py`：Epoch/Segment 分层与工作状态。`ThreadState.load_recent_messages_bounded()` 仅服务 LLM token 上下文；`list_thread_messages_page()` 仅服务 UI 历史，以 `turn_sequence` keyset 游标分页，二者不得混用。

**记忆（memory/）**：`memory_write_service`（统一事务写入）、`conflict_resolver`、`automatic_recall` + `recall_fusion`（多路召回融合）、`memory_maintenance*`（Phase 4 维护）、`core_memory_projection`（核心记忆投影）、`memory_key_registry`（键治理）。

**检索（retrieval/）**：Phase 5 统一检索，generation 机制保证查询真相源唯一，倒排 token 表 + Entry 投影，可原子重建。

**工具（tools/）**：`registry.py`（注册表 + 线程池超时执行）、`builtin_tools.py`（全部内置工具）、`safe_delete.py`（唯一删除入口）、`langchain_adapter.py`。

**Worker（worker/）**：`outbox_worker` + `handler_registry` + `outbox_heartbeat`（租约），`scheduler_daemon`（APScheduler 精确调度 + 各类扫描器），`task_worker`（到期任务扫描并原子入队）。提醒投递统一由 `reminder_delivery` Outbox Handler 执行：任务状态按 `pending → dispatching → completed` 流转，必须由 Agent 生成真实回复；重试耗尽后进入 `failed`，不生成伪回复。

**定时工具任务 TODO**：`schedule_tool_task` 当前未实现、未注册且不可用。现有调度链只支持真实提醒任务，不能持久化“到期后调用某个 ToolRegistry 工具”的请求。后续实现必须包含 Task 持久化、到期 Worker 唤醒、权限/风险复核、`ToolRegistry.execute()`、trace/action card 与终态通知的端到端闭环；在闭环完成前不得注册占位或假执行工具。

**遗忘/保留（forget/、retention/）**：Phase 6A 遗忘 Saga（HMAC 指纹、Shield、可见性过滤），Phase 6B 保留治理（安全谓词、真空清理、维护租约）。

**其他**：`knowledge/`（文档摄入 + Qdrant），`mcp/`（能力发现/安装/沙箱），`selfdev/`（补丁计划/槽位提升回滚），`supervisor/`（进程健康）。

### 5. 数据模型概览

约 45 张表，可分为：会话核心（Thread/Event/LLMCall/TurnRecord/ContextSnapshot）、记忆（MemoryRecord/MemoryProposal/MemoryEvidence/MemoryLineage/CoreMemoryBlock/MemoryRecall*）、Epoch 分层（Epoch/Segment/SegmentSummary/EpochCheckpoint/WorkingState/Artifact/*CompactionInput/*Run）、维护（MemoryMaintenanceRun/Batch/Input/Action）、检索（RetrievalIndexGeneration/Entry/Token/Run）、异步（OutboxJob）、能力/自进化（Capability*/MCPInstallRecord/CapabilityPlan/SelfDevRequest/PatchOperation）、任务/知识（Task/AttentionState/Document/Chunk）。

设计亮点：大量使用 `UniqueConstraint`（operation_id / outbox_job_id 一对一）、`CheckConstraint`（状态枚举、密封需摘要）、部分唯一索引（`is_current` 单当前版本）保证不变量。

---

## 第二部分：审查发现与建议

> 说明：`docs/audit/CODE_REVIEW_REPORT.md`（2026-07-09）已**过时**，其针对的 `agent_loop.py`/`intent_classifier.py`/`graph.py` 均已在后续重构中删除，代之以 `agent_graph.py`。本节基于当前代码重新审查。

### A. 逻辑错误 / 潜在 Bug

**A.1 `intent_type` 恒为 `"plain_chat"`（响应字段失真）— ✅ 已修复（2026-07-17）**
`runtime/turn_execution.py` 曾在 `_finalize_turn` 中硬编码 `"intent_type": "plain_chat"`。由于意图分派已改为 LLM 原生 `tool_calls`，该字段失去意义；经核实前端 `chat.ts` 仅在接口类型中声明、全项目零处实际读取。
**处理**：从响应契约中移除该死字段，同步删除前端 `ChatResponse.intent_type` 声明，并删除已退化的 `runtime/intent_router.py`（全项目零外部 import）。未复活一个"真 intent_type"，因为意图识别已统一交由 LLM。

**A.2 副作用工具超时后真实状态与返回状态可能相反 — ✅ 已修复（2026-07-20）**
原 `tools/registry.py` 在线程池内直接执行全部工具；等待超时只会停止 `Future.result()`，无法终止已运行线程，`builtin_tools._db_handler` 仍可能稍后 `commit()`。因此 UI/Event 可记录 failed，而数据库随后真实成功；用户重试还可能产生重复副作用。此前仅配置化线程池和超时时间，不能解决该一致性问题。

**处理（持久化 Worker 最终架构）**：
- 新增 `tool_operations` 唯一事实表；稳定幂等键绑定 `turn_record_id + tool_call_id + capability_id + params_hash`，同一调用重试只复用原 operation。
- 所有 `writes_external_world/can_delete` 工具由 `ToolRegistry` 原子写入 `ToolOperation + OutboxJob`，真实 handler 只由注册在 `HandlerRegistry` 的 `tool_operation` Worker 执行；同步调用仅有限等待数据库终态。
- 数据库 handler 由 Worker 注入业务 Session，在同一事务提交业务副作用、`status=committed` receipt 和终态 Event；失败整体回滚后以短事务记录 failed。
- 等待超时返回 `execution_unknown + operation_id + tool_call_id`，不得返回普通 failed；Chat、Event、WorkingState、action card 和 pending_operations 均保留 unknown 语义。后台终态通过 WebSocket 推送，payload 同时携带 `operation_id` 与 `tool_call_id`：前者精确更新 action card，后者精确更新工具调用卡片；亦可调用 `GET /api/tool-operations/{operation_id}` 查询。
- WebSocket `new_message` 携带与持久化 `llm_response` Event 一致的 `event_id`；前端以该字段作为消息身份并执行幂等去重，不使用消息正文去重。
- UI 历史分页保留 `failed`、`interrupted_unknown` 与在途 Turn 的真实工具事件；没有 `llm_response` 时仅生成空正文的结构化工具消息容器，不伪造回复。工具状态由后端统一规范化，前端对未知状态 fail-closed 为 `execution_unknown`，禁止默认显示为完成。
- 外部不可重复工具发生进程中断或不确定异常时保持 `execution_unknown`，禁止 Worker 自动重放；确定性终态才清理 `uncommitted_side_effects`。
- 关闭 `/api/tools/safe-delete` 直接 Service 旁路，删除必须经 Chat → ToolRegistry → 审批 → ToolOperation → Worker。

**A.3 记忆信号失败时默认 `EXTRACT_ASYNC`（保守但会放大成本）**
`runtime/agent_graph.py:710-714` 与 `core/action_planner.py:132-139`：分类失败一律回退 `extract_async`，即为每个失败轮次都排一个后台提取 Job。分类本身又是一次额外 LLM 调用。
**建议**：这是设计取舍（宁可多记不可漏记），可接受，但应确认 `memory_gate` 下游能幂等去重空内容，避免失败风暴时堆积无效 Job。

**A.4 Alembic 程序化调用禁用宿主应用日志 — ✅ 已修复（2026-07-17）**
切到 `alembic upgrade head` 后，应用启动时 `env.py` 的 `fileConfig(config.config_file_name)` 以默认 `disable_existing_loggers=True` 重配 logging，静默禁用 uvicorn 及 `aiive` 的既有 logger，导致迁移日志之后不再有任何输出，**表面像"启动卡住"，实则服务已正常启动**（`/health` 正常响应）。
**处理**：`env.py` 改为 `fileConfig(..., disable_existing_loggers=False)`，保留宿主应用 logger。修复后可见完整的 `Application startup complete` 等启动日志。

**A.5 ContextBudget 窗口值配错且不可配置 — ✅ 已修复（2026-07-17）**
`context_budget.py` 曾硬编码 `model_context_window=128000`（老模型 `deepseek-chat` 值），且各分区 hard 之和 130496 > 128000，`validate()` 每次启动必抛 `ValueError`（被 lifespan 的 try/except 吞掉，故长期未被察觉，直到 A.4 修复后日志才显形）。而 `.env` 实际使用 `deepseek-v4-flash`（约 1M 窗口），预算严重低估模型能力。
**处理**：新增 `.env` 可配置项 `AIIVE_LLM_CONTEXT_WINDOW`（默认 256000）与 `AIIVE_LLM_MAX_OUTPUT_TOKENS`；`ContextBudget.default()` 与 `ModelProfile` 均从配置读取窗口，`recent_messages` 分区**弹性吸收**扣除固定分区后的剩余空间（`hard = window - 固定分区和`），使 `sum(hard)` 恒等于窗口、`validate()` 恒通过。已验证 128K/256K/1M 三档均自洽，用户改一个 .env 值即整体自适应。

**A.6 ContextBudget 双来源与前端流占位竞态 — ✅ 已修复（2026-07-22）**
- `ContextBudget.from_env()` 现为预算加载与校验的统一入口：模块默认实例、`TurnExecutionService` 共享缓存、`ContextAssembler` 缺省构造和应用启动校验均复用该入口。非法 token 类型或分区 hard 总额超限会在加载阶段明确失败；环境配置属于启动期配置，运行中修改需重启进程。
- Chat 流式 token、工具调用、工具结果、完成、中断和错误清理均按本次请求创建的占位消息 ID 精确更新，不再依赖闭包中的 `messages.length` 或“最后一条空 Agent 消息”。同步 `inFlightRef` 在 React 状态提交前阻止重复发送。
- 提醒按钮继续遵循 `/api/chat/system → TurnExecutionService → ToolRegistry` 统一执行链。`confirm_reminder` / `snooze_reminder` 在事务内锁定目标 Event，校验 `reminder_created` 类型与 `alerting` 前置状态；重复确认或延期返回已有确定性结果，延期目标写回原事件，避免不同 tool call 创建多个后续提醒。前端成功后移除原提醒操作卡片。
- `remember_or_update` 继续作为 `writes_external_world=True` 的事务型副作用工具走 ToolOperation/Outbox；新增真实 Worker 集成测试，验证记忆、提案与 committed receipt 的落库闭环，不改成同步旁路。

### B. 架构可优化点

**B.1 Schema 管理双轨制（Alembic 与运行时建表并存）— ✅ 已修复（2026-07-17）**
原 `main.py._ensure_schema()` 在启动时 `create_all()` 并手工执行 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`，而项目同时维护 31 个 Alembic 迁移脚本。两套机制并存导致迁移历史与实际 schema 漂移，`create_all` 掩盖缺失的迁移——`outbox_jobs.terminal_at` 缺列报错即其现实爆发。

**根因深挖**：切换过程中通过"对空库从零跑迁移"暴露出 Alembic 这一轨**本身长期是坏的**——旧链根本无法从零建库：(1) `553f2bf14edf` 与 `12579c3101b3` 重复 `ADD COLUMN memory_key`；(2) `memory_records` 的 `validity_state` 等约 30 个 memory V2 字段从未写入任何迁移，一直靠 `create_all()` 补齐；(3) 迁移链存在双 head 分叉（Phase6B 未接 Phase5/6A 的 merge 节点）。这些缺陷全被双轨掩盖。

**处理（方案 A：彻底切换 + squash 基线）**：
1. 修复双 head：将 Phase6B 的 `down_revision` 指向 merge 节点。
2. 在 `alembic/env.py` 补充 `import forget_models / retention_models`，修复 autogenerate 长期漏扫这两个模块（12 张表）的根因。
3. 归档并删除 31 个损坏的历史迁移，用单一基线 `69da20d1e47c_baseline_full_schema` 取代（基于完整 `Base.metadata` autogenerate）。
4. 严格验证：空库从零 `upgrade head` 成功；与 `create_all()` schema 全量对比——**57 张表、183 索引、7 CheckConstraint、partial unique index 全部零差异**。
5. 主库清空 `alembic_version` 后重新 `stamp` 到新基线。
6. `_ensure_schema()` 改为唯一真相源 `alembic upgrade head`，删除 `create_all()` + 手工 ALTER。模拟全新部署（空库启动）建出完整 58 表验证通过。

此后 schema 唯一真相源为 Alembic；任何模型变更必须新增迁移脚本。测试路径（conftest 的 SQLite `create_all`）与 `main.py` 无关，187 项测试全通过。

**B.2 `AgentGraph` 每轮构造一个完整 `OutboxWorker` 却从不使用（死代码 + 性能浪费）— ✅ 已修复（2026-07-17）**
`runtime/agent_graph.py` 曾在每次实例化 `AgentGraph`（即每个 Turn）时执行 `register_all(_hr)` 并 `new OutboxWorker(...)`，但 `self._outbox` 全文件除赋值和 docstring 外从未被读取。
**处理**：删除 `self._outbox` 及其 `HandlerRegistry`/`ActiveClaimRegistry` 构造，连同 `register_all`、`OutboxWorker` 两个 import。Outbox 的真实入口是 `main.py` lifespan 中注册的全局 worker，图内无需持有。同时删除只写不读的 `_last_intent`/`_last_decision`/`_last_ctx_items`/`_last_ctx_meta` 实例字段。

**B.3 跨模块访问私有成员（耦合脆弱）**
`runtime/turn_execution.py:37` 导入 `agent_graph._flatten_tool_result`（私有），并在 `:584` 调用 `graph._execute_graph(...)`（私有方法），均带 `reportPrivateUsage` 抑制注释。
**处理（部分修复 2026-07-17）**：已将 `_flatten_tool_result` 提为模块级公开函数 `flatten_tool_result`，`turn_execution.py` 改用公开名导入，去除该处 `reportPrivateUsage` 抑制。仍待办：为 `AgentGraph` 暴露公开的 `execute(...)` 方法替代 `_execute_graph` 私有调用，进一步降低耦合。

**B.4 `AgentGraph` 写而不读的实例字段**
`agent_graph.py` 的 `_last_ctx_items` / `_last_ctx_meta` / `_last_intent` / `_last_decision`：`_last_intent`、`_last_decision` 始终为初值从未被消费；`_last_ctx_items`、`_last_ctx_meta` 只写不读。属重构残留。
**状态（已修复 2026-07-17）**：删除四个 `_last_*` 实例字段及其在 `_execute_graph` 中的赋值；`ctx_items_data`/`ctx_meta` 仍作为局部变量填充 `AgentGraphResult`，行为不变。

### C. 死代码 / 退化模块

**C.1 `runtime/intent_router.py` 已退化为纯数据结构**
文件自述"不再做正则意图路由"，`IntentResult` 未见运行时消费，`IntentType` 仅零散引用。
**状态（已修复 2026-07-17）**：经全项目检索确认 `IntentType`/`IntentResult` 无任何代码 import（仅历史文档提及），整文件已删除。意图识别完全由 LLM 原生 `tool_calls` 承担，无需该退化模块。

**C.2 重复的 markdown 代码围栏剥离逻辑**
`core/action_planner.py`、`memory/memory_extractor.py`、`mcp/capability_planner.py` 三处存在几乎相同的 ```` ```json / ``` ```` 剥离代码。
**状态（已修复 2026-07-17）**：新增共享工具 `core/text_utils.py::strip_code_fence(text)`，用一处正则同时处理首部 ```` ```lang ```` 与尾部 ```` ``` ````（含首尾嵌套围栏边界），三处调用点全部改为调用该函数。`memory_extractor` 保留其后续 `json_repair` 回退逻辑不变。

**C.3 `MemoryStore.get_active` 标注 DEPRECATED 仍保留**
`memory/memory_store.py` 曾保留委托 `get_active_valid` 的弃用封装。
**状态（已修复 2026-07-21）**：经全项目检索确认 `get_active(` 与 `get_active_valid(` 均零调用方，两个无范围读取方法均已删除；仍被专用查询复用的 active+valid 过滤条件保留。`update_lifecycle()` 直写旁路也已删除，生命周期变更统一经 `MemoryLifecycleService`。

**C.4 内置 `forget_memory` 兼容工具已退役（2026-07-21）**
旧 `_handle_forget_memory` 及其 ToolRegistry 注册项已删除，不再与结构化 `forget` 工具同时暴露给 LLM。遗忘请求统一使用 `forget` 的 `mode`、目标 ID、`canonical_key` 等显式参数，避免旧 `scope` 推断与风险元数据漂移；旧 HTTP 兼容端点不受影响。

### D. 安全 / 配置

**D.1 遗忘 HMAC 默认密钥硬编码（本轮暂不改，仅分析）**
`config.py:34`：`forget_hmac_secret = "aiive-dev-hmac-key-change-me-in-production"`。

**性质分析**：这个 HMAC 与 CORS/DB 口令性质不同，不是普通"部署配置"，而是"遗忘"功能的密码学安全根基。从 `forget/fingerprint.py` 看，它用于生成被遗忘内容的**不可恢复 tombstone 指纹**——模块头部明确写道：普通 SHA-256 熵低、可被字典/彩虹表反推，故改用 `HMAC-SHA256(secret, canonical_value)`。其防反推能力**完全依赖 secret 的保密性**。若生产沿用公开的默认值，任何拿到数据库的人都知道密钥，可对候选内容逐一计算 HMAC 比对指纹，等价于退回"可被字典反推"，HMAC 的意义被完全抵消，直接违背"被遗忘权"的隐私承诺。
**结论**：属 P1 安全项，但对本地开发无影响（本地照常运行）。**建议**（本轮按用户要求不改）：生产启动时检测到 secret 仍为默认值应 fail-fast（拒绝启动），而非静默降级——既保护生产，又不影响本地。

**D.2 CORS 与数据库凭据（复核结论：无需处理）**
- **CORS**：`main.py` 当前为 `allow_origins=["http://localhost:5173"]`（白名单，非通配），仅 methods/headers 为 `*`。真正危险的 `allow_origins=["*"] + allow_credentials=True` 组合并不存在。对本地个人 Agent 完全合理，**无需改动**。
- **数据库口令**：`config.py:27` 的明文口令是 pydantic 默认值，`.env` 的 `DATABASE_URL` 会覆盖且指向 localhost，属本地开发便利。只需注意勿将真实生产口令写进源码（用 `.env`/环境变量即可），当前写法**无需改动**。

**D.3 清空全部记忆的确认策略需复核**
`forget` 工具支持 `all_user_data=True`（经 `builtin_tools.py:469-470` 的 `scope="all"` 委托）。审批策略现已启用，`can_delete`、显式确认以及 high/critical 风险工具会触发 CONFIRM。

**D.4 检索可解释性闭环**
- 每轮 Turn 在上下文装配前生成统一 `trace_id`，ContextAssembler 只通过 `UnifiedRetriever` 发起检索；`AutomaticRecallEngine` 仅作为其内部 MemoryRecord 路由，不再由调用方二次 fallback。exact/memory/index/raw-history 路由独立降级并写入稳定 notes，全路由或编排失败返回 fail-closed 空结果。
- `retrieval_runs/retrieval_candidates` 和 `memory_recall_runs/memory_recall_candidates` 使用独立短事务持久化；诊断写入失败仅影响可观测性，不污染上下文装配 Session、不触发第二次检索。随后 AgentGraph、action card 和最终回复复用同一 trace。
- Inspector 支持按 `trace_id` 发现 run，再按 `run_id` 读取真实候选；前端“检索”页只展示这些数据库事实，不生成候选假数据。
- Context Inspector 主接口和 item 详情接口在快照或条目不存在时统一返回 HTTP 404（稳定 `context_snapshot_not_found` / `context_item_not_found` code）；前端明确区分未找到与服务错误，并在 trace 切换时立即清理旧快照，避免展示上一 trace 的过期内容。

**D.5 Chat 错误契约**
- 同步 `/api/chat` 的 LLM 错误统一返回 `detail={code,message,retryable,trace_id,retry_after_seconds}`，并按超时、限流、配置和上游故障映射 HTTP 状态。
- SSE 使用相同字段并增加 `status`；错误事件会终止本轮流，不会再以空回复 `done` 伪装成功。
- 前端仅展示安全 `message`，内部 SDK 异常保留在后端日志并通过 `trace_id` 关联。

### D.6 工具审批基础设施（已启用）

`check_tool_calls()` 阻止未注册工具，并将 `requires_confirmation`、`can_delete` 或
high/critical 风险工具送入审批；普通读写保持个人 Agent 的高权限直通。Web、Android
和 Electron UI 均展示审批卡片，Capability Dashboard 展示“需确认”徽标。

`approval_requests` 是唯一审批事实源：服务端冻结工具参数和风险快照，使用条件状态
迁移、工具描述指纹、来源 Turn 与执行 fencing 防止客户端篡改和重复执行。Desktop
审批额外冻结节点 ID；节点离线或变化后原审批不会漂移到另一台电脑。

### E. 前端

**E.0 普通用户页面与开发者边界**
- 普通用户导航提供 Chat、Memory Dashboard、Capability Dashboard、Event/Context/Retrieval Inspector、Tools 和 Notifications。
- 本项目定位为本地单用户个人应用。Event/Context/Retrieval Inspector 是用户观察本人 Agent 上下文、事件和检索过程的产品能力，允许返回原始上下文正文、查询和事件 payload，不经过开发者网关或诊断脱敏；若未来支持远程或多用户部署，必须先增加身份认证、资源归属校验并重新评估敏感字段边界。
- Memory Dashboard 只展示 `GET /api/memories` 返回的最近 100 条可见记录，搜索为当前列表本地筛选；创建、sleep、archive、forget 均调用真实后端 API 并在成功后重新读取数据库状态。
- Capability Dashboard 只读展示 `/api/mcp/capabilities` 与 `/api/tools` 的真实状态和安全声明。MCP 安装、激活、自进化和后台维护不直接调用 Service 路由，继续通过 Chat、ToolRegistry 与真实 smoke 链路执行；高危工具进入统一用户审批。
- debug、outbox、epochs 等内部读取端点进入独立的 `/developer` 只读诊断页：前端构建开关 `VITE_AIIVE_DEVELOPER_UI_ENABLED` 与后端 `AIIVE_DEVELOPER_DIAGNOSTICS_ENABLED` 均默认关闭，后端启用后仍同时限制 loopback 客户端和 Host。这些内部端点的 LLM 预览与 Outbox 错误详情在服务端脱敏后才返回；该规则不适用于作为普通用户观察能力的 Inspector。页面不提供 seal、rollover、retry 或 requeue 操作。`POST /api/epochs/{thread_id}/seal-segment` 与 `POST /api/epochs/{thread_id}/rollover` 是现有业务写接口，不属于 Developer 只读 guard 的保护范围，继续保持兼容。
- Capability activate 在真实 MCP 安装、启动、`tools/list`、`tools/call`、smoke 和 ToolRegistry 注册链路完成前保持 fail-closed，计划转为 `needs_user_review`，禁止硬编码 smoke 成功或写入不可调用的 active 状态。
- Forget Phase A 使用 Session 同步更新：同一事务立即读取 `lifecycle_state=forgotten`；正文保留到异步 purge 阶段处理，Shield/Tombstone 在此期间保证 fail-closed。

**E.1 通知实时推送（已落地）**
已移除 `App.tsx` 的 30s 轮询。改为：后端新增全局通知通道 `ws_manager.GLOBAL_THREAD_ID = "__global__"`，前端 `useNotificationSocket` 单例连接 `/ws/__global__`，连接时即收到 pending 数量快照，之后在提醒触发（成功/回退）、确认、延时、删除等变更点通过 `broadcast_pending_count` 主动推送最新 `pending_count`。`NotificationsPage` 订阅该通道在通知变更时单次刷新收件箱（非周期轮询）。
**结论**：通知角标与收件箱均为 WebSocket 即时更新，无 HTTP 轮询。删除通知会将事件标记为 `payload.dismissed=true` 并从通知读模型隐藏，底层 Event 审计事实保留；关联 Task 仅在 `pending/dispatching` 时转为 `cancelled`，尚未 claim 的 `reminder_delivery` Outbox 同事务取消。Handler 在 Agent 执行前后均检查取消状态；已 `completed` 的提醒只能隐藏历史通知，接口不得宣称撤回已完成投递。`POST /tasks/{id}/check-now` 通过 `TaskManager.enqueue_reminder_now()` 按 ID 锁定并入队，不扫描其他任务、不复活终态 Task。

---

## 修复优先级建议

| 优先级 | 项 | 状态 | 理由 |
|---|---|---|---|
| P0 | B.1 Schema 双轨制 | ✅ 已修复(2026-07-17) | 切换 Alembic 单一真相源 + squash 基线 |
| P0 | A.2 副作用工具超时状态失真 | ✅ 已修复(2026-07-20) | ToolOperation + Outbox Worker + 稳定幂等键；超时返回 execution_unknown，数据库副作用与 committed receipt 原子提交 |
| P1 | A.4 Alembic 禁用宿主日志 | ✅ 已修复(2026-07-17) | 假"卡住"；fileConfig disable_existing_loggers=False |
| P1 | A.5 ContextBudget 窗口配错/不可配 | ✅ 已修复(2026-07-17) | 窗口配置化 + recent_messages 弹性派生 |
| P1 | A.1 intent_type 失真 | ✅ 已修复(2026-07-17) | 契约字段误导下游 |
| P1 | D.1 HMAC 默认密钥 fail-fast | 待处理(用户指定暂缓) | 遗忘完整性安全 |
| P1 | B.2 每轮构造无用 OutboxWorker | ✅ 已修复(2026-07-17) | 死代码 + 每轮性能浪费 |
| P2 | B.3 私有耦合 | ✅ 部分修复(2026-07-17) | `_flatten_tool_result` 已公开；`_execute_graph` 仍私有 |
| P2 | B.4 写而不读的实例字段 | ✅ 已修复(2026-07-17) | 可维护性 |
| P2 | C.1 intent_router 死文件 | ✅ 已删除(2026-07-17) | 降低理解成本 |
| P2 | C.2 围栏剥离去重 | ✅ 已修复(2026-07-17) | 抽取 `strip_code_fence` 共享函数 |
| P2 | C.3 get_active 死方法 | ✅ 已删除(2026-07-17) | 零调用方 |
| P2 | C.4 forget_memory 兼容入口 | ✅ 已移除(2026-07-21) | LLM 工具面统一为结构化 `forget` |
| P3 | D.2 CORS/DB 凭据 | 无需处理 | 本地场景合理 |
| P3 | E.1 前端 WebSocket 推送 | 待处理 | 体验 |

> 本报告为静态审查结论。截至 2026-07-17，已完成 P0（B.1 Schema 双轨制、A.2 工具超时线程泄漏）、P1（A.1 / A.4 / A.5 / B.2）、P2（B.3 部分 / B.4 / C.1 / C.2 / C.3）各项修复，全部 187 个测试通过、服务可正常启动。剩余待处理：A.3（记忆信号成本核查）、B.3 剩余（`_execute_graph` 公开化）、C.4（`forget_memory` 迁移确认）、D.1（HMAC fail-fast，用户指定暂缓）、D.3（清空记忆审批核查）、E.1（前端 WebSocket 推送）。每项修复后运行 `python -m pytest tests/ -v` 回归。
