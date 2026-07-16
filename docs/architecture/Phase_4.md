# Phase 4：Daily Dream 与长期记忆生命周期维护

> **状态**：设计已确认，Phase 4 实施中
> **前置**：Phase 0.5A / 0.5B / Phase 1 / Phase 2 / Phase 3（均已实施并验收）
> **约束**：不新建 Supervisor LLM；不物理删除；不做 Forget Saga；不做冷存储迁移；不碰 Qdrant/FTS/KG；不做基于关键词的意图路由；不修改系统提示词；不修改 Phase 3 Segment/Epoch compaction。
> **本文件性质**：代码审计 + 实施方案 + 实施修订记录。业务代码变更见 git diff。

---

# 〇、真实代码审计（必输项 1–10）

## 1. MemoryRecord / MemoryProposal / MemoryEvidence / MemoryLineage 当前真实 ORM

> 来源：`backend/aiive/db/models.py`

### MemoryRecord（`models.py:159`）
真实字段（节选，与 Phase 4 相关）：

| 字段 | 真实列名 | 类型 | 说明 |
|------|----------|------|------|
| 生命周期 | `lifecycle_state` | String(32)，默认 `candidate`，**已建索引** | candidate/active/sleeping/archived/forgotten |
| 有效性 | `validity_state` | String(32)，默认 `valid` | valid/superseded/contradicted/expired |
| 保留策略 | `retention_policy` | String(32)，默认 `normal` | ephemeral/normal/pinned（Phase 2 已加） |
| 过期时间 | `valid_to` | DateTime nullable | Phase 2 已加，ephemeral 必填 |
| 置信度 | `confidence` | float，默认 0.5 | 真实存在，由 reinforce/promote 更新 |
| 重要度 | `importance` | float，默认 0.5 | **真实存在但当前任何路径都不写/更新它** |
| 稳定性 | `stability` | String(32)，默认 contextual | 真实存在（字符串） |
| 稳定性分 | `stability_score` | float nullable | 真实存在 |
| 钉住 | `pinned` | bool，默认 False | 真实存在 |
| 版本 | `record_version` | int，默认 1 | 每次写/生命周期变更 `+= 1` |
| 强化次数 | `reinforce_count` | int，默认 0 | reinforce / `_execute_reinforce` 更新 |
| 最近访问 | `last_accessed_at` | —— | **不存在，需新增** |
| 最近观察 | `observed_at` | DateTime，默认 now | 真实存在，recency 评分使用它 |
| 最近强化 | `last_reinforced_at` | DateTime nullable | 真实存在 |
| 内容哈希 | `content_hash` / `structured_value_hash` | String(32) nullable | 真实存在，冲突去重使用 |
| 更新时间 | `updated_at` | DateTime，默认 now，`onupdate=now` | 真实存在，**已可用于 dirty-set** |
| 业务键 | `canonical_key` / `scope_type` / `scope_id` | String，**canonical_key 与 scope_type 已建索引** | 真实存在 |

### MemoryProposal（`models.py:440`）
真实字段：`proposal_id`(UNIQUE)、`source_event_ids`(JSON)、`memory_type`、`canonical_key`、`scope_type`、`scope_id`、`content`、`structured_value`、`evidence`(JSON)、`trust_level`、`confidence`、`importance`、`stability`、`stability_score`、`proposed_operation`、`gate_decision`、`gate_reason`、`final_operation`、`final_memory_id`、`idempotency_key`(UNIQUE, nullable)、`raw_payload`、`normalized_payload`、`source_turn_id`、`ingestion_run_id`、`proposal_index`、`execution_mode`(Phase 2)、`retention_policy`(Phase 2)、`valid_to`(Phase 2)、`durable`(Phase 2)、`source_turn_record_id`(Phase 2，nullable)。

### MemoryEvidence（`models.py:528`）
真实字段：`id`、`memory_id`(FK，已索引)、`source_event_id`(String(36) **nullable，无索引、无外键**)、`source_type`(String(32))、`trust_level`、`relation`、`content_span`、`created_at`。
**关键缺口（第 8 点）**：当前没有 `source_turn_record_id` 字段。v1 **不新增该列**；独立 evidence 计数改用 `COUNT(DISTINCT source_event_id)`，且只统计 `source_event` 确属真实 `user_message` Event 的 evidence（统计时 JOIN events 过滤 `event_type='user_message'`）。若后续证明 `source_event_id` 不足以区分独立证据，则届时新增带**真实 FK→TurnRecord** 的 `source_turn_record_id`，并补齐 Evidence 写入传播与测试，不允许只加无 FK 的 String 列。

### MemoryLineage（`models.py:549`）
真实字段：`id`、`predecessor_id`(FK)、`successor_id`(FK)、`operation`(String(32))、`reason`(Text)、`proposal_id`(nullable)、`created_at`。`operation` 取值范围见 `LineageOperation`（`memory_types.py:147`）：create/reinforce/revise/supersede/merge/promote/sleep/wake/archive/forget。

## 2. 列真实存在性与实际写入情况

| 字段 | 真实列名 | 存在？ | 实际写入位置 |
|------|----------|--------|--------------|
| lifecycle_state | lifecycle_state | ✅ | create_record、promote、`execute_maintenance`（`memory_write_service.py`）、forget |
| retention_policy | retention_policy | ✅ | `MemoryStore.create_record` 由 proposal 写入（`memory_store.py:84`） |
| valid_to | valid_to | ✅ | create_record 由 proposal 写入 |
| confidence | confidence | ✅ | reinforce（`+0.1/0.05`）、promote（`+0.1`）、create（来自 proposal） |
| importance | importance | ✅ | **仅 create 时由 proposal 写入，后续无任何维护路径更新** |
| stability | stability | ✅ | create 时由 proposal 写入；无维护更新 |
| last_accessed_at | —— | ❌ | **不存在**，需新增 |
| reinforce_count | reinforce_count | ✅ | reinforce / _execute_reinforce（`memory_write_service.py:266,600`）更新 |
| record_version | record_version | ✅ | 每次写/生命周期变更 `+= 1` |

**结论**：除 `last_accessed_at` 外，其余字段均存在；但 `importance`、`stability` 在维护期不会被自动更新（Phase 4 冷却规则依赖 `importance`，因此 v1 直接读取它，不改动它；若需维护期衰减，留待后续 Phase）。

## 3. candidate/active/sleeping/archived/forgotten 当前真实转换路径

**现状：全部为显式/人工触发，没有任何自动生命周期转换。** 具体路径：

| 转换 | 真实代码路径 | 触发方 |
|------|--------------|--------|
| candidate → active | `MemoryWriteService.promote()`（`memory_write_service.py:490`）、`_execute_promote_candidate()`（:527） | 仅当用户/工具显式调用 promote（**当前无调用点**） |
| candidate → archived | **不存在** | —— |
| active → sleeping | `execute_maintenance` 中 `op=="sleep"`（:405） | API `POST /api/memories/{id}/sleep`（`routes_memories.py:157`） |
| sleeping → active | `execute_maintenance` 中 `op=="wake"`（:418） | 当前无调用点 |
| active/sleeping → archived | `execute_maintenance` 中 `op=="archive"`（:411，同时置 validity=expired） | API `POST /api/memories/{id}/archive`（`routes_memories.py:181`） |
| archived → forgotten | `MemoryWriteService.forget()`（:430，物理 scrub 内容+删 evidence+写 ForgetRequest） | API forget、工具 forget_memory |
| 任意 → archived（过期 ephemeral / 冷却） | **不存在** | —— |

**遗留 `MemoryMaintenance`（`memory/memory_maintenance.py`）只“生成 maintenance proposal”，并不执行状态变更**；其 `scan()` 是全表扫描（`SELECT ... WHERE lifecycle_state IN (active,candidate,sleeping)`），且 `generate_*_proposals` 构造的 proposal 从未被喂给 `MemoryWriteService`。因此 Phase 2/3 之后它是一条**死链路**（仅被诊断 API/工具调用返回统计数字）。

## 4. MemoryWriteService 与 ConflictResolver 当前支持的维护动作

### MemoryWriteService（`memory_write_service.py`）
- `write()` / `write_batch()`：create / reinforce / supersede / revise / promote / merge / ignore（经 Gate+Resolver）。
- `execute_maintenance(proposal, ctx)`：仅 `sleep` / `archive` / `wake`，目标为 `proposal.source_event_ids[0]`（单条记忆）。
- `promote(memory_id, ctx)`：candidate→active（带 lineage）。
- `forget(...)`：Saga 物理删除（Phase 4 不做）。
- `_execute_reinforce` / `_execute_reinforce_in_transaction`：强化，按 `source_event_id` 去重（v1 仍仅 `source_event_id`；不引入 `source_turn_record_id`）。

**已闭合**：已有共享 `MemoryMutationExecutor`（被 `MemoryLifecycleService` 与 `MemoryWriteService` 单向共用），统一带 `record_version` + `record_state_hash`/`decision_hash` 校验。`MemoryWriteService.promote` / `execute_maintenance` 现已委托 `MemoryLifecycleService`，后者走 executor（版本 bump + lineage + 投影），不再直接 `store.update_lifecycle`，消除无版本校验旁路（第 7 点单向依赖）。

### ConflictResolver（`conflict_resolver.py`）
- 支持：`create` / `reinforce` / `revise` / `supersede` / `promote` / `ignore`。
- **单基数（single cardinality）** 冲突：内容相同→reinforce；不同→`latest_value_wins` 走 supersede，否则按变化量选 revise/supersede（:85-98）。
- **多基数（multi）**：按 content_hash 去重 reinforce、候选匹配 promote、否则 create（:110-143）。
- **`merge` 操作从未由 Resolver 返回**（只有 `_execute_merge` 在 write service 内部可用，但 resolver 永不产生 `merge` 决策）。因此“exact duplicate reinforce/merge”与“single-cardinality 冲突维护”必须由 Phase 4 的 `MemoryLifecycleService` 显式处理（复用 Resolver 的 single-cardinality 判定 + 现有 `_execute_merge`）。

## 5. MemoryRetriever 是否更新 last_accessed_at

- 召回引擎为 `AutomaticRecallEngine`（`memory/automatic_recall.py`）。所有 route（`_route_exact`/`_route_fts`/`_route_recent_episode`）只**读** `MemoryRecord`，**不写 `last_accessed_at`**（该字段本就不存在）。
- recency 评分 `_recency_score`（:65）使用 `observed_at or updated_at or created_at`。
- 结论：**Phase 4 新增 `last_accessed_at` 列（nullable、无默认、不历史回填）**，由新建 `MemoryAccessTracker` 负责触达（第 9 点）：**只 touch 最终实际注入上下文的 memory id**（非全部召回候选）；批量短事务、最小 touch 间隔、不更新 `record_version`、不触发投影、失败不阻断 Turn。维护冷却基准回退顺序：`last_accessed_at` → `observed_at` → `created_at`。
- **接线点（已实现）**：`ContextAssembler` 在 hard gate 通过、组装 `AssembledContext` 返回前，仅对最终选中并注入的 `pack.items` 调 `MemoryAccessTracker.touch()`；`memory_search` 工具在返回前仅对实际返回的 memory id 调 `touch()`（Agent-Initiated Recall 属“扩大召回/高相关”，传 `include_sleeping=True`，会触达 sleeping → best-effort wake）。两条路径均只 touch 真正进入模型上下文的记录，不 touch 初筛/裁剪掉的候选。

## 6. Core Memory / Projection 当前如何刷新

- 投影表 `core_memory_blocks`（`models.py:602`）。
- `CoreMemoryProjection.refresh(db, memory_id, record_version, config)`（`core_memory_projection.py:123`）：从 `memory_records` 实时 `build_blocks()`（仅取 `Active+Valid` 且 `canonical_key` 在 core_memory_role 内的记录），按 `record_version` 做陈旧跳过，upsert 到投影表。
- 触发入口：`MemoryWriteService._enqueue_projection()`（:821）：当 `record.canonical_key` 属于 core memory key 时，入队 `OutboxJob(job_type="core_memory_refresh", payload={memory_id, record_version})`，由 `handle_core_memory_refresh`（`outbox_handlers.py:291`）消费。
- 结论：Phase 4 维护成功后，**复用上述 `_enqueue_projection` / `core_memory_refresh` 链路**即可刷新 Core Memory，无需新建投影机制。

## 7. Scheduler / OutboxWorker / HandlerRegistry 当前实现

- **SchedulerDaemon**（`worker/scheduler_daemon.py`）：APScheduler `BackgroundScheduler`。已挂载：`_poll_outbox`（Outbox poll）、`_idle_scanner_job`（Phase 3 空闲扫描，30s）、`_enqueue_reconciler_job`（Phase 3 补发，60s）。Phase 4 新增 `_maintenance_scanner_job`（IntervalTrigger，默认 3600s）。
- **OutboxWorker**（`worker/outbox_worker.py`）：`claim_one`（行锁 `FOR UPDATE SKIP LOCKED`）、`enqueue`（**allowlist 校验**，不在 `ENABLED_OUTBOX_JOB_TYPES` 直接 `raise ValueError`）、`poll`、`_dispatch_one`、`_finalize_job` / `_retry_later` / `_retry_or_deadletter` / `_deadletter_job_and_ingestion_run`（均带 fencing）。
- **HandlerRegistry**（`worker/handler_registry.py`）：`register(job_type, handler, supported_schema_versions)`，`get` / `is_schema_supported`。`register_all` 在 `outbox_handlers.py:344`。
- **关键缺口**：`_deadletter_job_and_ingestion_run`（:346）硬编码只处理 `segment_sealing` / `epoch_checkpoint` 的 Run（CompactionRun/CheckpointRun）。Phase 4 的 `memory_maintenance` Run 必须在此处加入 `MemoryMaintenanceRun` 的原子 deadletter（F 节原子契约）。
- `ENABLED_OUTBOX_JOB_TYPES`（`recall_config.py:79`）需追加 `"memory_maintenance"`。

## 8. 是否已有 steward / dream / maintenance 遗留实现

- **有遗留 `MemoryMaintenance`**（`memory/memory_maintenance.py`）：全表扫描 + 生成 proposal，但不执行；API `POST /api/maintenance/memory-scan`（`routes_memories.py:205`）与工具 `run_memory_maintenance`（`tools/builtin_tools.py:502`）调用其 `scan()` 仅返回统计。
- 无 `steward` / `dream` 模块。
- 遗留 `forget/sleep/archive` 方法已标 DEPRECATED 并返回 `{ok:False}`。
- **Phase 4 处理（第 12 点）**：保留 `scan()` 作为只读诊断（继续服务 `/maintenance/memory-scan`）；新增真正执行的 `handle_memory_maintenance` Outbox handler。工具 `run_memory_maintenance` 必须改为**真正 enqueue maintenance OutboxJob**，或重命名为 `scan_memory_maintenance` 保留只读统计；禁止“名字表示执行而实际只返回统计”。维护的确定性执行全部走 Outbox，不依赖遗留 `scan()`。

## 9. 所有直接修改 MemoryRecord.lifecycle_state 的旁路

全仓 `lifecycle_state` 写点：

| 位置 | 方式 | 是否合规 |
|------|------|----------|
| `MemoryStore.update_lifecycle()`（`memory_store.py:93`） | 直接赋值 + `updated_at` | ✅ 设计为唯一 sanctioned mutator，仅 `MemoryWriteService` 调用 |
| `MemoryStore.update_validity()`（`memory_store.py:101`） | 直接赋值 | ✅ 同上 |
| `MemoryWriteService.promote` / `_execute_promote_candidate` | 委托 `MemoryLifecycleService.promote` → executor 加锁 + `record_version+=1` + lineage + 投影 | ✅ 走共享 executor，带 `record_state_hash`/`decision_hash` 校验（单向依赖，不在 WriteService 内直接改 lifecycle） |
| `MemoryWriteService.forget` | `record.lifecycle_state = FORGOTTEN` | ⚠️ 内部，Phase 4 范围之外 |
| `MemoryWriteService.execute_maintenance` | 委托 `MemoryLifecycleService.sleep/archive/wake` → executor | ✅ 单条、带版本校验 + lineage + 投影，消除无版本校验旁路 |
| `MemoryStore.create_record` | 新记录赋初值 | ✅ |
| 其余（`automatic_recall` / `core_memory_projection` / `projection` / `routes_*` / `builtin_tools` / `rhythm_manager`） | 仅 `SELECT` 过滤，无写 | ✅ |

**结论**：除 WriteService 内部外，无业务旁路直接改 `lifecycle_state`。Phase 4 新增共享 `MemoryMutationExecutor`（被 `MemoryWriteService` 与 `MemoryLifecycleService` 单向共用），统一带 `record_version` + `record_state_hash`/`decision_hash` 校验；`execute_maintenance` 与 `promote` 已落地委托 `MemoryLifecycleService` 走 executor，消除无版本校验的旁路（第 7 点单向依赖）。

## 10. 当前可用于 dirty-set 查询的 updated_at / version / index

- `updated_at`：存在，`onupdate=now`（每次写自动刷新）。**可作为 dirty-set 主游标**。
- `record_version`：存在，随每次写 `+= 1`。
- 现有索引：`ix_memory_records_lifecycle_state`（lifecycle_state）、`ix_memory_records_canonical_key`（canonical_key）、`ix_memory_records_scope_type`（scope_type）、`memory_key` 索引。
- **缺失的 dirty-set 复合索引**：
  - `(lifecycle_state, valid_to)` —— 过期 ephemeral 候选（`valid_to<=now AND lifecycle IN (active,candidate)`）。
  - `(lifecycle_state, updated_at)` —— candidate 晋升/过期检查、active 冷却检查的有界窗口。
  - `(canonical_key, scope_type, scope_id)` —— 冲突/重复邻域扩展（已有单列索引，缺复合）。
  - `(lifecycle_state, last_accessed_at)` 或 `(lifecycle_state, observed_at)` —— sleeping 冷却检查。
- `content_hash` / `structured_value_hash` 已存在，供 exact-duplicate 判重。

---

# A. 当前真实模型和调用链

- **写入唯一入口**：`MemoryWriteService`（`memory_write_service.py`）→ `MemoryStore`（持久化）+ `MemoryGate`（准入）+ `ConflictResolver`（冲突）→ `EventLogger`（事件）+ Outbox enqueue（投影）。
- **读取**：`MemoryStore` 提供 `get_active_by_key_scope_locked` / `get_candidates_for_promotion` 等；召回走 `AutomaticRecallEngine`。
- **生命周期现状**：见审计第 3、9 项。所有自动转换（晋升/过期/冷却/唤醒/合并）**目前均不存在**，只有显式 sleep/archive/wake API。
- **遗留维护**：`MemoryMaintenance.scan()` 全表扫描、不执行，属死链路（诊断用）；其阈值现已从 `MaintenanceConfig` 读取（`candidate_ttl_days` / `sleep_cooling_days` / `importance_sleep_threshold`），与 `plan_batch` 决策口径一致，诊断数字不再误导。
- **证据去重（v1）**：独立 evidence 计数用 `COUNT(DISTINCT source_event_id)` 且过滤真实 `user_message` Event；不新增 `source_turn_record_id`（第 8 点）。

# B. 现有生命周期转换

| 转换 | 现状 |
|------|------|
| candidate→active | 仅 `promote()`，无自动触发 |
| candidate→archived（过期） | ❌ 无 |
| active→sleeping（冷却） | 仅 API sleep |
| sleeping→active（唤醒） | `execute_maintenance` wake；**以及 AccessTracker.touch 闭合 J.4 冷却回路**（context_assembler 注入 / `memory_search` 返回触达 sleeping 记忆时 best-effort `MemoryLifecycleService.wake()`） |
| active/sleeping→archived | 仅 API archive |
| expired ephemeral→archived | ❌ 无 |
| exact duplicate merge | ❌ Resolver 不返回 merge |
| forgotten | forget Saga（Phase 4 不做） |

# C. dirty-set 可行性、多 lane 游标 与 high-water 语义

**可行**，依据：
- `updated_at` / `valid_to` / `candidate_deadline`(派生) / `effective_last_accessed_at`(派生) 真实存在或可由现有字段派生 → 作多 lane 游标维度。
- 但需新增复合索引（见审计第 10 项 + K 节 migration）支撑有界扫描。

## C.1 due 判定与 dirty 下界分离（第 1 点）
- daily/idle 是否 **due** 依据 `previous_successful_run.completed_at`（见 D 节）。
- 下一 Run 的 dirty 增量下界是 `previous_successful_run.cutoff_updated_at`（**禁止用 `completed_at` 作 `updated_at` 下界**），否则会漏掉旧 Run 执行期间发生的写入（`updated_at` 介于 `cutoff_updated_at` 与 `completed_at` 之间者会被跳过）。

## C.2 四条独立候选 lane（第 1 点）
**禁止所有任务统一依赖单一 `(updated_at, id)` 游标**。按触发原因拆成四条独立 lane，各用自身时间维 + id 复合游标，互不阻塞：

| lane | 游标时间维 | 选择条件（简式） |
|------|------------|------------------|
| `changed` | `updated_at` | `updated_at <= cutoff_updated_at` 且 dirty 增量下界之上的变更记忆 |
| `expired_ephemeral` | `valid_to` | `valid_to <= now` 且 `lifecycle_state IN (active,candidate)` 且 `retention_policy=ephemeral` |
| `candidate_due` | `candidate_deadline` | `candidate_deadline <= now` 且 `lifecycle_state=candidate`（派生列：`created_at + candidate_ttl_days`，或直接以 `created_at + TTL` 判定） |
| `sleep_due` | `effective_last_accessed_at` | `(last_accessed_at OR observed_at OR created_at) <= now - cooling_days` 且 `lifecycle_state=active/sleeping` |

- **时间到期记录不受 `updated_at` 旧 high-water 限制**：`expired_ephemeral` / `candidate_due` / `sleep_due` 三条 lane 以各自时间维为游标，**即便 `updated_at <= previous_cutoff` 也必须被处理**（典型场景：很久未更新但今天刚到 TTL 的 candidate、今天 `valid_to` 到期的 ephemeral、长期未访问但 importance 偏低需 sleeping 的 active）。`changed` lane 仍受 dirty 增量下界约束。
- 每条 lane 有独立的 `(lane_cursor_time, lane_cursor_id)` 续跑游标，存于 `MemoryMaintenanceBatch`（见 E.3）。

## C.3 seed 与 neighbor（第 4 点）
- Phase A 先按各 lane 的 dirty 下界选出 **seed** 记忆；再对每条 seed 扩展 `相同 canonical_key + scope_type + scope_id` 的 **neighbor**（用 `(canonical_key, scope_type, scope_id)` 复合索引）。seed 与 neighbor 都冻结进 `MemoryMaintenanceInput` 并标注 `input_role = seed | neighbor`。
- **seed 游标只由 seed 推进，neighbor 不推进游标**；复合动作的全部参与记录必须同属一个不可变 Batch 的 Input（见 E.3）。

## C.4 固定 cutoff + 复合游标续跑（第 3 点核心）
- Phase A 固定 `cutoff_updated_at = now`（仅 `changed` lane 用，作其 dirty 上界）。
- 每批从某一 lane 取 `max_maintenance_batch` 条 seed，按该 lane 的 `(lane_cursor_time, lane_cursor_id)` 升序；处理完推进该 lane 游标（仅 seed 推进）。
- **只有所有 lane 的候选均处理完，Run 才标记 `succeeded`**。`max_maintenance_batch` 只限制单批大小，**不得**让未处理的旧记录被下一次 high-water mark 跳过——续跑复用同一 Run 同一 `cutoff_updated_at`，直到穷尽。
- 因此 dirty 下界（下次 Run 的 `cutoff_updated_at`）只在上一 Run 成功完成后推进，不会跨越未处理记录。

- 不使用全表扫描；100/1k/1w/10w 规模下每轮工作量只与各 lane 的 dirty set + 续跑批数成正比。

# D. Daily / Idle trigger

- Scheduler 每 3600s 跑 `_maintenance_scanner_job`（新）。
- `last_successful_maintenance_at`：从 `MemoryMaintenanceRun` 中该 scope 最近 succeeded 的 `completed_at` 推导（避免新增状态表；可按 scope 建少量索引）。
- Daily 条件：`last_successful_maintenance_at <= now-24h` AND 不存在 `pending/running` 的 `memory_maintenance` OutboxJob（同 scope）AND 存在 dirty candidate（用轻量 `EXISTS` 查询，不展开全量）。
- Idle 条件：`threads.last_activity_at <= now - idle_threshold` AND 上次维护后存在 dirty memory（同样 EXISTS 轻量判定）AND 当前 idle window 未执行过（`operation_id` window_bucket 去重）。
- `_has_dirty_memory` 的轻量 EXISTS 判定各 lane 现已具真实筛选意义：
  - `changed` lane：`updated_at > last_successful_maintenance_at`（从未维护过则任何记录均视为 dirty）；`AccessTracker.touch` 不更新 `updated_at`，不会误触发。
  - `sleep_due` lane：非 pinned 的 `active/sleeping` 记录且 `observed_at <= now - sleep_cooling_days`，未达冷却不触发，避免每日空转 Run。
  - `expired_ephemeral` / `candidate_due` 仍按各自时间维游标判定。
- `operation_id = memory_maintenance:all_user_memories:{policy_version}:{window_bucket}`（第 8 点，`maintenance_scope_key = all_user_memories`，**不使用 `scope_id=None` 字符串**）。`UNIQUE(operation_id)` 保证幂等；**`enqueue_maintenance_job` 在调用 `OutboxWorker.enqueue` 之前统一做 `EXISTS` 去重检查（worker 与 fallback 两条路径都走）**，因此生产态（`_outbox_worker` 已注册）同 window_bucket 不会因裸 `db.add` 触发 `UNIQUE` 冲突 `IntegrityError`。无 dirty memory 时不创建空 Run/Job。
- v1 使用一个**全量**维护 Run（`maintenance_scope_key = all_user_memories`）。注意：Run 自身的 `scope_type` 是维护调度标识，**绝不等同于** `MemoryRecord.scope_type=global`（第 6/8 点）。该 Run 覆盖数据库中**所有** MemoryRecord（无论其自身 `scope_type`/`scope_id` 是 project/thread/...）。规划分组仍按每条记忆真实的 `canonical_key + scope_type + scope_id`（见 J 节）。

# E. MemoryMaintenanceRun（新增表）

新增独立表 **`memory_maintenance_runs`**，不复用 `MemoryIngestionRun` / `CompactionRun`：

| 列 | 类型 | 说明 |
|----|------|------|
| id | String(36) PK | |
| scope_type | String(32) | 维护调度标识；P4 v1 固定 `all_user_memories`（**不等于** `MemoryRecord.scope_type=global`，见第 8 点） |
| scope_id | String(128) nullable | P4 v1 为 NULL（不使用字符串占位） |
| outbox_job_id | String(36) FK → outbox_jobs.id | **NOT NULL**，`UNIQUE`（一 Job 一 Run，强外键保证） |
| operation_id | String(128) **NOT NULL** | **UNIQUE**（= OutboxJob.operation_id，幂等键） |
| status | String(32) | running/succeeded/failed_retryable/deadletter |
| execution_token | String(128) nullable | 每次新 claim 重新生成 |
| claim_count | int | 默认 0，每次 claim +1（原 `attempt_count` 语义，仅计接管/认领次数，非失败次数，见第 8 点） |
| failure_attempt_count | int | 默认 0，仅 `RETRYABLE_ERROR` 真正失败重试时 +1；`CONTINUE` 不计（见第 8 点） |
| policy_version | String(32) | 如 "phase4.v1" |
| window_start / window_end | DateTime nullable | 维护窗口边界 |
| cutoff_updated_at | DateTime | 固定为 Phase A 开始时的 now（dirty-set 上界，提交后不可变） |
| cursor_updated_at | DateTime nullable | 续跑游标：已处理区间的上界 `(updated_at, id)` 中的 updated_at |
| cursor_id | String(36) nullable | 续跑游标：同一 updated_at 内的最后处理 id（与 cursor_updated_at 组成复合游标，消除单字符串歧义） |
| plan_hash | String(64) nullable | Phase B 确定性 plan 的 sha256（由 `MemoryMaintenanceInput` 全量输入 + config 派生，可复现） |
| candidate_count / applied_count / skipped_stale_count | int | 统计 |
| error_message | Text nullable | |
| started_at / completed_at / created_at / updated_at | DateTime | |

**唯一约束**：`UNIQUE(outbox_job_id)`、`UNIQUE(operation_id)`。
**接管语义**：同 Phase 3 —— 同一 OutboxJob 重试/重放复用同一 Run，`execution_token` 重新生成、`claim_count+1`；`failure_attempt_count` 仅真正失败重试时 +1（`CONTINUE` 不计）；Phase A 已提交的 `cursor_updated_at`/`cursor_id`/`cutoff_updated_at`/`MemoryMaintenanceInput` 全部不可变，新 Worker 直接续跑。

# E.2 MemoryMaintenanceInput（不可变维护输入，新增表）

Phase A 提交前冻结全部候选快照，使 Phase A 提交后崩溃、新 Worker 能从 DB 恢复**完全相同的输入与 plan**（Phase B 为确定性纯函数，输入固定则 plan 固定）。不复用 Run 的 JSON 列，独立建表保证可索引、可审计：

| 列 | 类型 | 说明 |
|----|------|------|
| id | String(36) PK | |
| run_id | String(36) FK → memory_maintenance_runs.id **NOT NULL** | 所属 Run（经 batch 冗余便于查询） |
| batch_id | String(36) FK → memory_maintenance_batches.id **NOT NULL** | 所属 Batch（第 3 点） |
| input_sequence | int | 候选在 Batch 内的稳定序号（Phase B 重算时按同一排序复现，Batch 内唯一，见第 3 点） |
| memory_record_id | String(36) FK → memory_records.id **NOT NULL** | 候选记忆 id |
| input_role | String(16) | `seed` \| `neighbor`（第 4 点：seed 推进游标，neighbor 不推进） |
| record_version | int | Phase A 冻结时的 record_version |
| record_state_hash | String(64) | 冻结时的记录态哈希（content/structured_value/lifecycle_state/validity_state/confidence/importance/retention_policy/valid_to/pinned/stability/stability_score/reinforce_count/last_reinforced_at 等，**不含访问时间**，见第 6 点） |
| decision_hash | String(64) | 冻结时的决策哈希（在 `record_state_hash` 基础上纳入 `last_accessed_at`/`effective_last_accessed_at` 等访问时间维度，仅 sleep/cooling 类决策依赖；普通 read 不会使 merge/supersede stale，见第 6 点） |
| user_required_protected | bool | 冻结时计算的 user-required 受保护标记（沿 lineage/merge/supersede 继承，见第 7 点） |
| user_required_source_ids | JSON | 触发保护的来源 proposal/lineage id 列表（第 7 点） |
| canonical_key / scope_type / scope_id | 同 MemoryRecord | 冻结时的分组键（规划按此分组） |
| snapshot_json | JSON | 冻结时的关键结构化字段（confidence/importance/retention_policy/valid_to/lifecycle_state/stability/stability_score/pinned/reinforce_count/last_reinforced_at/last_accessed_at 等），供 Phase B 纯函数重算，避免二次读库漂移 |

**唯一约束**：`UNIQUE(batch_id, memory_record_id)`、`UNIQUE(batch_id, input_sequence)`（第 3/10 点：同一记录可在同一长 Run 的**不同 Batch** 中再次作为 neighbor 出现，故唯一约束以 batch 为界）。
**不可变**：写入后不再更新；续跑复用既有 Batch 的 Input，不重建。

# E.3 MemoryMaintenanceBatch（新增表，第 1/3 点）

将“一个 Run 内的有界批次”显式持久化，使 crash 后能恢复**同一未完成 Batch** 而非重选。每条 Batch 只属于一条 lane（第 1 点），游标按 lane 各自推进：

| 列 | 类型 | 说明 |
|----|------|------|
| id | String(36) PK | |
| run_id | String(36) FK → memory_maintenance_runs.id **NOT NULL** | 所属 Run |
| batch_no | int | Run 内批次序号（从 0 递增） |
| candidate_lane | String(32) | 本批所属 lane：`changed` / `expired_ephemeral` / `candidate_due` / `sleep_due`（第 1 点） |
| status | String(32) | frozen / planned / applying / done / deadletter（统一终态，禁止混用 `aborted+abort_reason`，见第 8 点） |
| lane_cursor_time | DateTime nullable | 本批 lane 游标时间维（语义随 `candidate_lane`：`changed`→updated_at；`expired_ephemeral`→valid_to；`candidate_due`→candidate_deadline；`sleep_due`→effective_last_accessed_at） |
| lane_cursor_id | String(36) nullable | 本批 lane 游标 id 维（`(lane_cursor_time, lane_cursor_id)` 复合，消除单字符串歧义） |
| cursor_start_time / cursor_start_id | DateTime / String(36) nullable | 本批 seed 游标起点（语义同上，随 lane） |
| cursor_end_time / cursor_end_id | DateTime / String(36) nullable | 本批 seed 游标终点（推进后回填） |
| input_hash | String(64) nullable | 本批 Input 集合的 sha256（冻结后不可变，用于恢复校验，见第 4 点） |
| plan_hash | String(64) nullable | 本批 plan 的 sha256（由 `input_hash` + canonical actions + `policy_version` 派生，见第 4 点） |
| candidate_count / action_count / applied_count / skipped_stale_count | int | 统计 |
| created_at / planned_at / completed_at | DateTime | |

**唯一约束**：`UNIQUE(run_id, batch_no)`（同 Run 内 batch_no 唯一；不同 lane 各自递增 batch_no）。
**恢复优先级（第 3 点）**：新 Worker 接管时，**优先处理已有 `frozen/planned/applying` 状态的 Batch**；仅当不存在未完成 Batch 时，才按 lane 游标选择下一批（新建 Batch 并冻结 Input）。
**lane 续跑（第 1 点）**：`expired_ephemeral`/`candidate_due`/`sleep_due` 三条 lane 以各自时间维为游标，**即便 `updated_at <= previous_cutoff` 也必须处理**；`changed` lane 受 dirty 增量下界约束。

# F. MemoryMaintenanceAction（新增审计表）

新增 **`memory_maintenance_actions`**：

| 列 | 类型 | 说明 |
|----|------|------|
| id | String(36) PK | |
| run_id | String(36) FK → memory_maintenance_runs.id | |
| batch_id | String(36) FK → memory_maintenance_batches.id **NOT NULL** | 所属 Batch |
| action_sequence | int | 该动作在 Batch plan 中的稳定序号（**不依赖数据库返回顺序**，由确定性排序生成，见第 3/4 点）；与 Input.input_sequence 非一一对应 |
| source_input_ids | JSON | 本 Action 来源的 Input id 列表；**一个 Input 可产生多个 Action**，故为列表而非单值（第 3 点） |
| operation_group_id | String(36) | 复合动作的组 id（见第 4 点）：`sha256(batch_id + action_type + sorted(participant_ids) + policy_version)`，merge/supersede 等涉及多记录的动作共享同一组 id，保证整组原子 skip_stale |
| subject_memory_record_id | String(36) | 主记录（动作直接作用对象） |
| related_record_ids | JSON | 复合动作的其余参与记录 id（如 merge 的被合并方、supersede 的被移除方）；单记录动作为空 |
| canonical_key / scope_type / scope_id | 同 MemoryRecord | 冗余便于审计 |
| action_type | String(32) | promote/reinforce/supersede/archive_expired/archive_candidate/sleep/wake/merge_exact_duplicate/no_op/skip_stale |
| reason_code | String(64) | expired_ephemeral / candidate_promoted / candidate_expired / cooled_to_sleeping / woken / pinned_protected / duplicate_merged / no_change / stale_plan / pinned_conflict 等 |
| expected_record_version | int | 主记录提交时校验的 record_version |
| preconditions | JSON | 复合动作的全部参与记录校验条件：`{memory_record_id: {record_version, record_state_hash, decision_hash}, ...}`；单记录动作只含 subject 一项（见第 6 点，区分记录态哈希与决策哈希） |
| after_hash | String(64) nullable | 提交后内容哈希 |
| status | String(32) | applied / skipped_stale / no_op |
| idempotency_key | String(128) | **UNIQUE**（`run_id:action_sequence:operation_group_id:subject_memory_record_id:action_type:policy_version`，第 3/4 点） |
| details | JSON nullable | 上下文（如匹配到的重复邻域 id） |
| created_at / applied_at | DateTime | |

**唯一约束**：`UNIQUE(idempotency_key)`、`UNIQUE(batch_id, action_sequence)`（第 3 点）。
**一个 Input 可对应多个 Action**（如一条记录同时触发 sleep + 投影刷新，或重复邻域中既是 winner 又产生 merge 动作），`source_input_ids` 显式记录来源，禁止“一个 Input 只产生一个 Action”的隐含假设（第 3 点）。

# G. 确定性维护策略

- **不调用任何新 LLM**。所有动作仅依据结构化字段 + provenance + `KeyPolicy`（`MemoryKeyRegistry`）。
- 语义模糊的跨 `canonical_key` 合并 **禁止**自动执行。
- 每条 Action 携带 `expected_record_version` + `preconditions`（见 I 节），`preconditions` 覆盖复合动作的全部参与记录。
- **pinned 统一保护**（见 J 节）：`effective_pinned = (pinned == true) OR (retention_policy == "pinned")`，任一成立即禁止自动 sleep / archive / merge-loser / supersede removal。
- 阈值（置信度阈值、importance 冷却阈值、candidate TTL、ephemeral 检查、idle 阈值、daily 24h）全部集中在 `RecallConfig`（`recall_config.py`）新增 `MaintenanceConfig` dataclass，可配置、可测试。默认阈值见第 13 点。
- **exact duplicate merge 确定性 winner（第 11 点）**：当多条记录 `content_hash`（`structured_value_hash`）相同构成重复邻域时，winner 选择**不依赖数据库返回顺序**，按固定优先级：
  1. `effective_pinned` 为真者胜（pinned 记录**绝不作 loser**）；
  2. `user_required` 者优先于 `system_best_effort`（含沿 lineage 继承的受保护记录，见 J.3）；
  3. `lifecycle_state` 优先级：`active` > `sleeping` > `candidate`；
  4. `confidence` 高者胜；
  5. `reinforce_count` 高者胜；
  6. `observed_at` 新者胜；
  7. 以上全平 → `id` 字典序最小者胜（稳定 tie-breaker）。
  loser 经 `MemoryLifecycleService`（复用 `ConflictResolver` + `MemoryMutationExecutor`）执行 supersede/merge，并写 lineage/event。
- **pinned 冲突禁止自动 merge（第 7 点）**：若重复邻域中存在**两个及以上 `effective_pinned` 记录**，禁止自动 merge，整体产出 `no_op(reason=pinned_conflict)`，保留全部 pinned 记录，不移除任何一方；仅当至多一个 pinned 时才走 winner/loser 合并。
- **user-required 保护继承（第 7 点）**：merge 的 winner 或 supersede 的 successor 继承来源记录的 `user_required_protected` 与 `user_required_source_ids`（见 J.3）；merge/supersede 涉及的**所有被修改记录**均在动作事务内刷新相应投影（第 12 点）。

# H. 三阶段 Handler（`handle_memory_maintenance`）

复用 Phase 3 三阶段骨架 + `_resolve_phase3_run` 同款 Run 接管逻辑（新增 `_resolve_maintenance_run`）。

**Phase A（短事务）**：
1. 验证 Outbox claim（lease/status/worker_id，复用 `_claim_matches`）。
2. 首次 claim：创建/接管 `MemoryMaintenanceRun`，固定 `cutoff_updated_at = now` 并写入 `outbox_job_id`/`operation_id`（提交后不可变）；续跑 claim 复用既有 Run 与 cutoff，不重新生成。
3. **恢复优先（第 3 点）**：若 Run 已有 `frozen/planned/applying` 状态的 `MemoryMaintenanceBatch`，接管该 Batch；否则按该 lane 的复合游标 `(lane_cursor_time, lane_cursor_id)` + 上界（`changed` lane 用 `cutoff_updated_at`）选本批 bounded **seed**（上限 `max_maintenance_batch`），并扩展每条 seed 的 `canonical_key+scope_type+scope_id` neighbor（见 C 节）；新建 Batch 时写入 `candidate_lane` 与 `cursor_start_*`。
4. 新建（或复用）Batch，冻结 seed+neighbor 快照写入 `MemoryMaintenanceInput`（`batch_id` + `input_role=seed|neighbor` + memory_record_id / record_version / `record_state_hash` / `decision_hash` / 分组键 / snapshot_json + `input_sequence`），置 Batch `status=frozen` 并记 `cursor_start_*`；commit。
5. 若此时崩溃，新 Worker 从既有 Batch + Input 恢复相同 plan（`input_hash`/`plan_hash` 校验）。

**Phase B1（无 DB Session，纯函数）**：
- 输入来源为所属 Batch 的 `MemoryMaintenanceInput`（DB 不可变快照），非实时重读；**此阶段不持有 DB Session，也不写库**。
- 按 `canonical_key + scope_type + scope_id` 分组（见 J 节）；复合动作的全部参与记录必须同属本 Batch Input（见 C 节）。
- 纯确定性生成 `MaintenanceAction` plan（规则见 B 节生命周期 + 审计第 4 项 Resolver 复用）。**Action 排序严格按确定性规则**（分组 → `action_type` 字典序 → `subject_memory_record_id` 字典序）得到 `action_sequence`；计算每个 Action 的 `operation_group_id`（第 4 点）。
- `input_hash = hash(canonical inputs)`、`plan_hash = hash(input_hash + canonical actions + policy_version)`（第 4 点）；**不调用 LLM**；同一 Batch Input 重算得同一 plan（崩溃恢复可持久化相同 ActionPlan，见测试 #54）。

**Phase B2（短事务：fencing/input_hash 校验 + 持久化）**：
- 重新 claim 校验 Outbox fencing + Run `execution_token` + Batch `id`；重算 `input_hash` 与冻结时的 `input_hash` 比对，不一致 → 终止（输入被篡改/漂移）。
- 幂等写入 `MemoryMaintenanceAction`（含 `action_sequence`/`source_input_ids`/`operation_group_id`/`preconditions`/`idempotency_key`）；Batch `status` 由 `frozen → planned`，写 `plan_hash` 与 `planned_at`；commit。
- 若 B1→B2 之间崩溃，新 Worker 在 B2 重新计算并持久化**完全相同**的 ActionPlan（确定性保证）。

**Phase C（分批短事务 + 续跑，只消费已持久化 Action）**：
- 验证 Outbox fencing + Run `execution_token` + Batch `id`；**只处理 `status` 尚无终态的 Action**（已 `applied`/`skipped_stale`/`no_op` 者跳过，不重复 mutation）。
- 按 `action_sequence` 顺序处理每个 Action：复合动作（merge/supersede）先按 `participant_ids` 升序 `SELECT ... FOR UPDATE` 锁定 **全部** 参与记录（subject + related_record_ids，均须在本 Batch Input 内）。
- 校验 `preconditions` 中每个参与记录的 `record_version` + `record_state_hash`/`decision_hash`；**任意不匹配 → 整组 `status=skip_stale`，禁止部分执行**。
- 全部匹配 → 经 `MemoryLifecycleService` 执行动作；**每条成功生命周期动作在「同一事务」内 `enqueue core_memory_refresh`**（第 12 点，不等整 Run 完成）；写 `MaintenanceAction` + `MemoryLineage` + event。
- **Batch finalize 与游标推进原子化（第 5 点）**：当本 Batch **最后一个** Action 完成时，在**同一事务**内：置该 Action 终态 → Batch `status=done` 并写统计与 `completed_at` 与 `cursor_end_*` → Run 游标 `cursor_updated_at`/`cursor_id`（仅 seed 推进）；若所有 lane 的 Batch 均已 done → Run `status=succeeded`。恢复时：若所有 Action 已终态但 Batch 未 done，只补做 finalize（不重复 mutation）。
- **正常分页用 `CONTINUE`，禁止伪装成错误重试（第 2/8 点）**：若本 lane 仍有未处理 seed（被 `max_maintenance_batch` 截断），Phase C 返回 `HandlerOutcome.CONTINUE`（**不是** `RETRYABLE_ERROR`）。Worker 对 `CONTINUE`：将同一 OutboxJob 重新置 `pending`、清除 claim/lease、`next_attempt_at=now`、**不增加** `failure_attempt_count`/retry/error/deadletter 计数；`MemoryMaintenanceRun` 保持 `running`。续跑复用同 Run 同 cutoff 与同未完成 Batch，直至全部 seed 处理完才标记 `succeeded`。`cutoff_updated_at` 在续跑间恒定，避免旧记录被下次 dirty 下界跳过。
- **备选**：若不引入 `CONTINUE`，则 Handler 必须在单次 claim 内完成所有有界批次并设置执行时间上限；不得使用 `RETRYABLE_ERROR` 做正常分页。
- 全部完成后：`MemoryMaintenanceRun→succeeded` + Handler 返回 `COMPLETED`；**Handler 不直接 finalize OutboxJob**（由 OutboxWorker 负责，同 Phase 3）。

# I. 并发与 stale plan（动作相关 precondition hash，第 6 点）

- 每个 Action 携带 `expected_record_version`（主记录 `record_version`）+ `preconditions`：`{memory_record_id: {record_version, record_state_hash, decision_hash}, ...}`，覆盖复合动作全部参与记录。
- **拆分两类哈希（第 6 点）**，禁止把所有字段塞进统一 `before_hash`：
  - `record_state_hash = sha256(canonical(content+structured_value+lifecycle_state+validity_state+confidence+importance+retention_policy+valid_to+pinned+stability+stability_score+reinforce_count+last_reinforced_at))`——**不含访问时间**，merge/supersede 等结构性动作主要依赖它。
  - `decision_hash = sha256(record_state_hash + last_accessed_at + effective_last_accessed_at)`——sleep/cooling 类决策额外依赖访问时间维度；**普通读（仅 touch `last_accessed_at`）不会使 merge/supersede 的 `record_state_hash` 失效**，因此不会令合并/supersede 变 stale（只有 sleep/cooling 计划会因新访问而 stale）。
- **确定性 `operation_group_id` 与哈希（第 4 点）**：
  - `operation_group_id = sha256(batch_id + action_type + sorted(participant_ids) + policy_version)`，`participant_ids` 为动作全部参与记录 id 升序排列；可重复计算、稳定。
  - `input_hash = hash(canonical inputs)`（本 Batch 全部 `MemoryMaintenanceInput` 按 `input_sequence` 规范序列化后 sha256）。
  - `plan_hash = hash(input_hash + canonical actions + policy_version)`（本 Batch 全部 Action 按 `action_sequence` 规范序列化后拼接 `policy_version` 再 sha256）。
  - Action 排序**不依赖数据库返回顺序**：先按 `canonical_key+scope_type+scope_id` 分组，组内按 `action_type` 字典序、再按 `subject_memory_record_id` 字典序确定 `action_sequence`，保证重算一致。
- Phase C 按 `participant_ids` 升序 `SELECT FOR UPDATE` 后逐项比对 `preconditions`：任一记录的 `record_version` 或对应哈希变化 → **整组 `skip_stale`**，不覆盖；下次维护重新规划。
- Run takeover 后旧 `execution_token` 在 Phase C 双重 fencing 失败 → `CLAIM_LOST`，旧 Worker 无法提交。
- Action `idempotency_key` = `run_id:action_sequence:operation_group_id:subject_memory_record_id:action_type:policy_version` → 同 Run 重试幂等（重复写 `applied` 不重复生效；复合动作整组共享 operation_group_id，整体 skip/apply 一致）。
- `MemoryMaintenanceInput` 已在 Phase A 提交且不可变，`input_hash`/`plan_hash` 可完全复现；Phase C 只校验实际记录，不依赖重算。

# J. Core Memory / Projection 接入、Scope 与保护策略

## J.1 Scope 不混淆（第 6 点）
- P4 v1 的 Run `scope_type=all_user_memories`（= `maintenance_scope_key`）表示“一个覆盖全部 MemoryRecord 的维护 Run”，**绝不等同于只处理 `MemoryRecord.scope_type=global` 的记忆**。
- Phase B 规划时，对 dirty 范围内的**每一条** MemoryRecord 按其自身真实的 `canonical_key + scope_type + scope_id` 分组与处理；冲突邻域扩展也用真实 `(canonical_key, scope_type, scope_id)`。
- 因此 project/thread 级别的记忆同样被纳入维护，只是统一由一个 global Run 调度。

## J.2 pinned 统一保护（第 10 点）
- 定义 `effective_pinned = (pinned == true) OR (retention_policy == "pinned")`。
- 任一成立，均**禁止**任何自动生命周期降级：
  - 不得自动 `sleep`；
  - 不得自动 `archive`（含 expired_ephemeral / candidate_expired / cooled_to_sleeping）；
  - merge 中不得作为 loser 被移除；
  - supersede 中不得作为被移除方。
- 该判定在 Phase B 规划时与 Phase C 提交前（preconditions 校验后、执行前）双重检查。

## J.3 user-required 来源、沿 lineage 继承与策略（第 7/11 点）
- `MemoryRecord` 当前**无** `source_proposal_id` 直连；可靠追溯路径为 `JOIN memory_proposals ON memory_proposals.final_memory_id = memory_records.id AND memory_proposals.execution_mode = 'user_required'`（直接 MemoryProposal）。
- **保护沿 lineage 继承（第 7 点）**：`is_user_required_protected(record)` 检查三类来源，任一命中即视为受保护：
  1. 直接 `MemoryProposal`（`execution_mode='user_required'` 且 `final_memory_id=record.id`）；
  2. `MemoryLineage.predecessor_id = record.id` 且对应 successor 源于 user-required proposal；
  3. 作为 merge/supersede 的**来源记录**（被合并/被移除方）且其来源 proposal 为 user-required。
- 任一来源受保护时，**winner/successor 继承保护**：合并后的 winner 或 supersede 的 successor 的 `user_required_protected=true`、`user_required_source_ids` 汇总所有来源 id，冻结进 `MemoryMaintenanceInput`（见 E.2，第 7 点）。
- 仅当上述追溯**命中**时，才认定该记录为 user-required；若未命中，**回退到 normal 策略**，不得臆测。
- **优先级（第 7 点）**，在 `effective_pinned` 之后判定：
  1. `effective_pinned == true` → 禁止任何自动降级（最高优先级，见 J.2）。
  2. `user_required AND retention_policy == ephemeral AND valid_to <= now` → **正常 archived**（显式有效期到期仍按时归档；用户明确设置的临时有效期不可被 90 天保护覆盖，普通 user-required 不覆盖用户自己设置的显式有效期）。
  3. `user_required AND retention_policy == normal` → 应用更长冷却（`user_required_sleep_cooling_days=90`，见第 13 点），**不因普通 candidate TTL / normal cooling 自动 archived**（冷却到期也只可 sleeping，不进 archived）。
  - **例外（确认项）**：`user_required + retention_policy=ephemeral + valid_to` 到期 → 仍然 `archived`；90 天保护只适用于 `user_required + normal`，不能覆盖用户明确设置的临时有效期。`effective_pinned` 才是最高级保护。
- 若后续需更强保证，可新增 `MemoryRecord.source_proposal_id` FK（不在 v1 强制），但当前 JOIN + lineage 追溯已足够可靠。

## J.4 冷却时间回退顺序 与 AccessTracker（第 6/9 点）
- 判定“长期未访问”的基准时间：`last_accessed_at` → `observed_at` → `created_at`（依次回退）。
- `MemoryAccessTracker` 只 touch **实际注入上下文**的 memory id（非全部召回候选）；批量短事务、最小 touch 间隔；**明确不更新 `updated_at`**（第 6 点）：使用专用 SQL `UPDATE memory_records SET last_accessed_at=:ts, updated_at=updated_at WHERE id IN (...)`（显式保持 `updated_at=updated_at`，绕过 ORM `onupdate`），因此 touch **不进入 `updated_at` dirty-set**、**不 bump `record_version`**、**不触发投影**、失败仅记日志不阻断 Turn。
- sleeping 记忆被实际注入上下文时的 wake 闭合（第 5 点）：AccessTracker 命中 sleeping 记忆 → best-effort `MemoryLifecycleService.wake()`；wake **必须** bump `record_version`、更新 `updated_at`、写 lineage/event、并按需刷新投影（与 AccessTracker 的“普通 touch 不 bump”区分：wake 是生命周期动作，单独走 mutation executor）。active 记忆的普通 touch 不 bump version。

## J.5 投影与召回（每动作即时刷新，第 12 点）
- **每条成功生命周期动作在「同一事务」内 `enqueue core_memory_refresh`**（不等待整 Run/多 Batch 完成），复用 `MemoryWriteService._enqueue_projection` 或等价链路；`handle_core_memory_refresh` + `CoreMemoryProjection.refresh` 消费。
- 检索投影：archived 默认排除（召回 route 已限定 `lifecycle_state==active`，天然不召回 archived）；pinned 保持 active 在线；sleeping 不在默认召回，仅在“扩大召回/高相关” route 中按 `lifecycle_state IN (active, sleeping)` 参与（已实现：`AutomaticRecallEngine.recall(request, include_sleeping=False)` 为默认，仅 active；`include_sleeping=True` 时 **exact / fts 两条高相关路由**放宽到 `IN (active, sleeping)`、**episode 兜底路由始终仅 active**，避免陈旧、低价值情节记忆被重新带回上下文）。`memory_search` 工具传 `include_sleeping=True`；`ContextAssembler` 自动召回保持默认 `False`。未来深度历史回溯应新增独立 `deep_recall` 模式，不复用 `include_sleeping`。
- 用户再次确认或显式更新 sleeping 记忆时，`MemoryWriteService` 的 write/reinforce 路径**也必须 wake**（第 5 点），保持与召回注入路径一致。
- 不新建向量库（规范明确）。

# K. Migration

> **revision / down_revision 不得硬编码**：实施时读取真实 Alembic head（`alembic current` / `script.get_revision()`）并据此生成新 revision，`down_revision` 指向当前 head。**migration 内不调用 LLM**。

### K.1 新增表
- `memory_maintenance_runs`（E 节，含 `outbox_job_id`/`cursor_updated_at`/`cursor_id`）。
- `memory_maintenance_batches`（E.3 节，有界批次，含 `batch_no`/`status`/游标/哈希）。
- `memory_maintenance_inputs`（E.2 节，不可变输入快照，含 `batch_id`/`input_role`）。
- `memory_maintenance_actions`（F 节，含 `batch_id`）。

### K.2 MemoryRecord 缺失/补强字段
- **新增 `last_accessed_at`** DateTime **nullable、无默认值、不历史回填**（第 9 点）。由 `MemoryAccessTracker` 在记忆注入上下文时写入；初始为 NULL，冷却回退到 `observed_at`/`created_at`。
- `record_version` / `reinforce_count` 已存在，本文档一律使用真实列名（规范原写 `version` / `reinforcement_count` 系笔误，不做映射）。
- `updated_at` 已存在，无需加。

### K.3 MemoryEvidence（第 8 点：v1 不新增列）
- **v1 不新增 `source_turn_record_id`**。独立 evidence 计数使用 `COUNT(DISTINCT source_event_id)`；仅 `source_event` 确属真实 `user_message` Event 的 evidence 计入自动晋升（需在 evidence 侧关联 Event 类型，或在统计时 JOIN events 表过滤 `event_type='user_message'`）。
- 若后续证明 `source_event_id` 不足以区分独立证据、必须新增列：届时新增 `source_turn_record_id` 并带**真实 FK 到 `TurnRecord`**，同时补齐 Evidence 写入传播链路（create_record / reinforce / proposal→evidence）与对应测试；不允许只加一个无 FK 的 String 列。

### K.4 dirty-set 索引（PostgreSQL，对应四条 lane，第 1 点）
- `ix_memory_records_lifecycle_valid_to` on `(lifecycle_state, valid_to)` —— `expired_ephemeral` lane
- `ix_memory_records_lifecycle_updated` on `(lifecycle_state, updated_at)` —— `changed` lane
- `ix_memory_records_key_scope` on `(canonical_key, scope_type, scope_id)` —— neighbor 扩展
- `ix_memory_records_lifecycle_accessed` on `(lifecycle_state, last_accessed_at)` —— `sleep_due` lane
- `ix_memory_records_lifecycle_created` on `(lifecycle_state, created_at)` —— `candidate_due` lane（deadline 由 `created_at + candidate_ttl_days` 派生）
- `ix_memory_maintenance_runs_outbox_job` on `(outbox_job_id)`（UNIQUE 已隐含，视引擎补单列索引）

### K.5 UNIQUE / FK / CHECK
- `memory_maintenance_runs`：`UNIQUE(outbox_job_id)`、`UNIQUE(operation_id)`；FK `outbox_job_id → outbox_jobs.id`（NOT NULL）；`scope_type` 不强制外键（scope 多态）；`claim_count` / `failure_attempt_count` 见 E 节（第 8 点）。
- `memory_maintenance_batches`：`UNIQUE(run_id, batch_no)`；FK `run_id → memory_maintenance_runs.id`（NOT NULL）；`candidate_lane` / `lane_cursor_time` / `lane_cursor_id` / `cursor_start_*` / `cursor_end_*` 见 E.3（第 1 点）；终态 `deadletter` 见第 8 点。
- `memory_maintenance_inputs`：`UNIQUE(batch_id, memory_record_id)`、`UNIQUE(batch_id, input_sequence)`；FK `batch_id → memory_maintenance_batches.id`（NOT NULL）；FK `run_id → memory_maintenance_runs.id`（NOT NULL）；FK `memory_record_id → memory_records.id`（NOT NULL）；`record_state_hash` / `decision_hash` / `user_required_protected` / `user_required_source_ids` 见 E.2（第 6/7 点）。
- `memory_maintenance_actions`：`UNIQUE(idempotency_key)`、`UNIQUE(batch_id, action_sequence)`；FK `run_id → memory_maintenance_runs.id`（NOT NULL）；FK `batch_id → memory_maintenance_batches.id`（NOT NULL）；FK `subject_memory_record_id → memory_records.id`；`source_input_ids` / `operation_group_id` 见 F（第 3/4 点）。
- `CHECK(lifecycle_state IN ('candidate','active','sleeping','archived','forgotten'))`（防御性，可选）。

### K.6 upgrade / downgrade
- 全部 `create_table` / `add_column`（`_column_exists` 守卫，兼容 SQLite 测试）/ `create_index` / `create_unique_constraint`。
- `downgrade` 反向 `drop_constraint` / `drop_index` / `drop_column` / `drop_table`。
- **无历史数据回填**：`last_accessed_at` 初始 NULL（不回填 `observed_at`）；不回填任何 lifecycle 状态（不自动把旧数据归档/降温，避免破坏性批量变更；维护在后续 daily 运行中按需处理）。

# L. 精确修改文件

| 文件 | 修改 |
|------|------|
| `db/models.py` | 新增 `MemoryMaintenanceRun`（含 `outbox_job_id`/`cursor_updated_at`/`cursor_id`/`claim_count`/`failure_attempt_count`）+ `MemoryMaintenanceBatch`（含 `batch_no`/`candidate_lane`/`lane_cursor_*`/游标/哈希，`UNIQUE(run_id,batch_no)`）+ `MemoryMaintenanceAction`（含 `batch_id`/`action_sequence`/`source_input_ids`/`operation_group_id`）+ `MemoryMaintenanceInput`（含 `batch_id`/`input_role`/`input_sequence`/`record_state_hash`/`decision_hash`/`user_required_protected`/`user_required_source_ids`，`UNIQUE(batch_id,memory_record_id)`/`UNIQUE(batch_id,input_sequence)`）；`MemoryRecord` 加 `last_accessed_at`（nullable 无默认）；**不**改 `MemoryEvidence`（v1 不加 `source_turn_record_id`） |
| `alembic/versions/<自动生成的 revision>.py`（新建） | 读取真实 head 后生成；E/E.2/E.3/F/K 节全部迁移（含四条 lane 索引、`input_sequence`/`action_sequence`/`source_input_ids`/`candidate_lane`/`lane_cursor_*`/`record_state_hash`/`decision_hash`/`user_required_protected` 等），**无回填** |
| `memory/recall_config.py` | 新增 `MaintenanceConfig`（阈值见第 13 点：`candidate_promotion_confidence=0.7`、`candidate_promotion_min_evidence`、`candidate_ttl_days=30`、`importance_sleep_threshold=0.3`、`sleep_cooling_days=30`、`user_required_sleep_cooling_days=90`、`daily_interval_hours=24`、`maintenance_scan_interval_seconds=3600`、`idle_threshold_seconds` 复用、`max_maintenance_batch=200`、`min_access_touch_interval_seconds`）；`ENABLED_OUTBOX_JOB_TYPES` 追加 `"memory_maintenance"` |
| `memory/memory_mutation.py`（新建，**共享 mutation executor**） | 抽离底层“锁 + record_version 校验 + record_state_hash/decision_hash 校验 + 写 lineage + event + 投影 enqueue”为独立组件，**单向被** `MemoryWriteService` 与 `MemoryLifecycleService` 共用；不含业务策略 |
| `memory/memory_lifecycle_service.py`（新建） | 依赖 `MemoryMutationExecutor` + `MemoryStore` + `EventLogger` + `ProjectionEnqueuer` + `ConflictResolver`；提供 `promote/sleep/wake/archive/reinforce/merge_exact_duplicate/supersede`，全部带 `preconditions` 校验、复合动作按 id 升序锁全参与记录；`wake` 必须 bump `record_version`、更新 `updated_at`、写 lineage、刷新投影（第 5 点）；exact duplicate merge 用确定性 winner 选择（第 11 点）；每条成功动作在事务内 `enqueue core_memory_refresh`（第 12 点）；**不得反向依赖 `MemoryWriteService`**（第 7 点）。**审计说明**：维护后台无会话线程，而 `events.thread_id` 为指向 `threads.id` 的非空外键，故维护动作**不写线程作用域的 `events` 表**，生命周期审计由 `MemoryLineage` + `MemoryMaintenanceAction`（含 `status`/`reason_code`/`after_hash`）承担；仅当调用方显式提供有效 thread 时才写事件表 |
| `memory/memory_write_service.py` | `execute_maintenance` 与 `promote` 改走 `MemoryMutationExecutor`（与 LifecycleService 共享）；write/reinforce 路径遇 sleeping 记忆须调用 `MemoryLifecycleService.wake`（第 5 点）；reinforce 去重仅用 `source_event_id`（v1 不加 `source_turn_record_id`）；**不**新增 `_touch_last_accessed`（改由 `MemoryAccessTracker` 负责） |
| `memory/automatic_recall.py` | 召回结果返回**实际注入上下文**的 memory id 列表（route 内收集，非全部候选）；新增 `include_sleeping` 选项；命中 sleeping 记忆时 best-effort 触发 wake（见 J.4） |
| `memory/memory_access_tracker.py`（新建，第 6/9 点） | `MemoryAccessTracker`：仅 touch 实际注入上下文的 memory；批量短事务；最小 touch 间隔；使用专用 SQL `SET last_accessed_at=:ts, updated_at=updated_at`（**显式保持 `updated_at` 不变**，绕过 ORM onupdate）；**不** bump `record_version`、不触发投影、不进入 `updated_at` dirty-set；touch 失败仅记日志不阻断 Turn |
| `memory/memory_maintenance.py`（保留） | `scan()` 继续作为只读诊断；plan 生成放到新模块 `memory/memory_maintenance_planner.py`（新建，纯函数、无 DB、无 LLM，输入为某 Batch 的 `MemoryMaintenanceInput` 快照） |
| `worker/outbox_handlers.py` | 新增 `handle_memory_maintenance(claimed)`（三阶段，复用 `_resolve_phase3_run` 改 `_resolve_maintenance_run`）；正常分页返回 `HandlerOutcome.CONTINUE`（第 2 点）；`register_all` 注册 v1 |
| `worker/outbox_worker.py` | 新增 `HandlerOutcome.CONTINUE` 处理：同 Job 重新 `pending`、清 claim/lease、`next_attempt_at=now`、不增 retry/error/deadletter 计数、Run 保持 running；`_deadletter_job_and_ingestion_run` 加入 `MemoryMaintenanceRun` + `MemoryMaintenanceBatch` 原子 deadletter（F 节原子契约） |
| `worker/scheduler_daemon.py` | 新增 `_maintenance_scanner_job`（3600s）+ Daily/Idle 判定（due 用 `completed_at`，dirty 下界用 `cutoff_updated_at`，见第 1 点）+ enqueue；`operation_id` 用 `memory_maintenance:all_user_memories:{policy_version}:{window_bucket}`（第 8 点）；`start_daemon` 注册 |
| `memory/memory_store.py` | 新增有界 dirty-set 查询（选 seed：复合游标 `(updated_at, id)` + cutoff；扩展 neighbor：`(canonical_key, scope_type, scope_id)`）、Batch 选择（优先未完成 Batch）、`COUNT(DISTINCT source_event_id)` 且过滤真实 `user_message` Event 的独立 evidence 统计 |
| `tools/builtin_tools.py` | `run_memory_maintenance` 工具改为**真正 enqueue maintenance OutboxJob**（确定性执行入口）；只读诊断继续由 `scan()` + API `/maintenance/memory-scan`（或另提供 `scan_memory_maintenance`）承担；禁止“名为 run 却只返回统计”（确认项：保留 scan 只读、run 真正 enqueue） |
| `tests/` | 见 M 节测试矩阵 |
| `docs/architecture/Phase_4.md` | 本文件 |

> 不修改：`memory_gate.py`（维护不重新过 Gate）、`conflict_resolver.py`（复用其 single-cardinality 判定，不重写）、Phase 3 compaction、`context_assembler.py`（除非 sleeping 召回需接入，按 J 节最小改动）。

# M. 测试矩阵（至少覆盖 28 项）

| 编号 | 测试 | 类型 |
|------|------|------|
| 1 | 24h due 触发：超过 24h 且有 dirty → enqueue maintenance OutboxJob | 单元/集成 |
| 2 | idle 触发：last_activity_at 超阈值且维护后有 dirty → enqueue | 集成 |
| 3 | 无 dirty memory 不创建空任务（不建 Run/Job） | 单元 |
| 4 | operation_id 幂等：重复 enqueue 只产生一个 Job（UNIQUE 生效） | 集成 |
| 5 | Outbox claim / takeover / fencing：旧 token 无法提交 | 集成 |
| 6 | expired ephemeral（`retention_policy=ephemeral AND valid_to<=now`）→ archived，`reason_code=expired_ephemeral` | 单元 |
| 7 | pinned 不被降级：active pinned 冷却检查跳过，仍 active | 单元 |
| 8 | user-required 优先级：user_required+normal 用 90 天 cooling 且不因普通 TTL/cooling 自动 archived；user_required+ephemeral 到期仍正常 archived（不覆盖用户显式有效期） | 单元 |
| 9 | candidate 多份**独立** evidence（不同 source_event_id）→ active | 单元 |
| 10 | 重复同一 `source_event_id`（或同 Event 非 `user_message` 类型）不计为独立 evidence | 单元 |
| 11 | stale candidate（超过 TTL 无新 evidence） → archived | 单元 |
| 12 | low-value inactive normal（`importance<阈值` 且长期未访问且非 pinned）→ sleeping | 单元 |
| 13 | sleeping 被召回/确认/用户更新 → active（wake） | 单元 |
| 14 | exact duplicate reinforce/merge（`content_hash` 相同邻域） | 单元 |
| 15 | single-cardinality 使用现有 ConflictResolver（latest_value_wins → supersede） | 单元 |
| 16 | 维护期间用户更新 → Phase C `skip_stale` | 集成 |
| 17 | `expected_record_version`/`preconditions` 生效：不匹配不覆盖 | 单元 |
| 18 | Action `idempotency_key` UNIQUE：重试同 Run 幂等 | 集成 |
| 19 | Run takeover 后旧 `execution_token` 无法提交 | 集成 |
| 20 | deadletter 时 Run + OutboxJob 原子终态（旧 claim 不得 deadletter 新 Run） | 集成 |
| 21 | Core Memory / projection refresh 入队（core_memory_refresh Job 产生） | 集成 |
| 22 | archived 不进入默认召回（`AutomaticRecallEngine` 默认不含 archived） | 集成 |
| 23 | sleeping 在 `include_sleeping=True` 时可被找到 | 集成 |
| 24 | pinned 始终可召回（active 在线） | 集成 |
| 25 | 100/1k/1w/10w 记忆下 bounded dirty-set（断言扫描行数只与 dirty 成正比，非全表） | 性能/集成 |
| 26 | 不发生物理删除（维护后 archived 记录仍存在，内容未 scrub） | 单元 |
| 27 | Phase 0.5A~P3 全量回归（现有 83 测试不退化） | 全量 |
| 28 | PostgreSQL migration `upgrade head && downgrade -1` 成功（revision 读取真实 head） | CI |
| 29 | Phase A 提交后崩溃，新 Worker 从 `MemoryMaintenanceInput` + cutoff 恢复相同 plan（plan_hash 一致） | 集成 |
| 30 | `max_maintenance_batch` 截断后 Run 不标记 succeeded，续跑复用同 cutoff 处理完剩余 dirty，无记录被 high-water 跳过 | 集成 |
| 31 | 复合动作（merge/supersede）按 id 升序 `SELECT FOR UPDATE` 锁全部参与记录 | 集成 |
| 32 | 复合动作中任一参与记录 stale → 整组 `skip_stale`，无部分执行 | 集成 |
| 33 | `MemoryAccessTracker` 只 touch 实际注入上下文的 memory；不更新 `record_version`；不触发 projection；间隔内去重；失败不阻断 Turn | 单元 |
| 34 | `MemoryAccessTracker` 专用 SQL 触达后：更新 `last_accessed_at`、**不更新 `updated_at`**、不增 `record_version`、不触发 projection、**不进入 `updated_at` dirty-set**（第 6 点） | 单元 |
| 35 | 全局 Run 覆盖 project/thread 级 MemoryRecord（非只 global scope） | 集成 |
| 36 | pinned 两种形态均受保护：`pinned=true` 与 `retention_policy='pinned'` 均不 sleep/archive/merge-loser | 单元 |
| 37 | user-required 通过 `JOIN memory_proposals.final_memory_id` 可靠追溯（ephemeral 到期仍 archived，normal 用 90 天 cooling） | 集成 |
| 38 | user-required 无法追溯时回退 normal cooling，不臆测 | 单元 |
| 39 | `run_memory_maintenance` 工具改为真正 enqueue Job（或重命名为 scan 只读） | 集成 |
| 40 | `MemoryWriteService` 与 `MemoryLifecycleService` 单向依赖、无循环（共享 `MemoryMutationExecutor`） | 单元 |
| 41 | **dirty 下界正确**：上一 Run 执行期间（`cutoff_updated_at` 与 `completed_at` 之间）发生的写入，不会被 `completed_at` high-water 漏掉（第 1 点） | 集成 |
| 42 | `CONTINUE` 不增加 `retry_count`/error/deadletter 计数，Run 保持 running，Job 重新 pending、清 claim、`next_attempt_at=now`（第 2 点） | 集成 |
| 43 | crash 后新 Worker 恢复**同一未完成 Batch**（frozen/planned/applying）继续，而非重选下一批（第 3 点） | 集成 |
| 44 | neighbor 被冻结（`input_role=neighbor`）但不推进 seed 游标；仅 seed 推进 `cursor_updated_at`/`cursor_id`（第 4 点） | 单元 |
| 45 | sleeping 记忆实际注入上下文 → touch + best-effort `wake`（bump `record_version`、更新 `updated_at`、写 lineage/event、刷新投影）；active 普通 touch 不 bump version（第 5 点） | 集成 |
| 46 | `MemoryAccessTracker` 不更新 `updated_at`（同 #34 扩展断言）且不进入 dirty-set（第 6 点） | 单元 |
| 47 | user-required+ephemeral 到期仍 archived；user-required+normal 不因普通 TTL 自动 archived（第 7 点） | 单元 |
| 48 | `operation_id = memory_maintenance:all_user_memories:{policy_version}:{window_bucket}` 稳定幂等，Run `scope_type=all_user_memories` 不与 `MemoryRecord.scope_type=global` 混淆（第 8 点） | 集成 |
| 49 | 同一记录在**不同 Batch** 中可再次作为 neighbor 出现（`UNIQUE(batch_id, memory_record_id)` 允许，第 10 点） | 单元 |
| 50 | exact duplicate merge winner 选择确定（effective_pinned > user_required > lifecycle > confidence > reinforce_count > observed_at > id），pinned 绝不作 loser，不依赖 DB 返回顺序（第 11 点） | 单元 |
| 51 | 每条成功生命周期动作在「同一事务」内立即 `enqueue core_memory_refresh`，不待整 Run/多 Batch 完成（第 12 点） | 集成 |
| 52 | 很久未更新但今天刚到 TTL 的 candidate 被 `candidate_due` lane 选中（不受 `updated_at` 旧 high-water 限制，第 1 点） | 单元 |
| 53 | expired ephemeral 不受 `changed` high-water 限制，由 `expired_ephemeral` lane 按 `valid_to` 选中（第 1 点） | 单元 |
| 54 | Phase B 计算后崩溃可恢复并持久化**相同** ActionPlan（`plan_hash`/`input_hash` 一致，B2 重算相同，第 2/4 点） | 集成 |
| 55 | 一个 Input 可产生多个稳定 Action（`source_input_ids` 多对一，非一一对应，第 3 点） | 单元 |
| 56 | `operation_group_id` 与 `plan_hash` 可重复计算、稳定（`sha256(batch_id+action_type+sorted(participant_ids)+policy_version)`，第 4 点） | 单元 |
| 57 | Action 已完成但游标未推进时，恢复只补 Batch finalize（不重复 mutation，第 5 点） | 集成 |
| 58 | 普通访问（仅 touch `last_accessed_at`）不会使 merge/supersede 变 stale（`record_state_hash` 不变，第 6 点） | 单元 |
| 59 | sleep/cooling 计划会因新访问（`decision_hash` 变化）而 stale（第 6 点） | 单元 |
| 60 | user-required 保护经 lineage/merge 继承（直接 proposal / predecessor lineage / merge 来源记录任一命中即继承，第 7 点） | 集成 |
| 61 | 重复组中存在两个及以上 pinned 记录 → 不自动 merge，`no_op(reason=pinned_conflict)`（第 7 点） | 单元 |
| 62 | `CONTINUE` 不计 `failure_attempt_count`/retry/error/deadletter，Run 保持 running（第 8 点） | 集成 |
| 63 | Batch deadletter 终态一致（`status=deadletter`，不混用 `aborted+abort_reason`，第 8 点） | 集成 |

> 测试用例命名沿用 `tests/test_phase4_*.py`，fixture 复用 `conftest.py` 的 `SessionLocal` + SQLite/PG 切换。

# N. 回滚方案

- Phase A 失败：Run 保持 running，Outbox retry；达 max_retries → Worker 原子 deadletter（OutboxJob + MemoryMaintenanceRun 同事务），旧 claim 不得 deadletter 新 Run。
- **Batch deadletter（第 8 点）**：Batch 进入终态失败（如 B2 持久化超限、Phase C 整批不可恢复）时统一置 `status=deadletter`（不得混用 `aborted+abort_reason`），与 Run deadletter 终态语义一致；恢复时不复用 `deadletter` Batch，仅复用 `frozen/planned/applying`。
- **Batch finalize 恢复（第 5 点）**：若本 Batch 所有 Action 已终态但 `status != done`，仅补做 finalize（置 done + 统计 + cursor_end_* + 推进 Run 游标），不重复 mutation；若仍有非终态 Action，只执行非终态 Action。
- Phase C 中单条 Action `skip_stale`：仅跳过该条，不回滚整 Run；其余 Action 继续。
- 维护 crash（Run running 中进程挂）：Outbox lease 过期后由其他 Worker 接管，新 `execution_token`、旧 Action 因 `idempotency_key` 幂等不重复生效；B1→B2 崩溃由 B2 重算相同 ActionPlan 恢复（第 2/4 点）。
- 不修改已写入数据：所有动作可逆至 archived/sleeping（非物理删除），`forgotten` 仍走既有 forget（Phase 4 不引入）。
- Migration `downgrade` 删除四新表（runs/batches/inputs/actions）+ 一新增列（`last_accessed_at`）+ 新索引。

> **补充迁移 `p4b2c3d4e5f6`**：为 `threads` 表新增 `last_activity_at` 列（nullable）。Idle Scanner（Phase 3）与维护 Idle 触发（Phase 4，第 8 点）依赖该列判定空闲线程；原先该列缺失导致 idle 分支查询在 SQLite 编译期失败（被异常吞掉），idle 触发形同虚设。该补充迁移独立于 Phase 4 主迁移，其 `downgrade` 仅删除 `threads.last_activity_at`。

---

# 待确认的关键决策（已全部确认，进入实施）

1. **`last_accessed_at` 触达方式**：✅ 同意。按此设计实施：nullable、无默认、不回填；只 touch 实际注入上下文的记忆；批量短事务；普通 touch 不增加 record_version、不触发投影、失败不阻断 Turn；touch 不能意外更新 `updated_at`（专用 SQL `SET last_accessed_at=:ts, updated_at=updated_at`）；sleeping 记忆真正被使用后要另走 `wake` 生命周期动作（第 6 点）。
2. **维护作用域（scope）**：✅ 同意。`maintenance_scope_key = all_user_memories`，`operation_id = memory_maintenance:all_user_memories:{policy_version}:{window_bucket}`；覆盖 global/project/thread 等全部 MemoryRecord；规划与冲突处理严格按 `canonical_key + scope_type + scope_id` 分组；维护作用域**不**与 `MemoryRecord.scope_type="global"` 混淆。
3. **`MemoryMaintenance.scan()` 去留**：✅ 同意保留为只读诊断；`run_memory_maintenance` 改为真正 enqueue `memory_maintenance` OutboxJob；只读扫描继续由 `/maintenance/memory-scan` 承担，或另提供 `scan_memory_maintenance`，不能让名为 run 的工具只返回统计。
4. **阈值缺省值（第 13 点）**：✅ 同意采用：candidate promotion confidence = 0.7、`candidate TTL = 30 days`、`importance sleep threshold = 0.3`、`normal cooling = 30 days`、`user_required cooling = 90 days`。**优先级例外（已确认）**：`user_required + retention_policy=ephemeral + valid_to` 到期 → 仍然 `archived`；90 天保护只适用于 `user_required + normal`，不能覆盖用户明确设置的临时有效期；`effective_pinned` 才是最高级保护。

> 以上确认后进入实施（先 migration → 模型 → 服务 → handler → scheduler → 测试）。实施允许直接编码，无需再提交完整方案。
