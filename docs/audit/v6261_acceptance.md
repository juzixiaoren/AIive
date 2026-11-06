# V62.6.1 Forget 来源闭包与跨线程执行授权验收

日期：2026-11-06（Asia/Shanghai）。基线、开始时 HEAD 与 fetch 得到的 origin/main 均为
`f966dae48c22eceb7bf191be27bf57a212c9b112`。开始时工作区干净，未回退、删除仓库源文件、提交或推送；两份临时测试库已按用户授权清理。

## 范围与实现

代码、功能测试与资源清理验收均已完成。用户明确授权的两份历史测试库已由限定目标脚本删除。

复用现有 Forget Saga、ContentProvenanceRef、Job Reference 契约、执行授权模块及 advisory lock
命名空间。无新 API、表或迁移。落实用户确认的规则 1：只有持久化提案证明为独立手动创建的
无 Event 来源记忆可以正常使用；其余缺失、异常、悬空来源全部 fail-closed。

| 问题 | 根因 | 修复后的实际执行流程 | 回归证据 |
|---|---|---|---|
| P0-1 | 只检查 Memory 自身，Event 的屏蔽不向下传递 | blocked_target_ids → Memory Provenance → Event/Turn 真实元数据 → Shield/Tombstone；filter、Worker、Core/Graph/Vector/Markdown、索引刷新和重建共用；写入持久化全部 Proposal/RunContext Event 来源，Merge/维护复制继承上游来源 | Event-only Shield 无 Memory Shield 时关闭；Handler 零调用，直接 Vector provider/embed 零调用；Index 重建不可搜索；真实手动 API 仍可用；派生记录先可用、来源遗忘后立即屏蔽 |
| P0-2 | Seed/Neighbor 未过滤，冻结和旧 Action 执行前不重查 | Store 筛选 → freeze 再检查 → subject/related/source_input 复核 → Mutation；屏蔽候选不冻结正文，只生成终态 skipped_stale / forget_source_blocked Action，聚合 Batch/Run 计数并推进实际扫描游标 | Seed/Neighbor、全屏蔽批次游标收敛、真实持久化历史 Batch、零 Mutation 调用 |
| P0-3 | 父线程持共享连接等待工具，子线程无 ContextVar 状态却等待排他锁 | Registry 提交前释放父连接；子线程自行创建连接，重读 claim/来源；Forget 在子线程业务 Session 提交；父线程恢复共享授权后返回；连接记录 owner_thread_id 并禁止异线程操作；Runtime/Broker 继续前复核授权和来源 | PG 真实 Handler→Runtime→Broker→Registry→ThreadPool→Forget，恢复成功/失败双路径，下一模型零额外调用，超时撤销/等待真实提交，Operation 不重复 |
| P1-1 | 用新 Outbox.created_at 代替旧业务来源时间 | AgentTask/Reminder 使用自身 created_at；同 Thread 多来源取最早时间；Turn 实体/逻辑 ID 归一并检查子 Event；未知时间关闭 | 无 source_turn 的旧 Task + 新 Job 仍阻断；独立新 Task 放行，补回旧 Turn 后重新阻断 |
| P1-2 | Evidence 仅查 Thread，且用 updated_at 判断 cutoff | Task/Action/Job 使用统一 JobReferences + DB 来源解析；Action 参数引用单独检查，Event/Turn/Thread 全覆盖，不能证明安全就丢弃 | Event-only Shield 覆盖 Task/Action/Job，实际 ImprovementDiscovery 的 LLM spy 零调用 |
| P1-3 | rollover 无 resolver，直接复制混合 WorkingState | 遍历 active Epoch/Segment/CI manifests/Summary；安全快照按项追溯 Turn/Event/Artifact/ToolOperation/Approval，旧项剔除、安全项保留、未知状态拒绝新快照；EpochManager 直接调用也检查 | 真实 rollover 混合项保留安全内容；未知来源拒绝；Event Shield 后 Handler 零 freeze/LLM 调用、零 CI/ECI 快照 |
| P1-4 | 坏数组元素被忽略、str() 将非法对象转成 ID | 引用统一使用 parse_reference_ids；非法数组元素、嵌套 Manifest、不完整必需引用标记 unresolved，完整解析失败即关闭 | 合法项混合对象/嵌套数组/数字、Manifest 多种异常、查询失败回归 |

## 范围与兼容语义

- Memory 自身 Shield/Tombstone 不变。检查上游 Event 时，不把 memory_only 生成的来源历史隔离
  反射到共享 Event 的其他 Memory，避免递归和范围扩大。history_only/everywhere 的历史屏蔽向下传递。
- 无来源人工豁免要求相关提案包含 `manual_memory_api + user_required + create`，且没有
  Event/Evidence/Turn 来源混入；空字段或 created_from 标签本身不能证明安全。
- Event ID 必须有真实行，时间/Thread 从业务表回源。部分 Goal event_id 是领域事件标识，
  按真实 Task、Knowledge 原始作业或 SelfDev Request 追溯，不误当聊天 Event ID。
- 现有生产者用空的可选标量 ID 表示缺省；必需来源由具体 resolver 校验。引用数组中的空元素、
  对象、数字、嵌套数组不允许静默丢弃。查询异常关闭整批，不能部分解析成功就放行。
- Maintenance 跳过动作使用负序号，planner 非负序号、版本检查、hash、幂等及 claim fencing 保持原协议。
  扫描按批次有界；skip 计数按 Batch/lane 统计。复制获胜值只继承胜方来源，Merge 继承全部合并来源。

## 关键时序对比

```text
Memory 之前：Phase A COMMIT(Event E) → M 仍 active → 只查 M → embed(M.content)
Memory 之后：Phase A COMMIT(Event E) → M → Provenance → E/Turn 元数据 → 跳过 → embed=0

授权之前：父持共享连接 A → submit → 父等 Future
          子无父 ContextVar → release 返回 → 等排他锁 → 等待环
授权之后：父释放 A → submit → 子创建共享连接 B、重查来源
          子进入 Phase A 前释放 B → 业务 Session C 取排他锁 → COMMIT/ROLLBACK
          子恢复/释放自己的授权 → 父创建共享授权 D → 成功后才返回
          Runtime 下一轮重新检查来源；被遗忘或恢复失败都停止推理/副作用
```

没有共享 SQLAlchemy Connection，也没有 copy_context。gate 仅同步 cancelled/forget_started。
超时撤销与 Phase A 启动在同一锁内线性化：未启动就撤销启动许可，已经启动则等待真实事务终态。
不会报告失败后让 Forget 在后台成功，也不重提 Forget。forget_old 的 Phase A 与后续写入仍由
同一业务 Session 原子提交，既有锁命名空间与锁序不变。

## 最终验证

最终套件已全部完成，以下为实际结果。

| 检查 | 结果 | 记录 |
|---|---|---|
| 全量单元/本地集成/Flow | 1716 passed / 118 skipped / 0 failed（293.25s） | `.data/v6261-cleanup-all.log` |
| PostgreSQL 全目录（含真实 BM25） | 105 passed / 13 skipped / 0 failed（230.68s） | `.data/v6261-cleanup-pg.log` |
| V62.6.1 SQLite 专项 | 21 passed | `.data/v6261-extra.log` |
| Ruff（与 CI 一致：backend/aiive --select F,E9） | passed | 工具输出 |
| 新专项测试 Ruff F/E9 | passed | 工具输出 |
| basedpyright | 0 errors / 0 warnings | `.data/v6261-types.log` |
| 前端 npm run build（tsc + Vite） | passed | `.data/v6261-frontend.log` |
| 隔离 PG Alembic upgrade head / check / current | passed；No new upgrade operations detected；head=f1a2c3d4e5b6 | `.data/v6261-alembic.log` |
| git diff --check | passed | 工具输出 |

命令：`.venv/Scripts/python.exe -m pytest tests -q`；PG 以 `AIIVE_PG_TESTS=1` 运行
`tests/integration/pg -q`。Windows 测试设置 `PYTHONUTF8=1`，并将已确认存在的 Git Bash
`E:\git\Git\bin` 放在 PATH 前，避免误调用 Windows WSL 启动器。

没有跳过断言或放宽生产校验来制造通过。旧正常记忆夹具显式补充真实 Event/Provenance；
悬空/孤儿专项不使用该助手。新增隐私用例使用可验证 mock/spy，零调用断言与真实数据结果并存。

## 清理、限制与未实现项

- 使用现有 SQLite 外层事务、临时库、tmp_path 自动清理；PG 测试容器由夹具自动删除；
  Alembic 使用另一个一次性测试容器并 finally 清理。未连接生产库、删除用户文件或已有容器。
  清理记录：`tests/artifacts/v6261/cleanup_report.json`，最终状态 **verified**。
  历史残留 `aiive_test_16856.db` 与 `aiive_test_44368.db` 已按用户授权由限定目标脚本删除；
  修复后的全量与 PG 测试进程（41084/47008）均自动清理，报告 cleaned，进程测试库均不存在。
  测试容器全部清理，原有 `aiive_alembic_pg` 容器保留。
- 本地真实 Embedding 服务未启动，既有 13 个 live vector/RAG 用例跳过。本次新增隐私用例的
  provider/embed spy 在真实 PG/ORM/Handler 链执行，不依赖该服务，未跳过。
- 未自动猜测或回填历史未知来源。来源不明的普通 Memory 被隔离，只有可证明的独立人工记忆豁免。
  非空旧 WorkingState 缺来源时停止 rollover；不以 updated_at 推断整个快照安全。
  原始 Segment manifest 已有屏蔽来源时停止新输入，WorkingState 独立有来源的条目分别保留/剔除。
- 已启动的 Phase A 可超过通用工具超时，等待真实终态，不提供 Python/DB 强杀硬截止。
- 未运行真实外部 LLM/Embedding smoke，未部署、提交或推送；测试通过不代表无限输入空间的安全证明。

## 文档与工程记录

同步更新 `forget_content_matrix.md`、`phase_6A.md`、`current_phase.md`、`CODE_MAP.md`、
`AIive_核心架构与实现深度分析.md`。旧版本小节保留作历史，以 V62.6.1 补记为当前实现。

路径教训：读源码前先通过 rg --files / rg 确认真实文件与符号，不猜模块路径。
本轮曾读取不存在的 working_state_projector.py，已改为追踪实际 working_state.py 与调用方。

## 修改文件及函数清单

由 AST 与基线比较生成，包含测试夹具和新增回归；文档及模块常量变化另列。

| 文件 | 新增/修改/移除函数 |
|---|---|
| `backend/aiive/control/capability_broker.py` | `CapabilityBroker.dispatch` |
| `backend/aiive/forget/visibility_service.py` | `ForgetVisibilityService._blocked_target_ids`、`ForgetVisibilityService._load_target_meta`（移除/公开重命名）、`ForgetVisibilityService._source_event_blocked_memory_ids`、`ForgetVisibilityService._source_memory_blocked_event_ids`、`ForgetVisibilityService._turn_blocked_event_ids`、`ForgetVisibilityService.batch_is_tombstone_blocked`、`ForgetVisibilityService.blocked_target_ids`、`ForgetVisibilityService.load_target_meta` |
| `backend/aiive/memory/core_memory_projection.py` | `build_blocks`、`load_core_memory` |
| `backend/aiive/memory/graph_projection.py` | `GraphProjectionService.refresh` |
| `backend/aiive/memory/memory_lifecycle_service.py` | `MemoryLifecycleService._apply`、`MemoryLifecycleService.apply_maintenance_action` |
| `backend/aiive/memory/memory_store.py` | `MemoryStore.__init__`、`MemoryStore._maintenance_visible`、`MemoryStore.expand_maintenance_neighbors`、`MemoryStore.select_maintenance_seeds` |
| `backend/aiive/memory/memory_write_service.py` | `MemoryWriteService._execute_forced_decision`、`MemoryWriteService._execute_merge`、`MemoryWriteService._execute_promote_candidate`、`MemoryWriteService._execute_reinforce`、`MemoryWriteService._execute_reinforce_in_transaction`、`MemoryWriteService._execute_supersede_or_revise`、`MemoryWriteService._is_reingestion_blocked`、`MemoryWriteService._write_evidence_batch`、`MemoryWriteService._write_single_in_transaction`、`MemoryWriteService.write` |
| `backend/aiive/memory/vector_projection.py` | `MemoryVectorProjectionService.refresh` |
| `backend/aiive/retrieval/indexing_service.py` | `refresh_source` |
| `backend/aiive/runtime/compaction.py` | `freeze_compaction_input` |
| `backend/aiive/runtime/epoch_manager.py` | `EpochManager.begin_epoch_sealing` |
| `backend/aiive/runtime/working_state.py` | `WorkingStateService.safe_boundary_snapshot`、`WorkingStateService.update_semantic_field` |
| `backend/aiive/selfdev/improvement_evidence.py` | `EvidenceCollector._action_items`、`EvidenceCollector._job_items`、`EvidenceCollector._source_blocked`、`EvidenceCollector._task_items`、`EvidenceCollector._thread_blocked`（移除/公开重命名） |
| `backend/aiive/task_runtime/runtime.py` | `PersistentTaskRuntime.run` |
| `backend/aiive/tools/builtin_runtime_state.py` | `handle_update_working_state` |
| `backend/aiive/tools/forget_tool.py` | `handle_forget` |
| `backend/aiive/tools/registry.py` | `ToolRegistry._execute_direct` |
| `backend/aiive/worker/handlers_agent_tasks.py` | `handle_agent_task_run` |
| `backend/aiive/worker/handlers_conflict.py` | `handle_memory_conflict_reply` |
| `backend/aiive/worker/job_source_guard.py` | `JobSources.empty`、`_add`、`_blocked_ids`、`_collect_manifest_ids`、`_resolve_conflict_case`、`_resolve_domain_event`、`_resolve_epoch_checkpoint`、`_resolve_epoch_rollover`、`_resolve_job_sources`、`_resolve_reminder_task`、`_resolve_task`、`blocked_source_reason`、`epoch_rollover_sources`、`resolve_job_sources`、`task_sources_visible` |
| `backend/aiive/worker/outbox_execution_auth.py` | `_AuthorizationState.assert_owner`、`_ToolGate.__init__`、`_acquire_shared`、`_release_shared`、`authorized_session_factory`、`execute_in_tool_thread`、`get_session_factory`、`release_execution_authorization`、`require_execution_authorization`、`resume_execution_authorization`、`shared_execution_authorization`、`with_execution_authorization` |
| `backend/aiive/worker/outbox_handlers.py` | `_create_batch_and_freeze`、`_execute_batch`、`_freeze_input`、`_maintenance_lane_has_more`、`_persist_actions` |
| `backend/aiive/worker/outbox_payload_contract.py` | `_add_ids`（移除/公开重命名）、`job_reference_ids`、`parse_reference_ids` |
| `tests/integration/backend/test_context_assembler_graph_expansion.py` | `_seed_memory` |
| `tests/integration/backend/test_forget_content_coverage.py` | `test_unified_retriever_cannot_recall_after_forget` |
| `tests/integration/backend/test_memory_conflict_confirmation.py` | `TestReplyEntryPoint.test_reply_handler_writes_through_write_service`、`_park_case`、`_seed_record` |
| `tests/integration/backend/test_memory_entity_binding.py` | `TestPendingIsolation.test_pending_keeps_binding_but_writes_no_graph`、`_seed_record`、`_write_and_project` |
| `tests/integration/backend/test_memory_policy.py` | `_record` |
| `tests/integration/backend/test_memory_sensitivity_paths.py` | `_memory` |
| `tests/integration/backend/test_phase3_compaction.py` | `test_11_freeze_captures_ws_snapshot_immutably` |
| `tests/integration/backend/test_phase4_lifecycle.py` | `_mk_record` |
| `tests/integration/backend/test_phase4_recall_sleeping_and_access.py` | `_mk_record` |
| `tests/integration/backend/test_phase4_scheduler_worker.py` | `_mk_record` |
| `tests/integration/backend/test_phase5_retrieval.py` | `_mk_memory` |
| `tests/integration/backend/test_phase6a_forget.py` | `test_blocked_target_ids_all_user_data_global`、`test_blocked_target_ids_selector_canonical_key`、`test_blocked_target_ids_selector_scope`、`test_blocked_target_ids_selector_time_range` |
| `tests/integration/pg/test_pg_bm25_recall.py` | `_mk` |
| `tests/integration/pg/test_pg_forget_exec_auth.py` | `_make_extraction_job` |
| `tests/integration/pg/test_pg_forget_scope_integrity.py` | `test_pg_unified_retriever_recall_after_forget` |
| `tests/support.py` | `attach_memory_event_source`、`create_memory_source_event` |
| `tests/unit/backend/test_goal_event_sources.py` | `test_cancelled_goal_is_not_woken`、`test_dispatch_failure_is_retried_by_outbox`、`test_duplicate_event_and_redelivery_are_idempotent`、`test_pending_dispatch_survives_restart` |
| `tests/unit/backend/test_improvement_evidence.py` | `_job` |
| `tests/unit/backend/test_memory_graph.py` | `_record`、`test_outbox_handler_projects_graph`、`test_write_service_persists_facts_and_enqueues_graph_job` |
| `tests/unit/backend/test_memory_read_model.py` | `_make_and_store` |
| `tests/unit/backend/test_memory_vector_projection.py` | `_record` |
| `tests/unit/backend/test_v2_recall.py` | `_mk` |
| `tests/integration/backend/test_forget_v6261.py` | `claimed`、`memory`、`now`、`shield`、`source`、`test_bad_manifest_element_is_unresolved`、`test_direct_vector_and_index_rebuild_blocked`、`test_epoch_rollover_filters_mixed_snapshot_and_blocks_unknown`、`test_epoch_rollover_handler_event_shield_creates_no_snapshot`、`test_event_shield_evidence_never_calls_discovery_llm`、`test_evidence_and_mixed_working_state`、`test_historical_persisted_batch_rechecked_before_mutation`、`test_maintenance_clone_inherits_event_sources`、`test_maintenance_skips_are_terminal_and_cursor_advances`、`test_maintenance_sources_and_frozen_action`、`test_malformed_references_fail_closed`、`test_memory_closure_and_missing_manual_proof`、`test_memory_only_does_not_reflect_to_sibling`、`test_old_task_time_and_turn_sources`、`test_projection_handlers_zero_calls_from_event_only_shield`、`test_real_manual_api_without_event_remains_visible`、`test_visibility_query_failure_closes_entire_batch`、`test_write_merge_inherits_all_event_sources`、`uid` |
| `tests/integration/pg/test_pg_forget_v6261.py` | `test_pg_agent_task_real_pool_forget`、`test_pg_source_paths`、`test_pg_started_forget_waits_for_commit`、`test_pg_timeout_revokes_late_forget` |

| `conftest.py` | `_cleanup_test_db`、`pytest_sessionfinish` |
| `tests/unit/backend/test_process_database_cleanup.py` | `test_process_database_is_removed_even_with_retained_session`、`test_cleanup_failure_sets_nonzero_exit_and_writes_report`、`_run_child` |

文档变更：`docs/AIive_核心架构与实现深度分析.md`、`docs/CODE_MAP.md`、`docs/architecture/forget_content_matrix.md`、`docs/architecture/phase_6A.md`、`docs/plans/current_phase.md`；本报告。


## 授权后的测试清理修复（2026-11-06）

用户已授权修复测试退出清理逻辑，并由脚本仅删除此前确认的两份临时测试库。
`conftest.py::_cleanup_test_db` 现在先关闭本进程 ORM Session 并 dispose 连接池，
验证 Temp 根目录与本进程 PID 文件名，再删除自己的进程测试库。
新增 `pytest_sessionfinish` 在 fixture teardown 后执行清理，失败写 JSON 报告并让 pytest 非零退出，
不再吞掉 OSError。atexit 仅作幂等后备，不会把已报告的清理失败改成成功。
修复后再次完整运行：全量 1716 passed / 118 skipped；PG 105 passed / 13 skipped，两个进程退出码均为 0，测试库实际不存在。
每次进程的报告位于 `tests/artifacts/validation/cleanup/<pid>/cleanup_report.json`。

真实子进程回归 `tests/unit/backend/test_process_database_cleanup.py`：
测试成功/失败时均保留一个活跃 Session 到退出阶段并验证测试库消失；注入清理 PermissionError 时
验证报告 failed、退出码 1，即使后备清理成功也不得覆盖失败状态。专项 3 passed。
一次性清理脚本 `.data/v6261_cleanup_authorized.py` 硬编码两份授权目标，先核对全部路径与 SQLite
文件头，再逐一删除，不枚举其他文件，不使用通配符、递归或自动删除 sidecar。

新增/修改函数：`conftest.py::{_cleanup_test_db,pytest_sessionfinish}`；
新测试文件内 `test_process_database_is_removed_even_with_retained_session`、
`test_cleanup_failure_sets_nonzero_exit_and_writes_report`、`_run_child`。
