# AIive 代码图谱（Code Map）

> **本文档是 AIive 项目的代码知识图谱**：按功能域描述"每块功能的核心实现代码、关键符号与模块间关联"，
> 目标是开发时先读本文档快速定位代码，避免通读无关文件。
>
> - **覆盖范围**：`backend/aiive/`（22 个包）、`frontend/src/`、`apps/`（android + desktop）、`tests/`、`scripts/`、`alembic/`
> - **路径约定**：均相对项目根（`AIive/`），如 `backend/aiive/main.py`、`frontend/src/pages/ChatPage.tsx`
> - **行号**：基于建图时的代码版本，仅供快速跳转，以实际代码为准
> - **维护规则**：见文末第 9 节。**新增/删除/移动文件、改 key 符号行号、改表结构后必须同步更新本文档**
> - 建图日期：2026-10-21；最近更新：2026-10-27（V49 单用户认证、V50 目标步骤触发规则 + 主动消息）

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
│  supervisor/  进程托管 (daemon / process_control / release_manager)│
└───────────────────────────┬────────────────────────────────────┘
                            ▼
        db/ (SQLAlchemy 73 表)  +  alembic/ (schema 唯一真相源)
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
| 主动消息写入时间线（V50） | `runtime/proactive_message.py`（`ProactiveMessageService.post`） | `runtime/epoch_manager.py::peek_next_turn_sequence`、`worker/handlers_goal_triggers.py`、`api/ws_manager.py` | `test_goal_triggers.py` |

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
| 记忆冲突待确认闭环（召回+图谱候选 → 语义裁决 → 用户确认 → 正式写入） | `memory/conflict_confirmation.py` | `memory/conflict_candidates.py`、`memory/conflict_adjudication.py`、`worker/handlers_conflict.py`、`memory/memory_write_service.py`（`conflict_plan` / `decision`）、`runtime/turn_execution.py`（回复入队）、`api/routes_memories.py`（`GET /memories/conflicts`） | `test_memory_conflict_confirmation.py`、`test_memory_conflict_adjudication.py` |
| 图谱实体绑定（同一次裁决复用既有实体锚点，避免同实体多节点；同名/同别名多实体保留歧义，绑定可定位到具体事实位置） | `memory/conflict_adjudication.py`（`validate_entity_bindings` / `apply_entity_bindings`） | `memory/conflict_candidates.py`（`EntityCandidate` 收集）、`memory/graph_types.py`（`GraphFact.subject_entity_id` / `object_entity_id`）、`memory/graph_projection.py`（`_resolve_entity` 投影前复核）、`memory/conflict_confirmation.py`（`decide` 回填） | `test_memory_entity_binding.py`（unit + integration） |
| 记忆 key 注册表（唯一真相源） | `memory/memory_key_registry.py` | `prompts/`（MEMORY_KEY_GUIDE） | `test_memory_key*.py` |
| 记忆召回（四路由 + 融合） | `memory/automatic_recall.py`、`memory/recall_fusion.py` | `memory/recall_config.py`、`memory/vector_projection.py`、`retrieval/` | `test_recall*.py` |
| 时序知识图谱（关系事实 → 投影 → 读侧扩展） | `memory/graph_projection.py`、`memory/graph_expansion.py`、`memory/graph_rebuild.py`、`memory/graph_predicates.py`（关系基数/谓词族） | `memory/graph_types.py`、`memory/memory_extractor.py::_verified_graph_facts`、`memory/memory_mutation.py::enqueue_projection`、`runtime/context_assembler.py::_expand_relations`、`worker/outbox_handlers.py::handle_memory_graph_refresh` | `test_memory_graph.py`、`test_context_assembler_graph_expansion.py` |
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
| 能力生命周期（验证/启用/停用/升级/回滚/卸载） | `capability/lifecycle.py::CapabilityLifecycleService` L143、`capability/states.py::CapabilityState` L22 | `mcp/bootstrap.py::registered_tool_ids` L208 / `unregister_capability_tools` L227、`mcp/installer.py::install_to_dir` L169、`api/routes_capabilities.py::/lifecycle/*` L255-329 | `test_capability_lifecycle.py` |
| 内置 MCP（aiive-essentials） | `mcp/builtin.py`、`mcp/builtin_server.py` | `main.py` lifespan | `test_mcp_builtin*.py` |
| 内置 Skill catalog | `skills/catalog.py` + `skills/builtin/*/SKILL.md` | `api/routes_skills.py`、`task_runtime/tools.py` | `test_skills*.py` |
| 自进化：补丁计划 | `selfdev/planner.py`、`selfdev/trusted_core.py` | `api/routes_selfdev.py` | `test_selfdev*.py` |
| 自进化：自主修复闭环（读码→补丁→测试→有限修复→安全门） | `selfdev/repair_loop.py::SelfDevRepairLoop`、`selfdev/project_reader.py::ProjectReader` | `api/routes_selfdev.py::/repair`、`tools/builtin_selfdev.py::handle_run_selfdev_repair`、`eval/gate.py::evaluate_promotion_gate`、`supervisor/release_manager.py::promote` | `test_selfdev_repair.py` |
| 自进化：修复并发锁 / 候选隔离 / 中断恢复 | `selfdev/repair_guard.py`（`RepairLock` owner-token 锁、`CandidateSlotGuard` 基线+指纹+恢复+不可晋升标记、`RepairJournal` 阶段日志、`recover_interrupted`）、`supervisor/slot_manager.py`（`tree_fingerprint`/`sync_app_tree`/`candidate_state.json` 读写） | `selfdev/repair_loop.py::run`（取锁→恢复→闭环，失败回滚）、`supervisor/release_manager.py::_candidate_block`（晋升前核对不可晋升标记与指纹） | `test_selfdev_repair_guard.py` |
| 自进化：槽位 promote/rollback | `supervisor/release_manager.py`（生产晋升入口）、`selfdev/promote_rollback.py`、`selfdev/patch_executor.py` | `supervisor/slot_manager.py` | `test_release_manager.py`、`test_selfdev_slot*.py` |
| 自进化：进程托管（真实启停/健康/回滚） | `supervisor/daemon.py`（`Supervisor` 守护循环）、`supervisor/process_control.py`（`ProcessController` 启停与所有权）、`supervisor/health_http.py`（HTTP 探针）、`supervisor/control.py`（请求/事件/锁）、`supervisor/client.py`（应用侧客户端） | `api/routes_selfdev.py::supervisor_status`、`release_manager.py::promote`（`client` 注入）、`worker/scheduler_daemon.py::_release_monitor_job` | `test_supervisor_lifecycle.py` |
| 自进化：运行证据驱动的改进候选发现（调度器→Outbox→候选→用户接受） | `selfdev/improvement_evidence.py`（`EvidenceCollector.collect`：任务/Action/死信/修复失败/Eval 失败 → 按能力+错误签名聚合，一次性故障丢弃）、`selfdev/improvement_discovery.py`（`ImprovementDiscovery.run`：水位游标→单次 LLM→服务端校验→`uuid5` 去重落 `selfdev_requests`；`accept`/`reject`） | `worker/handlers_improvement.py::handle_improvement_discovery`、`worker/scheduler_daemon.py::_improvement_discovery_scanner_job`（6h 窗口幂等入队）、`api/routes_selfdev.py::/candidates*`（accept 需 trusted core token） | `tests/unit/backend/test_improvement_{evidence,discovery}.py`、`tests/integration/backend/test_improvement_discovery_flow.py` |
| 自进化：安全闸门（Replay Eval + Kernel Contract Checks） | `eval/`（`contracts.py`/`replay.py`/`gate.py`/`scenarios.py`/`persistence.py`/`cli.py`/`models.py`） | `supervisor/release_manager.py::promote` 与 `selfdev/promote_rollback.py::promote`（共用 `eval/gate.py::evaluate_promotion_gate`，fail-closed）、`api/routes_eval.py`（只读查询 `/api/eval/runs`、`/runs/latest`） | `tests/unit/backend/test_eval_*.py`、`test_release_manager.py` |

### 2.5 任务 / 桌面 / 知识库 / 通知

| 功能 | 核心文件 | 关联文件 | 测试 |
|---|---|---|---|
| 持久 Agent 任务主循环 | `task_runtime/runtime.py` | `task_runtime/repository.py`、`worker/handlers_agent_tasks.py` | `tests/unit/backend/test_agent_task*.py` |
| 任务 API + WS 事件 | `api/routes_agent_tasks.py` | `task_runtime/repository.py`（commit 后广播） | 同上 |
| 任务能力边界（Broker） | `control/capability_broker.py`、`control/scope.py`、`control/task_policy.py` | `tools/registry.py::execute_brokered`、`task_runtime/resource_locks.py` | `test_capability_broker*.py` |
| 任务 Watcher / 资源锁 / 证据 | `task_runtime/watchers.py`、`task_runtime/resource_locks.py`、`task_runtime/evidence.py` | `worker/scheduler_daemon.py::_agent_task_scanner_job` | `test_task_watch*.py` |
| 长期目标（Goal） | `task_runtime/goal_plan.py`、`task_runtime/goal_service.py` | `task_runtime/runtime.py`（执行钩子）、`task_runtime/watchers.py`（`goal_step_id`）、`api/routes_agent_tasks.py`（`/goal/*`）、`worker/scheduler_daemon.py`（`scan_due_reminders`） | `tests/unit/backend/test_long_term_goal.py` |
| 目标步骤触发规则 + 决策闭环（V50） | `task_runtime/goal_triggers.py`（纯规则求值：once_at/recurring/window_once/event_condition/opportunistic）、`task_runtime/goal_trigger_service.py`（确定性扫描 → Outbox 入队）、`task_runtime/goal_decision.py`（上下文/LLM 结构化决策/服务端校验/执行/Trace） | `task_runtime/goal_plan.py`（`TriggerRule`/`trigger_of`）、`worker/handlers_goal_triggers.py`（`goal_trigger_decision`）、`runtime/proactive_message.py`（真实时间线消息）、`worker/scheduler_daemon.py::_agent_task_scanner_job`、`worker/job_types.py` | `tests/unit/backend/test_goal_triggers.py`（19 例） |
| 提醒 / 条件任务 | `runtime/task_manager.py` | `worker/task_worker.py`、`tools/builtin_tools.py`（schedule_reminder 等） | `test_reminder*.py`、`test_task_manager*.py` |
| 通知 | `api/routes_notifications.py` | `worker/outbox_handlers.py`、`frontend/src/pages/NotificationsPage.tsx` | `test_notification*.py` |
| 桌面节点服务端 | `desktop/node_service.py`、`desktop/connection_manager.py`、`desktop/protocol.py` | `api/routes_desktop.py`、`desktop/registry_overlay.py` | `test_desktop*.py` |
| 桌面节点客户端（Electron） | `apps/desktop/src/node-host/client.mjs` | `apps/desktop/src/node-host/locks.mjs`、`journal.mjs` | `apps/desktop/src/node-host/journal.test.mjs` |
| 知识库摄入 / 分块 / 检索 | `knowledge/ingestor.py`（`search_chunks` 词法+向量 RRF 融合）、`knowledge/chunker.py` | `knowledge/document_reader.py`、`api/routes_knowledge.py`、`tools/builtin_knowledge.py` | `test_ingestor.py`、`test_knowledge*.py` |
| 知识分块向量（派生投影 + 混合召回） | `knowledge/chunk_vectors.py`（`ChunkVectorService.refresh_document/delete_document/search/backfill_missing`） | `knowledge/ingestor.py::_refresh_chunk_vectors`（摄取/重建后刷新、删除后清理）、`knowledge/maintenance.py::_repair_chunk_vectors`（缺失回填 + 孤儿清理）、`memory/embedding_provider.py::embed_batch`、`db/models.py::KnowledgeChunkVector` | `tests/integration/pg/test_pg_vector_rag.py`（真实 PG+TEI） |
| 知识库维护闭环（版本/删除/孤儿清理/向量回填） | `knowledge/maintenance.py`、`knowledge/ingestor.py` | `worker/handlers_knowledge.py`、`worker/scheduler_daemon.py::_knowledge_maintenance_scanner_job`、`api/routes_knowledge.py`、`knowledge/chunk_vectors.py`（`missing_chunk_vectors`/`orphan_chunk_vectors`） | `test_knowledge_maintenance.py` |
| 文档读取（多格式） | `knowledge/document_reader.py` | `tools/builtin_knowledge.py::handle_read_document` | `test_document*.py` |

### 2.6 前端 / 客户端 / 基础设施

| 功能 | 核心文件 | 关联文件 | 测试 |
|---|---|---|---|
| 聊天页（SSE 状态机，最复杂 57KB） | `frontend/src/pages/ChatPage.tsx` | `frontend/src/api/chat.ts`、`frontend/src/components/Markdown.tsx` | — |
| 前端导航 / 主题 / 角标 | `frontend/src/App.tsx` | `frontend/src/hooks/useNotificationSocket.ts`、`frontend/src/lib/base.ts` | — |
| 任务中心页 | `frontend/src/pages/TaskCenterPage.tsx` | `frontend/src/api/agentTasks.ts`、`frontend/src/hooks/useTaskEvents.ts` | — |
| 新增一个前端页面 | `frontend/src/pages/XxxPage.tsx` + `App.tsx`（lazy + Tab） | `frontend/src/api/` 加客户端函数 | — |
| 登录 / 认证（V49） | `frontend/src/hooks/useAuth.tsx`、`frontend/src/pages/LoginPage.tsx` | `frontend/src/api/auth.ts`、`frontend/src/api/client.ts`（统一注入 Bearer、401 触发过期事件）、`backend/aiive/auth/` | `test_auth.py`、`test_auth_frontend_contract.py` |
| 新增受保护接口 | 后端无需额外代码：`auth/guard.py` 默认拦截白名单外全部路径 | 前端一律经 `api/client.ts`，页面与业务模块不得直接 `fetch` | `test_auth_frontend_contract.py` |
| Android 客户端 | `apps/android/src/App.tsx`、`apps/android/src/api/chat.ts` | `apps/android/src/config.ts`（后端地址） | — |
| 新增 DB 表 / 改表 | `backend/aiive/db/models.py` + `backend/alembic/versions/` 新建迁移 | `db/forget_models.py`、`db/retention_models.py`、`alembic/env.py`（需显式导入新模型模块） | `test_db_models*.py` |
| 新增配置项 | `backend/aiive/config.py::Settings` | `.env` / `.env.example` | `test_config*.py` |
| 启动装配 / 生命周期 | `backend/aiive/main.py` | `worker/scheduler_daemon.py::start_daemon`、`mcp/bootstrap.py::restore_active_capabilities` | `test_main*.py` |
| 部署 | `docker-compose.yml`、`backend/Dockerfile`、`frontend/Dockerfile` | `frontend/nginx.conf`、`scripts/start.sh`、`scripts/embedding_model.sh` | — |

---

## 3. 后端模块地图（`backend/aiive/`）

> 每个包给出：职责、核心文件表、关键符号（文件:行）、与其他包的关联。

### 3.1 入口与配置

| 文件 | 职责 |
|---|---|
| `main.py` | FastAPI 装配：`create_app()` L131；`lifespan` L142（启动序列：事件循环绑定→prompt catalog 校验→`_ensure_schema` L87 Alembic upgrade→HMAC 密钥→`_ensure_system_thread` L51→检索 generation→backfill→ContextBudget 校验→向量运行时→MCP 恢复 `restore_active_capabilities`→`start_daemon`→OutboxWorker+Heartbeat+APScheduler）；26 个 `include_router` L322-347；全局认证中间件 `auth_guard` 在 CORS 之前注册（后注册者位于外层，OPTIONS 预检先被 CORS 处理） |
| `config.py` | `Settings` L13（pydantic-settings，`.env` 加载）；`validate_memory_features()` L126 启动校验；单例 `settings` L186；V50 新增 `aiive_goal_trigger_max_decisions_per_day`（默认 12，>0 校验）作为主动决策的每日硬上限。**几乎所有模块 `from aiive.config import settings`** |
| `auth/` | 单用户登录（V49）：`guard.py` 全局 HTTP 中间件 + WS 首帧认证；`security.py` Argon2id / HS256 / 持久签名密钥；`service.py` 初始化、注册、登录、限流。`aiive_auth_enabled` 默认开，非 loopback 禁止关闭 |

### 3.2 `api/` — REST/WS 路由层

公共基建：`ws_manager.py`（`ConnectionManager` L18，按 thread_id 分组，单例 `ws_manager` L100，`broadcast_to_thread_sync` 供后台线程推送）；`developer_security.py`（`require_local_developer` L61 loopback 守卫、`redact_diagnostic_payload` L87 脱敏）。

| 路由文件 | 前缀 / 端点 |
|---|---|
| `routes_chat.py` | `/api/chat` L116、`/chat/system` L136、`/chat/stream` L163（SSE 核心入口） |
| `routes_memories.py` | `/api/memories` CRUD + `/{id}/forget|sleep|archive` + `/{id}/relations`（时序图谱关系，只读）+ `GET /memories/conflicts`（待确认冲突案例，只读）+ `/maintenance/*`（含 `POST /maintenance/graph-rebuild`） |
| `routes_tools.py` | `/api/tools`、`/tool-operations/{id}`、`POST /tools/safe-delete` |
| `routes_agent_tasks.py` | `/api/agent-tasks` 增删查改 + `WS /ws/{task_id}` |
| `routes_approval.py` | `POST /api/approval/respond` |
| `routes_capabilities.py` | `/api/capabilities/plan-from-goal`、`/{plan_id}/activate`；生命周期 `/lifecycle/audit`、`/lifecycle/audit/reconcile`、`/lifecycle/{id}`（status）、`/{id}/verify|enable|disable|upgrade|rollback`、`DELETE /lifecycle/{id}` |
| `routes_mcp.py` / `routes_mcp_install.py` | `/api/mcp/search`、`/capabilities`；`/{name}/install-sandbox`、`/{id}/smoke` |
| `routes_skills.py` | `/api/skills`、`/{skill_id}`（catalog + ready/degraded 对照） |
| `routes_selfdev.py` | `/api/selfdev/slots`、`/slots/health-check`、`/supervisor`、`/plan`、`/repair`、`GET /requests/{id}`、`/{id}/apply-inactive|promote|rollback`（仅 `/repair` 与三个 `/{id}/...` 需 Trusted Core Token） |
| `routes_forget.py` | `/api/forget` + `/{operation_key}/status` |
| `routes_knowledge.py` | `/api/knowledge/ingest`、`/knowledge`、`/knowledge/maintenance`（GET 报告 / POST 清理）、`DELETE /knowledge/{id}`、`/{id}/source`、`/{id}/reindex`、`/search` |
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
| `routes_auth.py` | `GET /api/auth/status`、`POST /api/auth/register`、`POST /api/auth/login`、`GET /api/auth/me`（前三个匿名，其余接口全部要求 Bearer） |

设计约定：路由函数只做"解析请求→调用 runtime/service→转 HTTP/SSE 错误契约"，业务逻辑不下沉到路由。

#### 3.2.1 `auth/` — 单用户登录（V49）

| 文件 | 职责 / 关键符号 |
|---|---|
| `guard.py` | `EXEMPT_PATHS`（仅 `/health`、`/api/auth/{status,register,login}` 匿名）；`auth_guard` 作为 HTTP 中间件在 CORS **之前**注册（后注册者在外层，OPTIONS 预检先被 CORS 处理）；`authenticate_websocket` 在 accept 后 10s 内等首帧 `{"type":"auth","access_token":...}`，失败 `close(1008)`，成功回 `auth_ok` |
| `security.py` | `hash_password`/`verify_password`（Argon2id）、`create_access_token`/`decode_access_token`（HS256，issuer=aiive，强制校验 sub/iat/exp）、`signing_secret`（配置优先，留空则生成一次并落盘 `.data/auth_secret`，重启后旧 Token 仍有效）、`AuthError`（code + status_code） |
| `service.py` | `status`/`register`/`login`/`me`；`auth_owners.singleton` 唯一约束是"只允许一个账号"的最终裁决（并发注册 409）；登录失败按 `(client, username)` 进程内滑动窗口限流（默认 5 次 / 900s，429）；账号不存在时用固定 dummy hash 做等价耗时校验 |
| `routes_auth.py` | `/api/auth/status|register|login|me`；错误统一 `{"detail": code}`，登录失败不区分"密码错"与"账号不存在" |

**边界**：认证只回答"是不是本人"，不替代授权——操作权限与审批仍由 `TaskPolicyEngine` / 工具审批决定（403 由业务层返回）。前端所有请求必须经 `api/client.ts`，页面不得直接 `fetch` 或自行拼 Authorization。

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
| `memory_extractor.py` / `proposal_normalizer.py` / `extraction_policy.py` | `UnifiedMemoryExtractor`（同一次调用产出 `graph_facts`，`_verified_graph_facts` 做原文证据校验）；提案规范化；提取信号枚举 |
| `graph_types.py` | 图谱契约：`GraphFact`/`GraphFactPayload`/`GraphRelationView`/`GraphConfig`；实体与 predicate 规范化、`make_relation_key`、事实时间解析（未知保持 None） |
| `graph_predicates.py` | 谓词基数配置：单值谓词表 + 别名族 + settings 扩展；`is_single_valued`/`predicate_family`/`conflicted_group_keys`（未知谓词默认多值） |
| `graph_projection.py` | `GraphProjectionService.refresh`：按 `(memory_id, record_version)` 幂等投影；candidate 不投影、失效关闭、遗忘物理删除；仅单值谓词 + 记忆层权威取代（`has_authoritative_supersede`）才结束旧关系，版本删事实记为 `retracted`；`_resolve_entity` 优先复用 `graph_facts` 的实体绑定，投影前复核实体存在且 Scope 与记录一致，失败退回按名称解析并把新写法记入 `aliases` |
| `graph_expansion.py` | `GraphExpansionService.expand`：召回种子 + 查询实体匹配 → 有界 BFS（visited/max_hops/batch）→ 来源校验/打分/历史对照/单值多客体冲突标记（`mark_single_valued_conflicts`）；`render_expansion_block` 预算内整条装配（当前/冲突/历史/低置信分组） |
| `graph_rebuild.py` | `GraphRebuildService.rebuild/verify/repair_overclosed`：从 `memory_records.graph_facts` 重建、清理孤儿行、修复旧规则误关（只恢复来源仍有效且仍主张的事实）；CLI `[--verify-only\|--repair-only] [--dry-run]` |
| `conflict_candidates.py` | `ConflictCandidateFinder.discover`：三路候选发现（精确 key+Scope、`UnifiedRetriever` 语义召回、`graph_facts` 反查同主体同谓词族同 Scope 的关系），合并去重 + 回源校验（lifecycle/validity/遗忘屏蔽）+ 剔除 content_hash 相同项；图谱关系只作证据；`_collect_entities` 复用同一批关系证据收集同 Scope 的实体候选（`EntityCandidate`，按精确名/别名/词重叠/包含/关系端点标注依据），别名命中直接产确定性绑定；同名/同别名命中多个实体时（`_find_entities` 返回 >1）不按顺序挑一个，改为保留待绑定并记 `entity_name_ambiguous` 交给模型判断 |
| `conflict_adjudication.py` | `SemanticConflictAdjudicator`（LLM 六类结论：compatible/complementary/conditional/temporal_change/contradiction/uncertain）、`adjudication_to_plan`（确定性自动处理 vs 转确认）、`ConflictQuestionGenerator`、`ConflictReplyParser`（9 类决策）；同一次调用附带 `entity_bindings`（reuse/create_new/unresolved），`validate_entity_bindings` 校验 ID 必须来自本次候选集合、名称必须在待绑定集合、证据 ID 必须来自本次证据，`apply_entity_bindings` 只回填已通过的 reuse；绑定目标支持名称级（作用于该名称全部出现）与事实级（`fact_index` + `role` 只作用于某条事实的某个位置），同一目标出现矛盾结论时整条作废（`conflicting_bindings`），与数组顺序无关；提示词内联构造 |
| `conflict_confirmation.py` | `MemoryConflictService`：`collect_candidates`/`decide`（两阶段，LLM 在事务外）、`park`（建案例+入队问题任务，幂等）、`publish_question`（通知）、`enqueue_reply_jobs`（回合入口入队）、`apply_decision`（版本/过期复核后经 `MemoryWriteService`/`Forget` 落库） |
| `memory_policy.py` / `memory_access_tracker.py` | 敏感度/读取/authority 策略；访问 touch（recency 去抖） |
| `projection.py` / `vector_projection.py` / `vector_bootstrap.py` / `embedding_provider.py` | markdown/json 落盘投影（可丢失）；pgvector 投影+fail-closed 校验；provider 工厂与运行时校验；`embed_batch` 批量推理（`aiive_embedding_batch_size` 控制并发/内存，单条 `embed` 载荷保持字符串不变） |

**其他记忆相关包**：`retrieval/`（lexical 检索索引代次：`retrieval_index_generations/_entries/_tokens/_runs` 表）；`forget/`（`fingerprint.py` 内容指纹、`phase_a_shield.py` 防再摄取盾牌、`selector_normalizer.py`、`visibility_service.py`、`reconcile_service.py`）；`retention/`（`engine.py` RetentionEngine、`config.py`、`maintenance_lease.py`、`safety_predicates.py`、`vacuum.py`）。

**记忆数据流**：
- 写入：`tools/remember_or_update` / chat 收尾入队 `memory_extraction` → `worker/outbox_handlers.handle_memory_extraction` → `MemoryWriteService.write_batch` → conflict_resolver → memory_store + lineage + 投影入队（vector/markdown/core_memory refresh 均为独立 outbox job）
- 召回：每轮 `ContextAssembler._unified_recall` → `AutomaticRecallEngine`（L0 精确/L1 core/L2 四路由/L3 recent）→ `recall_fusion` → `MemoryRecallPack` 注入上下文
- 图谱写入：`memory_records.graph_facts` → `MemoryMutationExecutor.enqueue_projection` 入队 `memory_graph_refresh` → `handle_memory_graph_refresh` → `GraphProjectionService.refresh` → `graph_entities`/`graph_relations`
- 图谱读取：`ContextAssembler._load_agent_context` 在 `UnifiedRetriever` 之后调用 `_expand_relations` → `GraphExpansionService.expand` → 与召回共享 `retrieved_memory` 动态预算，注入“时序关系（图谱检索证据）”块
- 遗忘：`forget` 工具 → `MemoryWriteService.forget` 起 Saga → `worker/handlers_forget` 五阶段（cascade→rebuild→purge→verify→reconcile），tombstone+shield 保证 fail-closed
- 冲突待确认：`handle_memory_extraction` Phase B2 → `collect_candidates`（短只读会话）+ `decide`（无会话 LLM 裁决）→ 需确认时 `write_batch` 只写 `memory_conflict_cases` + `memory_conflict_question` job（不写记忆/图谱）→ 用户回复 → `memory_conflict_reply` → `apply_decision` → `MemoryWriteService.write(decision=...)` / `forget`

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

**38 个内置工具清单**（注册于 `builtin_tools.py::register_builtin_tools` L141 起；handler 实现分布于上表 `builtin_*.py` 领域模块，经 `builtin_tools.py` 末尾 re-export 保持导入契约不变）：
基础：`echo`、`get_current_time`；本机 Shell：`run_shell`；提醒/任务：`schedule_reminder`、`remind_alert`、`confirm_reminder`、`snooze_reminder`、`list_tasks`、`cancel_task`、`show_notifications`、`dismiss_notifications`；记忆写入：`remember_or_update`、`forget`、`forget_status`、`run_memory_maintenance`；记忆召回：`memory_search`、`memory_timeline`、`memory_event_log`、`history_search`；文件：`safe_delete`、`read_text_file`、`read_document`；知识库：`ingest_document`、`reindex_document`、`search_knowledge`；联网：`web_search`、`fetch_web_page`；MCP：`search_mcp`、`install_mcp_sandbox`、`plan_capability`；自进化：`create_selfdev_plan`、`apply_patch_to_inactive_slot`、`promote_slot`、`rollback_slot`、`run_selfdev_repair`；节奏/注意力：`query_rhythm`、`query_attention`；WorkingState：`update_working_state`。
动态补充：桌面节点 14 个 `desktop_*` 能力（`desktop/protocol.py::DESKTOP_CAPABILITIES`）+ MCP 桥接工具（`mcp/bootstrap.py`）。

### 3.7 `skills/` 与 `mcp/` 与 `selfdev/`

- `skills/catalog.py`：`SkillDefinition` L10（`as_dict(include_instructions=False)` L23）；`_DEFINITIONS` L35-92（4 个：document_processing/web_research/knowledge_base/mcp_management）；`list_skills()` L100、`get_skill()` L108；指令文本在 `skills/builtin/<id>/SKILL.md`。被 `task_runtime/tools.py:43`、`task_runtime/context_assembler.py:111`、`api/routes_skills.py` 使用。
- `mcp/`：`discovery.py`（`_BUILTIN_CATALOG` L61-110、`search_mcp_candidates` L113、`allowed_npm_packages` L193）；`capability_planner.py`（`plan_from_goal` L118、`_compute_risk` L170）；`installer.py`（`SANDBOX_ROOT` L39=`<repo>/.data/mcp_sandbox`、`install_to_dir` L169（装到指定/版本化目录，供升级）、`install_sandbox` L253、`run_smoke` L375（`auto_activate=False` 只到 verified））；`runtime_client.py`（`MCPRuntimeClient` L361、每能力长驻 `_SessionWorker`、`get_runtime_client` L534）；`bootstrap.py`（`register_capability_tools` L121 untrusted 包装、`capability_tool_ids` L195、`registered_tool_ids` L208、`unregister_capability_tools` L227、`activate_capability_tools` L243、`restore_active_capabilities` L283 启动恢复）；`builtin.py`（`ensure_builtin_mcp_capability` L83、`BUILTIN_MCP_TOOLS` L17-57）；`builtin_server.py`（FastMCP stdio server，3 工具）。
- `capability/`：`states.py`（`CapabilityState` L22、`LIFECYCLE_TRANSITIONS`、`ensure_transition`——非法转换抛 `InvalidCapabilityTransition`）；`lifecycle.py`（`CapabilityLifecycleService` L143：`status/verify/probe/enable/disable/upgrade/rollback/uninstall/audit/reconcile`，进程内 `_capability_lock` L75 + DB `FOR UPDATE`，生命周期元数据写 `definition["lifecycle"]`，历史有界 20 条）。
- `selfdev/`：`trusted_core.py`（`TRUSTED_CORE_PREFIXES` L9-23，13 条受保护路径，fail-closed）；`planner.py`（LLM 生成 patch plan+逐条校验；`plan(..., context=, previous_plan=, failure_feedback=)` 复用同一提示词注入只读代码与真实测试错误）；`project_reader.py`（`ProjectReader`：仓库范围内只读、有界检索/读取，拒绝密钥与 `.data/runtime/slots`；`format_context_for_prompt`）；`patch_executor.py`（`apply_to_inactive`：复制活跃槽位基线→应用）；`targeted_test_runner.py`（按变更文件前缀匹配测试，frontend 变更跑 `npm run build`）；`repair_loop.py`（`SelfDevRepairLoop`：读码→规划→应用→定向测试→有限修复→安全门→可选晋升；`patch_fingerprint` 去重、`persist_repair_run` 落库；`run` 先取 `RepairLock`（占用时返回 `repair_busy`）→ `recover_interrupted` 恢复上次中断 → 闭环，失败走 `_abort` 回滚候选槽位）；`repair_guard.py`（`RepairLock`：`O_EXCL` + owner token，只有持有者能释放、持有进程存活时绝不抢占、进程死亡后可接管；`CandidateSlotGuard`：`capture_baseline` 记活跃槽位版本+指纹、`mark_not_promotable`/`mark_promotable` 写 `slots/<X>/candidate_state.json`、`restore` 整树回滚并复核指纹；`RepairJournal`：`runtime/selfdev/runs/<run_id>.json` 阶段快照（随槽位 runtime 目录，无基线时不写）+ 7 天终态清理；`recover_interrupted`：跳过存活持有者，按日志回滚/隔离，状态只写 `interrupted*`）；`promote_rollback.py`（health check + 安全门后切活跃槽位指针；与 `release_manager.promote` 共用 `evaluate_promotion_gate`，无生产调用方）；`improvement_evidence.py`（`EvidenceCollector`：只读采集任务/Action/死信/修复失败/Eval 失败，`normalize_signature` 归一错误签名后聚合；单次出现、单次瞬态故障与超量组被确定性丢弃，`is_transient` 识别超时/连接重置/限流等）；`improvement_discovery.py`（`ImprovementDiscovery.run`：事件游标→无新证据不调模型→一次调用产出候选→`validate` 校验证据 ID/次数/验收标准→`uuid5(dedupe_key)` 幂等落 `selfdev_requests`（`candidate_pending/accepted/rejected`）；`accept` 复用 `SelfDevRepairLoop` 且固定 `promote=False`；`_ensure_system_thread` 保证进度事件可落）。**复用 `selfdev_requests`，不新增表。**

### 3.8 `worker/` — Outbox 事务队列 + 调度守护

| 文件 | 职责 / 关键符号 |
|---|---|
| `outbox_worker.py` | `OutboxWorker` L47：`claim_one` L69（skip_locked+双重 lease+claim_token）、`enqueue` L152（allowlist 强校验）、`poll` L182、`_dispatch_one` L218；终态 `_finalize_job` L366 / 重试 `_retry_later` L405 / 死信 L519 / forget 阶段死信 L677 |
| `handler_registry.py` | `HandlerRegistry` L10：job_type→handler + schema_version 白名单 |
| `outbox_dto.py` | `HandlerOutcome` 枚举 L16（COMPLETED/RETRY_LATER/NON_RETRYABLE/RETRYABLE_ERROR/CLAIM_LOST/CONTINUE）；`ClaimedJob`/`HandlerResult`/`ActiveClaim`；`FencingViolationError` L102 |
| `outbox_handlers.py`（100KB） | 装配 `register_all` L1398（26 类 job_type，schema_version=1；含 `memory_conflict_question` / `memory_conflict_reply` / `memory_conflict_cleanup` / `improvement_discovery` / `goal_trigger_decision`→`worker/handlers_goal_triggers.py`）；`handle_memory_extraction` L106、`handle_reminder_delivery` L350、`handle_core_memory_refresh` L617、`handle_memory_vector_refresh` L639、`handle_memory_markdown_project` L664、`handle_retrieval_index_refresh` L697、`handle_memory_maintenance` L1165、`handle_segment_sealing` L1622、`handle_epoch_rollover` L1961、`handle_epoch_checkpoint` L2055 |
| `handlers_forget.py`（71KB） | 遗忘 Saga：`handle_forget_cascade` L133（目标物化 L239/墓碑 L435/依赖发现 L619）、`handle_forget_rebuild` L807、`handle_forget_purge` L1356（`_scrub_wide` L1535）、`handle_forget_verify` L1677、`handle_forget_reconcile` L1801；`BATCH_SIZE=200` |
| `handlers_retention.py` / `handlers_agent_tasks.py` | `handle_retention_cleanup` L27 分批驱动 RetentionEngine；`handle_agent_task_run` L16 驱动 PersistentTaskRuntime |
| `handlers_knowledge.py` | `handle_knowledge_maintenance` L29：扫描→分批清理→commit，无进展时 COMPLETED(partial) 而非空转；`MAX_MAINTENANCE_BATCHES=5` |
| `handlers_improvement.py` | `handle_improvement_discovery`：改进候选发现的 Outbox 入口（`discovery` 参数仅供测试注入）；模型调用在事务外，`llm_failed` 返回 RETRYABLE_ERROR 且不推进水位，非法输出记 COMPLETED 不重试 |
| `handlers_conflict.py` | 冲突闭环两个 Handler，均按"短事务读快照 → 事务外 LLM → 短事务落库"：`handle_memory_conflict_question`（生成澄清问题并写 `notification_created`，状态 pending→asking）、`handle_memory_conflict_reply`（解析回复后调 `MemoryConflictService.apply_decision`） |
| `scheduler_daemon.py`（27KB） | 模块级 `scheduler` L33；`start_daemon` L90（10 个周期 job；含 `improvement_discovery_scanner` 6h 窗口）；`schedule_outbox_poll` L149；`_poll_tool_operations` L193（独立线程防饿死）；`_agent_task_scanner_job` L216（stale run 恢复/锁过期/watcher/审批过期；V50 追加 `GoalService.scan_due_reminders()` + `GoalTriggerService(db).scan()` 确定性触发扫描）；`_idle_scanner_job` L263；`_forget_reconcile_scanner_job` L423；`_conflict_expiry_scanner_job`（冲突案例 TTL 清扫，`enqueue_conflict_cleanup_job` 按小时窗口幂等入队）；`enqueue_maintenance_job` L453、`enqueue_retention_job` L509、`enqueue_knowledge_maintenance_job`、`knowledge_needs_maintenance`、`_knowledge_maintenance_scanner_job`（在途判定只认 pending/running；DB 侧不一致即时入队 + 每日对象对账；终态任务不阻塞，补跑分配 `#n` 后缀 operation_id） |
| `outbox_heartbeat.py` / `job_types.py` / `task_worker.py` | 租约续期线程 + `ActiveClaimRegistry`；`ENABLED_OUTBOX_JOB_TYPES` allowlist；到期提醒原子入队 `reminder_delivery`（FOR UPDATE SKIP LOCKED） |

### 3.9 `task_runtime/` + `control/` — 持久任务与执行边界

- `task_runtime/runtime.py`：`PersistentTaskRuntime.run` L43、`_dispatch_ready` L262、`_complete` L346、`_budget_issue` L425（LLM 只提案不执行，Broker 统一派发）
- `task_runtime/repository.py`：`TaskRepository` 唯一写入口：`create_task` L121、`transition_task` L185、`append_event` L217、`checkpoint` L265、`enqueue_run` L310、`start_run` L329、`create_action` L400、`ready_action` L481、`cancel_task` L487、`send_input` L526、`detail` L570；`recover_stale_tasks` L660 / `reconcile_queued_tasks` L704（scheduler 调用）；commit 后 WS 广播 L46（SAVEPOINT 水位防幽灵消息）
- `task_runtime/tool_injection.py`：`WORKER_RESIDENT_CAPABILITIES`（L0 常驻清单）、`FULL_INJECTION_MAX_TOOLS=12`、`discovery_state/record_search/record_definition/merge_state`（只存 id + 指纹）、`build_tool_view`（执行视图与发现视图分离，`in_scope` 标注；loaded 按当前注册表重建，`definition_changed`/`unavailable` 不返回旧 Schema）
- `task_runtime/agent_executor.py`（`decide` L31，JSON 修复解析）、`context_assembler.py`（L20，绝不加载会话 raw history；`_bounded_value` 超限降级 sha256+preview；`snapshot` 输出 `tools.*` 而非全量 capabilities）、`tools.py`（`build_main_agent_registry` L190：主 Agent 仅见 delegate/status/cancel/input/list_skills）、`router.py`、`state_machine.py`、`resource_locks.py`（`ResourceLockService` L30，固定顺序获取防死锁）、`evidence.py`（`EvidenceStore` L30 内容寻址）、`watchers.py`（time/file/node 三类，`wake_node_tasks` L95）、`worktree.py`（Git 隔离）、`schemas.py`
- `task_runtime/goal_plan.py`：`GoalPlan`（status/summary/deadline/timezone/acceptance/steps/current_step_id/revision/reminder/missed_streak）、
  `GoalStep`（id/kind/status/depends_on/acceptance/schedule/requires_evidence/evidence_refs/attempts/missed_count/next_trigger_at/approved_at，
  以及 V50 触发字段 trigger/trigger_count/last_trigger_key/last_trigger_at/armed_at/expired_at/cooldown_until/opportunistic_hits/last_message_key）、
  `GoalStepKind`（autonomous/interactive/approval_required）、`GoalStepStatus`（pending/ready/running/waiting_user/waiting_time/done/failed/skipped/missed）、
  `TriggerKind`（once_at/recurring/window_once/event_condition/opportunistic）、`TriggerRule`（at/start/end/frequency/weekday/time/timezone/until/event_types/match/cooldown_hours/max_per_day/max_per_week/evaluate_on_step_done，`validate_shape` 逐 kind 校验）、
  `timezone_for`、`next_occurrence`、`trigger_from_schedule`/`trigger_of`（旧 `schedule` 归一化为 TriggerRule，向后兼容）、`next_time_trigger`（唯一的日历排期实现，旧 `next_occurrence` 委托给它）、`dependencies_met`、`next_runnable_step`（READY 优先；interactive 与显式 trigger 步骤都不按时间自动起跑，后者只由触发扫描器置 READY）、`upcoming_steps`、`plan_complete`、`summarize`。
- `task_runtime/goal_service.py`：`GoalService` 唯一目标写入口：`attach_plan`（提案，落 task_state["goal"]+checkpoint）、
  `confirm/reject/pause/resume/end/modify`、`start_step`（交互步骤用户开始）、`approve_step`（approval_required 审批）、
  `on_run_started`（无步骤返回 False，运行时不调 LLM）、`on_step_finished`（证据校验/推进/完成）、`on_step_awaiting_user`、
  `on_watch_due`（到期结算 + 下一场排期）、`schedule_pending_watches`/`cancel_watches`、`scan_due_reminders`（沉默提醒，确定性扫描）、
  V50 触发入口：`save_plan`（决策改计划后回写）、`begin_step_from_trigger`（规则触发起跑：校验 ACTIVE/依赖/审批 → ready → queued + `enqueue_run` + checkpoint `goal_step_triggered`）、
  `mark_step_waiting_user`（LLM 判定需用户输入时只阻塞该步，要求目标 ACTIVE）、`complete_goal`（校验 `plan_complete` + 证据，且任务必须处于 BLOCKED_*/QUEUED，再走完状态机到 SUCCEEDED）。
  `modify` 的 `_copy_step_state` 会保留全部 V50 触发状态（trigger_count/last_trigger_key/armed_at/cooldown_until/opportunistic_hits 等），避免改计划后重复触发。
  `modify` 区分执行内容（title/kind/acceptance/depends_on/requires_evidence）与调度时间：内容变化清空状态/证据/审批并递归失效下游（`dependency_changed:<dep>`），仅改时间保留已完成步骤；运行中拒绝（`goal_running`）。
  `approve_step`/`start_step`/`on_run_started` 均校验依赖（`step_dependencies_unmet`），审批不绕过计划顺序；`approve_step` 写 `goal_step_approved` checkpoint。
- `task_runtime/goal_triggers.py`（V50，纯函数、无 DB）：`TriggerEvent`（thread 事件 / 步骤完成事件）、`TriggerFiring`（`key`/`reason`/`fired_at`/`next_trigger_at`）、`arm_rule`（首次装载 armed_at）、
  `match_event`（事件类型 + payload 子集匹配）、`_within_caps`（日/周频率上限）、`_latest_occurrence`（时区感知的最近一次日历落点）、`evaluate_rule`（返回**稳定幂等键**：`once_at:<iso>`、`recurring:<iso>`、`window_once:<start>:<event_id>`、`event_condition:<event_id>`、`opportunistic:<event_id>`）、`expiry_of`（window_once 到期未满足）。
  所有触发都要求「有真实事件或已定义评估时机」，绝不做无条件周期 LLM 调用。
- `task_runtime/goal_trigger_service.py`（V50）：`GoalTriggerService.scan(now, limit)` 在 scheduler 里确定性扫描 ACTIVE 目标（QUEUED/BLOCKED_*，跳过运行中）；逐步骤 `trigger_of` → 已完成/运行中/依赖未满足跳过 → `arm_rule` → `expiry_of`（MISSED + `expired_at`）→ `evaluate_rule` → `_fire`：写 `goal_trigger_fired` AgentTaskEvent（`dedupe_key=trigger:<operation_id>`）+ 入队 `OutboxJob(job_type=goal_trigger_decision, operation_id=goal_trigger:<task>:<step>:<sha1(key)[:16]>, trace_id=uuid5(operation_id))`。事件来源：线程 `Event`（排除自触发类型）与 `goal_step_completed` 任务事件。
- `task_runtime/goal_decision.py`（V50）：`GoalDecisionEngine.context`（目标/步骤/依赖/触发原因/证据/相关记忆/近期对话/当前时间）→ `messages`（system + user，system 只进 Trace 不进聊天）→ `decide`（复用 `default_llm_client`，json_mode）→ `parse`（json_repair 兜底）→ `validate`（服务端复核 action/步骤状态/依赖/审批/证据，LLM 不能绕过）→ `apply`（先校验 `step.last_trigger_key`，过期决策返回 `stale_trigger`，再分发到 `GoalService`；主动消息在同一事务内写入，被拒不发消息、写失败整体回滚）。`_record` 写 `goal_decision` 事件（dedupe_key 幂等），`write_trace` 写 `ContextSnapshot`（含完整 system/历史/触发事件）+ `LLMCall` + `goal_trigger_decision` 事件。`decisions_today` 供每日硬上限。
- `runtime/proactive_message.py`（V50）：`ProactiveMessageService.post` 把主动消息写成 `TurnRecord(source=runtime_event, status=completed, turn_sequence=peek_next_turn_sequence)` + `Event(llm_response)`，与用户消息共用唯一时间线；检测到同线程有活跃租约的 running Turn 时抛 `ProactiveBlocked`，由 Outbox 重试，避免并发乱序。
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

- `knowledge/`：
  - `ingestor.py`：`KnowledgeIngestor`（`ingest` 版本感知去重、`delete` 软删除、`reindex`、`read_source`）、`search_chunks`（ILIKE 多词覆盖度排序，**只检索活跃状态文档**）、状态常量 `ACTIVE_STATUSES`/`RECOVERABLE_STATUSES`/`INACTIVE_STATUSES`。写序固定"先置失效旧版本并 flush，再写入/激活新版本"，配合 `documents` 的部分唯一索引 `ix_documents_active_source_path`（活跃三态，根治并发导入同路径竞态；迁移 `d1e2f3a4b5c6`）。
  - `maintenance.py`：`scan_knowledge_base`（7 类一致性问题）、`apply_report`（幂等清理：失效分块/孤儿分块/重复分块/缺失分块重建/对象丢失标记/过期 superseded 对象/孤儿对象）；`SUPERSEDED_OBJECT_RETENTION_DAYS=30`。
  - `chunker.py`、`document_reader.py`（TXT/MD/HTML/JSON/CSV/TSV/DOCX/PDF）、`access.py`（访问控制）。
  - 设计说明见 `docs/architecture/knowledge_维护.md`。
- `supervisor/`：
  - `slot_manager.py`：A/B 槽位与 `runtime/active_slot` 原子写（manifest excludes 含 data/postgres/object_store/logs/.env/slots/runtime）；另有 `tree_fingerprint(app)`（相对路径+内容 SHA-256，忽略 `__pycache__`/构建产物，返回 None 表示"无法验证"）、`sync_app_tree`（整树同步，apply 与回滚共用）、`read/write/clear_candidate_state`（`slots/<X>/candidate_state.json`：`promotable`/`fingerprint`/`run_id`/`reason`）。
  - `health_probe.py`：槽位静态健康（manifest、app 目录、槽位内 `import aiive.main`）；`launcher.py`：槽位状态概览。
  - `release_manager.py`：**生产晋升入口**，先静态健康 + 安全门 `evaluate_promotion_gate`，再决定"自己切指针"还是"交给 Supervisor"（`client` 注入；未启用时保持指针切换 + 观察窗回滚）。
  - `daemon.py`：`Supervisor` 守护循环（recover/tick/run_forever），负责暂存端口验证、主端口切换、观察窗监测、异常退出重启预算、中断切换恢复；`python -m aiive.supervisor.daemon` 为独立进程入口。
  - `process_control.py`：`ProcessController` 启动/停止/存活判定，`processes.json` 是唯一所有权凭据（未登记 pid 拒绝操作）。
  - `health_http.py`：运行中进程的 HTTP 健康探测（不走代理）。
  - `control.py`：控制面文件协议（原子写、事件 JSONL、`supervisor.lock`/`switch.lock`、幂等结果查询）。
  - `client.py`：应用侧客户端（提交请求即返回；切换会重启当前进程，不能同步等待）。
  - 设计说明见 `docs/architecture/supervisor_进程托管.md`。
- `context/run_context.py`：运行上下文；`storage/object_store.py`：对象/工件存储；`web/client.py`：Web 检索 HTTP 客户端（SSRF/重定向/大小限制）
- `prompts/`：LLM prompt 模板（`manifest.toml` + 16 个 md），`main.py::validate_prompt_catalog` 启动校验

### 3.12 `db/` 与 `alembic/`

- `db/base.py`：`engine` L18、`SessionLocal` L27、`get_db()` L30（FastAPI 依赖）、`create_all()` L43
- `db/models.py`（63 表，`Base` L17）、`db/forget_models.py`（10 表）、`db/retention_models.py`（2 表）—— 表清单见第 6 节
- `alembic/env.py`：`target_metadata = Base.metadata` L29，**显式导入 forget/retention 模型** L12-13 防漏表；URL 取自 `settings.database_url`
- 迁移链：baseline `69da20d1e47c`（全量 schema）→ … → HEAD `a7c4e9f1b8d2`（auth_owners，V49），现行链 22 个；`versions/_archive_migrations_20260717/` 为历史归档（已被 baseline squash）
- **新增表流程**：`db/models.py` 加模型 → `alembic/env.py` 确认导入 → `alembic revision --autogenerate` → 检查生成的迁移（尤其索引/约束）→ 应用

---

## 4. 前端地图（`frontend/`）

技术栈：React 19 + TS + Vite 6 + Tailwind 3。**无 react-router**：App 内 Tab state + `history.pushState`；部署 base `/aiive/`（`vite.config.ts`，`AIIVE_BASE_PATH=/` 可覆盖）；`src/lib/base.ts` 导出 `BASE_PATH` 供 API/WS/静态资源拼接。

| 层 | 文件 | 说明 |
|---|---|---|
| 入口 | `src/main.tsx` | createRoot + StrictMode |
| 根组件 | `src/App.tsx`（`AppGate` + `AuthenticatedApp`） | 认证门禁：`loading` 占位、`anonymous` 只渲染 `LoginPage`、登录后回跳 `safeInternalPath` 校验过的原路径；10 页 lazy Tab 导航、通知角标、亮/暗主题、`trace_id` 跨页传递、`/aiive/developer` 路径识别、退出登录 |
| 认证门禁 | `src/hooks/useAuth.tsx`（`AuthProvider`/`useAuth`） | `phase: loading|anonymous|authenticated`、登录/注册/登出、启动时 `GET api/auth/me` 恢复会话、监听 `AUTH_EXPIRED_EVENT` 被动登出 |
| 登录页 | `src/pages/LoginPage.tsx` | 首次使用走注册（`GET api/auth/status` 判 `initialized`），之后走登录；错误码文案映射，密码最短 8 位 |
| API 统一客户端 | `src/api/client.ts` | 唯一允许调用 `fetch` 的地方：注入 `Authorization: Bearer`、JSON 编解码、`status === 401` 时清 Token 并派发 `AUTH_EXPIRED_EVENT` |
| API 客户端 | `src/api/auth.ts` | `getAuthStatus`、`register`、`login`、`getMe`；Token 只经 `client.ts` 存取 |
| 样式 | `src/index.css` | Tailwind + 语义色 CSS 变量（`:root`/`[data-theme=dark]`）+ hljs 代码高亮 token |
| API 客户端 | `src/api/chat.ts` | `getThreadMessages` L64、`sendMessageStream` L124（SSE）、`resetThread` L272、`respondApproval` L303；`ApiError` L89/`StreamIncompleteError` L104 |
| API 客户端 | `src/api/agentTasks.ts` | `listAgentTasks` L88、`getAgentTask` L94、`createAgentTask` L98、`cancelAgentTask` L113、`sendAgentTaskInput` L117、`decideTaskApproval` L125；长期目标：`getAgentTaskGoal`、`controlAgentTaskGoal`（confirm/reject/pause/resume/end/modify）、`approveAgentTaskGoalStep`、`startAgentTaskGoalStep` |
| Hooks | `src/hooks/useNotificationSocket.ts` | `useNotificationCount` L70 / `useNotificationListener` L77（单例 WS `/ws/__global__`，3s 重连） |
| Hooks | `src/hooks/useTaskEvents.ts` | `useTaskEvents` L7（每任务 WS，100ms 去抖 refresh） |
| 组件 | `src/components/Markdown.tsx` | react-markdown + remark-gfm + rehype-highlight（surface/colored 两变体） |

**页面清单**（均 lazy 加载于 `App.tsx`）：

| 页面 | Tab | 后端 API |
|---|---|---|
| `pages/LoginPage.tsx` | —（`/aiive/login`，匿名可访问） | `GET api/auth/status`、`POST api/auth/register`、`POST api/auth/login` |
| `pages/ChatPage.tsx`（57KB，最复杂） | chat | `POST api/chat/stream`(SSE)、`GET api/threads/{id}/messages`、`POST api/thread/reset`、`POST api/approval/respond`、`POST api/chat/system`；WS `/ws/{threadId}`。含：SSE 打字机、流式纯文本/完成 Markdown 切换、断流恢复、工具状态机 `normalizeToolCallStatus`、`ToolCallCard` L133、reminder/approval/maintenance_report 卡片、`inFlightRef` 防重发、AbortController、离底未读计数、历史分页 |
| `pages/TaskCenterPage.tsx` | tasks | agent-tasks 全套 + `evidence/{id}`、`artifacts/{id}`；长期目标面板：`GET {id}/goal` + `POST {id}/goal/{confirm,reject,pause,resume,end}`、`steps/{sid}/{start,approve}`；WS `api/agent-tasks/ws/{id}`；4s 轮询兜底 |
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
`src/main.tsx` → `src/App.tsx`（登录门禁 + 消息列表/输入/主题/流式/审批卡，localStorage 最近 200 条）；`src/api/chat.ts`（`streamMessage` SSE 事件 started/token/done/error、`resetConversation`、`respondApproval`）；`src/api/auth.ts`（status/register/login/me + 统一 `authFetch` 注入 Bearer，401 清 Token 回登录页）；`src/LoginScreen.tsx`（首次注册 / 日常登录）；`src/config.ts`（后端地址 `VITE_AIIVE_API_BASE_URL`，默认空防误连）；`capacitor.config.ts`（appId `com.aiive.companion`，cleartext 允许 http）；`android/app/src/main/java/com/aiive/companion/MainActivity.java`（Capacitor 壳）。Token 存 Capacitor Preferences（键 `aiive_access_token`），不再匿名访问。

### 5.2 `apps/desktop/` — Electron + 本地执行节点
双重身份：(a) BrowserWindow 壳（`webUrl = {backend}/aiive/`）；(b) utilityProcess 子进程 Node Host，把本机 14 个能力以 WS 长连接注册给后端。
`src/main/index.mjs`（窗口/托盘/开机自启 + fork node-host，崩溃指数退避 1s→30s；`aiive-desktop:set-auth-token` IPC 把用户 Token 经 `postMessage` 下发给 Node Host，只驻留内存）；`src/preload/index.cjs`（contextBridge `getRuntimeInfo` + `setAuthToken`）；`src/node-host/client.mjs`（27KB 核心：声明能力、连 `ws://{backend}/ws/desktop/{nodeId}` protocol v2、hello 里带 `access_token`、收 `operation` 指令本地执行、回 ack/result；无 Token 时不连接、登出即停连；hello/heartbeat/operation.ack/operation.result/journal.snapshot）；`src/node-host/journal.mjs`（`~/.aiive/action-journal.json` + InFlightActionCoordinator 合并同 Action）；`src/node-host/locks.mjs`（`assertTaskScope`/`assertResolvedPathScope` realpath 防逃逸/租约）；`src/node-host/journal.test.mjs`（`node --test`）。双重凭据：节点令牌 `AIIVE_DESKTOP_NODE_AUTH_TOKEN`（证明节点）+ 用户 `access_token`（证明本人）。

---

## 6. 数据库表地图（75 表）

> 模型定义：`db/models.py`（63）+ `db/forget_models.py`（10）+ `db/retention_models.py`（2）。迁移：baseline `69da20d1e47c`，HEAD `a7c4e9f1b8d2`。

| 域 | 表 | 说明 |
|---|---|---|
| 会话基础 | `threads`、`events`、`llm_calls`、`context_snapshots` | 线程/事件日志/LLM 调用/上下文快照 |
| 回合执行 | `turn_records` | 单回合状态机（租约/指纹/响应） |
| 上下文压缩 | `epochs`、`segments`、`segment_summaries`、`epoch_checkpoints`、`working_states`、`compaction_inputs`、`compaction_runs`、`epoch_compaction_inputs`、`checkpoint_runs` | Epoch/Segment/摘要/检查点/工作状态/压缩流水线 |
| 记忆 V2 | `memory_records`、`memory_proposals`、`memory_ingestion_runs`、`memory_evidence`、`memory_lineage` | 记忆主档/提案/摄取运行/证据/血缘 |
| 记忆向量 | `memory_vector_projections` | pgvector 512 维投影 |
| 记忆图谱 | `graph_entities`、`graph_relations` | 时序知识图谱派生投影（实体规范化 / 三元组 + 有效期 + 来源记忆版本）；真相源是 `memory_records.graph_facts` |
| 记忆冲突待确认 | `memory_conflict_cases` | 待确认案例：冲突双方 id/版本、提案快照、裁决结论与证据、澄清问题、决策与结果；pending 期间不写记忆、不写关系（状态机 pending/asking/resolved/rejected/stale/expired，`idempotency_key` 唯一） |
| 记忆召回 | `memory_recall_runs`、`memory_recall_candidates`、`core_memory_blocks` | 召回运行/候选/核心记忆块 |
| 记忆维护 | `memory_maintenance_runs`、`_batches`、`_inputs`、`_actions` | 维护 run/batch/input/action |
| 检索索引 | `retrieval_index_generations`、`_entries`、`_tokens`、`_runs` | 索引代次/条目/倒排 token/运行 |
| 知识库 | `documents`、`chunks`、`retrieval_runs`、`retrieval_candidates` | 文档/分块/检索运行/候选 |
| 知识分块向量 | `knowledge_chunk_vectors` | pgvector 512 维派生投影（主键 chunk_id，随分块级联删除；HNSW + vector_cosine_ops；可 TRUNCATE 后由维护任务回填） |
| 遗忘 Saga | `forget_operations`、`forget_selector_manifests`、`forget_shields`、`forget_targets`、`forget_dependencies`、`forget_batches`、`forget_actions`、`forget_tombstones`、`forget_stage_runs`、`content_provenance_refs` | Phase 6A 遗忘全家桶 |
| 保留治理 | `retention_cleanup_runs`、`retention_cleanup_batches` | Phase 6B |
| 能力/MCP | `capabilities`、`capability_versions`（含 `source`/`state`/`sandbox_path`/`definition_snapshot` 生命周期列）、`mcp_install_records`、`capability_plans` | |
| 自进化 | `selfdev_requests`、`patch_operations` | |
| 桌面节点 | `desktop_nodes`、`thread_desktop_bindings`、`desktop_action_receipts` | |
| 审批 | `approval_requests` | 工具调用人工审批 |
| Outbox/工具 | `outbox_jobs`、`tool_operations` | 事务发件箱/工具操作（幂等） |
| 任务/Agent | `tasks`、`attention_states`、`agent_tasks`、`agent_runs`、`agent_actions`、`agent_task_events`、`agent_task_checkpoints`、`task_evidence`、`task_artifacts`、`task_resource_locks`、`agent_task_watches` | 条件任务/注意力/持久 Agent 任务全家桶 |
| 工件 | `artifacts` | 通用工件存储 |
| 认证 | `auth_owners` | 单用户所有者账号（V49）：`singleton` 恒 1 且唯一（整库最多一行）、`username` 唯一、Argon2id `password_hash`、`last_login_at` |

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
              → _expand_relations（召回之后）→ memory/graph_expansion.py::expand
                    → 关系 + 历史对照，与召回共享 retrieved_memory 动态预算
                    → 注入「时序关系（图谱检索证据）」system 消息
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

长期目标（task_type=long_term_goal，无新表）：
```
delegate_task(goal_plan) → goal_service.attach_plan → blocked_user(goal_proposed)
  → POST /api/agent-tasks/{id}/goal/confirm → _advance
      ├ 可执行步骤 → ready → outbox agent_task_run(trigger=goal_confirmed)
      └ 仅未来/交互/待审批 → blocked_user + 通知（不调用 LLM）
AgentTaskWatch(goal_step_id, occurrence) 到期 → WatcherService.poll_due
  → GoalService.on_watch_due → autonomous=ready / interactive=waiting_user(仅通知)
  → 下一场 next_occurrence 建新 watch（幂等键含 occurrence）
POST /goal/modify → 内容变化则清证据+清审批+递归失效下游；仅改时间保留已完成步骤
PersistentTaskRuntime.run → on_run_started / on_step_finished / on_step_awaiting_user
  → _advance：下一步 ready 入队；等待写 wake_at；全部完成且证据齐 → completed
scheduler _agent_task_scanner_job → GoalService.scan_due_reminders（wake_at<=now，确定性）
```

目标步骤触发 → LLM 决策 → 主动消息（V50，全部复用既有 Outbox/Trace/时间线）：
```
scheduler _agent_task_scanner_job (10s) → GoalTriggerService.scan()
  → 逐步骤 trigger_of → 已完成/运行中/依赖未满足 跳过
  → arm_rule（armed_at）/ expiry_of（window_once 到期 → MISSED）
  → evaluate_rule（once_at / recurring 时区 / window_once / event_condition / opportunistic：事件+冷却+日周上限）
  → 命中：AgentTaskEvent(goal_trigger_fired, dedupe_key) + OutboxJob(goal_trigger_decision, operation_id=goal_trigger:<task>:<step>:<sha1(key)>)
worker/handlers_goal_triggers.py::handle_goal_trigger_decision
  → 短事务：读目标/步骤/触发事件；复核 ACTIVE、步骤未完成、last_trigger_key 未过期、任务不忙、当日决策上限
  → GoalDecisionEngine.context/messages → LLM（事务外）→ GoalDecision 结构化 JSON
  → 主动消息：ProactiveMessageService.post（TurnRecord source=runtime_event + Event(llm_response)）；前台 Turn 持租约 → RETRY_LATER
  → GoalDecisionEngine.apply（再次校验 last_trigger_key → stale_trigger）+ write_trace（ContextSnapshot 含完整 system 提示词、LLMCall、goal_trigger_decision）
  → ws_manager.broadcast_to_thread_sync(new_message)
```

### 7.5 记忆冲突待确认闭环（自动写入 ↔ 用户确认）

```
handle_memory_extraction (worker/outbox_handlers.py L106)
  Phase B  UnifiedMemoryExtractor.extract → proposals（含 graph_facts）
  Phase B2 MemoryConflictService.collect_candidates   （短只读会话：精确键 + UnifiedRetriever + graph_relations
                                                       + 同 Scope 实体候选 EntityCandidate / pending_entity_names）
           MemoryConflictService.decide              （无会话：SemanticConflictAdjudicator → adjudication_to_plan
                                                       同一次调用还产出 entity_bindings：reuse / create_new / unresolved
                                                       → validate_entity_bindings → 回填 graph_facts 的
                                                         subject_entity_id / object_entity_id）
  Phase C  MemoryWriteService.write_batch(conflict_plan=...)
             ├ plan=proceed → 原有 Gate/ConflictResolver/写入/投影
             └ plan=defer   → MemoryConflictService.park
                               ├ memory_conflict_cases(pending) + memory_proposals(conflict_pending)
                               └ outbox memory_conflict_question（不写 memory_records / graph_relations）

memory_conflict_question (worker/handlers_conflict.py)
  短事务读快照 → ConflictQuestionGenerator（事务外 LLM）→ publish_question
  → Event(notification_created, status=pending) + 角标广播 → case.status=asking

用户回复（会话入口）
  TurnExecutionService._resolve_and_preempt
  → MemoryConflictService.enqueue_reply_jobs（同 Turn 幂等）→ outbox memory_conflict_reply

memory_conflict_reply (worker/handlers_conflict.py)
  短事务读快照 → ConflictReplyParser（事务外 LLM，9 类决策）
  → MemoryConflictService.apply_decision（复核 status / expires_at / old_record_version）
      ├ keep_old | reject_new   → 只关案例，不写记忆、不改关系
      ├ accept_new | revise | merge → MemoryWriteService.write(decision=..., decision_target_id=old)
      │      → 旧记录 validity=superseded + superseded_by=新记录
      │      → Outbox memory_graph_refresh → GraphProjectionService.refresh
      │        （仅单值谓词 + has_authoritative_supersede 才关闭旧关系并记 valid_to）
      ├ keep_both               → 单值键/单值谓词且无明确条件时继续询问；否则 create
      ├ forget_old              → MemoryWriteService.forget（Phase A shield → 五阶段 Saga，图谱关系清空）
      └ clarify                 → 轮次+1 再入队问题任务（超上限则保留旧记忆并结束）

过期清扫（后台，不依赖用户在场）
  scheduler_daemon._conflict_expiry_scanner_job（15min 轮询，按小时窗口幂等入队）
  → outbox memory_conflict_cleanup → handlers_conflict.handle_memory_conflict_cleanup
      → MemoryConflictService.expire_open_cases（行锁 + 批量 200，超出则 CONTINUE 分页）
        → status=expired / resolution_reason=ttl_expired + 该案例通知置 cancelled
        → 只改案例与通知：不写 memory_records、不写 graph_relations、不触发投影
```

### 7.6 启动装配（`main.py::lifespan`）

```
绑定事件循环 → validate_prompt_catalog → _ensure_schema (Alembic upgrade head)
  → HMAC 密钥 → _ensure_system_thread → _ensure_retrieval_generation
  → ContextBudget 校验 → 向量运行时 (vector_bootstrap)
  → mcp/bootstrap.py::restore_active_capabilities (L235) + ensure_builtin_mcp_capability
  → supervisor start_daemon (scheduler_daemon.py L90, 10 个周期 job)
  → OutboxWorker + OutboxHeartbeat + ws_manager + desktop_connection_manager
```

---

### 7.7 改进候选发现闭环（运行证据 → 候选 → 用户接受）

```
scheduler(_improvement_discovery_scanner_job, 6h)
  → enqueue_improvement_discovery_job: operation_id=improvement_discovery:{policy}:{bucket} 幂等
  → OutboxWorker claim → handle_improvement_discovery
      Phase A 短事务: Event(improvement_discovery_run) 取水位 → EvidenceCollector.collect
                      （任务终态/Action 失败/Outbox 死信/修复失败/Eval 失败 → 按能力+签名聚合）
      Phase B 事务外: 无新证据 → 不调用模型；有证据 → 一次 chat(json_mode) 生成候选
      Phase C 短事务: 服务端校验（证据 ID 属于本次输入、occurrences 不超过证据支撑、验收标准非空）
                      → uuid5(dedupe_key) 写 selfdev_requests(candidate_pending)
                      → notification_created(category=selfdev_improvement) + 运行事件推进水位
  → 用户 GET /api/selfdev/candidates → POST /candidates/{id}/reject（直接拒绝）
                                     → POST /candidates/{id}/accept（需 trusted core token）
                                         → SelfDevRepairLoop(promote=False, run_gate=True)
                                         → persist_repair_run 写修复记录，候选转 candidate_accepted
```

不变量：候选只描述目标、不产生补丁；接受前不触碰候选槽位；任何路径都不自动晋升；
同一批证据重复处理由 uuid5 主键与"待处理候选同组去重"共同保证不新增行。

---

### 7.8 认证链路（V49）

HTTP：

```
前端 api/client.ts 注入 Authorization: Bearer <access_token>
  → auth_guard（白名单外全部路径）
      无/坏 Token → 401 {"detail": "not_authenticated" | "token_expired" | "invalid_token"} + WWW-Authenticate: Bearer
      有效       → request.state.username = sub → 业务路由（权限/审批仍由 TaskPolicyEngine 决定）
  → 前端收到 401：清 Token + 派发 AUTH_EXPIRED_EVENT → useAuth 置 anonymous → 渲染登录页
```

WebSocket（3 个入口：`/ws/{thread_id}`、`/ws/__global__`、`/ws/{task_id}`）：

```
accept → 服务端 10s 内等首帧 {"type":"auth","access_token":...}
  成功 → 回 {"type":"auth_ok","data":{"username"}} → 之后才接受业务消息
  失败/超时/非 auth 首帧 → close(1008)，不发送任何业务数据
```

桌面节点 `/ws/desktop/{node_id}`：hello 帧同时带节点令牌与用户 `access_token`（节点身份 + 用户身份双凭据）；
无用户 Token 时 Node Host 不连接，登出后停止重连。

首次使用与登录：

```
GET  /api/auth/status → {"initialized": false} → 注册页
POST /api/auth/register（仅未初始化时可用；singleton 唯一约束兜住并发，后到者 409）
POST /api/auth/login（失败统一 invalid_credentials；同 (client, username) 超 5 次/15min → 429 too_many_attempts）
```

---

## 8. 测试与脚本地图

### 8.1 测试

三层结构（全部离线，无真实外部 API 调用）：

| 位置 | 层级 | 说明 |
|---|---|---|
| `tests/unit/backend/` | 单元 | 后端单元测试（83 个 `test_*.py`），命名 `test_<被测试模块>.py`；`tests/unit/conftest.py` 经"分发式 SessionLocal"把模块级 DB 绑定重定向到按用例隔离的测试库 |
| `tests/integration/backend/` | 本地集成 | 多组件组合测试（Worker/Handler/调度/检索/生命周期），共享进程级 SQLite + 每用例重建表；`_util.py` 为共享夹具辅助 |
| `tests/flow/` | 关键流程 | 跨模块高价值链路（Turn 执行、提醒闭环），外部边界离线模拟 |
| `tests/conftest.py` | 公共 | 从 `.env` 仅加载 `AIIVE_` 前缀变量；`db_session` 夹具；按目录自动标注层级 marker |
| `tests/support.py` | 公共 | SessionLocal 分发器安装（先于 aiive 模块导入） |
| `apps/desktop/src/node-host/journal.test.mjs` | Node | Node 内置 `node --test`（`npm run test:node-host`） |
| `tests/unit/backend/test_auth.py` | 单元（认证验收） | 23 例：初始化状态、唯一注册、Argon2id 哈希、登录失败不可区分、限流、Token 过期/签名错误、重启后仍有效、全部路由匿名访问矩阵、WS 首帧认证、桌面节点用户级认证 |
| `tests/unit/backend/test_goal_triggers.py` | 单元（V50 触发验收） | 19 例：once_at 幂等/重启、recurring 时区落点与下一场、window_once 首次命中与过期、event_condition、opportunistic 冷却/日上限/无事件不调 LLM、运行中任务跳过、no_action+完整 Trace、run_step 只入队一次、modify_plan 版本递增、complete_goal 证据门槛、ask_user 真实助手消息、聊天不泄露 system 提示词而 Trace 保留、前台 Turn 占用 → RETRY_LATER 后按序补发、用户沉默只阻塞依赖步骤、旧 schedule 归一化、window_once 同窗口不重复触发、显式规则不被时间路径起跑、modify 保留触发状态 |
| `tests/unit/backend/test_auth_frontend_contract.py` | 静态守卫 | 11 例源码扫描：页面禁止裸 `fetch`、WS 必须发认证帧、页面/API 模块不得自管 Token、App 登录门禁与安全回跳、桌面 IPC 只传 Token 不落盘 |

跑测试：
- 默认快速（单元+流程，无需任何外部服务）：`python -m pytest tests/unit tests/flow`
- 本地集成：`python -m pytest tests/integration`
- 完整：`python -m pytest`
- 按层级筛选：`-m unit` / `-m integration` / `-m flow`；按模块 `-k "memory"`

### 8.2 脚本与构建

| 文件 | 用途 |
|---|---|
| `scripts/start.sh` | 本地一键启动：按 `.env` 向量开关 + provider 决定是否拉起 local-embedding profile，模型缺失时先自动下载 |
| `scripts/embedding_model.sh` | 本地 Embedding 模型缓存管理：`status`/`path`/`image-tag`/`ensure`；按 HF 缓存布局检测快照完整性，缺失时用 TEI 容器下载并支持多下载源回退 |
| `scripts/build-app.sh` | 统一客户端构建入口（`list`/`android debug|release`/`desktop debug|release`），分发到 `scripts/app-builders/` |
| `scripts/dump_agent_context.py` | 导出 Agent 上下文（调试） |
| `scripts/prepare_brand_assets.py` | 品牌资源派生 |
| `scripts/smoke/` | 冒烟脚本 |
| `scripts/rag_eval/` | 检索效果评测：`corpus.json` + `queries.json`（27 条人工标注）+ `run_eval.py`（纯关键词/纯向量/混合三模式 Recall@5、MRR@5、延迟） |
| `scripts/memtest/` | 单容器 2 GiB cgroup 实测：`Dockerfile`（postgres+pgvector + TEI + backend 同 cgroup）、`supervisord.conf`、`load_test.py`（真实负载 + cgroup 采样）、`run_memtest.sh` |
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
- 晋升前另有一道**候选状态核对**（`ReleaseManager._candidate_block`，fail-closed）：槽位 `candidate_state.json` 为 `promotable=false`（修复失败/中断/恢复失败）或当前 `app/` 指纹与登记指纹不一致（验证后代码被改动）时直接阻断；无状态文件的槽位保持原有行为。

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
