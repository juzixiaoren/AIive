# AIive 代码图谱（Code Map）

> **本文档是 AIive 项目的代码知识图谱**：按功能域描述"每块功能的核心实现代码、关键符号与模块间关联"，
> 目标是开发时先读本文档快速定位代码，避免通读无关文件。
>
> - **覆盖范围**：`backend/aiive/`（22 个包）、`frontend/src/`、`apps/`（android + desktop）、`tests/`、`scripts/`、`alembic/`
> - **路径约定**：均相对项目根（`AIive/`），如 `backend/aiive/main.py`、`frontend/src/pages/ChatPage.tsx`
> - **行号**：基于建图时的代码版本，仅供快速跳转，以实际代码为准
> - **维护规则**：见文末第 9 节。**新增/删除/移动文件、改 key 符号行号、改表结构后必须同步更新本文档**
> - 建图日期：2026-10-21

---

## 1. 全局架构图

```
┌──────────────────────────── 客户端层 ────────────────────────────┐
│ frontend/ (Web SPA)      apps/android/ (Capacitor)   apps/desktop/ (Electron)│
│ 9 个 Tab 页面             遥控聊天 + 审批放行           Web 壳 + Node Host    │
└───────┬───────────────────────┬───────────────────────────┬───────┘
        │ HTTP / SSE / WS       │                           │ WS /ws/desktop/{node_id}
        ▼                       ▼                           ▼
┌─────────────────────── FastAPI app ──────────────────────────────┐
│ backend/aiive/main.py::create_app()          ← 25 个 include_router│
│   lifespan: alembic upgrade head → MCP 恢复 → daemon → OutboxWorker│
│                                                                │
│  api/routes_*.py  (REST/WS 瘦路由：解析请求 → 调 service → 转错误契约)│
│    │                                                           │
│    ├─ /api/chat/* ──► runtime/turn_execution.py::TurnExecutionService│
│    │       ├─ context_assembler (预算装配 + 统一召回)            │
│    │       ├─ agent_graph (LangGraph: assistant→policy→validate │
│    │       │        →tools⇄assistant / confirm)                 │
│    │       │        ├─ core/llm_client.py                       │
│    │       │        ├─ tools/registry.py  (执行 + 策略门控)      │
│    │       │        └─ runtime/policy_engine.py                 │
│    │       └─ _finalize_turn: Event + LLMCall + memory Outbox 入队│
│    │                                                           │
│    ├─ /api/agent-tasks ──► task_runtime/ (PersistentTaskRuntime)│
│    ├─ /api/memories /forget ──► memory/ + worker/handlers_forget│
│    ├─ /api/knowledge ──► knowledge/ingestor.py                  │
│    ├─ /api/mcp* /capabilities ──► mcp/                          │
│    ├─ /api/selfdev ──► selfdev/                                 │
│    ├─ /api/desktop + /ws/desktop/{id} ──► desktop/              │
│    └─ /ws/{thread_id} ──► api/ws_manager.py                     │
│                                                                │
│  worker/  Outbox 事务队列 (Postgres outbox_jobs)                │
│    ├─ scheduler_daemon.py (APScheduler 周期扫描入队 + poll)      │
│    ├─ outbox_handlers.py   (记忆提取/提醒/分段/epoch/维护/检索索引)│
│    ├─ handlers_forget.py   (遗忘 Saga 五阶段)                    │
│    ├─ handlers_retention.py / handlers_agent_tasks.py / task_worker.py│
│                                                                │
│  supervisor/  守护进程 (health_probe / launcher / slot_manager)  │
└───────────────────────────┬────────────────────────────────────┘
                            ▼
        db/ (SQLAlchemy 71 表)  +  alembic/ (schema 唯一真相源)
        PostgreSQL 16 + pgvector
```

**核心不变量**：
- schema 唯一真相源 = Alembic 迁移；启动时 `main.py::_ensure_schema` 执行 `upgrade head`，失败阻断启动。
- 所有副作用（记忆写入/遗忘/提醒/工具外部效应）都走 `outbox_jobs` 事务队列可靠投递，不在请求线程内直接执行外部副作用。
- 记忆召回 fail-closed：已遗忘记忆不经任何检索路径复现。

---

## 2. 快速定位表（要改什么 → 去哪些文件）

> 用法：先在本表找到功能行，只读"核心文件"列；必要时再按"关联"列扩展。测试列给出对应测试文件位置。

### 2.1 聊天与 Agent 核心

| 功能 | 核心文件 | 关联文件 | 测试 |
|---|---|---|---|
| 聊天入口（同步/SSE/错误契约） | `api/routes_chat.py` | `runtime/turn_execution.py`、`api/ws_manager.py` | `tests/unit/backend/test_chat_*.py` |
| 回合生命周期（抢占/租约/心跳/收尾） | `runtime/turn_execution.py` | `db/models.py::turn_records`、`runtime/event_logger.py` | `test_turn_execution*.py` |
| Agent 图节点 / 工具循环 / 流式 | `runtime/agent_graph.py` | `tools/langchain_adapter.py`、`core/llm_client.py` | `test_agent_graph*.py` |
| 上下文组装与 token 预算 | `runtime/context_assembler.py`、`runtime/context_budget.py` | `memory/automatic_recall.py`、`runtime/thread_state.py` | `test_context_assembler*.py` |
| LLM 客户端（json_mode 降级/错误归一） | `core/llm_client.py` | `config.py`（LLM 配置项） | `test_llm_client*.py` |
| 上下文压缩（segment/epoch/checkpoint） | `runtime/compaction.py`、`runtime/epoch_manager.py` | `worker/outbox_handlers.py`（`handle_segment_sealing` 等） | `test_compaction*.py`、`test_epoch*.py` |
| 注意力 / 节奏 | `runtime/attention_manager.py`、`runtime/rhythm_manager.py` | `api/routes_attention.py` | `test_attention*.py` |
| WorkingState（运行中状态） | `runtime/working_state.py` | `runtime/agent_graph.py::_tools_node` | `test_working_state*.py` |

### 2.2 工具与策略

| 功能 | 核心文件 | 关联文件 | 测试 |
|---|---|---|---|
| 工具注册表 / 安全元数据 / 三态执行入口 | `tools/registry.py` | `tools/builtin_tools.py`、`tools/tool_validation.py` | `test_tool_registry*.py` |
| 新增/修改内置工具 | `tools/builtin_tools.py`（统一注册入口 + 共享 `_build_safety`/`db_handler`/`require_ctx`；实际 handler 实现见 `builtin_*.py` 领域模块的 `handle_*`） | `tools/registry.py::_build_safety`、`prompts/`（工具说明） | `test_builtin_tools*.py`、`test_builtin_mcp*.py` |
| 工具参数校验（Pydantic 动态模型） | `tools/tool_validation.py` | `tools/registry.py::execute` | `test_tool_validation*.py` |
| 工具策略门控（ALLOW/BLOCK/CONFIRM） | `runtime/policy_engine.py` | `runtime/agent_graph.py::_policy_check`、`control/approval_service.py` | `test_policy*.py` |
| 审批流（人工确认） | `control/approval_service.py` | `api/routes_approval.py`、`runtime/agent_graph.py::_confirm_node` | `test_approval*.py` |
| 副作用工具 Outbox 化执行 | `tools/operation_executor.py` | `worker/scheduler_daemon.py::_poll_tool_operations` | `test_tool_operation*.py` |
| 安全删除 | `tools/safe_delete.py` | `tools/builtin_files.py::handle_safe_delete` | `test_safe_delete*.py` |
| 工具结果引用化（大结果→Artifact） | `runtime/tool_normalizer.py`、`runtime/tool_executor.py` | `storage/object_store.py`、`runtime/action_cards.py` | — |
| 工具注入方案（分层/tool_plan/检索选型分析） | `docs/architecture/工具注入方案分析.md` | 下述注入面实证 | — |

**工具注入面（改绑定/可见性前必读）**：对话 Agent 每轮**只绑定 5 个任务元工具**（`task_runtime/tools.py::build_main_agent_registry`，`context_assembler.py:256` 固定注入，`tool_definitions` 分区 4000/6000 token 前缀截断），架构不变。持久任务 Worker 采用**分层注入 + 自主发现**（`task_runtime/tool_injection.py`）：L0 常驻能力（长期记忆读取、基础搜索、本机 `run_shell`、`desktop_exec`、任务管理）始终注入完整定义；其余能力由 Worker 通过 `search_capabilities` / `describe_capability` 两个只读决策按需发现，`TaskContextAssembler` 只注入 `tools.resident / available / discovered / loaded`，**不再整段注入 `list_all()` 清单**；目录 ≤12 个工具时直接全量注入 Schema，超过则转为发现模式。发现范围覆盖全部可用能力（内置 + 在线桌面 + MCP + 自进化），**不受 task_type / Skill Profile 收窄**（`TaskScope.capability_policy=unified`）；执行仍由 `CapabilityBroker` 的授权视图 + `TaskPolicyEngine` + 审批 + 资源锁 + `descriptor_hash` 裁决，发现不扩大权限。方案与实施记录见 `docs/architecture/工具注入方案分析.md`。

### 2.3 记忆系统

| 功能 | 核心文件 | 关联文件 | 测试 |
|---|---|---|---|
| 记忆写入 Saga / supersede | `memory/memory_write_service.py` | `memory/conflict_resolver.py`、`memory/memory_gate.py`、`memory/memory_mutation.py` | `test_memory_write*.py`、`test_conflict*.py` |
| 冲突消解策略 | `memory/conflict_resolver.py` | `memory/memory_key_registry.py` | `test_conflict_resolver*.py` |
| 记忆 key 注册表（唯一真相源） | `memory/memory_key_registry.py` | `prompts/`（MEMORY_KEY_GUIDE） | `test_memory_key*.py` |
| 记忆召回（四路由 + 融合） | `memory/automatic_recall.py`、`memory/recall_fusion.py` | `memory/recall_config.py`、`memory/vector_projection.py`、`retrieval/` | `test_recall*.py` |
| 记忆提取（LLM） | `memory/memory_extractor.py`、`memory/proposal_normalizer.py` | `core/action_planner.py`、`worker/outbox_handlers.py::handle_memory_extraction` | `test_memory_extract*.py` |
| 记忆生命周期（wake/promote/sleep/archive） | `memory/memory_lifecycle_service.py` | `api/routes_memories.py` | `test_memory_lifecycle*.py` |
| 记忆维护扫描 | `memory/memory_maintenance.py`、`memory/memory_maintenance_planner.py` | `worker/outbox_handlers.py::handle_memory_maintenance` | `test_maintenance*.py` |
| 遗忘（工具入口） | `tools/forget_tool.py` | `memory/memory_write_service.py::forget` | `test_forget*.py` |
| 遗忘 Saga（级联/重建/清除/校验/对账） | `worker/handlers_forget.py` | `forget/`（fingerprint/shield/selector/visibility/reconcile）、`db/forget_models.py` | `test_forget_saga*.py` |
| 保留治理（retention 清理） | `retention/engine.py` | `worker/handlers_retention.py`、`db/retention_models.py` | `test_retention*.py` |
| 向量召回（pgvector 投影） | `memory/vector_projection.py`、`memory/vector_bootstrap.py` | `memory/embedding_provider.py`、`db/models.py::memory_vector_projections` | `test_vector*.py` |
| 检索索引代次（lexical 索引） | `retrieval/` | `worker/outbox_handlers.py::handle_retrieval_index_refresh` | `test_retrieval*.py` |
| Core Memory Block | `memory/core_memory_projection.py` | `worker/outbox_handlers.py::handle_core_memory_refresh` | `test_core_memory*.py` |

### 2.4 能力扩展（MCP / Skills / 自进化）

| 功能 | 核心文件 | 关联文件 | 测试 |
|---|---|---|---|
| MCP 搜索 / 目录 | `mcp/discovery.py` | `api/routes_mcp.py`、`tools/builtin_mcp.py::handle_search_mcp` | `test_mcp*.py` |
| MCP 风险评估与计划 | `mcp/capability_planner.py` | `api/routes_capabilities.py` | `test_capability_planner*.py` |
| MCP 沙箱安装 / 冒烟 | `mcp/installer.py` | `api/routes_mcp_install.py`、`.data/mcp_sandbox/` | `test_mcp_install*.py` |
| MCP 运行时桥接（stdio） | `mcp/runtime_client.py`、`mcp/bootstrap.py` | `tools/registry.py::register` | `test_mcp_bootstrap*.py` |
| 内置 MCP（aiive-essentials） | `mcp/builtin.py`、`mcp/builtin_server.py` | `main.py` lifespan | `test_mcp_builtin*.py` |
| 内置 Skill catalog | `skills/catalog.py` + `skills/builtin/*/SKILL.md` | `api/routes_skills.py`、`task_runtime/tools.py` | `test_skills*.py` |
| 自进化：补丁计划 | `selfdev/planner.py`、`selfdev/trusted_core.py` | `api/routes_selfdev.py` | `test_selfdev*.py` |
| 自进化：槽位 promote/rollback | `supervisor/release_manager.py`（生产晋升入口）、`selfdev/promote_rollback.py`、`selfdev/patch_executor.py` | `supervisor/slot_manager.py` | `test_release_manager.py`、`test_selfdev_slot*.py` |
| 自进化：安全闸门（Replay Eval + Kernel Contract Checks） | `eval/`（`contracts.py`/`replay.py`/`gate.py`/`scenarios.py`/`persistence.py`/`cli.py`/`models.py`） | `supervisor/release_manager.py::promote` 与 `selfdev/promote_rollback.py::promote`（共用 `eval/gate.py::evaluate_promotion_gate`，fail-closed）、`api/routes_eval.py`（只读查询 `/api/eval/runs`、`/runs/latest`） | `tests/unit/backend/test_eval_*.py`、`test_release_manager.py` |

### 2.5 任务 / 桌面 / 知识库 / 通知

| 功能 | 核心文件 | 关联文件 | 测试 |
|---|---|---|---|
| 持久 Agent 任务主循环 | `task_runtime/runtime.py` | `task_runtime/repository.py`、`worker/handlers_agent_tasks.py` | `tests/unit/backend/test_agent_task*.py` |
| 任务 API + WS 事件 | `api/routes_agent_tasks.py` | `task_runtime/repository.py`（commit 后广播） | 同上 |
| 任务能力边界（Broker） | `control/capability_broker.py`、`control/scope.py`、`control/task_policy.py` | `tools/registry.py::execute_brokered`、`task_runtime/resource_locks.py` | `test_capability_broker*.py` |
| 任务 Watcher / 资源锁 / 证据 | `task_runtime/watchers.py`、`task_runtime/resource_locks.py`、`task_runtime/evidence.py` | `worker/scheduler_daemon.py::_agent_task_scanner_job` | `test_task_watch*.py` |
| 提醒 / 条件任务 | `runtime/task_manager.py` | `worker/task_worker.py`、`tools/builtin_tools.py`（schedule_reminder 等） | `test_reminder*.py`、`test_task_manager*.py` |
| 通知 | `api/routes_notifications.py` | `worker/outbox_handlers.py`、`frontend/src/pages/NotificationsPage.tsx` | `test_notification*.py` |
| 桌面节点服务端 | `desktop/node_service.py`、`desktop/connection_manager.py`、`desktop/protocol.py` | `api/routes_desktop.py`、`desktop/registry_overlay.py` | `test_desktop*.py` |
| 桌面节点客户端（Electron） | `apps/desktop/src/node-host/client.mjs` | `apps/desktop/src/node-host/locks.mjs`、`journal.mjs` | `apps/desktop/src/node-host/journal.test.mjs` |
| 知识库摄入 / 分块 / 检索 | `knowledge/ingestor.py`、`knowledge/chunker.py` | `knowledge/document_reader.py`、`api/routes_knowledge.py` | `test_knowledge*.py` |
| 文档读取（多格式） | `knowledge/document_reader.py` | `tools/builtin_knowledge.py::handle_read_document` | `test_document*.py` |

### 2.6 前端 / 客户端 / 基础设施

| 功能 | 核心文件 | 关联文件 | 测试 |
|---|---|---|---|
| 聊天页（SSE 状态机，最复杂 57KB） | `frontend/src/pages/ChatPage.tsx` | `frontend/src/api/chat.ts`、`frontend/src/components/Markdown.tsx` | — |
| 前端导航 / 主题 / 角标 | `frontend/src/App.tsx` | `frontend/src/hooks/useNotificationSocket.ts`、`frontend/src/lib/base.ts` | — |
| 任务中心页 | `frontend/src/pages/TaskCenterPage.tsx` | `frontend/src/api/agentTasks.ts`、`frontend/src/hooks/useTaskEvents.ts` | — |
| 新增一个前端页面 | `frontend/src/pages/XxxPage.tsx` + `App.tsx`（lazy + Tab） | `frontend/src/api/` 加客户端函数 | — |
| Android 客户端 | `apps/android/src/App.tsx`、`apps/android/src/api/chat.ts` | `apps/android/src/config.ts`（后端地址） | — |
| 新增 DB 表 / 改表 | `backend/aiive/db/models.py` + `backend/alembic/versions/` 新建迁移 | `db/forget_models.py`、`db/retention_models.py`、`alembic/env.py`（需显式导入新模型模块） | `test_db_models*.py` |
| 新增配置项 | `backend/aiive/config.py::Settings` | `.env` / `.env.example` | `test_config*.py` |
| 启动装配 / 生命周期 | `backend/aiive/main.py` | `worker/scheduler_daemon.py::start_daemon`、`mcp/bootstrap.py::restore_active_capabilities` | `test_main*.py` |
| 部署 | `docker-compose.yml`、`backend/Dockerfile`、`frontend/Dockerfile` | `frontend/nginx.conf`、`scripts/start.sh` | — |

---

## 3. 后端模块地图（`backend/aiive/`）

> 每个包给出：职责、核心文件表、关键符号（文件:行）、与其他包的关联。

### 3.1 入口与配置

| 文件 | 职责 |
|---|---|
| `main.py` | FastAPI 装配：`create_app()` L131；`lifespan` L142（启动序列：事件循环绑定→prompt catalog 校验→`_ensure_schema` L87 Alembic upgrade→HMAC 密钥→`_ensure_system_thread` L51→检索 generation→backfill→ContextBudget 校验→向量运行时→MCP 恢复 `restore_active_capabilities`→`start_daemon`→OutboxWorker+Heartbeat+APScheduler）；25 个 `include_router` L308-332 |
| `config.py` | `Settings` L13（pydantic-settings，`.env` 加载）；`validate_memory_features()` L126 启动校验；单例 `settings` L186。**几乎所有模块 `from aiive.config import settings`** |

### 3.2 `api/` — REST/WS 路由层

公共基建：`ws_manager.py`（`ConnectionManager` L18，按 thread_id 分组，单例 `ws_manager` L100，`broadcast_to_thread_sync` 供后台线程推送）；`developer_security.py`（`require_local_developer` L61 loopback 守卫、`redact_diagnostic_payload` L87 脱敏）。

| 路由文件 | 前缀 / 端点 |
|---|---|
| `routes_chat.py` | `/api/chat` L116、`/chat/system` L136、`/chat/stream` L163（SSE 核心入口） |
| `routes_memories.py` | `/api/memories` CRUD + `/{id}/forget|sleep|archive` + `/maintenance/*` |
| `routes_tools.py` | `/api/tools`、`/tool-operations/{id}`、`POST /tools/safe-delete` |
| `routes_agent_tasks.py` | `/api/agent-tasks` 增删查改 + `WS /ws/{task_id}` |
| `routes_approval.py` | `POST /api/approval/respond` |
| `routes_capabilities.py` | `/api/capabilities/plan-from-goal`、`/{plan_id}/activate` |
| `routes_mcp.py` / `routes_mcp_install.py` | `/api/mcp/search`、`/capabilities`；`/{name}/install-sandbox`、`/{id}/smoke` |
| `routes_skills.py` | `/api/skills`、`/{skill_id}`（catalog + ready/degraded 对照） |
| `routes_selfdev.py` | `/api/selfdev/slots`、`/plan`、`/{id}/apply-inactive|promote|rollback` |
| `routes_forget.py` | `/api/forget` + `/{operation_key}/status` |
| `routes_knowledge.py` | `/api/knowledge/ingest`、`/knowledge`、`/search` |
| `routes_epochs.py` | `/api/epochs/{thread_id}`、`/{id}/seal-segment|rollover` |
| `routes_inspector.py` | `/api/context-runs/{trace_id}`（+`/items/{id}`）、检索运行查询 |
| `routes_tasks.py` | `/api/tasks` 条件任务 CRUD + `/{id}/cancel|check-now` |
| `routes_notifications.py` | `/api/notifications` |
| `routes_attention.py` | `/api/attention/current`、`/rhythm/daily|weekly` |
| `routes_threads.py` | `POST /api/thread/reset`、`GET /threads/{id}/messages` |
| `routes_desktop.py` | `GET /api/desktop/nodes`、`POST /api/desktop/bind`、`WS /ws/desktop/{node_id}` |
| `routes_ws.py` | `WS /ws/{thread_id}` |
| `routes_debug.py` | `/api/debug/events`、`/llm_calls`、`/traces/{id}`（`require_local_developer`） |
| `routes_outbox.py` | `GET /api/outbox/jobs`（开发者守卫） |
| `routes_eval.py` | `GET /api/eval/runs`、`/runs/latest`（只读安全门结果，`require_local_developer`） |
| `routes_personal.py` | `GET /api/personal-signals` |
| `routes_health.py` | `GET /health` |

设计约定：路由函数只做"解析请求→调用 runtime/service→转 HTTP/SSE 错误契约"，业务逻辑不下沉到路由。

### 3.3 `core/` — LLM 与规划原语

| 文件 | 关键符号 |
|---|---|
| `llm_client.py` | `LLMResponse` L20、`LLMClientError` L41、`normalize_llm_error()` L61、`LLMClient` L112（`chat()` L153，json_mode 自动降级 L210）、`default_llm_client()` L353、`FakeLLMClient` L370（测试替身） |
| `action_planner.py` | `MemorySignalDecision` L25、`ActionPlanner` L43、`classify_memory_signal()` L52（一次廉价分类调用，2 次重试，失败回退 extract_async）；依赖 `memory/extraction_policy` + `prompts/` |
| `text_utils.py` | `strip_code_fence()` L17 |

### 3.4 `runtime/` — Agent 图编排与上下文引擎（最重）

| 文件 | 职责 / 关键符号 |
|---|---|
| `turn_execution.py` | **回合统一入口**：`TurnExecutionService` L195（`execute_turn` L222 / `execute_turn_stream` L228 / `_execute_turn_impl` L977）；`TurnHeartbeat` L100（30s 续租）；`_resolve_and_preempt` L451（Thread 行锁+epoch 归属+幂等）；`_load_context` L1090；`_finalize_turn` L705（Event+ApprovalRequest+memory_extraction Outbox 入队+LLMCall）；`_rotate_snapshot` L907 |
| `agent_graph.py` | LangGraph `AgentGraph` L324；`_build_graph` L521 六节点：`_assistant` L546（bind_tools）→`_policy_check` L569→`_validate_tools` L619→`_tools_node` L766⇄assistant / `_confirm_node` L733；编译 L911；同步 `_execute_graph` L1110 / 流式 `_execute_graph_stream` L1178（`astream_events(v2)`）。第一轮重构后，同步与流式共享四个提取方法：上下文与图装配 `_prepare_execution` L919、`_extract_reply` L995（最终回复提取）、`_classify_memory_signal` L1012（记忆信号分类，流式经 `asyncio.to_thread` 包裹）、`_assemble_agent_result` L1036（统一 ToolRecord 转换 + blocked 合并 + approval 卡片 + 上下文快照）；纯数据 DTO `AgentGraphResult` L304、`ToolRecord` L262 契约不变。 |
| `context_assembler.py` | `ContextAssembler` L185：唯一上下文组装器（有界+hard-gate+统一检索）；`_unified_recall` L518 → `MemoryRecallPack` |
| `context_budget.py` | `ContextBudget`/`PartitionBudget`：12 个上下文分区 token 预算 |
| `attention_manager.py` | `AttentionManager` L28；`resolve_for_turn` L46；空闲切换阈值 4h/12h |
| `policy_engine.py` | `check_tool_calls`；`PolicyAction` L17（ALLOW/BLOCK/CONFIRM），基于 ToolRegistry 元数据 |
| `working_state.py` | `WorkingStateService`：有界结构化工作状态（运行中工具/未提交副作用/待审批） |
| `epoch_manager.py` / `compaction.py` | Epoch/Segment 生命周期+密封冻结；Segment 压缩（source_hash+LLM 输出校验） |
| `thread_state.py` | `ThreadState`：历史消息有界读取分页 |
| `tool_normalizer.py` / `tool_executor.py` / `action_cards.py` | 大工具结果→`Artifact` 引用化；`build_action_cards`/`build_pending_operations`；`ActionCard`/`PendingOperation`/`ChatResponse` 契约 |
| `token_counter.py` / `token_models.py` | LiteLLM token 计数；`TokenCount`/`ModelProfile` |
| `task_manager.py` | `TaskManager`：提醒/条件监视/例行任务 CRUD 与 Outbox 原子入队 |
| `rhythm_manager.py` / `thread_bootstrap.py` / `context_inspector.py` | 节奏摘要；Thread 行独立短事务；离线上下文导出（debug） |
| `event_logger.py` / `trace.py` / `execution_context.py` / `message_normalizer.py` | Event 写入；trace_id；`TurnExecutionContext`（单一事实源）；工具消息字段规范化 |

### 3.5 `memory/` + `retrieval/` + `forget/` + `retention/` — 记忆系统

**memory/（29 文件，写路径心脏）**

| 文件 | 职责 / 关键符号 |
|---|---|
| `memory_types.py` | 全部枚举 + `MemoryProposal`/`EvidenceItem` 契约 |
| `memory_key_registry.py` | 记忆 key 唯一真相源（cardinality/conflict_policy/core_memory_role） |
| `memory_store.py` | 纯持久化 CRUD 仓库 `MemoryStore` |
| `memory_write_service.py` | **写服务 Saga**：`MemoryWriteService` L114；`write_batch` L153、`write` L351、`forget` L534、`promote` L629、`execute_maintenance` L469；`_execute_supersede_or_revise` L785（旧记录 SUPERSEDED+superseded_by 双向链接+lineage+双投影入队）；`_execute_reinforce` L696、`_execute_merge` L874 |
| `conflict_resolver.py` | `ConflictResolver.resolve` L41；`latest_value_wins`→supersede；`_is_minor_change` L160（长度相近+词重叠>60%→revise） |
| `memory_gate.py` | 写入准入（authority/confirmation/candidate 降级） |
| `memory_mutation.py` | `MemoryMutationExecutor` L64：锁/前置校验/lineage/事件/投影入队 |
| `memory_maintenance.py` / `memory_maintenance_planner.py` / `maintenance_hashes.py` | 维护扫描（sleep/archive/consolidate 候选）；`plan_batch` L424；state_hash/decision_hash 乐观并发 |
| `memory_lifecycle_service.py` | wake/promote/sleep/archive |
| `automatic_recall.py` | `AutomaticRecallEngine.recall` L136；四路由：`_route_exact` L200 / `_route_lexical` L236 / `_route_vector` L268 / `_route_recent_episode` L294 |
| `recall_fusion.py` / `recall_config.py` / `recall_models.py` | `fuse_and_pack` L32（RRF k=60+6 信号加权+阈值 0.08+top_k 8）；全部预算权重阈值；Pydantic 契约 |
| `core_memory_projection.py` / `memory_read_model.py` | L1 Core Memory Block 构建；L0 精确 key 读取 |
| `scope_resolver.py` / `context_assembly.py` | Scope Chain（thread→global）；`render_core_memory`/`assemble_system_content` |
| `memory_extractor.py` / `proposal_normalizer.py` / `extraction_policy.py` | `UnifiedMemoryExtractor`；提案规范化；提取信号枚举 |
| `memory_policy.py` / `memory_access_tracker.py` | 敏感度/读取/authority 策略；访问 touch（recency 去抖） |
| `projection.py` / `vector_projection.py` / `vector_bootstrap.py` / `embedding_provider.py` | markdown/json 落盘投影（可丢失）；pgvector 投影+fail-closed 校验；provider 工厂与运行时校验 |

**其他记忆相关包**：`retrieval/`（lexical 检索索引代次：`retrieval_index_generations/_entries/_tokens/_runs` 表）；`forget/`（`fingerprint.py` 内容指纹、`phase_a_shield.py` 防再摄取盾牌、`selector_normalizer.py`、`visibility_service.py`、`reconcile_service.py`）；`retention/`（`engine.py` RetentionEngine、`config.py`、`maintenance_lease.py`、`safety_predicates.py`、`vacuum.py`）。

**记忆数据流**：
- 写入：`tools/remember_or_update` / chat 收尾入队 `memory_extraction` → `worker/outbox_handlers.handle_memory_extraction` → `MemoryWriteService.write_batch` → conflict_resolver → memory_store + lineage + 投影入队（vector/markdown/core_memory refresh 均为独立 outbox job）
- 召回：每轮 `ContextAssembler._unified_recall` → `AutomaticRecallEngine`（L0 精确/L1 core/L2 四路由/L3 recent）→ `recall_fusion` → `MemoryRecallPack` 注入上下文
- 遗忘：`forget` 工具 → `MemoryWriteService.forget` 起 Saga → `worker/handlers_forget` 五阶段（cascade→rebuild→purge→verify→reconcile），tombstone+shield 保证 fail-closed

### 3.6 `tools/` — 工具注册表与执行层（核心枢纽）

| 文件 | 职责 / 关键符号 |
|---|---|
| `registry.py` | `CapabilitySafetySchema` L36（definition_source/trust_level/risk_level/requires_confirmation/writes_external_world/can_delete/uses_network/allowed_instruction_sources/timeout/effect_mode）；`ToolRegistration` L86；`compute_descriptor_hash` L71；`ToolRegistry` L135：`register` L150、`unregister`（卸载，目录即时一致）、`catalog`（轻量目录，不含 Schema）、`search`（本地关键词检索，确定性打分）、`definition`（单工具完整定义）、`snapshot` L197、`scoped` L207、**`execute` L244**（指令来源授权→参数校验→副作用入队/直接执行）、**`execute_approved` L368**（审批后，强制指纹一致）、**`execute_brokered` L436**（Task Action 经 CapabilityBroker）、`_execute_direct` L319（线程池+超时）；单例 `get_tool_registry` L469 |
| `builtin_tools.py` | 内置工具**注册与组合入口** + 共享机制（第一轮架构重构后）：`register_builtin_tools` L141（统一注册全部 37 工具，逐项 cap_id/handler/desc/params/risk/writes_ext/can_del + effect_mode 映射）；共享机制（_build_safety / db_handler / require_ctx）定义在本模块（单一实现，领域模块复用，不复制装饰器）；其中 SessionLocal 与 db_handler 必须同处 builtin_tools 命名空间——测试与 tests/support.py 会 monkeypatch aiive.tools.builtin_tools.SessionLocal 把会话重定向到隔离测试库。register_builtin_tools 统一注册全部 37 工具；11 个领域模块用公开名 handle_* 实现，本模块以私有别名 _handle_* re-export（见下表），保持对 aiive.tools.builtin_tools 的导入契约完全不变；forget/forget_status 由 forget_tool.py 提供并同样 re-export。依赖方向：领域模块 → builtin_tools（仅 import db_handler/require_ctx），builtin_tools 末尾再 import 领域模块做 re-export；共享符号先于 re-export 定义，故为可安全导入的惰性环（详见 §10）。 |
| `builtin_shell.py` / `local_shell.py` | 本机 Bash（`run_shell`，常驻核心）：`handle_run_shell` → `run_bash`（真实 bash 子进程，`resolve_bash` 按 配置→PATH→Git Bash 路径 探测，找不到返回 `shell_unavailable` 且不回退 cmd/powershell）；临时文件承载 stdout/stderr 并限量读取；超时 `_terminate_tree`（POSIX 进程组 / Windows `taskkill /T`）；`isolated_env` 仅透传白名单变量 |
| `shell_policy.py` | `classify_shell_command`（只读白名单 + 复合操作符拦截 + git 子命令细分 + 网络命令识别）→ `ShellVerdict(read_only, uses_network, reason)`；`shell_path_violations`（命令内路径须落在 `allowed_roots`，越界 block；`$VAR` 等动态路径降级审批）；供 `control/task_policy.py` 与 `runtime/policy_engine.py` 决定放行/审批/联网拒绝 |
| `tool_validation.py` | `build_tool_args_model` L150（`create_model(strict=True, extra="forbid")`）；`validate_tool_params` L340 返回可重试 `error_payload()` |
| `operation_executor.py` | `enqueue_tool_operation` L89（幂等键=sha256(turn_record_id\|tool_call_id\|capability_id\|params_hash)）；`handle_tool_operation` L219（`with_for_update` fencing+execution_token） |
| `safe_delete.py` | 全仓唯一合法删除入口：`SafeDeleteScopeRegistry` L16、`safe_delete` L126（scope 白名单+符号链接+危险路径；trash/quarantine/hard_delete_for_test_only） |
| `forget_tool.py` | `handle_forget` / `handle_forget_status`（Phase 6A） |
| `langchain_adapter.py` | ToolRegistry → LangChain `StructuredTool` 桥（闭包注入 RunContext） |

**内置工具 handler 领域拆分（`builtin_*.py`，第一轮架构重构从 `builtin_tools.py` 抽出，仅放实现，注册仍由 `register_builtin_tools` 统一完成；`builtin_tools.py` 末尾 re-export 全部 handler 以保持导入契约不变）：**

| 领域模块 | 工具 handler |
|---|---|
| `builtin_basic.py` | `echo`、`get_current_time` |
| `builtin_reminders.py` | `schedule_reminder`、`remind_alert`、`confirm_reminder`、`snooze_reminder`、`list_tasks`、`cancel_task`、`dismiss_notifications`、`show_notifications` |
| `builtin_memory_write.py` | `remember_or_update`、`run_memory_maintenance` |
| `builtin_memory_recall.py` | `memory_search`、`memory_timeline`、`memory_event_log`、`history_search` |
| `builtin_files.py` | `safe_delete`、`read_text_file` |
| `builtin_knowledge.py` | `read_document`、`ingest_document`、`reindex_document`、`search_knowledge` |
| `builtin_web.py` | `web_search`、`fetch_web_page` |
| `builtin_mcp.py` | `search_mcp`、`install_mcp_sandbox`、`plan_capability` |
| `builtin_selfdev.py` | `create_selfdev_plan`、`apply_patch_to_inactive_slot`、`promote_slot`、`rollback_slot` |
| `builtin_runtime_state.py` | `query_rhythm`、`query_attention`、`update_working_state` |
| `forget_tool.py`（同属内置工具实现，非 `builtin_*` 命名） | `forget`、`forget_status` |

**工具执行三入口**（改执行语义必读）：`execute`（正常 LLM 调用）/ `execute_approved`（人工审批后）/ `execute_brokered`（Task Action 独立执行边界）。读操作直接线程池执行；写外部效应入 Outbox；`executor_kind=desktop_node` 走 `DesktopConnectionManager.dispatch_sync`。

**36 个内置工具清单**（注册于 `builtin_tools.py::register_builtin_tools` L141 起；handler 实现分布于上表 `builtin_*.py` 领域模块，经 `builtin_tools.py` 末尾 re-export 保持导入契约不变）：
基础：`echo`、`get_current_time`；提醒/任务：`schedule_reminder`、`remind_alert`、`confirm_reminder`、`snooze_reminder`、`list_tasks`、`cancel_task`、`show_notifications`、`dismiss_notifications`；记忆写入：`remember_or_update`、`forget`、`forget_status`、`run_memory_maintenance`；记忆召回：`memory_search`、`memory_timeline`、`memory_event_log`、`history_search`；文件：`safe_delete`、`read_text_file`、`read_document`；知识库：`ingest_document`、`reindex_document`、`search_knowledge`；联网：`web_search`、`fetch_web_page`；MCP：`search_mcp`、`install_mcp_sandbox`、`plan_capability`；自进化：`create_selfdev_plan`、`apply_patch_to_inactive_slot`、`promote_slot`、`rollback_slot`；节奏/注意力：`query_rhythm`、`query_attention`；WorkingState：`update_working_state`。
动态补充：桌面节点 14 个 `desktop_*` 能力（`desktop/protocol.py::DESKTOP_CAPABILITIES`）+ MCP 桥接工具（`mcp/bootstrap.py`）。

### 3.7 `skills/` 与 `mcp/` 与 `selfdev/`

- `skills/catalog.py`：`SkillDefinition` L10（`as_dict(include_instructions=False)` L23）；`_DEFINITIONS` L35-92（4 个：document_processing/web_research/knowledge_base/mcp_management）；`list_skills()` L100、`get_skill()` L108；指令文本在 `skills/builtin/<id>/SKILL.md`。被 `task_runtime/tools.py:43`、`task_runtime/context_assembler.py:111`、`api/routes_skills.py` 使用。
- `mcp/`：`discovery.py`（`_BUILTIN_CATALOG` L61-110、`search_mcp_candidates` L113、`allowed_npm_packages` L193）；`capability_planner.py`（`plan_from_goal` L118、`_compute_risk` L170）；`installer.py`（`SANDBOX_ROOT` L39=`<repo>/.data/mcp_sandbox`、`install_sandbox` L165、`run_smoke` L315）；`runtime_client.py`（`MCPRuntimeClient` L361、每能力长驻 `_SessionWorker`、`get_runtime_client` L534）；`bootstrap.py`（`register_capability_tools` L121 untrusted 包装、`restore_active_capabilities` L235 启动恢复）；`builtin.py`（`ensure_builtin_mcp_capability` L83、`BUILTIN_MCP_TOOLS` L17-57）；`builtin_server.py`（FastMCP stdio server，3 工具）。
- `selfdev/`：`trusted_core.py`（`TRUSTED_CORE_PREFIXES` L9-23，13 条受保护路径，fail-closed）；`planner.py`（LLM 生成 patch plan+逐条校验）；`patch_executor.py`（`apply_to_inactive`：复制活跃槽位基线→应用）；`targeted_test_runner.py`（按变更文件前缀匹配测试，frontend 变更跑 `npm run build`）；`promote_rollback.py`（health check + 安全门后切活跃槽位指针；与 `release_manager.promote` 共用 `evaluate_promotion_gate`，无生产调用方）。

### 3.8 `worker/` — Outbox 事务队列 + 调度守护

| 文件 | 职责 / 关键符号 |
|---|---|
| `outbox_worker.py` | `OutboxWorker` L47：`claim_one` L69（skip_locked+双重 lease+claim_token）、`enqueue` L152（allowlist 强校验）、`poll` L182、`_dispatch_one` L218；终态 `_finalize_job` L366 / 重试 `_retry_later` L405 / 死信 L519 / forget 阶段死信 L677 |
| `handler_registry.py` | `HandlerRegistry` L10：job_type→handler + schema_version 白名单 |
| `outbox_dto.py` | `HandlerOutcome` 枚举 L16（COMPLETED/RETRY_LATER/NON_RETRYABLE/RETRYABLE_ERROR/CLAIM_LOST/CONTINUE）；`ClaimedJob`/`HandlerResult`/`ActiveClaim`；`FencingViolationError` L102 |
| `outbox_handlers.py`（96KB） | 装配 `register_all` L1338（17 类 job_type，schema_version=1）；`handle_memory_extraction` L106、`handle_reminder_delivery` L350、`handle_core_memory_refresh` L617、`handle_memory_vector_refresh` L639、`handle_memory_markdown_project` L664、`handle_retrieval_index_refresh` L697、`handle_memory_maintenance` L1165、`handle_segment_sealing` L1622、`handle_epoch_rollover` L1961、`handle_epoch_checkpoint` L2055 |
| `handlers_forget.py`（71KB） | 遗忘 Saga：`handle_forget_cascade` L133（目标物化 L239/墓碑 L435/依赖发现 L619）、`handle_forget_rebuild` L807、`handle_forget_purge` L1356（`_scrub_wide` L1535）、`handle_forget_verify` L1677、`handle_forget_reconcile` L1801；`BATCH_SIZE=200` |
| `handlers_retention.py` / `handlers_agent_tasks.py` | `handle_retention_cleanup` L27 分批驱动 RetentionEngine；`handle_agent_task_run` L16 驱动 PersistentTaskRuntime |
| `scheduler_daemon.py`（27KB） | 模块级 `scheduler` L33；`start_daemon` L90（8 个周期 job）；`schedule_outbox_poll` L149；`_poll_tool_operations` L193（独立线程防饿死）；`_agent_task_scanner_job` L216（stale run 恢复/锁过期/watcher/审批过期）；`_idle_scanner_job` L263；`_forget_reconcile_scanner_job` L423；`enqueue_maintenance_job` L453、`enqueue_retention_job` L509 |
| `outbox_heartbeat.py` / `job_types.py` / `task_worker.py` | 租约续期线程 + `ActiveClaimRegistry`；`ENABLED_OUTBOX_JOB_TYPES` allowlist；到期提醒原子入队 `reminder_delivery`（FOR UPDATE SKIP LOCKED） |

### 3.9 `task_runtime/` + `control/` — 持久任务与执行边界

- `task_runtime/runtime.py`：`PersistentTaskRuntime.run` L43、`_dispatch_ready` L262、`_complete` L346、`_budget_issue` L425（LLM 只提案不执行，Broker 统一派发）
- `task_runtime/repository.py`：`TaskRepository` 唯一写入口：`create_task` L121、`transition_task` L185、`append_event` L217、`checkpoint` L265、`enqueue_run` L310、`start_run` L329、`create_action` L400、`ready_action` L481、`cancel_task` L487、`send_input` L526、`detail` L570；`recover_stale_tasks` L660 / `reconcile_queued_tasks` L704（scheduler 调用）；commit 后 WS 广播 L46（SAVEPOINT 水位防幽灵消息）
- `task_runtime/tool_injection.py`：`WORKER_RESIDENT_CAPABILITIES`（L0 常驻清单）、`FULL_INJECTION_MAX_TOOLS=12`、`discovery_state/record_search/record_definition/merge_state`（只存 id + 指纹）、`build_tool_view`（执行视图与发现视图分离，`in_scope` 标注；loaded 按当前注册表重建，`definition_changed`/`unavailable` 不返回旧 Schema）
- `task_runtime/agent_executor.py`（`decide` L31，JSON 修复解析）、`context_assembler.py`（L20，绝不加载会话 raw history；`_bounded_value` 超限降级 sha256+preview；`snapshot` 输出 `tools.*` 而非全量 capabilities）、`tools.py`（`build_main_agent_registry` L190：主 Agent 仅见 delegate/status/cancel/input/list_skills）、`router.py`、`state_machine.py`、`resource_locks.py`（`ResourceLockService` L30，固定顺序获取防死锁）、`evidence.py`（`EvidenceStore` L30 内容寻址）、`watchers.py`（time/file/node 三类，`wake_node_tasks` L95）、`worktree.py`（Git 隔离）、`schemas.py`
- `control/`：`capability_broker.py`（`CapabilityBroker`：TaskScope 校验→TaskPolicyEngine→ResourceLock→`registry.execute_brokered`）；`scope.py`（`TaskScope.from_brief` L47/`validate_arguments` L62/`as_envelope` L97）；`task_policy.py`（allow/approval/block）；`approval_service.py`（审批生命周期）

### 3.10 `desktop/` — 桌面执行节点通道

| 文件 | 关键符号 |
|---|---|
| `protocol.py` | `DESKTOP_CAPABILITIES` L43（14 个能力，服务端独裁安全属性）、`definition_for` L142、`DesktopActionEnvelope` L146（fencing_token/idempotency_key/arguments_hash） |
| `schemas.py` | hello/能力/绑定/结果消息 Pydantic 校验（能力名必须 `desktop_` 前缀） |
| `node_service.py` | 节点持久化唯一入口：`register_online` L20、`heartbeat` L44、`expire_stale` L64、`resolve_task_node` L112 |
| `connection_manager.py` | node_id→唯一 WS 映射；`dispatch_sync` L80、`dispatch_action_sync` L122、`resolve_ack` L198、`resolve_result` L206、`detach` L54 |
| `registry_overlay.py` | `build_registry_for_thread()` L66：基础注册表+在线节点能力快照 |
| `reconciliation.py` | Node Journal 服务端投影：`apply_entry` L58、`apply_missing` L149、`outstanding_action_ids` L26 |

### 3.11 其余包

- `knowledge/`：`ingestor.py`（摄入+`search_chunks` Postgres ILIKE）、`chunker.py`、`document_reader.py`（TXT/MD/HTML/JSON/CSV/TSV/DOCX/PDF）、`access.py`（访问控制）
- `supervisor/`：`health_probe.py`（健康探针）、`launcher.py`（进程守护拉起）、`release_manager.py`（**生产晋升入口**：`promote(run_gate=True)` 先健康检查再跑安全门 `evaluate_promotion_gate`，切换后进入观察窗，异常自动回滚；回滚不设闸门）、`slot_manager.py`（A/B 槽位管理，manifest excludes 列表含 qdrant——全仓未用 Qdrant，向量走 pgvector）
- `context/run_context.py`：运行上下文；`storage/object_store.py`：对象/工件存储；`web/client.py`：Web 检索 HTTP 客户端（SSRF/重定向/大小限制）
- `prompts/`：LLM prompt 模板（`manifest.toml` + 16 个 md），`main.py::validate_prompt_catalog` 启动校验

### 3.12 `db/` 与 `alembic/`

- `db/base.py`：`engine` L18、`SessionLocal` L27、`get_db()` L30（FastAPI 依赖）、`create_all()` L43
- `db/models.py`（59 表，`Base` L17）、`db/forget_models.py`（10 表）、`db/retention_models.py`（2 表）—— 表清单见第 6 节
- `alembic/env.py`：`target_metadata = Base.metadata` L29，**显式导入 forget/retention 模型** L12-13 防漏表；URL 取自 `settings.database_url`
- 迁移链：baseline `69da20d1e47c`（全量 schema）→ … → HEAD `e2f6a8c4d1b7`（persistent_agent_tasks），现行链 15 个；`versions/_archive_migrations_20260717/` 为历史归档（已被 baseline squash）
- **新增表流程**：`db/models.py` 加模型 → `alembic/env.py` 确认导入 → `alembic revision --autogenerate` → 检查生成的迁移（尤其索引/约束）→ 应用

---

## 4. 前端地图（`frontend/`）

技术栈：React 19 + TS + Vite 6 + Tailwind 3。**无 react-router**：App 内 Tab state + `history.pushState`；部署 base `/aiive/`（`vite.config.ts`，`AIIVE_BASE_PATH=/` 可覆盖）；`src/lib/base.ts` 导出 `BASE_PATH` 供 API/WS/静态资源拼接。

| 层 | 文件 | 说明 |
|---|---|---|
| 入口 | `src/main.tsx` | createRoot + StrictMode |
| 根组件 | `src/App.tsx`（`App` L53） | 9 页 lazy Tab 导航、通知角标、亮/暗主题、`trace_id` 跨页传递、`/aiive/developer` 路径识别 |
| 样式 | `src/index.css` | Tailwind + 语义色 CSS 变量（`:root`/`[data-theme=dark]`）+ hljs 代码高亮 token |
| API 客户端 | `src/api/chat.ts` | `getThreadMessages` L64、`sendMessageStream` L124（SSE）、`resetThread` L272、`respondApproval` L303；`ApiError` L89/`StreamIncompleteError` L104 |
| API 客户端 | `src/api/agentTasks.ts` | `listAgentTasks` L88、`getAgentTask` L94、`createAgentTask` L98、`cancelAgentTask` L113、`sendAgentTaskInput` L117、`decideTaskApproval` L125 |
| Hooks | `src/hooks/useNotificationSocket.ts` | `useNotificationCount` L70 / `useNotificationListener` L77（单例 WS `/ws/__global__`，3s 重连） |
| Hooks | `src/hooks/useTaskEvents.ts` | `useTaskEvents` L7（每任务 WS，100ms 去抖 refresh） |
| 组件 | `src/components/Markdown.tsx` | react-markdown + remark-gfm + rehype-highlight（surface/colored 两变体） |

**页面清单**（均 lazy 加载于 `App.tsx`）：

| 页面 | Tab | 后端 API |
|---|---|---|
| `pages/ChatPage.tsx`（57KB，最复杂） | chat | `POST api/chat/stream`(SSE)、`GET api/threads/{id}/messages`、`POST api/thread/reset`、`POST api/approval/respond`、`POST api/chat/system`；WS `/ws/{threadId}`。含：SSE 打字机、流式纯文本/完成 Markdown 切换、断流恢复、工具状态机 `normalizeToolCallStatus`、`ToolCallCard` L133、reminder/approval/maintenance_report 卡片、`inFlightRef` 防重发、AbortController、离底未读计数、历史分页 |
| `pages/TaskCenterPage.tsx` | tasks | agent-tasks 全套 + `evidence/{id}`、`artifacts/{id}`；WS `api/agent-tasks/ws/{id}`；4s 轮询兜底 |
| `pages/MemoriesPage.tsx` | memories | `api/memories` + `/{id}/{sleep|archive|forget}` |
| `pages/CapabilitiesPage.tsx` | capabilities | `api/mcp/capabilities`、`api/tools`、`api/skills`、`api/capabilities?state=candidate`、`api/knowledge` |
| `pages/EventTimeline.tsx` | events | `GET api/inspector/events?trace_id=&limit=50` |
| `pages/ContextInspector.tsx` | context | `GET api/context-runs/{traceId}`（+`/items/{itemId}`） |
| `pages/RetrievalInspector.tsx` | retrieval | `api/retrieval-runs`、`api/memory-recall-runs`（+`/{id}`） |
| `pages/NotificationsPage.tsx` | notifs | `GET api/notifications?category=`、`DELETE api/notifications/{id}` |
| `pages/DeveloperPage.tsx` | developer（独立路径 `/aiive/developer`，需 `VITE_AIIVE_DEVELOPER_UI_ENABLED=true`） | `api/debug/events`、`api/debug/llm_calls`、`api/epochs/{threadId}`、`api/outbox/jobs` |

**新增前端功能套路**：`src/api/xxx.ts` 加客户端函数 → `src/pages/XxxPage.tsx` → `App.tsx` 注册 lazy + Tab。

---

## 5. 客户端地图（`apps/`）

### 5.1 `apps/android/` — Capacitor 遥控端
纯遥控：HTTP/SSE 对话 + 远程放行桌面高危操作审批。不承载 Agent 逻辑、不本地执行工具。
`src/main.tsx` → `src/App.tsx`（消息列表/输入/主题/流式/审批卡，localStorage 最近 200 条）；`src/api/chat.ts`（`streamMessage` SSE 事件 started/token/done/error、`resetConversation`、`respondApproval`）；`src/config.ts`（后端地址 `VITE_AIIVE_API_BASE_URL`，默认空防误连）；`capacitor.config.ts`（appId `com.aiive.companion`，cleartext 允许 http）；`android/app/src/main/java/com/aiive/companion/MainActivity.java`（Capacitor 壳）。无鉴权头，依赖后端 CORS 白名单（含 `capacitor://localhost`）。

### 5.2 `apps/desktop/` — Electron + 本地执行节点
双重身份：(a) BrowserWindow 壳（`webUrl = {backend}/aiive/`）；(b) utilityProcess 子进程 Node Host，把本机 14 个能力以 WS 长连接注册给后端。
`src/main/index.mjs`（窗口/托盘/开机自启 + fork node-host，崩溃指数退避 1s→30s）；`src/preload/index.cjs`（contextBridge `getRuntimeInfo`）；`src/node-host/client.mjs`（27KB 核心：声明能力、连 `ws://{backend}/ws/desktop/{nodeId}` protocol v2、收 `operation` 指令本地执行、回 ack/result；hello/heartbeat/operation.ack/operation.result/journal.snapshot）；`src/node-host/journal.mjs`（`~/.aiive/action-journal.json` + InFlightActionCoordinator 合并同 Action）；`src/node-host/locks.mjs`（`assertTaskScope`/`assertResolvedPathScope` realpath 防逃逸/租约）；`src/node-host/journal.test.mjs`（`node --test`）。鉴权 `AIIVE_DESKTOP_NODE_AUTH_TOKEN`。

---

## 6. 数据库表地图（71 表）

> 模型定义：`db/models.py`（59）+ `db/forget_models.py`（10）+ `db/retention_models.py`（2）。迁移：baseline `69da20d1e47c`，HEAD `e2f6a8c4d1b7`。

| 域 | 表 | 说明 |
|---|---|---|
| 会话基础 | `threads`、`events`、`llm_calls`、`context_snapshots` | 线程/事件日志/LLM 调用/上下文快照 |
| 回合执行 | `turn_records` | 单回合状态机（租约/指纹/响应） |
| 上下文压缩 | `epochs`、`segments`、`segment_summaries`、`epoch_checkpoints`、`working_states`、`compaction_inputs`、`compaction_runs`、`epoch_compaction_inputs`、`checkpoint_runs` | Epoch/Segment/摘要/检查点/工作状态/压缩流水线 |
| 记忆 V2 | `memory_records`、`memory_proposals`、`memory_ingestion_runs`、`memory_evidence`、`memory_lineage` | 记忆主档/提案/摄取运行/证据/血缘 |
| 记忆向量 | `memory_vector_projections` | pgvector 512 维投影 |
| 记忆召回 | `memory_recall_runs`、`memory_recall_candidates`、`core_memory_blocks` | 召回运行/候选/核心记忆块 |
| 记忆维护 | `memory_maintenance_runs`、`_batches`、`_inputs`、`_actions` | 维护 run/batch/input/action |
| 检索索引 | `retrieval_index_generations`、`_entries`、`_tokens`、`_runs` | 索引代次/条目/倒排 token/运行 |
| 知识库 | `documents`、`chunks`、`retrieval_runs`、`retrieval_candidates` | 文档/分块/检索运行/候选 |
| 遗忘 Saga | `forget_operations`、`forget_selector_manifests`、`forget_shields`、`forget_targets`、`forget_dependencies`、`forget_batches`、`forget_actions`、`forget_tombstones`、`forget_stage_runs`、`content_provenance_refs` | Phase 6A 遗忘全家桶 |
| 保留治理 | `retention_cleanup_runs`、`retention_cleanup_batches` | Phase 6B |
| 能力/MCP | `capabilities`、`capability_versions`、`mcp_install_records`、`capability_plans` | |
| 自进化 | `selfdev_requests`、`patch_operations` | |
| 桌面节点 | `desktop_nodes`、`thread_desktop_bindings`、`desktop_action_receipts` | |
| 审批 | `approval_requests` | 工具调用人工审批 |
| Outbox/工具 | `outbox_jobs`、`tool_operations` | 事务发件箱/工具操作（幂等） |
| 任务/Agent | `tasks`、`attention_states`、`agent_tasks`、`agent_runs`、`agent_actions`、`agent_task_events`、`agent_task_checkpoints`、`task_evidence`、`task_artifacts`、`task_resource_locks`、`agent_task_watches` | 条件任务/注意力/持久 Agent 任务全家桶 |
| 工件 | `artifacts` | 通用工件存储 |

---

## 7. 关键链路追踪（一次请求的完整路径）

### 7.1 一次聊天回合（`POST /api/chat/stream`）

```
routes_chat.py (L163)
  → runtime/turn_execution.py::TurnExecutionService.execute_turn_stream (L228)
      → _resolve_and_preempt (L451)  Thread 行锁 + epoch/segment 归属 + 幂等
      → TurnHeartbeat (L100)  30s 续租
      → _load_context (L1090)
          → context_assembler.py::ContextAssembler (L185)
              → context_budget.py 分区预算裁剪
              → _unified_recall (L518) → memory/automatic_recall.py::recall (L136)
                    → recall_fusion.py::fuse_and_pack (L32) → MemoryRecallPack
      → agent_graph.py::AgentGraph._execute_graph_stream (L1084)
          → _assistant (L546, llm_client.chat + bind_tools)
          → _policy_check (L569, policy_engine.check_tool_calls)
          → _validate_tools (L619, tool_validation) → _tools_node (L766, registry.execute)
                ↳ 读操作: _execute_direct 线程池
                ↳ 写外部: operation_executor.enqueue_tool_operation → outbox_jobs
                ↳ desktop_node: DesktopConnectionManager.dispatch_sync → Electron
                ↳ high risk: _confirm_node (L733) → ApprovalRequest → 前端/Android 放行
      → _finalize_turn (L705)
          → Event 写入 + LLMCall + memory_extraction Outbox 入队
          → action_planner.classify_memory_signal (是否提取记忆)
      → _rotate_snapshot (L907)  上下文快照轮转
  ← SSE token/工具事件流 (api/ws_manager 同步推送通知)
```

### 7.2 记忆写入（异步）

```
turn 收尾入队 memory_extraction
  → worker/outbox_worker.py::poll (L182) → _dispatch_one (L218)
  → worker/outbox_handlers.py::handle_memory_extraction (L106)
  → memory/memory_extractor.py (LLM 提取提案) → proposal_normalizer
  → memory/memory_write_service.py::MemoryWriteService.write_batch (L153)
  → memory/conflict_resolver.py::resolve (L41)  → supersede/revise/reinforce/merge/create
  → memory/memory_store.py 持久化 + memory_lineage + 事件
  → 入队三个投影 job: core_memory_refresh / vector_refresh / markdown_project
```

### 7.3 遗忘 Saga（五阶段，全部 Outbox 驱动）

```
forget 工具 (tools/forget_tool.py) → MemoryWriteService.forget (L534)
  → forget_operations 行 + 5 个 stage job 入队
  → worker/handlers_forget.py:
      handle_forget_cascade (L133)   目标物化+选择器解析+墓碑+依赖发现
      handle_forget_rebuild (L807)   摘要/检查点重建
      handle_forget_purge  (L1356)   宽表擦除 (_scrub_wide L1535)
      handle_forget_verify  (L1677)  校验无残留
      handle_forget_reconcile (L1801) 对账 + shield 防再摄取
  → scheduler_daemon._forget_reconcile_scanner_job (L423) 周期对账兜底
```

### 7.4 持久 Agent 任务

```
api/routes_agent_tasks.py 创建任务 → task_runtime/repository.py::create_task (L121)
  → 入队 agent_task_run → worker/handlers_agent_tasks.py::handle_agent_task_run (L16)
  → task_runtime/runtime.py::PersistentTaskRuntime.run (L43)
      → context_assembler (独立 Task Context，不加载会话 raw history)
      → agent_executor.decide (L31, LLM 只提案 JSON)
      → _dispatch_ready (L262) → control/capability_broker.py
            → TaskScope 校验 → TaskPolicyEngine → ResourceLockService
            → tools/registry.py::execute_brokered (L436)
      → evidence.py 落 Evidence/Artifact → repository.append_event → commit 后 WS 广播 (L46)
      → watchers.py 到期唤醒 / scheduler._agent_task_scanner_job (L216) 恢复 stale run
```

### 7.5 启动装配（`main.py::lifespan`）

```
绑定事件循环 → validate_prompt_catalog → _ensure_schema (Alembic upgrade head)
  → HMAC 密钥 → _ensure_system_thread → _ensure_retrieval_generation
  → ContextBudget 校验 → 向量运行时 (vector_bootstrap)
  → mcp/bootstrap.py::restore_active_capabilities (L235) + ensure_builtin_mcp_capability
  → supervisor start_daemon (scheduler_daemon.py L90, 8 个周期 job)
  → OutboxWorker + OutboxHeartbeat + ws_manager + desktop_connection_manager
```

---

## 8. 测试与脚本地图

### 8.1 测试

三层结构（全部离线，无真实外部 API 调用）：

| 位置 | 层级 | 说明 |
|---|---|---|
| `tests/unit/backend/` | 单元 | 后端单元测试（67 个 `test_*.py`），命名 `test_<被测试模块>.py`；`tests/unit/conftest.py` 经"分发式 SessionLocal"把模块级 DB 绑定重定向到按用例隔离的测试库 |
| `tests/integration/backend/` | 本地集成 | 多组件组合测试（Worker/Handler/调度/检索/生命周期），共享进程级 SQLite + 每用例重建表；`_util.py` 为共享夹具辅助 |
| `tests/flow/` | 关键流程 | 跨模块高价值链路（Turn 执行、提醒闭环），外部边界离线模拟 |
| `tests/conftest.py` | 公共 | 从 `.env` 仅加载 `AIIVE_` 前缀变量；`db_session` 夹具；按目录自动标注层级 marker |
| `tests/support.py` | 公共 | SessionLocal 分发器安装（先于 aiive 模块导入） |
| `apps/desktop/src/node-host/journal.test.mjs` | Node | Node 内置 `node --test`（`npm run test:node-host`） |

跑测试：
- 默认快速（单元+流程，无需任何外部服务）：`python -m pytest tests/unit tests/flow`
- 本地集成：`python -m pytest tests/integration`
- 完整：`python -m pytest`
- 按层级筛选：`-m unit` / `-m integration` / `-m flow`；按模块 `-k "memory"`

### 8.2 脚本与构建

| 文件 | 用途 |
|---|---|
| `scripts/start.sh` | 本地一键启动（检测 local-embedding profile） |
| `scripts/build-app.sh` | 统一客户端构建入口（`list`/`android debug|release`/`desktop debug|release`），分发到 `scripts/app-builders/` |
| `scripts/dump_agent_context.py` | 导出 Agent 上下文（调试） |
| `scripts/prepare_brand_assets.py` | 品牌资源派生 |
| `scripts/smoke/` | 冒烟脚本 |
| `scripts/check_single_file_tests.sh` | 单文件隔离运行防线：每个测试文件单独进程跑一遍，捕获跨文件顺序依赖（CI 与本地共用） |
| `docker-compose.yml` | postgres(pgvector:pg16, 127.0.0.1:5432) + backend(8000, env_file .env) + frontend(127.0.0.1:8080) + 可选 embedding(TEI, profile local-embedding) |
| `backend/Dockerfile` | python:3.12-slim；apt/pip 源由 build-arg 控制（`APT_MIRROR` 默认阿里云、`PIP_INDEX_URL` 默认腾讯云，CI 传官方源）；uvicorn `aiive.main:app` |
| `frontend/Dockerfile` | node 多阶段构建（`NPM_REGISTRY` build-arg，默认腾讯云）→ nginx:alpine:8080；`nginx.conf`：SPA 回退 + `/api/` 反代 backend:8000（SSE 关缓冲）+ `/ws/` WebSocket 反代 |
| `pyproject.toml` | 依赖：fastapi/langgraph/langchain/mcp<2/apscheduler/litellm/pgvector；dev=pytest；package-data 含 prompts 与 skills SKILL.md |

---

## 9. 图谱维护规则（强制）

**开发前（必做）**：
1. 先读本文档第 2 节快速定位表，确定要改的功能涉及的核心文件与关联文件；
2. 只读定位到的文件，不通读无关模块；
3. 若第 2 节找不到对应功能，按第 3 节模块地图逐包定位，定位后**把新功能补进第 2 节**。

**开发后（必做）**：完成后对照实际改动更新本文档——
- 新增/删除/重命名文件 → 更新对应模块文件表；
- 新增类/函数且属于"核心符号"→ 更新关键符号表（行号允许近似，标注即可）；
- 新增工具/API 端点/页面/表 → 更新第 2 节定位表、第 3.2 节路由表、第 4/6 节相应清单；
- 修改模块间依赖或数据流 → 更新第 1 节架构图与第 7 节链路；
- 删除功能 → 移除对应条目，不留死链。

**一致性要求**：文档中的路径必须真实存在；行号漂移可容忍但每季度（或大重构后）应校正一次。修改 `docs/CODE_MAP.md` 本身应随功能改动在同一提交内完成。


---



---

---

## 10. 模块依赖图与循环依赖核查（2026-10-21，第一轮重构）

> 依据目标阶段一(1.4)与阶段六(6.3)：梳理 backend/aiive 的 import 依赖、调用链与潜在循环依赖，先确认现状再记录。

### 10.1 方法
- 对 backend/aiive 全量 .py 做 AST 静态 import 扫描，构建模块依赖图，Tarjan 求强连通分量（SCC）。
- 扫描规模：约 212 个模块（含 10 个 builtin_*.py 领域模块与 eval/ 安全门 9 个模块）。
- 同时实跑 import + register_builtin_tools：成功注册 36 个工具，无 ImportError，证明所有静态环均为惰性（lazy）延迟导入，加载期可安全解析。

### 10.2 依赖方向（允许方向）
- 共享机制 _build_safety / db_handler / require_ctx 定义在 tools/builtin_tools.py（单一实现，领域模块复用，不复制装饰器）。
- 领域模块 builtin_*.py → builtin_tools（仅取 db_handler / require_ctx）。
- builtin_tools.py → 领域模块（末尾 re-export）+ 共享机制（本模块定义）。
- 关键约束：SessionLocal 与 db_handler 必须同处 builtin_tools 命名空间——测试与 tests/support.py 会 monkeypatch aiive.tools.builtin_tools.SessionLocal 把会话重定向到隔离测试库。因此曾尝试的「下沉到独立 _shared 叶子模块」方案已回退（会破坏该 monkeypatch 契约）。
- tools/registry.py 仅在 get_tool_registry() 内部延迟 import builtin_tools，不构成加载期环。
- eval/ 安全门包是纯消费方：eval.replay → tools.registry（仅模块导入）、eval.gate → eval.contracts/replay/models；
  selfdev/promote_rollback 与 supervisor/release_manager → eval.gate（顶层导入，已实测无加载期环）。

### 10.3 检测到的环（SCC>1）
共 3 个 SCC，全部为延迟/惰性导入，加载期可正常导入：
1. tools 集群（最大 SCC）：control.approval_service、control.capability_broker、control.task_policy、desktop.registry_overlay、tools.builtin_*（10 个领域模块）、tools.builtin_tools、tools.operation_executor、tools.registry、worker.scheduler_daemon。
   - 主因：既有 registry ↔ builtin_tools 延迟依赖 + control/desktop/worker 既有耦合；领域模块 ↔ builtin_tools 为惰性环（共享符号在 re-export 之前定义，import 期安全）。
2. runtime.agent_graph ↔ runtime.context_assembler —— 既有（本轮未改变其相互引用语义），惰性安全。
3. worker.handlers_forget ↔ worker.outbox_handlers —— 既有（遗忘 Saga 与 Outbox 处理器互引），惰性安全。

### 10.4 结论与处置
- 本轮重构未引入加载期循环依赖；工具注册、导入均正常（37 工具，含本机 `run_shell`，无 ImportError）。
- 残留 3 个 SCC 均为既有惰性环，加载与运行正常；修复需侵入式改动（涉及 control/worker/registry 核心契约），超出本轮「不改变业务行为」边界，列为下一轮治理项（见审计报告 §8.5）。

### 10.5 静态检查基线（2026-10-21）

> 本轮已把两类静态检查实际跑通并固化为基线，后续改动应以此为对照，不得通过关闭规则或放宽配置制造通过。

| 工具 | 版本 | 安装方式 | 全仓结果 | 本轮处理 |
| --- | --- | --- | --- | --- |
| ruff | 0.17.1 | pip（本机全局 Scripts） | 59 issues | 修 2×F821 真实缺陷、15×I001、1×UP035、6×F841；余为既有风格项 |
| basedpyright | 1.40.2 | pip | 5 errors / 67 warnings | 修 1 个由本轮引入的 reportMissingTypeArgument；余 4 error 在未触碰文件 |

命令（仓库根）：

- `ruff check backend/aiive`（按需 `--select F821,F841,I001` 定位真实缺陷）
- `basedpyright`（读取 pyproject.toml 的 [tool.basedpyright]；重构核心 `basedpyright backend/aiive/tools backend/aiive/runtime/agent_graph.py` 应为 0 errors）

已知遗留（非本轮引入，勿重复排查）：

- basedpyright error（第一轮已全部修复，第二轮复查全仓 0 error）：`forget/visibility_service.py:189/214/257`（setdefault / update 的 str | None）、`worker/handlers_forget.py:1502`（list[str | None] → list[str]）。
- ruff 既有风格项：UP037（本文件族沿用引号注解风格）、UP017、B023、BLE001、B010、C408、agent_graph.py 第三方导入块 I001。
- basedpyright warning 主体（第一轮遗留 66 条）：域模块 re-export 私有 `_handle_*` 触发的 reportPrivateUsage / reportUnusedFunction。
  第二轮已消除：领域模块 handler 改为公开名 `handle_*`，`builtin_tools` 以私有别名 `_handle_*` re-export（tests 引用不变），
  共享机制同步改为 `db_handler` / `require_ctx`；并移除 8 条多余 `# pyright: ignore`、修正 1 处 `func.max` 空表判断的类型收窄。

**第二轮复跑基线（2026-10-22）**

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| ruff | `ruff check backend/aiive --select F,E9` | 0 error（已纳入 CI 门禁） |
| basedpyright | `basedpyright`（全仓） | 0 errors / 0 warnings（第一轮遗留 66 warnings 清零） |
| 全量测试 | `python -m pytest` | 857 passed / 0 failed（改名与类型收窄后复跑） |
| 单文件隔离 | `bash scripts/check_single_file_tests.sh` | 91/91 个测试文件单独运行通过（已纳入 CI） |

**第三轮复跑基线（2026-10-22，安全门接入 ReleaseManager + 契约口径修正后）**

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| ruff | `ruff check backend/aiive --select F,E9` | 0 error |
| ruff（改动文件全规则） | `ruff check <改动文件>` | 新增/改写文件 0 issue；`builtin_tools.py` 维持 4 项 HEAD 存量（C408、B010×2、re-export 块 I001，均非本轮引入） |
| basedpyright | `basedpyright`（全仓） | **0 errors / 0 warnings / 0 notes** |
| 全量测试 | `python -m pytest` | **884 passed / 0 failed / 153.79s**（876 + 本轮新增 8） |
| 单文件隔离（本轮改动的 7 个测试文件） | 逐文件单独进程 | 7/7 通过 |

第三轮修复记录（勿重复排查）：

- `eval/replay.py` 访问 `bt.SessionLocal` 曾触发 3 条 `reportPrivateLocalImportUsage`——**仅全仓跑法可见，局部文件跑法不触发**；静态检查声明必须以全仓入口（CI 的 `basedpyright`）为准。已在 `builtin_tools.py` 用同名别名显式再导出修复（`# noqa: PLC0414`，monkeypatch 契约不变）。
- 闸门契约口径：`check_memory_single_source` 改为按 `memory_key_registry` 的 cardinality 判定（multi/未注册键的多条 active 合法），与 `MemoryStore._infer_cardinality`、`memory_maintenance_planner` 的既有口径一致。
- gate severity 语义落地：仅 critical 失败阻断；warning 失败（`check_context_snapshot_persisted`）记入 `GateResult.notes` 不阻断。
- 晋升双入口共用 `evaluate_promotion_gate`；`ReleaseManager.promote(run_gate=True)` 为生产路径（REST 与 `promote_slot` 工具），回滚刻意不设闸门。

### 10.6 测试分层入口与耗时基线（2026-10-21）

> 全仓唯一入口是仓库根 pyproject.toml 的 [tool.pytest.ini_options]，testpaths = ["tests"]。
> 层级筛选必须用目录路径（pytest 9 的 -m 过滤先于 conftest 打标记，`-m unit` 不生效）。

| 入口 | 用例数 | 实测耗时（2026-10-22 安全门修复轮复测） |
| --- | --- | --- |
| `python -m pytest tests/unit` | 559 | 126.78s（夹具改造后 78.47s→安全门用例加入后回升；eval 四文件合计约 50s） |
| `python -m pytest tests/integration` | 315 | 31.48s |
| `python -m pytest tests/flow` | 10 | 9.13s |
| `python -m pytest`（全量） | 884 | **153.79s** |
| `python -m pytest tests/unit tests/flow`（默认快速集） | 569 | 约 136s（unit+flow 之和） |
| `PYTHON_BIN=.venv/Scripts/python.exe JOBS=4 bash scripts/check_single_file_tests.sh` | 91 个文件 | 约 3 分钟（91/91 通过；本轮另对 7 个改动测试文件单独复跑 7/7 通过） |

测试基础设施要点（改动前先读）：

- 根 `conftest.py`：注入 SQLite 地址与向量开关、为进程级库建表一次、统一结果输出。
- `tests/support.py`：SessionLocal 分发器（先于业务模块导入安装），`db_session` 会把隔离会话注册进来。
- `tests/conftest.py`：`db_session` 夹具（会话级模板库 `_db_template` 只建一次表，逐用例 `copyfile` 出独立库 + 外层事务回滚）；单元/流程层用 `db_session`。
  不要把模板库改回逐用例 `create_all`：实测建表 1.226s/次、拷贝 0.008s/次，改回去会让 unit 层从 78s 回到 330s。
- `tests/integration/conftest.py`：`db` 夹具（进程级共享库，会话级建表 + 用例级清空数据）；集成层用 `db`，不要用 `db_session`。
- 不请求 `db_session` 的用例会落到进程级库（已建表），适合需要生产代码自开会话提交的场景。

### 3.x `eval/` — 自进化安全闸门（Offline Replay Eval + Kernel Contract Checks）

> 最小可用的自进化安全门：promote 前必须离线跑通“内核契约检查 + 重放评测”，任一失败/异常/结果不完整一律 fail-closed 阻断晋升。无 LLM、无网络、无 API key，全部复用真实执行链（ToolRegistry.execute / MemoryStore / ORM 模型）。

| 文件 | 职责 / 关键符号 |
|---|---|
| `models.py` | 纯数据契约：`ContractResult`、`StepResult`、`EvalCaseResult`、`EvalRunResult`（`.ok`=无异常且 failed==0）、`GateResult`（`.to_summary()`）；无外部依赖，可 JSON 序列化 |
| `contracts.py` | 5 项内核契约检查（fail-closed，单检查异常记为 critical 失败）：`check_memory_single_source`（**按注册表 cardinality 判定**：single 基数键唯一；multi/未注册键多条 active 合法，见 memory_key_registry）、`check_key_events_persisted`（completed turn 必须有 chat_ended Event）、`check_outbox_idempotent`（operation_id 唯一 + 重复入队被唯一约束拒绝）、`check_safe_delete_guard`（危险/越界拒绝、域内允许）、`check_context_snapshot_persisted`（warning 级，不阻断）；`KERNEL_CONTRACTS` 列表 + `run_contract_checks(session, only=None)` |
| `scenarios.py` | 24 个离线确定性场景（eval.s01–eval.s24）：工具调用（echo/current_time/list_tasks/.../memory_search）、失败处理（未知工具/未授权来源/非法参数/safe_delete 拦截/域内允许）、记忆读写、Event/Outbox/Snapshot 写入与断言、组合流。`SCENARIOS` 列表；每场景用唯一 key/id 互不干扰 |
| `replay.py` | `isolated_eval_session()`（一次性 SQLite + 重定向 `aiive.tools.builtin_tools.SessionLocal` 给 db 工具用）；`ReplayRunner.run(session)` 把每个 step 派发到 `_exec_*` 走真实执行链；`run_replay_isolated()`（崩溃即 fail-closed 返回 `EvalRunResult(error=...)`） |
| `gate.py` | `SafetyGate.evaluate(live_session=None, candidate_app_dir=None, persist=False)`：先跑契约（有 live_session 用现场，否则隔离库），必跑 `run_replay_isolated()`，可选跑候选子进程（候选 backend 前置 PYTHONPATH 阴影当前 aiive，fail-closed 于非零/不可解析/缺 eval 包）；**severity 语义：仅 critical 失败阻断，warning 失败记入 notes 不阻断**；`ok = 关键契约 and 重放 and 候选`；`persist=True` 落盘 `runtime/eval_runs/*.json`。`evaluate_promotion_gate(candidate_slot_root, persist=True, gate=None)`：晋升统一入口（PromoteRollback / ReleaseManager 共用），异常一律转换为 ok=False |
| `persistence.py` | 结果落盘/读取：`save_gate_result` / `load_runs` / `latest`。目录解析优先级：`AIIVE_EVAL_RUNS_DIR` 环境变量 → 从 `__file__`/cwd 向上探测仓库根（含 `alembic.ini` 或 `pyproject.toml`+`backend/`）→ 历史 `parents[3]` 回退；源码与 wheel/site-packages（Docker）布局下均落在 `<repo>/runtime/eval_runs/` |
| `cli.py` | `python -m aiive.eval run [--json] [--out PATH]`：跑 `run_replay_isolated()` 并打印摘要或 JSON |
| `__init__.py` | 公共表面再导出 contracts/replay/gate/persistence/scenarios + 模型类 |

**接入点**：两个晋升入口在健康检查后、切换活动槽位前共用 `eval/gate.py::evaluate_promotion_gate(inactive_slot.root)`（fail-closed，异常也阻断）：
- `supervisor/release_manager.py::promote(run_gate=True)` —— **生产入口**（REST `POST /api/selfdev/{id}/promote` 与 `promote_slot` 工具共用；`ReleaseManager(manager=, probe=, state_file=, gate=)` 支持注入闸门替身）；
- `selfdev/promote_rollback.py::promote(run_gate=True)` —— 同语义保留入口（无生产调用方）。
回滚（rollback）是紧急逃生通道，刻意不设闸门。只读查询路由见 `api/routes_eval.py`（`GET /api/eval/runs`、`GET /api/eval/runs/latest`，`require_local_developer` 守卫）。

**测试**：`tests/unit/backend/test_eval_contracts.py`（5 契约正负例 + multi 基数合法重复）、`test_eval_replay.py`（24 场景全过 + 隔离稳定 + 损坏场景 fail-closed + 未授权工具拒绝）、`test_eval_gate.py`（干净系统通过 / 契约违规阻断 / warning 级不阻断 / 候选子进程失败阻断 / 候选缺 eval 包阻断 / promote 被阻断 / 通过后 proceed）、`test_eval_persistence.py`（落盘读取往返 + 目录解析）、`test_release_manager.py`（观察窗回滚 + 生产入口闸门阻断/放行/显式跳过）。

### 3.y PostgreSQL 核心契约测试（`tests/integration/pg/`）

> 在真实 PostgreSQL 上验证 SQLite 无法覆盖的事务/锁/并发语义。默认快速测试跳过（`pg` marker），
> 设置 `AIIVE_PG_TESTS=1` 后运行；CI 有独立 Job `postgres-contract`。

| 文件 | 职责 / 关键符号 |
|---|---|
| `tests/integration/pg/conftest.py` | `pg_url`（Docker `pgvector/pgvector:pg16` 一次性容器或 `AIIVE_PG_TEST_URL`）、`pg_engine`（`CREATE EXTENSION vector` + `create_all`）、`pg_sessionmaker`、`_route_sessionlocal`（把生产 `SessionLocal()` 重定向到 PG，复用 `tests/support.py` 分发器）、`_clean_pg`（每用例全表 TRUNCATE） |
| `tests/integration/pg/test_pg_contracts.py` | 8 个契约用例：PG 自检、advisory lock 真实持有、同键并发写只留 1 条 active、并发 claim 不重复、SKIP LOCKED 跳过锁定行不阻塞、forget cascade 异常回滚无半状态、forget deadletter 三表原子一致、写入回滚无记忆/投影残留 |

**被测实现（只读复用，未改动）**：`memory/memory_write_service.py::_acquire_lock`（`pg_advisory_xact_lock`）、`worker/outbox_worker.py::claim_one`（`with_for_update(skip_locked=True)`）、`worker/handlers_forget.py`（cascade 事务边界）、`worker/outbox_worker.py::_deadletter_forget_stage`（deadletter 原子更新）。

**迁移一致性**：ORM 与 Alembic 迁移后的库结构由 `alembic check` 守护（CI `alembic` Job 在 `upgrade head` 后执行）。
`memory_vector_projections` 的 HNSW 索引 `ix_memory_vector_embedding_hnsw` 由迁移以裸 DDL 创建，
模型侧在 `MemoryVectorProjection.__table_args__` 同步声明（`postgresql_using="hnsw"` +
`postgresql_ops={"embedding": "vector_cosine_ops"}`，`.ddl_if(dialect="postgresql")` 抑制 SQLite），
因此 `create_all` 与迁移建库的索引定义一致。
