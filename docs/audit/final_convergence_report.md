# AIive 终局架构收敛审计报告（V33）

> 审计日期：2026-10-22　审计对象：`e:/AIive/AIive` @ `main`（v0.1.0-alpha.2 之后的工作区）
> 基准文档：`docs/plans/final_project_plan_v7.md`（下称 v7）、`docs/plans/versions/project_plan_v33.md`
> 结论一句话：**架构已收敛为一个可运行、可回归、全离线的单用户本地 Agent OS；距 v7 终局形态的差距集中在“刹车系统”（eval / kernel contract）与“资源调度”（model routing / retrieval planner / supervisor 进程托管），而不是核心执行链。**

---

## 0. 执行摘要

| 维度 | 结论 | 证据 |
| --- | --- | --- |
| 模块实现度 | v7 §26 共 77 个具名模块：完整实现 35（45%）、部分实现 18（23%）、缺失 24（31%）。含部分实现口径 69% | §2 逐模块表 |
| 核心执行链 | 完整：Turn → LangGraph 六节点 → 工具 → Outbox → 记忆写入/投影 → 事件/快照 | `runtime/agent_graph.py`、`worker/outbox_worker.py`、`memory/memory_write_service.py` |
| 最大结构性缺口 | 无 eval harness / 无 kernel contract checks / 无 model router / 无 retrieval planner / 无 temporal graph | §2、§10 |
| 测试 | 857 passed / 0 failed；91/91 个文件单独运行通过；ruff 0 error；basedpyright 0 error / 0 warning | §7.1 |
| 测试耗时 | 全量 363.80s → **112.35s**（夹具改造，3.2×）；默认快速集 89.17s | §7.6 |
| 外部依赖 | 测试全部离线：0 处真实网络、0 处 `time.sleep`、0 skip；唯一真实等待是 1 个 30.15s 超时用例 | §7.3、§7.4 |
| 数据层偏离 | 向量库用 pgvector 取代 v7 的 Qdrant；对象存储为本地 FS adapter | §3 |
| A/B 自我修改 | 槽位/promote/rollback 全链路存在，但 launcher 不托管进程，切槽需人工重启才生效 | §8.3 |

---

## 1. 审计方法与可复现命令

审计只读代码 + 实跑测试，不做功能开发。全部数字均为本机实测（Windows 11 / Python 3.12 / SQLite）。

```bash
# 全量（含最慢 30）
AIIVE_MEMORY_VECTOR_ENABLED=false .venv/Scripts/python.exe -m pytest -q --durations=30 --tb=line -ra
# 分层
AIIVE_MEMORY_VECTOR_ENABLED=false .venv/Scripts/python.exe -m pytest tests/unit -q
AIIVE_MEMORY_VECTOR_ENABLED=false .venv/Scripts/python.exe -m pytest tests/integration -q
AIIVE_MEMORY_VECTOR_ENABLED=false .venv/Scripts/python.exe -m pytest tests/flow -q
# 单文件隔离（必须显式指定解释器，否则默认 python 缺依赖会 91/91 全 FAIL）
PYTHON_BIN=.venv/Scripts/python.exe JOBS=4 bash scripts/check_single_file_tests.sh
# 静态检查
.venv/Scripts/python.exe -m ruff check backend/aiive --select F,E9
/e/python/Scripts/basedpyright
```

---

## 2. v7 §26 模块实现状态（逐模块）

状态口径：**✅ 完整**（有独立实现 + 覆盖测试）/ **◐ 部分**（adapter、简化版或职责被其他模块吸收）/ **✗ 缺失**（无实现，或仅有同名表而无服务）。

统计范围：v7 §26 的 core/kernel/memory/knowledge/capabilities/maintenance/eval/runtime 共 **77 个功能模块**；`ui/`（9 页面）与 `api/`（6 个文件）属形态层，单列在 §2.8 与 §4/§5，不计入 77。

### 2.1 core/（21）— ✅9 / ◐4 / ✗8

| v7 模块 | 状态 | 实际落点 | 说明 |
| --- | --- | --- | --- |
| `llm_client.py` | ✅ | `core/llm_client.py` | httpx 直连 OpenAI-compatible，超时/错误归一化；`test_llm_client` 覆盖 |
| `model_router.py` | ✗ | — | 单一 `settings.llm_model`，无按任务/成本/延迟选模 |
| `model_boundary.py` | ✗ | 分散在 `memory/memory_gate.py` + 工具返回值 `untrusted` 包装 | 无统一 Model Boundary Policy 模块（v7 §8.5.1） |
| `context_policy.py` | ◐ | `memory/context_assembly.py` + `runtime/context_budget.py` | 有分区预算，无 v7 §22.5 的统一评分/装配策略 |
| `context_builder.py` | ✅ | `runtime/context_assembler.py` | 强类型快照 + 软阈值 pending seal；2 个集成用例覆盖 |
| `context_router.py` | ✗ | — | 无上下文路由层 |
| `decision_engine.py` | ◐ | `runtime/policy_engine.py` + `control/task_policy.py` | 只做工具批次/动作策略判定，不是通用决策引擎 |
| `retrieval_planner.py` | ✗ | `retrieval/unified_retriever.py` | 统一检索直接执行，无“先规划检索式”环节 |
| `retrieval_reflector.py` | ✗ | — | 无检索反思/二次召回 |
| `memory_extractor.py` | ✅ | `memory/memory_extractor.py` | 规则 + LLM 双通道，proposal normalizer |
| `memory_gate.py` | ✅ | `memory/memory_gate.py` | trust boundary / evidence 规则 |
| `reranker.py` | ✗ | `memory/recall_fusion.py` | 只有融合打分，无 cross-encoder/LLM 重排 |
| `context_compressor.py` | ✅ | `runtime/compaction.py` | 冻结输入 + 清单哈希 + 语义输出校验 |
| `context_packer.py` | ◐ | `runtime/context_budget.py` | 有预算分区，无 `packed_context_items` 持久化 |
| `attention_manager.py` | ✅ | `runtime/attention_manager.py` | 有 `/api/attention` 只读查询与清理 |
| `conflict_resolver.py` | ✅ | `memory/conflict_resolver.py` | supersede / merge / reject |
| `project_reader.py` | ✗ | — | 无项目代码库读取器 |
| `patch_generator.py` | ◐ | `selfdev/planner.py` | 只生成补丁计划，不生成代码补丁 |
| `update_executor.py` | ✅ | `selfdev/patch_executor.py` | 应用到非活跃槽 |
| `self_repair_loop.py` | ✗ | — | 无失败自修复闭环 |
| `agent_loop.py` | ✅ | `runtime/agent_graph.py` | LangGraph 六节点：assistant → policy → validate → tools ⇄ assistant / confirm |

### 2.2 kernel/（18）— ✅10 / ◐6 / ✗2

| v7 模块 | 状态 | 实际落点 | 说明 |
| --- | --- | --- | --- |
| `version_manager.py` | ◐ | `supervisor/slot_manager.py`、`release_manager.py` | 版本状态落在文件（`active_slot` 指针 + release_state.json），不入库 |
| `version_manifest.py` | ✅ | `slot_manager.create_version_manifest()` | 排除 data/postgres/qdrant/object_store/logs/.env |
| `slot_manager.py` | ✅ | `supervisor/slot_manager.py` | A/B 槽 + manifest 校验和 |
| `health_check.py` | ✅ | `supervisor/health_probe.py` | manifest / app_dir / backend_import 三项 |
| `test_runner.py` | ✅ | `selfdev/targeted_test_runner.py` | 映射缺失时 fail-closed |
| `rollback.py` | ✅ | `selfdev/promote_rollback.py` + `release_manager.monitor_once()` | 含不健康自动回滚 |
| `supervisor.py` | ◐ | `supervisor/launcher.py` | **只读状态与健康，不 spawn/不重启进程** |
| `launcher.py` | ◐ | 同上 | 无 `subprocess.Popen` / `os.execv` / systemd 调用 |
| `migration_manager.py` | ◐ | alembic（17 个版本，全部含 downgrade） | 无 A/B 双向兼容校验器 |
| `permission_manager.py` | ✅ | `control/task_policy.py` + `control/approval_service.py` | 策略判定 + 审批令牌哈希 |
| `capability_security.py` | ◐ | `control/capability_broker.py` + 工具 `trust_level` | 无 `capability_security_profiles` 表 |
| `tool_definition_auditor.py` | ✗ | `tools/tool_validation.py`（仅 schema 校验） | v7 §9.4 的工具安全声明审计缺失 |
| `kernel_contract_checks.py` | ✗ | — | 无内核契约检查（v7 §11.4） |
| `delete_guard.py` | ✅ | `tools/safe_delete.py` | 危险路径守卫 |
| `safe_delete.py` | ✅ | `tools/safe_delete.py` | 软删 + 引用清理 |
| `scope_registry.py` | ✅ | `control/scope.py` | `TaskScope` / `path_within` / 资源键 |
| `saga_manager.py` | ◐ | `forget/*` + `worker/handlers_forget.py` | Forget 是事实上的多阶段 saga（shield/cascade/rebuild/purge/verify），但无通用 saga 框架 |
| `outbox_worker.py` | ✅ | `worker/outbox_worker.py` | 心跳、重试、deadletter、幂等 |

### 2.3 memory/（8）— ✅4 / ◐3 / ✗1

| v7 模块 | 状态 | 实际落点 | 说明 |
| --- | --- | --- | --- |
| `temporal_graph_adapter.py` | ✗ | — | 无 `graph_entities` / `graph_relations` 表 |
| `mem0_style_extractor.py` | ◐ | `memory/memory_extractor.py` | 自研提取，非 mem0 适配 |
| `memory_store.py` | ✅ | `memory/memory_store.py` | 唯一真相源 CRUD |
| `memory_write_pipeline.py` | ✅ | `memory/memory_write_service.py` | 事务 + advisory lock + 幂等 |
| `memory_projection.py` | ✅ | `memory/projection.py`、`core_memory_projection.py`、`vector_projection.py` | Markdown / core block / pgvector 三投影 |
| `memory_lineage.py` | ◐ | `db/models.py` 的 `memory_lineage` 表 | 有表，无独立服务层 |
| `memory_score.py` | ◐ | `memory/memory_policy.py` + `memory_maintenance_planner` 打分 | 评分分散在写入与维护两处 |
| `conflict_detector.py` | ✅ | `memory/conflict_resolver.py` | 单基数键冲突检测 |

### 2.4 knowledge/（7）— ✅2 / ◐1 / ✗4

| v7 模块 | 状态 | 实际落点 | 说明 |
| --- | --- | --- | --- |
| `qdrant_retriever.py` | ✗ | pgvector 路径：`memory/vector_projection.py`、`retrieval/*` | **架构偏离**：未用 Qdrant，向量检索走 PG + pgvector |
| `document_ingestor.py` | ✅ | `knowledge/ingestor.py` | 去重 + 原文持久 + 重索引 |
| `chunker.py` | ✅ | `knowledge/chunker.py` | 文档分块 |
| `code_chunker.py` | ✗ | — | 无代码专用分块（AST/符号级） |
| `source_store.py` | ◐ | `documents` 表 + `storage/object_store.py` | 本地 FS adapter，非 bucket 服务 |
| `kb_reranker.py` | ✗ | — | 无知识库重排 |
| `kb_maintenance.py` | ✗ | — | 无知识库维护（失效文档/孤儿 chunk 清理） |

### 2.5 capabilities/（3）— ✅1 / ◐2

| v7 模块 | 状态 | 实际落点 | 说明 |
| --- | --- | --- | --- |
| `registry_manager.py` | ✅ | `capabilities` 表 + `api/routes_capabilities.py` | 注册/版本/激活 |
| `capability_router.py` | ◐ | `control/capability_broker.py` | 路由即“能力准备”，无独立打分路由 |
| `lifecycle_manager.py` | ◐ | 激活 + MCP 冒烟（`mcp/installer.py`） | 无卸载/降级/GC 生命周期 |

### 2.6 maintenance/（12）— ✅6 / ◐2 / ✗4

| v7 模块 | 状态 | 实际落点 | 说明 |
| --- | --- | --- | --- |
| `scheduler.py` | ✅ | `worker/scheduler_daemon.py` | 周期调度 |
| `memory_gc.py` | ◐ | `memory/memory_maintenance.py` | 合并/休眠/清理动作齐全，无独立 GC 周期指标 |
| `memory_consolidator.py` | ◐ | `memory/memory_maintenance_planner.py` | 合并动作在 planner 内 |
| `context_compactor.py` | ✅ | `runtime/compaction.py` | 段压缩 + epoch checkpoint |
| `self_model_rewriter.py` | ✗ | — | 无自我模型重写 |
| `capability_gc.py` | ✗ | — | 无能力回收 |
| `kb_maintenance.py` | ✗ | — | 同 2.4 |
| `event_log_archiver.py` | ✗ | — | 无事件归档（只有 retention 清理） |
| `retention_manager.py` | ✅ | `retention/engine.py` + `retention/vacuum.py` | 保留策略 + 真空 |
| `forget_manager.py` | ✅ | `forget/*`（5 个阶段服务） | 墓碑 + 对账 |
| `maintenance_planner.py` | ✅ | `memory/memory_maintenance_planner.py` | 冻结输入 + 决策哈希 |
| `maintenance_executor.py` | ✅ | `worker/handlers_retention.py`、`worker/outbox_handlers.py` | 经 Outbox 执行 |

### 2.7 eval/（4）— ✗4

| v7 模块 | 状态 | 说明 |
| --- | --- | --- |
| `harness.py` | ✗ | 全仓无 `eval/` 包、无 `eval_runs` / `eval_cases` 表 |
| `metrics.py` | ✗ | 同上 |
| `regression_runner.py` | ✗ | 同上 |
| `cases/` | ✗ | 同上 |

> 影响：v7 §27.7 把“自动化 Replay / Kernel Contract Checks”定义为自我修改的刹车。当前自我修改只有单测级别的 `targeted_test_runner`，**没有端到端回归门禁**，这是本次审计认定的最高优先级缺口。

### 2.8 api/、runtime/、ui/（形态层面）

| v7 | 状态 | 实际 | 说明 |
| --- | --- | --- | --- |
| `api/flask_app.py` 等 | ✅（形态偏离） | FastAPI，`backend/aiive/main.py` + 25 个 `routes_*.py` | 框架替换，职责等价 |
| `runtime/langgraph_app.py` | ✅ | `runtime/agent_graph.py` | LangGraph 原生图 |
| `runtime/thread_state_schema.py` | ✅ | `runtime/thread_state.py` | 线程状态 + 分页游标 |
| `runtime/event_logger.py` | ✅ | `runtime/event_logger.py` | 22 类事件 |
| `runtime/local_event_bus.py` | ✗ | 以 Outbox + WebSocket 取代 | 无进程内事件总线抽象 |
| `ui/react-vite-app/` 9 页面 | ◐ | 9 个页面，命名与 v7 不同 | 见 §5 |

---

## 3. 依赖接入状态：真实接入 / 仅 adapter / 未完成

| 依赖 | 状态 | 证据 |
| --- | --- | --- |
| LLM（OpenAI-compatible） | **真实接入** | `core/llm_client.py` httpx 直连；测试只替换传输层 |
| Embedding（OpenAI-compatible / 本地 TEI） | **真实接入** | `memory/embedding_provider.py`；`test_embedding_provider.py` 替换 httpx |
| 向量检索 | **真实接入（选型偏离）** | pgvector + `memory_vector_projections`；v7 §25.2 要求 Qdrant，实际未使用 |
| PostgreSQL | **真实接入** | 生产默认 PG；测试强制 SQLite（`conftest.py` 注入） |
| 对象存储 | **仅 adapter** | `storage/object_store.py` 本地 FS，保留 bucket/key 接口，可换远端 |
| MCP（stdio） | **真实接入** | `mcp/runtime_client.py` 真 stdio；`mcp/installer.py` 真 `npm install` 到 `.data/mcp_sandbox` |
| MCP 包源 | **仅 adapter** | 包名必须命中 catalog 候选（`routes_mcp_install.py:73`），非任意 npm 包 |
| Desktop（Electron Node） | **真实接入** | `desktop/node_service.py` 租约 + 工具覆盖层 |
| Qdrant | **未完成** | 无客户端代码 |
| mem0 / 时序图 | **未完成** | 无 adapter，无 graph 表 |
| Eval harness | **未完成** | 无实现 |
| Reranker（cross-encoder/LLM） | **未完成** | 只有 `recall_fusion` 打分 |

---

## 4. 哪些能力没有 Chat 入口

Chat 入口 = 已注册为工具（`register_builtin_tools`，共 38 个：36 个内置 + `forget` / `forget_status`）。

**已有 Chat 入口（38）**：echo、get_current_time、safe_delete、read_text_file、read_document、ingest_document、reindex_document、search_knowledge、plan_capability、search_mcp、install_mcp_sandbox、memory_search、history_search、memory_timeline、memory_event_log、remember_or_update、run_memory_maintenance、schedule_reminder、remind_alert、confirm_reminder、snooze_reminder、list_tasks、cancel_task、dismiss_notifications、show_notifications、query_rhythm、query_attention、update_working_state、create_selfdev_plan、apply_patch_to_inactive_slot、promote_slot、rollback_slot、web_search、fetch_web_page、forget、forget_status。

**仅 API / 无 Chat 入口**：

| 能力 | 入口 | 缺口影响 |
| --- | --- | --- |
| 保留治理（retention / vacuum） | `/api/...` + 调度器 | 用户无法用自然语言触发“清理旧数据” |
| Outbox 巡检 / 重放 | `api/routes_outbox.py` | 死信只能靠 UI/接口处理 |
| Desktop 节点绑定/解绑 | `api/routes_desktop.py` | 需手动操作 |
| 发布监控（release monitor / 自动回滚） | `api/routes_selfdev.py` | 只能由后台任务触发 |
| Epoch / 压缩管理 | `api/routes_epochs.py` | 不可对话管理 |
| 健康检查 / 诊断 | `api/routes_health.py`、`routes_debug.py` | 可接受（运维接口） |
| 审批确认（approval） | `/api/approval` + 卡片 | 有卡片但无对应工具，模型不能自查审批状态 |

---

## 5. 哪些能力没有 UI 可观测

v7 §10.3 期望 9 个页面，实际 9 个页面但映射不同：

| v7 页面 | 实际页面 | 状态 |
| --- | --- | --- |
| `ChatPage` | `ChatPage.tsx` | ✅ |
| `MemoryDashboard` | `MemoriesPage.tsx` | ✅ |
| `CapabilityDashboard` | `CapabilitiesPage.tsx` | ✅ |
| `EventTimeline` | `EventTimeline.tsx` | ✅ |
| `ContextInspector` | `ContextInspector.tsx` | ✅ |
| `RetrievalInspector` | `RetrievalInspector.tsx` | ✅ |
| `MaintenanceReport` | 无独立页（仅 ChatPage 内卡片） | ✗ |
| `SelfDevDashboard` | `DeveloperPage.tsx` | ◐（槽位/补丁可见，缺 A/B 对比视图） |
| `ApprovalInbox` | 无独立页（ChatPage / TaskCenterPage 内联卡片） | ◐ |
| — | `TaskCenterPage.tsx`、`NotificationsPage.tsx` | 额外，v7 未列 |

**后端有数据但 UI 不可见**：retention 清理批次（`retention_cleanup_runs`）、记忆维护批次（`memory_maintenance_runs`）、压缩运行（`compaction_runs`）、Outbox 死信详情、slot 切换历史（文件态）。

---

## 6. 哪些能力没有 event / trace / context / action card

现有 22 类事件：`user_message`、`llm_response`、`tool_call`、`tool_result`、`tool_operation_terminal`、`chat_ended`、`context_reset`、`run_started`、`task_*`（8 类）、`reminder_*`（3 类）、`notification_created`、`approval_denied`、`memory.forgotten`、`maintenance_report_terminal`。

| 能力 | event | trace | context | action card |
| --- | --- | --- | --- | --- |
| Chat / Turn | ✅ | ✅ `trace_id` | ✅ 快照含 trace_id | ✅ |
| 工具调用（副作用） | ✅ | ✅ | ✅ | ✅ |
| 记忆写入/遗忘 | ✅（仅 forget 终态） | ✅ | — | — |
| 主动任务/提醒 | ✅ | ✅ | — | ✅ |
| 维护/保留 | ✅（`maintenance_report_terminal`） | ◐ | — | — |
| MCP 安装/冒烟 | ✗ | ✗ | — | — |
| Slot promote / rollback | ✗ | ✗ | — | — |
| 保留清理（retention run） | ✗ | ✗ | — | — |
| 压缩（compaction run） | ✗ | ✗ | ✅ 段/epoch | — |
| Eval / contract check | ✗（能力本身缺失） | — | — | — |

结论：**执行链可观测性完备，治理链（MCP 安装、槽位切换、保留清理）缺事件与 trace**，导致“为什么这台机器状态变了”无法回溯。

---

## 7. 测试体系治理

### 7.1 基线（改造前，2026-10-22 实测）

| 指标 | 结果 |
| --- | --- |
| 测试文件 | 91（`find tests -name "test_*.py"`） |
| 用例总数 | 857 = unit 532 + integration 315 + flow 10 |
| 全量耗时 | **363.80s**（6:03） |
| unit | 532 passed / 330.83s |
| integration | 315 passed / 23.27s |
| flow | 10 passed / 19.76s |
| 默认快速集（unit+flow） | 542 passed / ~350s |
| 结果 | 857 passed / 0 failed / 0 error / 0 skipped |

> 公开报告中的“857 用例、91 文件、CI 全绿”与当前工作区**一致**（用例数与文件数逐项核对通过）。差异只在耗时：历史报告记录的 315~408s 是夹具改造前的数字。

### 7.2 逐文件分类（A/B/C/D）

对 91 个文件逐一核对：文件头说明 → 被测模块是否仍存在 → 断言是否指向真实行为。

| 类别 | 数量 | 说明 |
| --- | --- | --- |
| A 有效且必要 | 91 | 全部对应现存模块与真实契约 |
| B 过时（被测对象已改名/迁移） | 0 | — |
| C 无效/低价值（空断言、恒真） | 0 | 无 `assert True` 式用例；`test_targeted_test_runner` 中的 `assert True` 是它自己生成的样例测试文件，不是断言 |
| D 不稳定（依赖时序/随机/顺序） | 0 | 0 处 `time.sleep`、0 处 `random`、0 处 `datetime.now()`；91/91 单文件隔离通过，无顺序依赖 |

因此**本轮不删除任何测试文件**。原计划“所有删除集中到最后”没有触发条件。

### 7.3 外部依赖核查（真实 API / 网络 / npm / skip）

| 检查项 | 结果 | 证据 |
| --- | --- | --- |
| 真实 HTTP 客户端 | 0 处 | `httpx.Client` / `requests.` / `urlopen` / `socket` 在 `tests/` 全无命中 |
| LLM / Embedding | 传输层替换 | `test_llm_client.py`、`test_embedding_provider.py` 保留请求构造与解析，仅替换 `httpx.post` |
| MCP stdio | 纯逻辑 / 假 transport | `test_mcp_runtime_client.py` 用内存流；`test_mcp_pipeline.py` 不依赖网络 |
| npm 安装 | monkeypatch | `test_mcp_installer.py` 替换 `_run_npm_install`，不断言真实安装 |
| subprocess | 仅本地 git | 唯一使用在 `test_persistent_task_runtime.py` 的 `git init/add`（worktree 场景） |
| skip / xfail | 0 | `-ra` 汇总无 skipped；唯一含 “skip” 的是用例名 `test_condition_watch_skip`（语义是“跳过提醒”） |
| 凭证依赖 | 0 | 根 `conftest.py` 只从 `.env` 读 `AIIVE_*`，不读任何 API Key；CI 用占位令牌 |

### 7.4 仍依赖真实等待的用例

只有 1 个：

| 用例 | 耗时 | 原因 |
| --- | --- | --- |
| `tests/unit/backend/test_tool_operation_execution.py::test_registry_timeout_returns_unknown_then_worker_commits_once` | 30.15s | 走 `registry.execute` 的真实超时路径，等 `settings.tool_default_timeout_seconds`（30s）到期才返回 `execution_unknown` |

它占默认快速集耗时的 34%。**未改动**（改测试属于用户治理范围）。最小改法供决策：在该用例内 `monkeypatch.setattr(settings, "tool_default_timeout_seconds", 0.5)`，语义不变、可省 29.6s。

其余慢用例（7.62s flow turn、3.25s targeted runner、2.1~2.7s schema migration）都是真实工作量（Alembic 全量建表、子进程跑 pytest），不是空转。

### 7.5 PostgreSQL 语义缺口（重要风险）

测试统一跑 SQLite，因此以下 PG 专属语义**没有被任何用例覆盖**：

| 语义 | 位置 | SQLite 下行为 |
| --- | --- | --- |
| `pg_advisory_xact_lock` | `memory/memory_write_service.py:1072` | `dialect != postgresql` 时静默 no-op（代码注释已声明是刻意设计） |
| `SELECT ... FOR UPDATE SKIP LOCKED` | `control/approval_service.py:192` | SQLite 忽略锁语义，多消费者抢占未被验证 |
| `FOR UPDATE` 串行化 | `forget/reconcile_service.py`、`memory/memory_mutation.py`、`memory/memory_store.py`、`retrieval/index_rebuild.py`、`api/routes_approval.py` | 同上 |
| 事务隔离级别 | 全局 | SQLite 单写者，PG 的读已提交/可重复读差异未覆盖 |
| pgvector 召回 | `memory/vector_projection.py` | 测试强制 `AIIVE_MEMORY_VECTOR_ENABLED=false` |

**定级：中高。** 单用户本地场景并发压力低，但“记忆写入幂等 + Outbox 抢占”正是 v7 §6.4 的硬约束。建议（V34）：用 docker-compose 的 PG 服务跑一个 `tests/pg_contract/`（标记 `slow`，默认入口不跑，CI 单独 job），只覆盖 4 个契约：并发写同键只产生一条 active、`SKIP LOCKED` 不重复派发、forget 阶段原子 deadletter、事务回滚不留半条记忆。

### 7.6 夹具改造：unit 层 5.5 分钟 → 1 分钟（本轮唯一代码改动）

**根因**：`tests/conftest.py` 的 `db_session` 每个用例新建 SQLite 文件并 `Base.metadata.create_all()`。实测单次 **1.226s**（5 次中位数），而 unit 层约 250 个用例请求该夹具 → 约 300s 纯建表。

**改法**：会话级模板库 `_db_template`（整进程只建一次表，并 `PRAGMA journal_mode=DELETE` 保证文件自包含）+ 用例侧 `shutil.copyfile`（实测 **0.008s**）。隔离语义不变：仍是“独立物理库 + 外层事务回滚”。

**验证**：

| 项 | 改造前 | 改造后 |
| --- | --- | --- |
| 全量 | 857 passed / 363.80s | **857 passed / 112.35s** |
| unit | 532 / 330.83s | **532 / 78.47s** |
| integration | 315 / 23.27s | 315 / 29.00s（同机波动，无回归） |
| flow | 10 / 19.76s | 10 / 19.76s（未受影响） |
| 默认快速集（unit+flow） | 542 / ~350s | **542 / 89.17s** |
| 单文件隔离 | 91/91 | **91/91**（JOBS=4） |
| basedpyright | 0/0 | **0 errors / 0 warnings** |
| ruff | 0 | **All checks passed** |

结论：目标“快速集 3~5 分钟”已达成并超出（1.5 分钟）。剩余可优化项只有 7.4 的 30s 等待，需改测试，未动。

---

## 8. Schema migration 与 A/B 兼容

### 8.1 现状

- Alembic 17 个版本，每个都有 `downgrade()`（可回滚）。
- `test_schema_migrations.py` 用 4 个用例校验 ORM 与迁移后结构一致（含 memory sensitivity、persistent task、desktop node、legacy forget 移除）。
- 迁移在应用 lifespan 内自动执行；CI 的 docker-smoke 已修正为“先等健康、再确认 head”，避免并发迁移。

### 8.2 与 v7 §25.1 表清单的差异

| v7 表 | 实际 | 说明 |
| --- | --- | --- |
| `outbox_deadletters` | 无独立表 | 用 `outbox_jobs.status=deadletter` 表达 |
| `operation_transactions` | `tool_operations` | 改名 |
| `working_windows` / `working_summaries` | `working_states` + `segments` / `segment_summaries` | 拆分 |
| `graph_entities` / `graph_relations` | **缺失** | 无时序图 |
| `capability_security_profiles` | **缺失** | 安全声明不入库 |
| `tool_definition_audits` | **缺失** | 无审计记录 |
| `candidates` / `version_slots` / `version_manifests` / `slot_switch_events` | **缺失** | 槽位状态全在文件系统 |
| `schema_versions` / `schema_migrations` | `alembic_version` | 交给 Alembic |
| `safe_delete_scopes` | 静态注册表 | 不入库（`control/scope.py`） |
| `delete_requests` | `forget_*`（7 张表） | 更细粒度 |
| `test_results` / `repair_attempts` | **缺失** | 无修复尝试记录 |
| `retrieval_plans` / `packed_context_items` | `retrieval_runs` / `retrieval_candidates`、`compaction_*` | 语义等价，命名不同 |
| `maintenance_plans` / `maintenance_actions` | `memory_maintenance_*`（4 张） | 范围更窄 |
| `eval_runs` / `eval_cases` / `kernel_contract_check_runs` | **缺失** | 能力未实现 |

### 8.3 A/B 自我修改的兼容性风险

1. **切槽不生效**：`PromoteRollback.promote/rollback` 只改 `active_slot` 指针文件；`supervisor/launcher.py` 无 `Popen`/`execv`/systemd 调用（全仓 grep 命中 0）。**必须人工重启进程**才真正切换。
2. **schema 双向兼容未验证**：A 槽旧代码 + B 槽新 schema 的组合没有测试覆盖；`migration_manager` 只有 Alembic 降级能力，没有“切槽前检查 schema 是否向后兼容”的校验器。
3. **数据目录保护正确**：`create_version_manifest` 明确排除 `data/`、`postgres/`、`qdrant/`、`object_store/`、`logs/`、`.env`，符合 v7 硬约束。
4. **健康回滚可用**：`ReleaseManager.monitor_once()` 在不健康时自动回滚（`test_release_manager.py` 覆盖），但同样只改指针。

---

## 9. MCP / capability 安全风险

| 风险 | 等级 | 证据 | 建议 |
| --- | --- | --- | --- |
| `npm install` 未加 `--ignore-scripts` | 中 | `mcp/installer.py` 的 `_run_npm_install` argv 无该开关，postinstall 脚本会执行 | 加 `--ignore-scripts`，需要构建的包显式白名单 |
| 包名来源 | 低（已缓解） | `routes_mcp_install.py:73` 强制 `package_ref` 命中 catalog 候选 | 保持；catalog 变更需评审 |
| 命令注入 | 低 | argv 列表传递，无 `shell=True`；`sanitize_dir_name` 清洗目录名 | 保持 |
| 外部内容信任 | 低（已实现） | 工具返回值统一 `{"ok", "result", "untrusted": True}`；`memory_gate` 按 `trust_level` 限权 | 保持 |
| 工具安全声明无审计 | 中 | 无 `tool_definition_auditor`，`tools/tool_validation.py` 只校验 schema | V34 补“声明 vs 实际副作用”审计 |
| capability 安全档案缺失 | 中 | 无 `capability_security_profiles` 表，`trust_level` 只存在 definition JSON | V34 落表 + 迁移 |
| MCP 安装/冒烟无事件 | 低 | 安装成功/失败不写 `events` | 补 2 个事件类型 |

---

## 10. V34+ 建议（按优先级）

### P0 — Eval harness + Kernel Contract Checks（自我修改的刹车）

v7 §11、§27.7 明确要求。当前自我修改只有“映射测试是否通过”（`targeted_test_runner`），缺少端到端 replay 与内核契约。最小落地：

1. `eval/cases/` 存 20~30 条离线 replay（用户输入 + 期望工具序列 + 期望记忆变更），跑 SQLite 即可；
2. `eval_runs` / `eval_cases` 表 + `/api/eval` 只读结果；
3. `kernel_contract_checks.py` 断言 5 条不可退化契约（记忆唯一真相源、事件必写、Outbox 幂等、safe_delete 守卫、上下文快照必存）；
4. promote 前强制跑一次，失败即拒绝切槽。

### P1 — Model Routing / Cost / Latency + 上下文装配统一评分

`model_router.py` 与 `context_policy.py`（v7 §15、§22.5）是仅有的两个“影响每次调用成本”的缺失模块。建议先做最小版：按任务类型（chat / extraction / compaction / tool-planning）路由到不同模型档位，并在 `llm_calls` 记录成本与延迟；上下文侧把现有 `context_budget` 的分区预算升级为显式评分（重要性 × 新鲜度 × 相关度）。

### P2 — Supervisor 真正托管进程

当前 A/B 自我修改是“改指针 + 人工重启”。补 `launcher` 的 `spawn/stop/restart` + 端口切换 + 启动健康回滚，才能让 promote 真正闭环；同时补 `slot_switch_events` 事件，让切槽可审计。

### 其他（不阻塞）

- PG 契约测试（§7.5）：4 条并发/锁语义；
- 补 `MaintenanceReport` 页面与 retention/compaction 事件（§5、§6）；
- `npm install --ignore-scripts`（§9）；
- 清理文档过期陈述（§11）。

---

## 11. 文档一致性核对（本次同步修正）

| 文件 | 过期陈述 | 处置 |
| --- | --- | --- |
| `docs/plans/current_phase.md` | 仍写 “project_plan_v11 / 149 tests / Next: V12”，与 857 用例、V33 状态不符 | 已改为 V33 收敛审计状态 |
| `docs/CODE_MAP.md` §10.6 | 耗时基线为夹具改造前（unit 362s、全量 315~401s），且把 `db_session` 描述为“逐用例建表” | 已更新为改造后实测与模板库说明 |
| `docs/audit/CODE_REVIEW_REPORT.md` §1-2 | 引用已删除的 `runtime/agent_loop.py`、`core/intent_classifier.py` | **保留原样**（该文件是 2026-10-21 的历史快照，§9 已记录修复结论）；本轮在其文首补一行“历史快照”提示 |

---

## 12. 剩余风险清单

| # | 风险 | 等级 | 说明 |
| --- | --- | --- | --- |
| R1 | 无 eval / contract 门禁 | 高 | 自我修改只有单测级保护 |
| R2 | PG 并发语义未验证 | 中高 | advisory lock 在测试中 no-op |
| R3 | 切槽需人工重启 | 中高 | 自我修改闭环不完整 |
| R4 | 无 model routing | 中 | 成本/延迟不可控，`llm_calls` 无成本字段 |
| R5 | 无 retrieval planner / reranker | 中 | 召回质量上限受限 |
| R6 | 无 temporal graph | 中 | 记忆关系查询不可用 |
| R7 | 治理链缺事件（MCP 安装、切槽、retention） | 中 | 状态变更不可回溯 |
| R8 | npm 安装未禁脚本 | 中 | 供应链执行面 |
| R9 | 单用例 30s 真实等待 | 低 | 占快速集 34% |
| R10 | 文档过期（已修正 4 处，历史报告保留原样） | 低 | — |

---

## 13. 本轮改动与验收

| 改动 | 文件 | 性质 |
| --- | --- | --- |
| `db_session` 改为会话级模板库 + 文件拷贝 | `tests/conftest.py` | 测试基础设施（经用户授权；不改任何断言与用例） |
| 新增本报告 | `docs/audit/final_convergence_report.md` | 文档 |
| 更新耗时基线与模块落点 | `docs/CODE_MAP.md` | 文档 |
| 更新当前阶段 | `docs/plans/current_phase.md` | 文档 |

验收：857 passed / 91-91 单文件通过 / ruff 0 / basedpyright 0-0。**未新增任何功能，未删除任何测试文件。**
