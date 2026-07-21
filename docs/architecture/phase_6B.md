# AIive Phase 6B：数据保留治理、generation 清理、真空回收与长期压力/混沌测试

> 本文件为 **Phase 6B 实施方案**（拆分自 `phase_6.md`，依赖 `phase_6A.md` 的 Forget Saga / Shield / Verifier）。
> 范围：数据保留治理 + 派生投影 generation 清理 + 数据库物理空间回收 + 长期压力与混沌测试 + 6B 测试矩阵。
> **本次不修改任何代码、不生成 migration、不执行 purge、不创建测试数据。**
> 所有保留/清理策略须遵守核心原则：**用户源数据不因普通保留策略物理删除；只有明确 forget 才删用户内容。**

---

## 0. Phase 6B 在 Phase 6 中的定位

Phase 6A 解决"忘得对、忘得彻底、可验证"。Phase 6B 收口**长期运行中的派生数据增长与物理空间回收**，并构建**可重复的长期模拟**验证 Saga 在规模/时间/崩溃下的不变量。

Phase 6B 不新增 Forget Saga 语义，但消费 6A 的产物：
- `forget_operations` / `forget_batches` / `forget_actions` 的终态记录用于 retention 清理；
- `ForgetTombstone` 的最小审计行长期保留；
- `ForgetShield` 的 active 行普通 retention 不得删除（见 Q.8）；
- `content_provenance_refs`（§Q.5/Q.8）用于按源定位 legacy 副本做 coarse purge 与删除前依赖校验。

---

### 审计：当前项目真实状态（Phase 6B 相关表/字段核对）

> 以下字段来源为 `backend/aiive/db/models.py` 与 `backend/aiive/db/forget_models.py` 的真实 ORM 定义，**非 6A 文档推断**。Phase 6B 方案中所有引用的字段名均以此为准，与原始方案不符的已在本次修订中纠正。

#### OutboxJob（models.py:352-385）
- `id`, `operation_id` String(128) **UNIQUE**（幂等键）, `job_type` String(64)
- `status` String(32), `payload` JSON, `error_message` Text（非 ≤500 字符）
- `terminal_reason` String(64) — **自然的最小错误码字段**，deadletter/non-retryable 时写入
- `claim_token` String(36), `lease_expires_at` DateTime, `locked_by` String(64), `retry_count`, `max_retries`
- `created_at`, `updated_at`（有 `onupdate=_utcnow`）
- ⚠ **没有 `terminal_at` 列**（原始 6B 方案误称 `completed_at`）。建议 6B migration 新增 `terminal_at` DateTime nullable，**原子写入一次**：所有 `completed` / `deadletter` / non-retryable 终态路径在翻转终态时写入 `terminal_at`；`claim` / `retry` / `payload` scrub **不得**更新 `terminal_at`（保证 retention cutoff 不被中间操作重置）。`terminal_at` 落地前暂以 `updated_at` 为 cutoff 近似，但 `updated_at` 会因 claim/retry/scrub 变化，仅作过渡。

#### CompactionInput（models.py:835-866）
- `turn_manifest` **list[JSON]**：完整清单，每条含 `turn_id` / `turn_sequence` / `turn_record_id` / `status`，**不含用户明文**
- `event_manifest` **list[JSON]**：完整清单，每条含 `event_id` / `turn_record_id` / `turn_event_index` / `event_type` / `content_hash`。其中 `content_hash = SHA-256(event_type + payload)`（compaction.py:47-51），**不含 payload 明文**
- `working_state_snapshot` **dict[JSON]**：含 open_loops / active_constraints 等用户正文快照 → 可按期 scrub
- `source_hash` String(64) **NOT NULL**（整体源哈希）
- `summary_version` int, `start_turn_sequence` / `end_turn_sequence`（turn/event 顺序）
- ⚠ **没有名为 `content_hash` 的顶层列**。原始方案中的 "content hash" 实际对应：event_manifest 内的逐条 `content_hash` + 表级的 `source_hash`。
- ⚠ `turn_manifest` / `event_manifest` 是**列表**（清单），非单值哈希。但每条仅含 ID + 状态 + 哈希，不含 payload 明文，因此可安全长期保留。DEEP 校验（raw_history_expander.py:11-12）逐条重算 Event 的 content_hash 并与 manifest 中的哈希比对，依赖的正是 manifest 中的哈希而非原文。

#### EpochCompactionInput（models.py:913-942）
- **可按期 scrub**（用户正文，独立列）：`current_objective` Text, `open_loops` list[JSON], `active_constraints` list[JSON], `artifact_refs` list[JSON], `verified_tool_states` list[JSON]
- **长期保留**（hash-only）：`source_segment_ids` list[JSON], `source_hashes` list[JSON], `snapshot_hash` String(64) NOT NULL, `checkpoint_version` int, `boundary_turn_sequence`, `epoch_id`
- ⚠ 与 `CompactionInput` 不同，**不设** `working_state_snapshot` 嵌套字段——正文分散在 5 个独立列中。

#### RetrievalIndexGeneration（models.py:1181-1206）
- `status` CHECK IN ('building','active','retired','failed') — **仅 retired/failed 可清理**
- `index_version` BigInteger UNIQUE
- `build_completed_at` DateTime nullable（retired generation 可用作触发字段，**但可能为 NULL**）
- ⚠ **failed generation 不得依赖可能为空的 `build_completed_at` 计算保留期**。建议 6B migration 新增真实且不可变的终态时间之一：`status_changed_at` DateTime nullable（任何 status 翻转原子写入），或 `retired_at` / `failed_at` DateTime nullable（仅进入 retired/failed 终态时原子写入一次）。retention cutoff 一律使用真实终态时间，不回退到可能为空的 `build_completed_at`。
- `policy_version` String(32) default "phase5.v1" — **版本化配置的既有先例**（§Q.2 的设计依据）

#### RetrievalIndexToken（models.py:1271-1294）
- `entry_id` FK `ondelete="CASCADE"`（删 Entry 时级联删 Token）
- ⚠ CASCADE 已存在。Q.7 的分批流程（先显式删 Token 再删 Entry）目的不是绕过 CASCADE，而是**控制每批行数、可校验、可续跑**——避免单条 `DELETE FROM entries WHERE index_version=...` 触发整 generation 的巨额级联。

#### MemoryMaintenanceInput（models.py:1083-1115）
- `snapshot_json` dict[JSON] nullable（含 MemoryRecord.content 等用户正文，来源 `_freeze_input`） → 可按期 scrub
- `record_state_hash` String(64), `decision_hash` String(64)（哈希，长期安全）

#### ForgetOperation（forget_models.py:58-85）
- `status` CHECK IN (requested, shielded, cascading, verifying, purge_ready, purging, purged, failed_retryable, deadletter, shielded_deadletter)
- `purged_at` DateTime nullable → 终态 "purged" 的审计时间（Q.8 的保留期触发依据）

#### ForgetStageRun（forget_models.py:349-374）
- `outbox_job_id` FK **UNIQUE**, `execution_token` String(64), `claim_count` int, `failure_count` int
- ⚠ `RetentionCleanupRun` 的字段组（`outbox_job_id UNIQUE + execution_token + claim_count + failure_count`）**镜像了这一既有模式**，非凭空设计。

#### ForgetShield / ForgetTombstone / ForgetTarget / ForgetDependency / ForgetBatch / ForgetAction / ContentProvenanceRef
- 均已在 Phase 6A 定义，具体字段见 `db/forget_models.py`。Phase 6B 不修改它们的 schema，仅在 Q.8 中定义保留策略。
- `ForgetTombstone.content_purged` Boolean default False — Selector Shield 删除条件（Q.8）依赖此字段判断内容是否已不可逆清除。

#### operational JSON 内容安全（修订点 5，需审计）
以下字段在 6A/6B 中**未显式约束只含元数据**，不得未经验证地声明"只含元数据"：
- `MemoryMaintenanceAction.details`（dict[JSON]）— 可能含 MemoryRecord 引用、字段级前后值；
- `MemoryMaintenanceAction.preconditions`（dict[JSON]）— 可能含 Memory 状态快照；
- `ForgetAction.details`（dict[JSON]）— 可能含 target 内容摘要；
- `RetentionCleanupRun` / `RetentionCleanupBatch` 的 error details（诊断 JSON）— 可能含被处理行的 payload 片段。

Phase 6B 必须二选一落地（不得留白，见 Q.3 说明）：
- **方案 A（推荐 v1）**：上述字段统一经 DTO 序列化，只允许写入 ID / hash / status / 数值 / 类型，拒绝任何用户正文（落库前即不含正文）；
- **方案 B（scrub lane）**：纳入 retention scrub lane，按保留期递归清除其中用户正文，保留 ID / hash / status / 数值。

#### 6B 新增 vs 复用总结

| 类别 | 对象 | 状态 |
|----|----|----|
| 全新表 | `RetentionCleanupRun` / `RetentionCleanupBatch` | Phase 6B 新增（镜像 ForgetStageRun 模式） |
| 全新配置 | `RetentionPolicyConfig` | v1 代码内版本化对象，不建表（镜像 Generation.policy_version 先例） |
| 全新机制 | `MaintenanceLease` / advisory lock | 用于 R.4 重任务互斥 |
| 修改已有表 | `OutboxJob.completed_at` | **当前不存在**，建议 6B migration 新增 DateTime nullable 列 |

---

本次修订重点（相对初版）：
1. CompactionInput 拆为"可按期 scrub 正文"与"长期保留 manifest"，不得整体删除；
2. OutboxJob 两阶段 retention（先 scrub 后删最小行），废弃含糊"归档"说法；
3. Retention 续跑改用 `RetentionCleanupRun`/`RetentionCleanupBatch`（lane + 复合游标，禁止 offset）；
4. 每类清理新增删除前安全谓词，扫描时与删除前均回源校验；
5. Retrieval generation 必须分批清理（Token→Entry→确认 0→Generation），不依赖大规模 CASCADE；
6. 数据库空间维护使用独立执行器（PostgreSQL AUTOCOMMIT + advisory lock；SQLite WAL/VACUUM 分开与阈值）；
7. 补齐 Phase 6A 表（Shield/Tombstone/Operation/Action/Batch/Dependency/Target）的保留策略；
8. 压力测试实现为确定性分级体系（可注入 Clock、固定 seed、benchmark JSON、分级执行）；
9. v1 `RetentionPolicy` 优先使用版本化配置对象，每个 Run 冻结 `policy_snapshot`；
10. scrub 与 delete 分阶段（如 OutboxJob 30 天 scrub / 180 天删行）；
11. 日志保留独立治理（logrotate/容器，不由 DB retention handler 管理，先禁止打印完整敏感内容）；
12. 重维护任务互斥（retrieval rebuild / 大规模 forget purge / generation cleanup / SQLite VACUUM / PostgreSQL REINDEX）。

---

## Q. Retention 清理策略（派生数据增长治理）

### Q.0 原则
- **用户源数据不因普通保留策略物理删除**（`events`/`turn_records`/`memory_records` 等 SOT 行，除非明确 forget 且走 6A Phase F 的不可逆 scrub/安全删）；
- **hash-only manifest 长期保留**：`compaction_inputs` 的 `turn_manifest` / `event_manifest`（ID+哈希清单，不含明文）、`source_hash`、`start/end_turn_sequence`、`summary_version` 及 `epoch_compaction_inputs` 的 `source_hashes` / `snapshot_hash` / `checkpoint_version` 等不可变编排元数据不得按保留期整体删除（见 Q.6）；
- 派生投影旧版本可按保留期清理；
- 已完成 operational job 采用**两阶段 retention**（先 scrub 敏感内容，再在确认无依赖后清结构，见 Q.3 / Q.7）；
- **deadletter 保留更久**；无真实归档存储时，不使用含糊的"归档"描述，仅称"最小审计行长期保留 / 超期后删除最小行"；
- 最小 `ForgetTombstone` 不含内容，可长期保留；active Shield / Tombstone / 非终态 Operation / shielded_deadletter Operation 永远不被普通 retention 删除（见 Q.8）；
- **所有清理任务有界、可续跑、可审计**（lane + 复合游标，禁止 offset，见 Q.4）；
- **每类清理在扫描时与真正删除前都回源校验安全谓词**（见 Q.5）；
- **不允许每日全表无界扫描**（续跑游标，复用 P4 lane 模式）。

### Q.1 复用现有结构
- `RetrievalIndexGeneration` 的 `status`：`building`/`active`/`retired`/`failed`——`retired`/`failed` generation 的 `RetrievalIndexEntry`/`Token` 可按 Q.7 分批清理（不影响 active generation）。
- 复用现有 Maintenance / Outbox / P4 续跑模式（claim/lease/fencing + `HandlerOutcome.CONTINUE`），**先审计现状，能复用则复用，不强行加无必要新表**。

### Q.2 v1 RetentionPolicy 版本化配置 + 续跑表

**v1 优先使用版本化配置对象，而非数据库表**（修订点 9）：

```text
RetentionPolicyConfig（代码内版本化配置对象，非 DB 表）
  policy_version   Integer              # 配置版本号，每次修改 bump
  lanes:
    - lane            String            # compaction_input | outbox_job | maintenance_input | retrieval_generation | summary_version | checkpoint_version | forget_action | ...
      retention_days  Integer           # 该 lane 的 scrub 阶段保留天数
      delete_days     Integer nullable  # 该 lane 的 delete 阶段天数（无引用时才删结构）
      batch_size      Integer
      enabled         Boolean
```

每个 `RetentionCleanupRun` 在启动时**冻结** `policy_snapshot`（整份 `RetentionPolicyConfig` 的 JSON 快照）写入 `RetentionCleanupRun.policy_snapshot`，运行中不再读取最新配置，保证一次 Run 内策略一致、可复现、可审计。

> 仅在确实需要在运行时动态修改策略（如运维热调 `retention_days`、`enabled`）时，才引入 `retention_policies` 数据库表；v1 不创建该表。

**续跑表（替代旧 `retention_cleanup_runs`，采用 lane + 复合游标，修订点 3）**：

```text
RetentionCleanupRun:
  id              PK
  outbox_job_id   FK outbox_jobs.id UNIQUE   # 复用 Outbox claim/lease/fencing 驱动
  operation_id    String UNIQUE              # retention:all:{policy_version}:{window_bucket}
  policy_version  Integer
  policy_snapshot JSON                       # 冻结的配置快照
  status          String                     # pending|running|done|failed|deadletter
  execution_token String nullable            # fencing token
  claim_count     Integer default 0
  failure_count   Integer default 0
  current_lane    String                     # 当前处理的 lane（一个 Run 覆盖全部 lane，按确定性顺序推进）
  cutoff_at       DateTime                   # 冻结的时间 cutoff（仅处理早于该时刻的数据）
  scanned_count   Integer default 0
  scrubbed_count  Integer default 0
  deleted_count   Integer default 0
  created_at / updated_at

RetentionCleanupBatch:
  id              PK
  run_id          FK RetentionCleanupRun.id
  lane            String
  batch_no        Integer
  cursor_start_json JSON                     # 复合续跑游标起始（时间+sequence+ID），禁止 offset
  cursor_end_json   JSON nullable
  cutoff_at       DateTime
  input_hash      String(64)                 # 批次输入确定性哈希（防重放/校验）
  status          String                     # pending|running|done|failed
  created_at / updated_at
  UNIQUE(run_id, lane, batch_no)
```

> **operation_id 必须包含 `window_bucket`（修订点 1）**：不得继续使用缺少 window bucket 的唯一 operation_id（如旧式 `retention:{lane}:{policy_version}`），否则周期运行会碰撞或误判幂等。
> - **推荐（默认）**：一个 Run 覆盖全部 lane，`operation_id = retention:all:{policy_version}:{window_bucket}`，Run 按确定性顺序推进各 lane，并用 `current_lane` 记录进度；
> - **可选**：每 lane 一个 Run，此时删除 `current_lane` 字段，`operation_id = retention:{lane}:{policy_version}:{window_bucket}`。
> `window_bucket` 为时间窗口分桶（如天级 `2026-07-17`）；同一 policy_version 可在不同 window_bucket 创建多个 Run，而同一 window_bucket 重复调度因 `operation_id` UNIQUE 约束仍幂等。

- `RetentionCleanupRun` 由 `OutboxJob`（job type 如 `retention_cleanup`）驱动，复用既有的 claim/lease/fencing（`execution_token`/`claim_count`/`failure_count` 与 Outbox 模型一致）；
- `RetentionCleanupBatch` 按 `lane` 分桶，每桶保存（时间, sequence, ID）复合游标，**禁止使用数据库 offset**；
- 正常分页使用 `HandlerOutcome.CONTINUE`，**不计为失败**（不增加 `failure_count`/`retry_count`）。

### Q.3 清理对象与保留期（scrub 与 delete 分阶段，修订点 2 & 10）

每个对象区分 **scrub 阶段**（移除敏感内容，保留结构/最小审计行）与 **delete 阶段**（在无任何引用依赖后删除最小结构行）。优先移除敏感内容，再清结构数据。

| 对象 | scrub 阶段（默认） | delete 阶段（默认，需无引用） | 备注 |
|----|----|----|----|
| 已完成 `OutboxJob`（两阶段，点 2 & 4） | 30 天：scrub `payload` / `error_message`，保留 `id`、`operation_id`、`job_type`、`status`、`terminal_reason`、`terminal_at` | 180 天：仅当无 Run/StageRun FK（含 ForgetStageRun、`RetentionCleanupRun`）、无幂等/reconciler 依赖时删除最小行 | 不称"归档"；⚠ 当前无 `terminal_at` 列，建议 6B migration 新增 `terminal_at` DateTime nullable，终态路径原子写入一次，claim/retry/scrub 不得更新 |
| `deadletter` OutboxJob | 不 scrub（需人工复盘） | 保留更久（>180 天），人工/运维确认后清 | 保留最久 |
| `MemoryMaintenanceInput`（点 10） | 30 天：scrub `snapshot_json` 中用户正文 | 90 天：无引用时删除行 | 不含用户源数据 |
| 旧 `MemoryMaintenanceAction`（派生 OPS） | 随 Input 一并（仅元数据，无原文） | 90 天：无引用时删除 | — |
| 旧 `RetrievalIndexGeneration`（retired/failed） | 见 Q.7 分批清理（先 Token→Entry，再 Generation） | 见 Q.7（确认数量 0 后删 Generation） | 不依赖大规模 CASCADE |
| 旧 `SegmentSummary` version | 60 天：scrub 内容字段 | 90 天：非当前 `Segment.summary_id` 超期且无引用 | — |
| 旧 `EpochCheckpoint` version | 60 天：scrub 内容字段 | 90 天：非当前 `Epoch.checkpoint_id` 超期且无引用 | — |
| `CompactionInput` / `EpochCompactionInput`（点 1） | 90 天：scrub 用户正文快照字段（见 Q.6），保留 hash-only manifest | 仅在对应历史已明确 forget 且经 Verifier 通过、无任何依赖后删 manifest 行 | 不整体删除 manifest |
| `ForgetAction`（点 7） | 随 Operation（仅元数据） | Operation `purged` 超期后，Action/Batch/Dependency/scrubbed Target 可清，保留最小 Operation + Tombstone | — |
| `RetentionCleanupRun`（自身 retention lane，点 3） | 30 天：scrub 非必要诊断内容（error details / 长 input_hash 等），保留 `id`/`operation_id`/`status`/`terminal_at` | Run 已删除且无其他引用：对应 `OutboxJob` 才允许物理删除；其 Batch 已清且无依赖后才删 Run | 普通 retention 不得删 running/failed/deadletter 的 Run |
| `RetentionCleanupBatch`（自身 retention lane，点 3） | 随 Run（仅诊断） | 90 天：completed Batch 删除（其 Run 仍在时仅清 Batch） | 普通 retention 不得删 running/failed/deadletter 的 Batch |
| 运行日志 / 临时 error payload | 30 天 | 30 天 | 见 Q.9（不进 DB retention handler） |
| `ForgetTombstone` | 不 scrub（无原文） | 不自动清（最小审计） | 见 Q.8 |
| `ForgetShield`（active） | 不 scrub | 不自动清（见 Q.8 删除条件） | — |
| `content_provenance_refs` | — | 关联副本已清后清除 | 同副本 |

> 说明：`retention_days` 为 scrub 触发，`delete_days` 为最小结构行删除触发；所有 `delete 阶段` 均须先通过 Q.5 安全谓词回源校验。
> **RetentionCleanupRun/Batch 自身清理顺序（修订点 3）**：30 天 scrub Run 诊断内容 → 90 天删除 completed Batch → Batch 已清且无依赖时删除 completed Run → Run 已删除且无其他引用（如 ForgetStageRun 之外）时，对应 `OutboxJob` 才允许物理删除。running/failed/deadletter 的 Run 与 Batch 普通 retention 一律不碰。
> **operational JSON 内容安全（修订点 5）**：`MemoryMaintenanceAction.details` / `MemoryMaintenanceAction.preconditions` / `ForgetAction.details` / `RetentionCleanupRun`·`RetentionCleanupBatch` error details 等 operational JSON 字段，**不得未经验证地声明只含元数据**。Phase 6B 二选一（见审计 operational JSON 章节）：方案 A（推荐 v1）经 DTO 序列化只允许写入 ID/hash/status/数值/类型，拒绝任何用户正文；方案 B 纳入 retention scrub lane 递归清除用户正文。v1 默认 A，落库前即不含正文；若采用 B，这些字段须进入 Q.3 scrub lane，不可裸存。

### Q.4 有界续跑（lane + 复合游标）

- 每轮清理以 `policy.lanes[lane].batch_size`（如 500 行）+ 复合游标（`cursor_start_json` 含 时间+sequence+ID）推进，正常分页 `CONTINUE`，不成长事务；
- `RetentionCleanupRun` 记录 `current_lane`/`scanned_count`/`scrubbed_count`/`deleted_count`，`RetentionCleanupBatch` 记录每 lane 的 `cursor_start_json`/`cursor_end_json`；崩溃后从 `cursor` 续跑，**禁止 offset**；
- reconciler 补发遗漏的清理 Job（基于未完成的 `RetentionCleanupBatch`）；
- 不同 lane 独立续跑、可并发（受 R.4 互斥约束的除外）。

### Q.5 每类清理的删除前安全谓词（修订点 4）

每类清理在 **扫描时** 与 **真正删除前** 都必须回源校验以下安全谓词（任一项不满足则跳过该对象，禁止删除）：

```text
1. 不是当前 Summary / Checkpoint 指针（Segment.summary_id / Epoch.checkpoint_id 指向的行不删）；
2. 不是 active / building 的 retrieval generation（retired/failed 之外不删）；
3. 无未完成的 rebuild / refresh（无 running 态的 RetrievalRun / Summary/Checkpoint 重建 Job）；
4. 无其他 ForgetOperation 关联数据可清理：只有 `status='purged'` 且 ForgetVerifier 已通过且超过保留期，才允许清理其 Action/Batch/Dependency/Target。**以下状态全部视为受保护、不可清理**（见 Q.8）：requested / shielded / cascading / verifying / purge_ready / purging / failed_retryable / deadletter / shielded_deadletter；
5. 无 pending OutboxJob 引用（无未完成/未 finalize 的 outbox_jobs 指向该对象）；
6. 无仍需 Verifier / DEEP 使用的 provenance（content_provenance_refs 或其他仍被引用的来源不删）；
7. 超过保留期（cutoff_at 之前的数据）。
```

- 扫描阶段用谓词做**宽过滤**（减少扫描量）；
- 删除阶段**再次回源校验**同一组谓词（防止扫描到删除之间状态变化）；
- 任何谓词失败 → 跳过该对象，记录到 `RetentionCleanupBatch` 统计，不计入失败。

### Q.6 CompactionInput 拆分语义：可按期 scrub vs 长期保留 manifest（修订点 1）

`compaction_inputs` 与 `epoch_compaction_inputs` 同时承载"用户正文快照"与"不可变编排 manifest"。**不得按保留期整体删除整行**，必须按字段语义拆分。

#### CompactionInput（models.py:835-866）

```text
可按期 scrub（用户正文快照，到保留期后不可逆 scrub）：
  working_state_snapshot    # dict[JSON]，含 open_loops / active_constraints 等用户原文

长期保留（ID+哈希清单，不含 Event/Turn 明文，不得按保留期删）：
  turn_manifest             # list[JSON]，每条：turn_id / turn_sequence / turn_record_id / status
  event_manifest            # list[JSON]，每条：event_id / turn_record_id / turn_event_index /
                            #   event_type / content_hash（= SHA-256(event_type+payload)）
  source_hash               # String(64) NOT NULL — 整体源哈希
  start_turn_sequence       # turn 区间起始
  end_turn_sequence         # turn 区间结束
  summary_version            # SegmentSummary 版本号
  segment_id                 # 所属 Segment
```

> manifest 为**列表**（非单值哈希），但每条仅含 ID + 状态 + 哈希，**不含 Event.payload 明文**（`event_content_hash` 仅对 `event_type + payload` 做 SHA-256，不保留原文；见 compaction.py:47-51）。因此 manifest 可长期保留而不泄漏用户正文。P5 DEEP 校验（raw_history_expander.py:11-12）逐条重算 Event 的 content_hash 并与 manifest 中的哈希比对，依赖的正是 manifest 中的哈希而非原文。

#### EpochCompactionInput（models.py:913-942）

```text
可按期 scrub（用户正文，独立列，到保留期后不可逆 scrub）：
  current_objective         # Text
  open_loops                # list[JSON]
  active_constraints        # list[JSON]
  artifact_refs             # list[JSON]
  verified_tool_states      # list[JSON]

长期保留（hash-only）：
  source_segment_ids        # list[JSON]
  source_hashes             # list[JSON]
  snapshot_hash             # String(64) NOT NULL — 整体快照哈希
  checkpoint_version        # 版本号
  boundary_turn_sequence    # Epoch 边界 turn 序号
  epoch_id                  # 所属 Epoch
```

> ⚠ 与 `CompactionInput` 不同，`EpochCompactionInput` **没有** `working_state_snapshot` 嵌套字段——正文分散在 5 个独立列中，scrub 时需逐列清空（不可整体置 null 误伤长期保留列）。

#### 两者一致的删除规则

- scrub 阶段只清空"可按期 scrub"字段（置为 `null` 或确定性占位），保留"长期保留"字段；
- **只有在对应历史已经明确 forget 且经 Verifier 通过、无任何依赖（无 active Shield、无 Tombstone 引用、无 provenance、无 rebuild 依赖）后，才允许删除整行 manifest**（对应测试 1）。

### Q.7 Retrieval generation 必须分批清理（修订点 5）

对 `status='retired'` 或 `status='failed'` 且**真实终态时间（`retired_at` / `failed_at` / `status_changed_at`，见审计 RetrievalIndexGeneration）超期**的 `RetrievalIndexGeneration`，必须按以下顺序分批执行：

```text
1. 分批删除 RetrievalIndexToken（按复合游标，每批 ≤ batch_size）；
2. 分批删除 RetrievalIndexEntry（按复合游标，每批 ≤ batch_size）；
3. 确认该 generation 下 Token 与 Entry 数量均为 0（按 index_version 统计）；
4. 删除 RetrievalIndexGeneration 行（仅当数量为 0）。
```

- ⚠ `RetrievalIndexToken.entry_id` FK 已有 `ondelete="CASCADE"`（models.py:1280-1284）。分批删 Entry 时 Token 被级联删除；先显式删 Token 的目的是**控制每批语句行数、可校验、可续跑**，而非绕过不存在的 CASCADE。
- **每批执行前重新确认** generation 仍为 `retired` / `failed`（若被复用为 `active` 则中止该 generation 清理）；
- 每批独立事务、可续跑（`RetentionCleanupBatch` 记录游标）；crash 后从 Token/Entry 批次续跑，不重复、不跳过（对应测试 6）；
- `active` / `building` generation **永远不能清理**（见 Q.5 谓词 2 与测试 7）。

### Q.8 Phase 6A 表的保留策略补齐（修订点 7）

普通 retention **永远不得删除**：

```text
- active ForgetShield
- ForgetTombstone（最小审计行）
- 受保护的 ForgetOperation：以下任一状态均永不清理其关联数据 ——
    requested / shielded / cascading / verifying / purge_ready / purging
    / failed_retryable / deadletter / shielded_deadletter
  （只有 status='purged' 且 ForgetVerifier 已通过且超过保留期，才允许清理其 Action/Batch/Dependency/Target，见 Q.5 谓词 4）
```

当 `ForgetOperation.status='purged'` 且保留期结束后，可以清理（仍先过 Q.5 谓词）：

```text
- ForgetAction（forget_actions）
- ForgetBatch（forget_batches）
- ForgetDependency（forget_dependencies）
- 已 scrub 的 ForgetTarget（forget_targets 内容已不可逆 scrub）
```

保留最小 `ForgetOperation` + `ForgetTombstone` 审计行（不删）。

**Selector Shield 删除条件（显式）**：只有在以下全部成立后才允许删除对应 `ForgetShield` 行：

```text
1. 逐条 ForgetTombstone 已完整覆盖该 Shield 作用域（Tombstone 行齐全，无遗漏目标）；
2. 对应内容已 purged（content_purged=true 或已不可逆 scrub）；
3. ForgetVerifier 已通过（无遗留 provenance / legacy_unverifiable 已处理）。
```

未满足上述条件前，Shield 保持 `active`，普通 retention 不碰（对应测试 10、11、5）。

### Q.9 日志保留独立治理（修订点 11）

- 应用日志（文件 / 容器 stdout）的保留由 **logrotate / 容器日志策略** 管理，**不由数据库 retention handler 管理**；
- retention handler 不写入、不清理应用日志；
- **首先禁止日志打印完整敏感内容**：`prompt`、`Event payload`、`Memory content`、`secret` 等不得在日志中输出完整明文（降噪，见 6A §U 日志审计项）；
- 若需保留审计痕迹，仅记录 ID / 类型 / hash / 状态等元数据。

---

## R. 数据库物理空间回收（修订点 6 & 12）

删除数据库行 ≠ 文件立即缩小。必须显式维护，且**不在用户请求事务内执行**。

### R.1 PostgreSQL：独立执行器

- 使用**独立 AUTOCOMMIT connection**（不加入任何业务事务/请求事务），所有 `VACUUM`/`REINDEX` 在该连接上执行；
- 使用 **maintenance / advisory lock** 保护，避免与业务 DDL/长事务冲突；
- **默认依赖 `autovacuum`**；`VACUUM ANALYZE` / `REINDEX` 仅在**真实膨胀指标**（如 `pgstattuple` / `pg_stat_user_tables` 的 dead tuple 比例、索引膨胀率）达到阈值时由独立低优先级维护任务执行；
- **`VACUUM FULL` 不自动执行**，仅在维护窗口由运维显式触发；6B 仅提供维护建议（哪些表需要 FULL、预期收益），不直接运行；
- 索引膨胀用 `REINDEX`（优先 `CONCURRENTLY`）；retrieval token 大量删除后建议定期 `VACUUM` + `REINDEX` 相关索引。

### R.2 SQLite：独立执行器

- **WAL checkpoint 与 VACUUM 分开**：`PRAGMA wal_checkpoint(TRUNCATE)` 可较频繁执行（不阻塞业务）；`VACUUM` 阻断性强，**仅在独立维护任务、低峰执行**；
- `VACUUM` **仅在阈值满足时执行**：`freelist`/`page` ratio、文件大小、空闲窗口（无活跃写任务）同时达标；
- **不得在普通请求或长事务中运行 `VACUUM`**；不在用户 forget 请求事务内执行；
- 有活跃写任务（如 retention cleanup、forget purge、generation cleanup）时跳过 VACUUM（对应测试 9）。

### R.3 触发策略

- 空间回收由**独立低优先级维护任务**执行（复用 P4 scheduler lane），不阻塞请求路径；
- 可基于表增长阈值（如 `events`/`retrieval_index_tokens` 删除量超阈值）触发，而非固定每日全表。

### R.4 重维护任务互斥（修订点 12）

以下任务**不得同时运行**，使用 `MaintenanceLease`（应用层租约）或数据库 **advisory lock** 互斥：

```text
- retrieval rebuild
- 大规模 forget purge
- generation cleanup
- SQLite VACUUM
- PostgreSQL REINDEX
```

- 每个重任务在开始前 `acquire_lease` / `pg_advisory_lock`，结束时释放；
- 获取失败 → 跳过本轮，下一轮重试，不阻塞、不报错升级；
- 互斥约束确保：VACUUM 不与 purge 争用、REINDEX 不与 rebuild 冲突、generation cleanup 不与 active generation 读写冲突（对应测试 15）。

---

## W. 长期压力与混沌测试（修订点 8）

### W.1 确定性分级体系（必备能力）

压力/混沌测试必须实现为**确定性分级体系**，提供：

```text
- 可注入 Clock（虚拟时钟，不依赖真实 wall-clock，可快进/重放）
- 固定 random seed（每次运行可复现）
- 确定性数据生成器（同 seed 生成完全相同的数据集）
- 显式 fault injection points（在指定阶段强制 crash / 注入延迟 / 注入失败）
- benchmark JSON 报告（结构化的延迟/吞吐/不变量结果）
- 固定 warmup / sample 规则（热身轮数、采样窗口、统计方法一致）
```

### W.2 数据规模分级

```text
CI:              1k Memory / 10k Event
nightly:         10k Memory / 100k Event
manual stress:   100k Memory / 1M Event / 365 天
```

### W.3 时间模拟（虚拟时钟）

- 30 / 90 / 365 天（由可注入 Clock 模拟，非真实等待）；
- 行为含：新对话、memory extraction、reinforce、supersede、sleep/wake、segment sealing、epoch rollover、retrieval refresh、index rebuild、forget memory / history / everywhere、retention cleanup、进程重启；
- 同环境、同数据 seed 下，建议默认性能回归阈值为 **p95 不超过基线 20%**，并检查关键大表（`events`/`memory_records`/`retrieval_index_tokens`/`forget_*`）无无界顺序扫描。

### W.4 混沌点（强制 crash，验证接管）

在以下阶段强制 crash，验证接管后：

- ForgetOperation 创建后 / shield 完成后 / 部分 Action 完成后 / Summary 重建前后 / Checkpoint 重建前后 / 索引 tombstone 前后 / OutboxJob scrub 后 / generation Token 批次后 / Entry 批次后 / generation 删除前后 / 物理 delete 前后 / verification 前后 / VACUUM 前后。
验证：
- 不重复删错误目标；
- 不重新暴露 shielded 内容；
- 不产生半条 Summary / 半条 Generation；
- 不泄漏旧 Checkpoint；
- 不丢未删数据；
- 不出现永久 running Job；
- CONTINUE 不增失败；
- generation cleanup crash 后从 Token/Entry 批次续跑（对应测试 6）。

### W.5 性能指标基线（基线 + 回归阈值）

ContextAssembler p50/p95、auto retrieval p50/p95、search/deep p50/p95、Memory write p50/p95、Daily Dream p50/p95、Forget shield 延迟、Forget cascade 完成批次数、索引增长、数据库增长、Outbox backlog、每轮 token 使用。

**回归阈值**：同 seed、同环境，p95 不超过基线 120%；关键大表无无界顺序扫描（执行计划检查）。

### W.6 必须验证的不变量（长期）

- 同一 ForgetOperation 只有一份 Selector Manifest + 冻结 Target/Dependency；
- 未在 Manifest 中的记录绝不删除；
- forgotten 内容任何读取模式均不可返回（Shield + Tombstone 双保险）；
- archived 仍可按 P5 规则显式读取；
- forget 优先级高于 pinned/user_required；
- 旧 source Event 不会重新生成已忘记 Memory；
- 新用户输入可以重新建立新事实；
- Summary/Checkpoint 不包含已删除来源；
- Retrieval token 不包含 forgotten 内容；
- Core Memory 不包含 forgotten 内容；
- 物理 purge 后不存在内容副本（或仅含不可恢复 scrub 残骸）；
- 最小 Tombstone 不包含原文；
- 正常重试不会重复执行副作用；
- 旧 Worker 无法提交新 claim 的结果；
- 上下文 token 仍严格有界；
- 长期维护工作量与增量相关（retention 有界续跑，无全表扫描）；
- retention 清理不删除用户源数据（除非 6A forget 已 scrub）；
- hash-only CompactionInput manifest 在 scrub 后仍能支持 P5 DEEP 校验（对应测试 1）。

---

## V. Phase 6B 测试矩阵（覆盖 spec 第 19 节相关项 + 规模/混沌）

### V.1 原有测试

1. retention 清理**不删除**任何用户源数据（events/turn_records/memory_records）行；
2. 派生投影旧版本（SegmentSummary/EpochCheckpoint/CompactionInput）按保留期清理，不影响当前版本；
3. `retired` RetrievalIndexGeneration 的 Entry/Token 清理不影响 `active` generation；
4. 已完成 OutboxJob 按保留期 scrub/清除（两阶段），deadletter 保留更久；
5. `ForgetAction` 在 Operation purged 超期后清除，但 `ForgetTombstone` 保留；
6. retention 清理任务有界续跑（cursor 推进，崩溃不重复删）；
7. reconciler 补发遗漏的 retention Job；
8. PostgreSQL `VACUUM ANALYZE` / `REINDEX` 由独立任务执行，不在请求路径；
9. SQLite `VACUUM` 仅在低峰独立任务执行；
10. retrieval token 大量删除后索引膨胀可控（`REINDEX` 生效）；
11. 30/90/365 天模拟不变量全通过（W.6）；
12. 100k Memory / 1M Event 压力测试下 ContextAssembler / auto retrieval p50/p95 不显著退化；
13. 任意阶段 crash 后不泄漏 forgotten 内容（接管正确）；
14. 长期运行 Outbox backlog 有界（retention + forget 续跑不留永久 running）；
15. 数据库增长可测量、可控制（retention 清理生效）；
16. 每轮 token 使用不随 forget/retention 增长（上下文持续有界）；
17. 不存在无界全表删除或扫描（所有清理走 cursor/batch）；
18. 无 Supervisor LLM、无关键词硬编码路由（回归）；
19. P0.5A～P6A 全量回归（6B 不引入回归）；
20. 6A + 6B 端到端：forget everywhere → verifier → purge → retention 清理 → 空间回收，全链路不变量成立。

### V.2 本次新增测试（对应修订 1–12）

1. scrub CompactionInput 后 manifest 仍能支持 DEEP 校验；
2. Outbox payload scrub 后 `operation_id` 幂等仍有效；
3. 被 StageRun 引用的 OutboxJob 不删除；
4. 非当前 Summary/Checkpoint 才能清理；
5. 非终态 ForgetOperation 引用的数据不能清理；
6. generation 清理 crash 后可以从 Token/Entry 批次续跑；
7. active/building generation 永远不能清理；
8. PostgreSQL VACUUM 不在事务块执行；
9. SQLite VACUUM 在有活跃写任务时跳过；
10. active Shield/Tombstone 不被 retention 删除；
11. purged Operation 的 Action/Batch 可按期清理；
12. 虚拟时钟可重复模拟 30/90/365 天；
13. 固定 seed 生成相同数据和故障序列；
14. CI/nightly/manual stress 分级生效；
15. maintenance lock 阻止重任务并发；
16. P0.5A～P6A 全量回归通过。
17. 相同 `policy_version` 可在不同 `window_bucket` 创建多个 `RetentionCleanupRun`（operation_id 含 window_bucket）；
18. 同一 `window_bucket` 重复调度 `RetentionCleanupRun` 仍然幂等（`operation_id` UNIQUE 约束兜底）；
19. `failed_retryable` / `shielded_deadletter` 状态的 ForgetOperation 关联数据不会被清理（受保护状态，见 Q.5/Q.8）；
20. 只有 ForgetVerifier 已通过的 `purged` Operation 的子记录（Action/Batch/Dependency/Target）可按期清理；
21. `RetentionCleanupRun` / `RetentionCleanupBatch` 自身 retention lane 生效，不会无限增长（30 天 scrub 诊断 / 90 天删 Batch / 依赖清后删 Run）；
22. 被 `RetentionCleanupRun` 引用的 `OutboxJob` 不会在 Run 删除前提前物理删除；
23. `OutboxJob.terminal_at` 不因 claim、retry 或 payload scrub 改变；
24. failed `RetrievalIndexGeneration` 使用真实 failed/status-change 时间（非可能为空的 `build_completed_at`）计算 retention cutoff；
25. operational JSON（`MemoryMaintenanceAction.details`/`preconditions`、`ForgetAction.details`、Run/Batch error details）不保存或能正确 scrub 用户正文；
26. P0.5A～P6A 全量回归通过（验收闸门，与 V.2-16 同义）。

---

## X. Phase 6B 回滚与降级

- **Retention 误删防护**：所有清理为幂等 + 有界 + 可审计（Q.4/Q.5）；误删仅影响派生旧版本（可重建），不影响用户源数据；删除前安全谓词失败即跳过（Q.5）。
- **Vacuum 失败**：独立任务失败不影响请求路径，下次重试；`VACUUM FULL` 仅在维护窗口，失败可回退到 `VACUUM ANALYZE`（R.1）；SQLite VACUUM 阈值不满足则跳过（R.2）。
- **混沌测试失败**：定位 Saga 不变量缺口，回 Phase 6A 修复（如 fencing / batch 续跑 / Tombstone 拦截），不在 6B 内打补丁绕过。
- **降级**：retention 任务可暂停（配置 `enabled=false`），暂停时仅停止清理，不回退已 forget 的屏蔽状态；R.4 互斥获取失败仅跳过本轮，不影响其他路径。
- **日志治理**：应用日志降级为 logrotate/容器策略，DB retention handler 不触碰（Q.9）。

---

## 附录：Phase 6B 依赖的 6A 产物与新增结构

6A 产物（消费，不修改 6A）：
- `forget_operations` / `forget_batches` / `forget_actions`（终态用于 retention）
- `forget_shields`（active 行普通 retention 不删，见 Q.8）
- `forget_tombstones`（最小审计，长期保留）
- `content_provenance_refs`（delete 前依赖校验，见 Q.5/Q.8）
- `ForgetVisibilityService`（读取路径 fail-closed，6B 不改动其屏蔽语义）

6B 新增结构（方案级，实施前再落 migration/ORM）：
- `RetentionCleanupRun`（替代旧 `retention_cleanup_runs`，lane + 复合游标 + 冻结 `policy_snapshot`）
- `RetentionCleanupBatch`（每 lane 复合游标续跑批次）
- `RetentionPolicyConfig`（v1 代码内版本化配置对象，非 DB 表；仅运行时需热调时才建 `retention_policies` 表）
- 重维护互斥：`MaintenanceLease` 或数据库 advisory lock（R.4）
- 建议 `outbox_jobs` 新增 `terminal_at` DateTime nullable 列（6B migration；终态原子写入，claim/retry/scrub 不更新；当前仅 `updated_at`）
- 建议 `retrieval_index_generations` 新增真实终态时间列（`status_changed_at` 或 `retired_at`/`failed_at`），failed generation 不得依赖可能为空的 `build_completed_at`

## 下一步

Phase 6A 先实施并验收；随后 Phase 6B 在 6A 验收基础上实施 retention / vacuum / 长期混沌测试。
