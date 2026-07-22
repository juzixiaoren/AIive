# AIive Phase 6A：Forget Saga、可验证删除、Fail-Closed 屏蔽与防重抽

> 本文件为 **Phase 6A 实施方案**（拆分自 `phase_6.md`）。
> 范围：真实代码审计 + 删除语义审计 + 数据依赖图 + Phase 6A 实施方案（Shield / Saga / Dependency 重建 / Verifier）+ migration 增量方案 + 6A 测试方案。
> **本次不修改任何代码、不生成 migration、不执行 purge、不创建测试数据、不开始压力数据生成。**
> 所有方案均基于仓库真实代码交叉核对（已读文件见末尾清单）。
> Phase 6B（Retention / generation cleanup / vacuum / 长期混沌测试）见 `phase_6B.md`。

---

## 0. 内容承载位置清单（真实表 / 真实字段）

以下每个位置都可能保存用户原文或可被反推的用户内容。Phase 6A 的 scrub / purge 必须逐一覆盖；Phase 6B 负责其长期保留治理。

| # | 表 | 字段（内容承载） | 分类 | 当前 forget 是否处理 |
|---|----|----|----|----|
| 1 | `memory_records` | `content`, `structured_value`, `content_hash`, `structured_value_hash` | source of truth | 仅把 `content` 替换为 tombstone 串，**未物理删除行** |
| 2 | `memory_proposals` | `content`, `structured_value`, `raw_payload`, `normalized_payload`, `evidence`(JSON) | operational audit | **未处理（泄漏）** |
| 3 | `memory_evidence` | `content_span` | derived | 删除（按 memory_id），但无幂等/重算 |
| 4 | `memory_lineage` | `reason` | operational audit | **未处理（仅元数据，无原文，低风险）** |
| 5 | `core_memory_blocks` | `content` | derived projection（可重建） | 依赖 `core_memory_refresh`，`build_blocks` 已按 `active+valid` 过滤，遗忘后自然消失 |
| 6 | `segment_summaries` | `goal`, `outcome`, `decisions`, `open_loops`, `entities`, `artifacts`, `important_tool_results`, `unresolved_failures`, `active_constraints`, `omitted_artifact_refs`, `source_event_ids` | derived projection | **未处理（含被忘事实的摘要文本）** |
| 7 | `epoch_checkpoints` | `current_goal`, `open_loops`, `active_constraints`, `current_decisions`, `referenced_artifacts`, `relevant_entities`, `latest_verified_tool_states`, `source_segment_ids` | derived projection | **未处理** |
| 8 | `compaction_inputs` | `working_state_snapshot`(JSON, 含 open_loops/constraints), `turn_manifest`/`event_manifest`(仅 ID+hash) | immutable manifest | **未处理（`working_state_snapshot` 含内容）** |
| 9 | `epoch_compaction_inputs` | `current_objective`, `open_loops`, `active_constraints`, `artifact_refs`, `verified_tool_states`, `source_segment_ids` | immutable manifest | **未处理** |
| 10 | `memory_maintenance_inputs` | `snapshot_json`(含 `content` 完整原文！见 `outbox_handlers._freeze_input`) | operational audit | **未处理（直接保存 MemoryRecord.content）** |
| 11 | `memory_maintenance_actions` | `details`(JSON), `preconditions` | operational audit | **未处理** |
| 12 | `retrieval_index_entries` | `title`, `search_text`, `snippet`, `metadata` | derived projection（内容副本） | `retrieval_index_refresh` 在 `memory.forgotten` 时 tombstone（清空正文+tokens） |
| 13 | `retrieval_index_tokens` | `token`, `term_frequency` | derived posting | tombstone 时随 entry 删除（FK ondelete CASCADE） |
| 14 | `working_states` | `open_loops`, `active_constraints`, `pending_approvals`, `artifact_refs`, `verified_tool_states`, `running_tool_state`, `uncommitted_side_effects` | source of truth（运行态） | **未处理** |
| 15 | `artifacts` | `content` | derived（工具结果原文） | **未处理** |
| 16 | `events` | `payload`(含 `content`) | source of truth（原始聊天） | **未物理删除（everywhere 模式才删）** |
| 17 | `turn_records` | `response_payload` | source of truth | **未处理** |
| 18 | `llm_calls` | `input_preview`, `output_preview` | operational audit | **未处理（无 provenance，见 §8）** |
| 19 | `context_snapshots` | `context_items`, `meta` | operational audit（含组装后上下文原文） | **未处理（无 provenance，见 §8）** |
| 20 | `outbox_jobs` | `payload`, `error_message`(≤500) | operational audit | 仅 `error_message` 可能含 ID/报错 |
| 21 | `tasks` | `description`, `condition` | source of truth（用户提醒） | **未处理** |
| 22 | `retrieval_runs` | `query` | operational audit | 含用户检索词（非记忆内容） |
| 23 | `logs` / 应用日志 | 可能含 Memory/Prompt/Event 正文 | operational audit | **待审计（E 第 20 项）** |

**关键隐蔽副本**：`memory_maintenance_inputs.snapshot_json.content`、`compaction_inputs.working_state_snapshot`、`core_memory_blocks.content`、`retrieval_index_entries.search_text`、`segment_summaries` 全字段、`epoch_checkpoints` 全字段、`working_states.*`、`artifacts.content`、`events.payload`、`context_snapshots.context_items` —— 这些是被遗忘事实最可能“复活”的位置。

**provenance 能力审计（§8）**：`#18/#19/#15/#20/#10` 当前**没有 source_id 反查能力**——只能按 thread/turn/trace/time range 粗粒度 scrub，或标记 `legacy_unverifiable`。未来写入须新增统一 `content_provenance_refs`（见 H）。

---

## A. 当前真实 Forget 调用链

1. **工具层**：`tools/builtin_tools.py` → `_handle_forget_memory`（scope ∈ `memory_id`/`memory_key`/`topic`/`all`）→ `MemoryWriteService.forget(...)`。
   - 注册名 `forget_memory`，`risk_level="medium"`，`can_be_deleted=False`，`writes_external_world=True`。
   - **模式不是由 Agent 显式参数决定**，而是工具内部的 `scope` 字符串推断（关键词/范围硬编码）—— 违反“不得用关键词 if/else 决定模式”。
2. **API 层**：`api/routes_memories.py` → `POST /api/memories/{memory_id}/forget`（body `{reason}`）→ `MemoryWriteService.forget(memory_id, reason=...)`，**不传 `run_context`**（导致 tombstone 事件因 `thread_id` 空被跳过）。
3. **服务层**：`MemoryWriteService.forget()`（`memory_write_service.py:464`）：
   - `content = tombstone`（串 `forgotten:{id8}:{iso}`），`lifecycle_state=forgotten`，`validity_state=superseded`，`structured_value=None`；
   - `DELETE memory_evidence WHERE memory_id=...`（无幂等键、无重算其他 Memory 的 evidence）；
   - `log_event("memory.forgotten", ...)`（仅当 `run_context` 有 thread_id）；
   - `enqueue_projection(record, "memory.forgotten")` → 入队 `memory_vector_refresh` + `retrieval_index_refresh`；向量 handler 回源后删除失效投影；
   - `ForgetRequest(memory_id, reason, tombstone)` 落库（`saga_state` 默认 `"running"`，**从未被更新**）。
4. **异步侧**：`retrieval_index_refresh` handler → `refresh_source` → `memory.forgotten ∈ _TOMBSTONE_EVENTS` → `tombstone_by_source`（清 entry 正文 + 删 token）。

**结论**：当前是“单事务即时 scrub + 入队投影清理”，**没有 Saga、没有 Selector Manifest/Target/Dependency、没有幂等 Batch/Action、没有 prevent-re-extraction tombstone、没有 verifier、没有物理 purge、没有 retention**。

---

## B. 所有内容承载表和字段

见 **第 0 节清单**（Phase 6A 必须把 #1–#22 全部纳入 scrub/purge 或显式判定为安全保留；#23 需日志审计）。

---

## C. 当前 archived / forgotten / purged 语义（含修订后定义）

- `active` / `sleeping` / `archived` / `candidate` / `forgotten` 都是 `memory_records.lifecycle_state` 的枚举值。
- **archived**：`MemoryLifecycleService.archive` 置 `lifecycle_state=archived` + `validity_state=expired`。内容**仍存在**；`UnifiedRetriever` 默认 `include_archived=False` 不返回，但**显式 `include_archived=True` 仍返回**（P5 规则）。属于可恢复生命周期。**与 forgotten 严格分离**（独立枚举、独立路径）。
- **forgotten**（修订后语义）：
  - 所有正常读取路径立即 fail-closed；
  - 自动召回、search、deep、Core Memory、Summary、索引均不得返回目标；
  - 异步删除 Saga 可以仍在运行；
  - 不允许重新写入或重新抽取同一已忘记事实（靠 `ForgetTombstone`）；
  - 派生内容可能尚未全部完成物理清理，但必须已被屏蔽（fail-closed）。
- **purged**（修订后定义，见 M §5）：**默认不物理删除 Event/TurnRecord 结构行，而是不可逆 scrub 内容**——保留 `id`、`turn_sequence`/`turn_event_index`、`event_type`、非内容审计 hash；清空 `payload`/`response_payload` 中的用户正文。只有当确认无 FK、无 manifest、无排序依赖、无 tool 配对依赖时，才物理删整行。`purged` 表示“不再存在可恢复用户正文”，**不要求所有结构行消失**。

不得把 `forgotten` 和 `purged` 混为同一状态。

---

## D. 当前读取路径泄漏面（fail-closed 缺口，含 ForgetShield）

| 读取路径 | 是否实时过滤 forgotten | 缺口 |
|----|----|----|
| `UnifiedRetriever._revalidate_candidates`（memory_record） | ✅ 始终过滤 forgotten/superseded/expired | 可靠 |
| `AutomaticRecallEngine` / `MemoryStore.get_active_valid` | ✅ 仅 `active+valid` | 可靠 |
| `CoreMemoryProjection.build_blocks` | ✅ 仅 `active+valid` | 可靠 |
| `ContextAssembler._load_segment_summaries` / `_load_epoch_checkpoint` | ❌ 不过滤 | Summary/Checkpoint 内可能含被忘事实，**泄漏面** |
| `ContextAssembler` recent history（`_load_history_bounded`） | ❌ 不过滤 | 即使 memory_only 保留 raw Event，正常上下文仍会重新纳入目标 Event，**泄漏面** |
| `RawHistoryExpander.expand`（DEEP 回溯原始 Event） | ❌ 不查 Tombstone/Shield | DEEP 仍可能返回目标 Event 正文，**泄漏面** |
| `WorkingStateService.render_for_context` | ❌ 不过滤 | `working_states` 可能含被忘内容 |
| `api/routes_memories.py list_memories` | ❌ 返回所有（含 forgotten 的 tombstone 串） | API 泄漏（返回了 `content` 字段） |
| `tasks` / `context_snapshots` / `memory_proposals` | ❌ 不过滤 | 审计/历史接口可能返回原文 |
| 应用日志 | 待审计（E 第 20 项） | 待补日志审计 |

**核心缺口**：没有统一 `ForgetVisibilityService` + 选择器级 `ForgetShield`。每个模块自行判断，DEEP、Summary/Checkpoint 加载、WorkingState、recent history、审计 API 未接入屏蔽。

**读取顺序（修订）**：所有读取路径先检查 **ForgetShield**（选择器级，快速失败），再检查逐条 **ForgetTombstone**（fail-closed）。显式历史审计模式可绕过 Shield 查看保留的 raw history，但**禁止**绕过 Tombstone 查看已 purged 的原文。

---

## E. 当前重新抽取风险（含精确 tombstone 限制，修订自 §7）

- `handle_memory_extraction` 通过 `MemoryIngestionRun`（`source_turn_record_id` + `extractor_name` + `extractor_version` 唯一约束）去重：已 `succeeded` 的旧 Turn **不会再次抽取**。
- **但这不构成“防止重新抽取”保证**：
  1. 旧 Turn 若被重新触发 ingestion（re-run、回放、测试），`MemoryIngestionRun` 已 succeeded → 跳过，安全；
  2. **新** Event/Turn 若包含同一事实 → 会被正常抽取并新建 MemoryRecord（这是允许的“用户未来主动重新提供”）。
  3. **风险点**：当前**没有任何 `ForgetTombstone`**。若系统错误地重新处理某个“已忘记来源”的 Event，将**静默重建已忘记事实**。
- **`ForgetTombstone` 精确拦截规则（§7）**：
  - **必须阻止**：
    - 旧 `source_event_id`（tombstone 记录的来源事件被再次处理）；
    - 旧 `source_turn_record_id`（tombstone 记录的来源轮次被再次抽取）；
    - 全部 provenance 来自 forgotten source 的 `MemoryProposal`（其所有来源 ID 都已 forgotten/shielded）。
  - **不得仅因** `canonical_key + scope_type + scope_id + value_hash` 就永久拒绝新的用户输入。
  - **新 Turn/Event 中用户重新明确提供同一事实（新的 event_id / turn_record_id）时必须允许重新建立 Memory**。
  - 不得用关键词字符串比较实现；按稳定 ID 与归一化 value hash 判断。

---

## F. 数据依赖图（真实 FK / provenance）

标记含义：`SOT`=真相源 · `DER`=派生投影 · `IMM`=不可变 manifest · `OPS`=操作审计 · `CB`=内容承载 · `HO`=仅哈希 · `SR`=安全保留 · `MS`=必须 scrub · `MR`=必须重建 · `MP`=必须物理删除（默认改为不可逆 scrub）。

```
TurnRecord(SOT, CB#17) ──< Event(SOT, CB#16) ──< MemoryEvidence(FK memory_id; CB#3, content_span)
   │ (turn_id, segment_id)                                   │ source_event_id
   ▼                                                         ▼
Segment(SOT) ──< SegmentSummary(SOT-DER, CB#6, MR)          MemoryRecord(SOT, CB#1, MP-scrub)
   │ summary_id                                                │ id
   ▼                                                          ├─< MemoryEvidence
Epoch(SOT) ──< EpochCheckpoint(SOT-DER, CB#7, MR)            ├─< MemoryLineage(FK predecessor/successor; CB#4 OPS)
   │ checkpoint_id                                             ├─< CoreMemoryBlock(DER, CB#5, 可重建)
   ▼                                                          ├─< MemoryProposal(FK final_memory_id; CB#2 OPS, MS)
CompactionInput(IMM, CB#8, MS) ── event_manifest(HO)         ├─< RetrievalIndexEntry(FK memory_record_id; CB#12 DER, MS)
EpochCompactionInput(IMM, CB#9, MS)                          └─< ForgetTombstone(新增)
MemoryMaintenanceRun(OPS) ──< MemoryMaintenanceBatch(OPS) ──< MemoryMaintenanceInput(OPS, CB#10 MS)
                                                                  └─< MemoryMaintenanceAction(OPS, CB#11)
RetrievalIndexGeneration(SOT) ──< RetrievalIndexEntry(DER, CB#12) ──< RetrievalIndexToken(FK entry_id CASCADE; CB#13)
RetrievalIndexRun(OPS) · OutboxJob(SOT-OPS) · WorkingState(SOT, CB#14) · Artifact(DER, CB#15)
ContextSnapshot(OPS, CB#19) · LLMCall(OPS, CB#18) · Task(SOT, CB#21) · RetrievalRun(OPS, CB#22) · Log(CB#23)
```

**隐蔽副本（必须 scrub）**：`snapshot_json.content`（#10）、`working_state_snapshot`（#8/#9）、`core_memory_blocks.content`（#5）、`retrieval_index_entries.search_text/snippet`（#12）、`segment_summaries.*`（#6）、`epoch_checkpoints.*`（#7）、`working_states.*`（#14）、`artifacts.content`（#15）、`events.payload`（#16）、`context_snapshots.context_items`（#19）、`llm_calls.*_preview`（#18）。

---

## G. memory_only / history_only / everywhere 设计（memory_only 语义修订）

**统一一个结构化 `forget` 工具**（替换旧的 `forget_memory` 的 scope 推断），参数显式：

```text
mode: "memory_only" | "history_only" | "everywhere"   # 必填，由主 Agent 在 tool call 中显式指定
memory_ids: list[str]                  # memory_only / everywhere
canonical_key / scope_type / scope_id # 辅助定位（按 canonical_key 全删）
thread_id / turn_ids / event_ids       # history_only / everywhere
time_from / time_to                   # history_only / everywhere（时间范围）
selector_type: "memory_ids" | "canonical_key" | "thread" | "turn_range" | "event_ids" | "time_range" | "all_user_data"
reason: str
```

### memory_only（修订）
只忘记长期记忆表示，但**被忘内容不得再进入自动上下文**：

- 删除 `MemoryRecord` / `CoreMemoryBlock` / `Memory retrieval projection`（`RetrievalIndexEntry`/`Token`）；
- 保留 `raw Event/Turn`（供显式历史审计）；
- **受影响 SegmentSummary / EpochCheckpoint 必须重建或 redacted**（原事实可能写入了摘要，必须重生或打码，见 K）；
- `ContextAssembler` recent history **必须屏蔽目标 Event**（写入选择器级 `ForgetShield` + 逐条 Tombstone，正常上下文不再纳入，即使 raw history 仍保留）；
- 默认 `auto` / `search` / `deep` 不返回目标；
- 只有**显式历史审计模式**（`include_raw_history=True` 且通过审计鉴权）才允许查看保留的 raw history；
- 增加防止重新抽取的 `ForgetTombstone`（按 `source_event_id`/`source_turn_record_id` + `canonical_key`+value hash），否则后台 Memory Extraction 会从旧聊天重建相同记忆；
- 工具返回需明确告知：“长期记忆已删除，原始聊天记录仍可能通过显式历史审计模式查看”。

### history_only（高级，显式）
删除指定 `thread` / `turn range` / `turn ids` / `event ids` / `time range`（#16/#17），并处理派生依赖：受影响 `SegmentSummary`(#6)/`EpochCheckpoint`(#7)/`RetrievalIndexEntry`(#12)/`MemoryEvidence`(#3)；仅以被删来源为证据的 `MemoryRecord` 要重算 evidence/置信度（见 L）。**不得**删其他 Thread。

### everywhere（默认自然语言“忘记 X”的语义）
全部进入同一 Forget Saga（#1–#15 相关项 + 派生快照 + Proposal/Evidence + 所有可重生成来源）。`memory_only`/`history_only` 仅作显式高级模式，不得由关键词推断。

**禁止**：用关键词 if/else 决定模式；自然语言“忘记/忘掉”默认映射 `everywhere`。

---

## H. Forget 数据模型（新 Saga 10 张表）

> 当前遗忘流程仅使用以下 10 张表：
> `forget_operations`、`forget_selector_manifests`、`forget_shields`、`forget_targets`、`forget_dependencies`、`forget_batches`、`forget_actions`、`forget_tombstones`、`forget_stage_runs`、`content_provenance_refs`。旧 `forget_requests` 已由后续迁移删除，不再提供 ORM 映射。

### 不可变层级（§3）
```text
ForgetSelectorManifest  ← Phase A 冻结 selector + cutoff（不可变）
ForgetTarget            ← 有界批次确定性物化（冻结后不重发现/扩大）
ForgetDependency        ← 由 Target 确定性推导的依赖（冻结后不重发现）
ForgetAction            ← 每个具体清理动作（幂等，由 Batch 驱动）
```

### `forget_operations`（Saga 根，替换原 `forget_requests` 的职责）
```text
id              PK String(36)
operation_key   String(128) UNIQUE          # 幂等键，跨重试/接管复用（stage operation_id = forget:{id}:{stage}）
mode            String(16)                   # memory_only|history_only|everywhere
selector_type   String(32)
selector_hash   String(64)                  # 结构化选择器的确定性哈希
status          String(32)                  # requested→shielded→cascading→verifying→purge_ready→purging→purged→failed_retryable→deadletter→shielded_deadletter
requested_by    String(36)                  # 触发 trace_id / user
reason_code     String(64)
target_count    Integer
shielded_at     DateTime nullable
verified_at     DateTime nullable
purged_at       DateTime nullable
error_message   Text nullable
legacy_request_id String(36) nullable       # 历史导入来源标识；新流程不写入
created_at / updated_at
```
CHECK(`status IN (...)`)，UNIQUE(`operation_key`)。

### `forget_selector_manifests`（§3，Phase A 冻结不可变规范化 selector）
```text
id                  PK
forget_operation_id FK forget_operations.id
selector_type       String(32)
selector_payload    JSON                       # 不可变规范化 selector：IDs/scope/时间范围/canonical_key 等结构化条件，不保存匹配原文
selector_payload_hash String(64)              # 结构化选择器 JSON 的确定性哈希
cutoff_created_at   DateTime                  # 冻结的时间 cutoff（仅处理早于该时刻的数据）
frozen_at           DateTime
UNIQUE(forget_operation_id, selector_payload_hash)   # 重试不重复物化
```
写入后不可修改；**Phase B 只能从该 `selector_payload` 物化 Target**；重试/接管复用同一份。

### `forget_shields`（§2，选择器级 + 实体级即时屏蔽）
```text
id                  PK
forget_operation_id FK forget_operations.id
selector_type       String(32)               # memory_ids|canonical_key|thread|turn_range|event_ids|time_range|all_user_data
target_type         String(32) nullable       # 实体级 Shield（memory_record/turn_record/event/...）时填
target_id           String(36) nullable
canonical_key       String(256) nullable
value_fingerprint   String(64) nullable       # HMAC-SHA256(secret, canonical_value)，见 §H 内容指纹
thread_id           String(36) nullable
time_from           DateTime nullable
time_to             DateTime nullable
scope_type          String(32) nullable
scope_id            String(128) nullable
all_user_data       Boolean default False
cutoff_created_at   DateTime                  # 同 selector_manifest 的 cutoff
status              String(16)                # active | superseded
normalized_shield_key String(256)               # 规范化幂等键（确定性哈希），用于重试去重
created_at
UNIQUE(forget_operation_id, normalized_shield_key)   # 禁止依赖包含 nullable 列的普通 UNIQUE 保证幂等
```
所有读取路径先检查 Shield（按 selector_type 命中 thread/time/scope/all_user_data/canonical_key 的选择器 Shield，或按 target_type/target_id 的实体级 Shield）快速 fail-closed，再检查逐条 Tombstone。**仅 `memory_ids` / `turn_ids` / `event_ids`（≤ `max_inline_shield_targets`）在 Phase A 写实体级 Shield**；`canonical_key` / `thread` / `time_range` / `scope` / `all_user_data` / 超限 ID 只写选择器 Shield，Target 与 Tombstone 由 Cascade Batch 处理。一个 broad selector 可物化多条 Shield 行，但 Phase A 只写 Shield，不枚举全部 Target。

### `forget_targets`（§3，有界批次确定性物化，冻结后不可变）
```text
id                  PK
forget_operation_id FK forget_operations.id
target_type    String(32)   # memory_record|thread|turn_record|event|segment|segment_summary|epoch|epoch_checkpoint|retrieval_entry|proposal|working_state
target_id      String(36)
source_version String(64) nullable
source_hash    String(64) nullable
scope_type     String(32) nullable
scope_id       String(128) nullable
canonical_key  String(256) nullable
value_hash     String(64) nullable              # 确定性内容哈希（用于幂等/重算，非安全指纹）
batch_no       Integer      # 物化批次
frozen_at      DateTime
UNIQUE(forget_operation_id, target_type, target_id)   # 重试不重复物化
```
**不保存原文**。写入后不可修改；已冻结批次不得在重试时重新发现或扩大。

### `forget_dependencies`（§3，由 Target 确定性推导）
```text
id                  PK
forget_operation_id FK forget_operations.id
target_id           FK forget_targets.id nullable
dependency_type     String(32)   # proposal|evidence|lineage|summary|checkpoint|retrieval_entry|retrieval_token|maintenance_snapshot|working_state|compaction_input|raw_event|raw_turn|content_provenance
dependency_id       String(36)
discovery_batch_no  Integer
status              String(16)   # pending|resolved|skipped
discovered_at       DateTime
UNIQUE(forget_operation_id, dependency_type, dependency_id)   # 重试不重复物化
```
仅基于已冻结 Target + 真实 provenance 推导（不再次扫描全表），重试复用同一批。

### `forget_batches`（§4，stage cursor 表，复合游标）
```text
id                  PK
forget_operation_id FK forget_operations.id
stage               String(16)   # shield|cascade|rebuild|recompute|scrub|purge|verify
dependency_type     String(32) nullable
batch_no           Integer
cursor_lane         String(32)   # 续跑 lane 标识（按依赖类型/source 分 lane）
cursor_start_json   JSON         # 复合续跑游标起始（时间+sequence+ID），禁止用数据库 offset
cursor_end_json     JSON nullable
cutoff              DateTime    # 同 selector cutoff
status              String(16)   # pending|running|done|failed
input_hash          String(64)   # 批次输入确定性哈希（防重放/校验）
action_count        Integer
completed_count     Integer
created_at / updated_at
UNIQUE(forget_operation_id, stage, dependency_type, batch_no)   # 重试不重复物化
```
恢复时优先处理未完成 Batch；正常分页使用 `HandlerOutcome.CONTINUE`，**不得计为失败**（不增加 `retry_count`）。不同 lane 保存对应的（时间, sequence, ID）复合游标，**禁止使用数据库 offset**；崩溃后无跳过、无重复。

### `forget_actions`（§4，每个具体清理动作，幂等）
```text
id                  PK
forget_operation_id FK forget_operations.id
target_id           FK forget_targets.id nullable
dependency_id       FK forget_dependencies.id nullable
action_type    String(32)   # shield|delete_evidence|scrub_proposal|rebuild_summary|rebuild_checkpoint|recompute_evidence|scrub_entry|delete_token|scrub_maintenance_snapshot|scrub_working_state|scrub_raw_event|scrub_raw_turn|physical_delete|verify
batch_no       Integer
status         String(16)   # pending|running|done|skipped|failed
idempotency_key String(128) UNIQUE
expected_version Integer nullable
before_hash    String(64) nullable
details        JSON nullable
started_at / completed_at / error_message
```
UNIQUE(`idempotency_key`)。

### `forget_stage_runs`（§7，Stage ↔ Outbox 关联，原子 deadletter）
```text
id                  PK
forget_operation_id FK forget_operations.id
stage               String(16)   # shield|cascade|rebuild|recompute|scrub|purge|verify
outbox_job_id       FK outbox_jobs.id NOT NULL UNIQUE   # 每个 stage 一个 Job，强关联
status              String(16)   # pending|running|done|failed|deadletter
execution_token     String(64) nullable
claim_count         Integer default 0
failure_count       Integer default 0
created_at / updated_at
```
OutboxJob `payload.forget_operation_id` 关联；stage operation_id = `forget:{forget_operation_id}:{stage}`。deadletter 时：`forget_operations.status = shielded_deadletter`、`forget_stage_runs.status = deadletter`、**`forget_shields.status` 保持 `active`**（不弱化为 superseded/删除），三者**原子**更新（见 P）；后台清理 deadletter 后可通过 reconciler 恢复 Shield 对应的 stage 继续执行；**不得因后台清理失败解除或弱化 Shield**。

### `forget_tombstones`（§3/§7，fail-closed + 防重抽 + 内容指纹 + 可审计性）
```text
id                  PK
forget_operation_id FK forget_operations.id
target_type         String(32)
target_id           String(36)
canonical_key       String(256) nullable
scope_type          String(32) nullable
scope_id            String(128) nullable
value_fingerprint   String(64) nullable              # HMAC-SHA256(secret, canonical_value)，见 §H 内容指纹
fingerprint_key_version Integer nullable
block_visibility    Boolean default True            # 读取路径 fail-closed 屏蔽
block_reingestion   Boolean default True            # 拦截旧来源重新抽取
content_purged      Boolean default False           # 内容是否已不可逆 scrub/删除（审计模式也不可绕过）
allow_audit_read    Boolean default False           # 是否允许经授权审计模式绕过可见性查看原始内容（memory_only=true）
source_event_id     String(36) nullable
source_turn_record_id String(36) nullable
reason_code         String(64)
created_at / purged_at
UNIQUE(forget_operation_id, target_type, target_id)   # 核心幂等（不依赖 nullable canonical_key）
UNIQUE(forget_operation_id, source_event_id) WHERE source_event_id IS NOT NULL   # partial unique，DDL 用 sqlite_where/postgresql_where
UNIQUE(forget_operation_id, source_turn_record_id) WHERE source_turn_record_id IS NOT NULL
```
**不含可恢复用户原文**。普通 SHA-256 低熵 `value_hash` 不得作为不可恢复指纹；须用 `value_fingerprint`（HMAC）。索引：`(canonical_key, scope_type, scope_id, value_fingerprint)`、`(source_event_id)`、`(source_turn_record_id)`。
拦截规则见 E 节 §7。四种效果：`block_visibility`（读取屏蔽）、`block_reingestion`（防重抽）、`content_purged`（内容已不可逆清除，任何模式不可见）、`allow_audit_read`（是否允许经授权审计绕过可见性）。审计可见性规则：仅当 `allow_raw_history && allow_audit_read && !content_purged` 同时成立，才能绕过普通可见性屏蔽。memory_only 旧 Event/Turn：`block_visibility=true, block_reingestion=true, content_purged=false, allow_audit_read=true`（保留 raw history，审计可读）；history_only/everywhere：`allow_audit_read=false`（即使在 scrub 完成前，审计模式也不可见）。

### 内容指纹（§5，长期保留）
`value_fingerprint = HMAC-SHA256(secret, canonical_value)`，`fingerprint_key_version` 记录密钥版本。普通 SHA-256 的 `value_hash` 熵低、可能被字典反推，**不得**作为不可恢复 tombstone 指纹；`value_hash` 仅用于幂等/重算（确定性哈希）。密钥由配置注入，轮转时 bump `fingerprint_key_version`。

### `content_provenance_refs`（§8，规范化逐行引用）
```text
id              PK
owner_type      String(32)   # llm_call|context_snapshot|artifact|outbox_job|maintenance_snapshot|log
owner_id        String(36)
source_type     String(32)   # memory_record|turn_record|event
source_id       String(36)
created_at
UNIQUE(owner_type, owner_id, source_type, source_id)
INDEX(source_type, source_id)            # 按源 ID 有界反查
```
供 ForgetVerifier 按 `source_type+source_id` 有界反查并定位需 scrub 的副本（llm_calls / context_snapshots / artifacts / outbox_jobs / maintenance_snapshots）；旧数据无此表则走粗粒度/legacy 标记（见 N）。

---

## I. Phase A：立即屏蔽（唯一必须优先完成的短事务，含 ForgetShield）

事务内（单 `ForgetSagaService` 短事务，**不持有长事务、不枚举整个 Thread/时间范围/all_user_data**）：

1. `INSERT forget_operations(status='requested')` + `operation_key`（幂等键）；
2. 冻结 `forget_selector_manifests`（写入 `selector_payload` 不可变规范化 selector + `cutoff_created_at`）；
3. 物化选择器级 `forget_shields`（按 thread/time/scope/all_user_data/canonical_key 写 selector Shield 行；**不展开到逐条 Target**）；
4. 仅对有界显式 ID（`memory_ids` / `turn_ids` / `event_ids`，且数量 ≤ `max_inline_shield_targets`）**立即写实体级 `forget_shields`**（`target_type`/`target_id` + `normalized_shield_key`）；超出上限的 ID 集合退化为 selector Shield + Cascade Batch 处理；
5. 仅对显式 Memory ID（有限量）冻结 `forget_targets`；以下选择器不在 Phase A 枚举 Target/Tombstone：`canonical_key` / `thread` / `time_range` / `scope` / `all_user_data` / 超出上限的 ID 集合（Target、Tombstone、投影清理由 Cascade Batch 完成）；
6. `INSERT forget_tombstones`：fail-closed 立即生效；memory_only 的旧 Event/Turn 写 tombstone 取 `block_visibility=true, block_reingestion=true, content_purged=false, allow_audit_read=true`（保留 raw history，审计可读）；history_only/everywhere 取 `allow_audit_read=false`（即使在 scrub 完成前，审计模式也不可见）；
7. 目标 `MemoryRecord.lifecycle_state = forgotten`（fail-closed 读取即不可见）；
8. `CoreMemoryBlock` 删除/清空（下一轮 `core_memory_refresh` 自然重建，或 Phase A 内直接删相关 block 行）；
9. `Memory retrieval projection`（`RetrievalIndexEntry`）立即 tombstone（清正文 + 删 token）；
10. `WorkingState` 中匹配目标的条目 scrub（见 O）；
11. `INSERT OutboxJob(operation_id="forget:{forget_operation_id}:cascade")`，`payload.forget_operation_id` 关联 `forget_stage_runs`；
12. `forget_operations.status='shielded'`, `shielded_at=now`；
13. commit。

**只有 Phase A 成功，工具才返回 `forget accepted and shielded`**。对于 `memory_only`/`history_only`/`everywhere`，即使后续异步失败也不可返回目标内容（Shield + Tombstone 双保险）。禁止只在创建异步 Job 尚未屏蔽时声称已忘记。

---

## J. 依赖发现与不可变 Manifest（§3）

Phase B（独立 Job `forget:{forget_operation_id}:cascade`）：
- 根据冻结 `forget_selector_manifests.selector_payload`（不可变规范化 selector）物化 `forget_targets`（有界批次、写后即冻），并结合已冻结 `forget_targets` + 真实 provenance（`MemoryEvidence.source_event_id`、Summary/Checkpoint 的 `source_event_ids`/`source_segment_ids`、`RetrievalIndexEntry.source_id`、CompactionInput manifest）定位 `Proposal`/`Evidence`/`Lineage`/`Summary`/`Checkpoint`/`Retrieval projection`/`Maintenance snapshot`/`Compaction manifest`/`raw Event/Turn`/`WorkingState`。
- **有界批次确定性物化**：将发现结果写入 `forget_dependencies`（按 `discovery_batch_no`），并生成幂等 `forget_actions`（UNIQUE `idempotency_key`），登记到 `forget_batches`。
- **已冻结批次不得在重试时重新发现或扩大**——只读冻结 Manifest，重试复用同一批 Dependency/Action（`forget_batches` 续跑）。

---

## K. Summary / Checkpoint 原子重建（§6，复用 P3 纯生成组件，不复用 sealed handler）

抽取并复用 P3 的**纯摘要生成与 cover-check 组件**（不调用 `handle_segment_sealing` 的完整 sealing 流程，避免触发不适用 sealed 状态的副作用）：

- `segment_summary_builder.build(summary_inputs, remaining_sources)`（复用 `_build_summary_prompt` + `merge_summary` + `_run_cover_checks`）；
- `epoch_checkpoint_aggregator.aggregate(epoch_id, new_summaries)`（复用确定性聚合逻辑）。

### Segment 原子重建（同事务提交）
```text
写新 SegmentSummary version（summary_version+1，复用 (segment_id, summary_version) UNIQUE）
更新 Segment.summary_id = 新 id
enqueue retrieval refresh/tombstone（旧 entry tombstone、新 entry 写入；非同步刷新）
旧 Summary 保持隐藏（is_searchable=False + retrieval tombstone）
以上同事务提交
```

### Epoch 原子重建（同事务提交）
```text
写新 EpochCheckpoint version（checkpoint_version+1）
更新 Epoch.checkpoint_id = 新 id
enqueue retrieval refresh/tombstone（旧 entry tombstone、新 entry 写入；非同步刷新）
旧 Checkpoint 保持隐藏
以上同事务提交
```

受影响 `SegmentSummary`：
1. 立即使旧 Summary 不可检索（`retrieval_index_refresh` tombstone + `is_searchable=False`）；
2. 判断剩余 `source_turn_ids`/`source_event_ids`；
3. 有剩余 → 复用 P3 生成新 `summary_version`；
4. 无剩余 → 生成确定性 empty/redacted Summary（固定占位文本，如 `[redacted: all source forgotten]`）；
5. 旧 Summary 内容进入 scrub/purge；
6. enqueue retrieval refresh/tombstone（旧 entry tombstone、新 entry 写入；非同步刷新）。

受影响 `EpochCheckpoint`：
1. 旧 Checkpoint 立即不可检索；
2. 依据新 SegmentSummary 重新聚合；
3. 必要时从受影响 Epoch 向后重建依赖 Checkpoint（按 `epoch_id` 顺序）；
4. 新版本切换完成后清理旧内容。

**不新增第二套摘要 LLM**；复用 P3 compaction。

---

## L. Evidence 重算

删除 Event/Turn 时：
- 删对应 `MemoryEvidence`；
- `MemoryStore.count_independent_evidence(memory_id)` 重算独立 evidence 数（已存在，按 `COUNT(DISTINCT source_event_id)` 且 `Event.event_type='user_message'`）；
- 重算 `confidence`/`lifecycle`：
  - 仍有可靠证据 → 保留 `MemoryRecord`，刷新版本（bump `record_version`，走共享 `MemoryMutationExecutor`）；
  - 无剩余证据 → `lifecycle_state=forgotten`；
- `effective_pinned`（`pinned` 或 `retention_policy='pinned'`）**不得**阻止用户明确 forget；
- `user_required`（`is_user_required_protected`）**不得**阻止用户明确 forget；
- **明确 forget 优先级高于所有保留策略**。

---

## M. 内容 scrub 与物理 purge（§5 修订 purged 定义）

### Phase E scrub（小批量，不成长事务）
- 目标字段：`content`/`structured_value`/`raw_payload`/`normalized_payload`/`content_span`/`search_text`/`snippet`/`metadata` 中的用户内容 / `snapshot_json` 中的用户内容 / 工具结果正文 / `working_states.*` / `segment_summaries.*` / `epoch_checkpoints.*` / `compaction_inputs.working_state_snapshot` / `epoch_compaction_inputs.*` / `memory_maintenance_inputs.snapshot_json` / `artifacts.content` / `context_snapshots.context_items` / `llm_calls.*_preview`。
- 保留最小审计信息仅限：`ID` / `类型` / `时间` / `哈希` / `状态` / `reason_code` / `forget_operation_id`。

### Phase F 物理 purge（§5 修订：**默认不可逆 scrub，非默认删整行**）
- 只有以下条件全部成立才能进入 purge：
  - 所有读取路径已屏蔽（Shield + Tombstone）；
  - 所有派生索引已 tombstone；
  - 受影响 Summary/Checkpoint 已重建或失效；
  - 所有内容承载表已 scrub；
  - 所有 Action 达到终态；
  - `ForgetVerifier` 预检查通过（见 N，且非 `legacy_unverifiable` 阻断）。
- **purge 行为**：
  - `Event` / `TurnRecord`：**默认不物理删除结构行**，而是不可逆 scrub 内容——保留 `id`、`turn_sequence`/`turn_event_index`、`event_type`、非内容审计 hash；清空 `payload`/`response_payload` 中的用户正文。
  - 仅当确认该结构行**无 FK、无 manifest、无排序依赖、无 tool 配对依赖**时，才物理删整行（如孤立的 `memory_records`、`memory_proposals`、`memory_evidence`、`memory_maintenance_inputs` 等）。
  - `retrieval_index_tokens` 随 entry tombstone 删除（FK CASCADE）。
- `purged` 表示“不再存在可恢复用户正文”，**不要求所有结构行消失**。
- 物理删除（仅安全行）应小批量进行，不得长事务级联删除大量数据。

---

## N. ForgetVerifier（可验证终态，含 §8 provenance）

必须验证（全部不返回 / 不存在 / 不含）：
- 默认 `ContextAssembler` 不返回（含 recent history 已屏蔽目标 Event）；
- `UnifiedRetriever` auto 不返回；
- `search` / `deep` 不返回；
- `include_archived` 不能绕过 forgotten；
- `memory_search` / `history_search` 不返回；
- CoreMemory 不返回；
- Retrieval token 不存在；
- Index Entry 无敏感正文；
- WorkingState 不包含；
- Summary/Checkpoint 不包含；
- Memory Extraction 不会重新创建（查 `forget_tombstones`）。

### provenance 验证（§8）
- 对 `content_provenance_refs` 中按 `source_type+source_id` 有界反查的副本（llm_calls / context_snapshots / artifacts / outbox_jobs / maintenance_snapshots），定位仍需 scrub 的 owner 行；
- 对**无 provenance 的旧数据**：按 thread/turn/trace/time range 粗粒度 scrub，或标记 `legacy_unverifiable`；
- Verifier 输出三态之一：
  - `verified`：所有目标内容已移除且 provenance 可验证；
  - `verified_with_coarse_purge`：存在无 provenance 数据，已做粗粒度 scrub（可接受为已 purge，但记录 coarse 标记）；
  - `legacy_unverifiable`：存在未清理的 legacy 无 provenance 内容。
- **存在未清理的 `legacy_unverifiable` 内容时，不得标记完全 `purged`**（status 停留在 `purge_ready` 或等价态，待人工/粗粒度清理后重试 verify）。

通过后：`forget_operations.status='purged'`, `purged_at=now`；最小 `forget_tombstones` 保留（不含原文，可长期保留）。

---

## O. 统一 fail-closed VisibilityService

新增 `ForgetVisibilityService`（单例，无状态、不持有长事务）：
- **先查 Shield 再查 Tombstone**：`is_shielded(selector_ctx)` 检查 `forget_shields`（按 selector_type 命中 thread/time/scope/all_user_data，或按 target_type/target_id、canonical_key、value_fingerprint 实体级命中；含 `cutoff_created_at`）；`is_forgotten(target_type, target_id)` / `batch_is_forgotten(ids)` 查 `forget_tombstones.block_visibility` + `memory_records.lifecycle_state='forgotten'`；
- `filter_retrieval_hits(hits)`：过滤 RetrievalHit；
- `filter_raw_events(events)`：DEEP 回溯时过滤被屏蔽 Event（先 Shield 后 Tombstone）；
- `filter_summary/checkpoint(...) / filter_core_memory(...) / filter_working_state(...)`；
- 显式历史审计模式：`allow_raw_history=True`（经审计鉴权）可绕过 `block_visibility` Shield 查看保留 raw history，但**必须 `allow_audit_read=true` 且 `content_purged=false`**——只有 memory_only 的 raw Event/Turn 满足此条件；history_only/everywhere 的 `allow_audit_read=false`，审计模式也不可见；
- 异常时 **fail-closed（默认不可见）**，不是 fail-open。
所有读取路径（ContextAssembler、Core Memory loader、AutomaticRecallEngine、UnifiedRetriever、memory_search、history_search、deep raw expansion、memory_timeline、memory_event_log、SegmentSummary loader、EpochCheckpoint loader、WorkingState、前端审计 API）统一调用本服务，禁止各模块自行实现 forgotten 判断。

---

## P. Outbox / 续跑 / fencing / deadletter / reconciler

复用 P0.5B 现有 `OutboxWorker`（claim/lease/fencing）与 `HandlerOutcome`：
- 新增 job types（注册进 `recall_config.ENABLED_OUTBOX_JOB_TYPES`）：
  - `forget_cascade`（Phase B/J 依赖发现 + 生成 actions/batches）
  - `forget_rebuild_dependencies`（Phase C/K/L Summary/Checkpoint 重建 + Evidence 重算）
  - `forget_purge`（Phase E/M scrub + 物理删除）
  - `forget_verify`（Phase N verifier）
  - `forget_reconcile`（补发遗漏 Job）
- stage operation_id：`forget:{forget_operation_id}:cascade|rebuild|purge|verify|reconcile`。
- OutboxJob 与 `forget_operations` 强关联（`payload.forget_operation_id` 关联 `forget_stage_runs.outbox_job_id`）；同一逻辑操作复用同一 Job；retry/takeover 不创建重复 Operation（按 `operation_key` UNIQUE）。
- 使用 claim/lease/fencing（`claim_token`/`lease_expires_at`）—— 旧 execution_token 无法提交（参考 `_finalize_job` 的 `affected != 1 → FencingViolationError`）。
- 正常分页用 `HandlerOutcome.CONTINUE`（不增加 `retry_count`/失败计数，参考 `_continue_later`）。
- Handler 不直接 finalize OutboxJob（由 Worker 负责）。
- deadletter 时：`ForgetOperation.status = shielded_deadletter`、`ForgetStageRun.status = deadletter`、**`ForgetShield.status` 保持 `active`**（不得解除或弱化），三者与 OutboxJob **原子**进入终态（参考 `_deadletter_job_and_ingestion_run`）；后台 reconciler 按退避策略复用并重置原 Stage Job，恢复 `ForgetOperation` 到该阶段的可执行状态，Shield 始终保持 `active`。
- 修改 `outbox_worker.py`：forget Job 在 deadletter/finalize 时联动更新 `forget_stage_runs`（status/execution_token/claim_count/failure_count）与 `forget_operations`，保证原子性；stage 状态变更通过 `forget_stage_runs` 持久化。
- `ForgetReconcileService` 按 `cascade → rebuild_dependencies → purge → verify` 检查 `ForgetStageRun` 与关联 `OutboxJob` 的真实状态：补发缺失阶段，自动恢复 `failed_retryable` 与 `shielded_deadletter`，对在途 Job 不重复入队。
- `scheduler_daemon` 每分钟分批触发 Forget Saga 对账；`forget_reconcile` Handler 复用同一服务，保留定向恢复入口。正常阶段执行仍统一由 OutboxWorker 承载。

---

## S. 工具 / API

- 合并为一个结构化 `forget` 工具（替换 `forget_memory` 的 scope 推断），参数含 `mode`/`memory_ids`/`canonical_key`/`scope_type`/`scope_id`/`thread_id`/`turn_ids`/`event_ids`/`time_from`/`time_to`/`reason`。
- 新增 `forget_status`（返回各阶段：`forget_operations.status` + `forget_batches` 进度 + `forget_actions` 汇总 + `shielded_at`/`verified_at`/`purged_at`，**不返回已 scrub 的原文**）。
- 工具返回 operation_key + 真实状态；**Phase A 屏蔽完成后才返回成功**；不宣称异步物理 purge 已完成。
- `memory_only` 明确说明原始历史是否保留（仅显式历史审计模式可见）。
- 禁止工具名与真实行为不一致。
- 工具经 `ToolRegistry` 注册（`risk_level`、`requires_confirmation`、`schema`、`trace_id`、`action_card` 一致现有约定）。
- API：`POST /api/forget` 仅接受严格 JSON Body（`mode` 枚举、selector 组合、scope 成对及时间范围校验；敏感 ID/reason 不进入 URL），`requested_by` 由服务端生成审计请求 ID；`GET /api/forget/{operation_key}/status` 查询状态。所有遗忘入口均委托新 Operation，不写入旧表。

---

## T. Migration（增量方案，§9）

- **Alembic 迁移链**：baseline 创建新 Saga 表及旧兼容表；后续增量迁移在当前 head 删除 `forget_requests`，不修改已发布 baseline。
- **删除旧 `forget_requests`**：新流程已完全使用 `forget_operations` 等 Saga 表，旧开发数据不再保留；upgrade 删除旧表及索引，downgrade 只重建空表结构，无法恢复已删除数据。
- **新 Saga 表（10 张，与 §H / ORM / migration 一致）**：`forget_operations`、`forget_selector_manifests`、`forget_shields`、`forget_targets`、`forget_dependencies`、`forget_batches`、`forget_actions`、`forget_tombstones`、`forget_stage_runs`、`content_provenance_refs`（见 H）。
- **FK / UNIQUE / CHECK**：见 H 各表；所有子表 FK 列统一命名 `forget_operation_id` → `forget_operations.id`；唯一约束：`forget_operations.operation_key` UNIQUE、`forget_selector_manifests(forget_operation_id, selector_payload_hash)`、`forget_targets(forget_operation_id, target_type, target_id)`、`forget_dependencies(forget_operation_id, dependency_type, dependency_id)`、`forget_batches(forget_operation_id, stage, dependency_type, batch_no)`、`forget_actions.idempotency_key` UNIQUE、`forget_tombstones(forget_operation_id, target_type, target_id, canonical_key)`、`forget_stage_runs.outbox_job_id` UNIQUE、`content_provenance_refs(owner_type, owner_id, source_type, source_id)` UNIQUE。
- **PostgreSQL 与 SQLite 兼容**：所有 DDL 用 SQLAlchemy `op.create_table` / `op.alter_column` 跨库语法；唯一部分索引保持 `postgresql_where`/`sqlite_where` 双后端写法。
- **upgrade / downgrade**：删除旧表迁移支持双向 schema 变更；downgrade 重建空的 `forget_requests`，但不恢复历史行。
- **历史 forgotten 数据策略**：旧 `forget_requests` 开发数据随表删除，不导入新 Operation；`memory_records` 中既有 forgotten 数据仍按下述策略处理。
- **当前已 forgotten 但未 purge 的数据迁移策略（§8 修订）**：旧 `memory_records.lifecycle_state='forgotten'` 行内容已是 tombstone 串（无用户原文），**不得直接迁移为 `purged`**。迁移为 `shielded` 或 `legacy_unverifiable`，随后走 Cascade + Verifier 后才能真正 `purged`。
- **不在 migration 中执行内容删除 / Summary 重建 / 索引 backfill / 修改已部署历史 migration**。

---

## U. 精确修改文件（Phase 6A 实现阶段清单，本次不执行）

**新增**：
- `backend/aiive/forget/forget_saga_service.py`（Phase A–F 编排）
- `backend/aiive/forget/forget_visibility_service.py`（O，含 Shield 检查）
- `backend/aiive/forget/forget_verifier.py`（N，含 provenance 三态）
- `backend/aiive/forget/forget_rebuild.py`（K/L 重建 + Evidence 重算，复用 P3 纯生成组件）
- `backend/aiive/worker/handlers_forget.py`（P：5 个 Outbox handler）
- `backend/aiive/db/forget_models.py`（H 10 张表，含 `forget_stage_runs` + `content_provenance_refs`）
- `backend/alembic/versions/<new>_phase6a_forget_saga.py`（T）
- `backend/aiive/tools/forget_tool.py`（S，替换 builtin 内 forget）
- `backend/aiive/api/routes_forget.py`（S）

**修改**：
- `backend/aiive/db/models.py`（新增 10 张表；`ForgetRequest` 保留不动）
- `backend/aiive/memory/memory_write_service.py`（`forget()` 改为只创建 Operation + Phase A；`write/write_batch` 加 tombstone 拦截 §7）
- `backend/aiive/memory/memory_store.py`（读路径接 VisibilityService）
- `backend/aiive/memory/memory_maintenance.py`（扫描跳过 forgotten/tombstone，不重复处理）
- `backend/aiive/memory/memory_extraction*.py` / `remember_or_update`（入口加 tombstone 拦截：旧 source_event_id/source_turn_record_id 拦截，新输入放行）
- `backend/aiive/retrieval/unified_retriever.py`（memory/索引 route 接 VisibilityService）
- `backend/aiive/retrieval/raw_history_expander.py`（DEEP 接 VisibilityService，先 Shield 后 Tombstone）
- `backend/aiive/runtime/context_assembler.py`（`_load_segment_summaries`/`_load_epoch_checkpoint`/`_load_history_bounded` 接 VisibilityService；recent history 屏蔽目标 Event）
- `backend/aiive/memory/core_memory_projection.py`（已安全，确认）
- `backend/aiive/worker/outbox_handlers.py`（注册新 handler）
- `backend/aiive/worker/outbox_worker.py`（forget Job deadletter/finalize 联动 `forget_stage_runs` + `forget_operations` + `forget_shields` 原子更新；支持 `shielded_deadletter`）
- `backend/aiive/memory/recall_config.py`（`ENABLED_OUTBOX_JOB_TYPES` 加 5 个 forget_*）
- `backend/aiive/tools/builtin_tools.py`（移除旧 `forget_memory` scope 推断，或委托新工具）
- `backend/aiive/api/routes_memories.py`（旧 forget endpoint 委托新 Operation，保留兼容）
- `frontend/...`（forget_status action_card 展示各阶段；审计 API 接 VisibilityService）

**日志审计（E 第 20 项）**：排查 `logger` 是否打印 Memory/Event/Prompt 正文，必要时降噪。

---

## V. Phase 6A 测试矩阵（覆盖 spec 第 19 节相关项 + §10 新增）

单元测试 + 集成测试（SQLite，复用现有 `tests/` 框架）：
1. archive 与 forget 语义不同；
2. Phase A 后所有读取路径立即过滤（Shield + Tombstone 双保险）；
3. memory_only 删 Memory 表示但保留 raw history；
4. **memory_only 不会通过 recent messages / Summary / Checkpoint 自动重新出现**（§10 新增）；
5. memory_only 不会从旧来源重新提取（tombstone 拦截旧 source_event_id/turn_record_id）；
6. history_only 删指定 Turn/Event；
7. history_only 不删其他 Thread；
8. everywhere 清理 Memory+History+Summary+Checkpoint+Index；
9. pinned 明确 forget 后仍被删；
10. user_required 明确 forget 后仍被删；
11. ForgetTarget Manifest 不可变（冻结后重试不扩大）；
12. Manifest 不保存原文；
13. 重复 Operation 幂等（operation_key UNIQUE）；
14. 旧 execution_token 无法提交（fencing）；
15. CONTINUE 不计失败（forget_batches 续跑不增 retry_count）；
16. shield 后 crash 仍不可见；
17. 部分 cascade 后 crash 可接管（相同 Target/Dependency Batch，§10 新增）；
18. Evidence 删后仍有其他证据则保留 Memory；
19. 无剩余证据则 Memory forgotten；
20. 受影响 SegmentSummary 新版本不含删除内容；
21. 空 Segment 生成 deterministic redacted Summary；
22. 受影响 EpochCheckpoint 重建；
23. **Summary/Checkpoint 新版本与指针原子切换（同事务提交，§10 新增）**；
24. Summary 重建失败时旧 Summary 仍不可见；
25. forgotten RetrievalIndexEntry 清空正文；
26. forgotten RetrievalIndexToken 删除；
27. archived Entry 不被错误 tombstone；
28. deep history 不能绕过 Tombstone（可经审计绕过 Shield 看 raw history，但 purged 原文不可见）；
29. memory_timeline 不泄漏；
30. WorkingState 不泄漏；
31. CoreMemoryBlock 删除；
32. Maintenance snapshot 中内容被 scrub；
33. Proposal raw_payload/normalized_payload 被 scrub；
34. **Event/Turn scrub 后 sequence 与 tool 配对仍完整（§10 新增，按 §5 purged 定义）**；
35. ForgetVerifier 检出残留副本；
36. verifier 通过后 Operation 才能 purged；
37. **无 provenance 的 legacy snapshot 不会被误标为 fully verified（legacy_unverifiable 阻断，§10 新增）**；
38. 新用户输入可重新建立新事实（新 event_id/turn_record_id 放行）；
39. **旧已忘记 Event 不能重建（tombstone 拦截，§10 新增）**；
40. broad selector 不越 scope（ForgetShield scope guard）；
41. **all_user_data Phase A 只写一条 selector Shield 即立即不可见（§10 新增）**；
42. **大范围选择器不在 Phase A 枚举全部目标（§10 新增）**；
43. deadletter 状态原子；
44. reconciler 补发遗漏任务（基于 forget_batches）；
45. PostgreSQL upgrade/downgrade；
46. SQLite upgrade/downgrade；
47. 任意阶段 crash 后不泄漏 forgotten 内容；
48. P0.5A～P5 全量回归（6A 不引入回归）；
49. **空库升级到 head 后不存在 `forget_requests`，且新遗忘流程仅写新 Saga 表**。

### Phase 6A 补充测试（§局部修订新增）
50. selector manifest 可在进程重启后完整恢复原选择器（`selector_payload` 不可变）；
51. `memory_ids` / `turn_ids` / `event_ids`（≤ `max_inline_shield_targets`）在 Phase A 可立即实体级 Shield，超限退化为 selector Shield；
52. memory_only 原始历史普通模式不可见、审计模式可见（`block_visibility` 放行但保留 raw history）；
53. `content_purged` 后审计模式也不可见（`content_purged` 不可绕过）；
54. reingestion block 不会错误阻止新 Turn/Event（新 source id 放行）；
55. 重试不会重复 Target / Dependency / Shield（UNIQUE 约束 + 幂等物化）；
56. 低熵 `value_hash` 无法通过数据库普通 hash 字典反推（须用 HMAC `value_fingerprint`）；
57. provenance 可通过 `source_type+source_id` 索引有界反查（`content_provenance_refs`）；
58. stage Job deadletter 后 Shield 仍为 `active`、Operation 为 `shielded_deadletter`、StageRun 为 `deadletter`（不弱化 Shield）；
59. 旧 forgotten 数据不会未经 Verifier 被标成 `purged`（迁移为 `shielded` / `legacy_unverifiable`）；
60. Summary/Checkpoint 切换与索引 enqueue 同事务（非同步刷新）；
61. P0.5A～P5 全量回归通过。

### Phase 6A 第三次补充测试（§5 点局部修订新增）
62. history_only / everywhere 在 scrub 完成前也不能通过审计模式读取（`allow_audit_read=false`）；
63. memory_only 原始历史可在授权审计模式读取（`allow_audit_read=true && allow_raw_history=true && !content_purged`）；
64. `canonical_key` / `all_user_data` 的 Phase A 不枚举目标（仅 selector Shield）；
65. 超大 ID 列表（>`max_inline_shield_targets`）自动转 selector Shield + Cascade Batch 处理；
66. nullable `canonical_key` 不会导致重复 Tombstone（UNIQUE 不含 canonical_key）；
67. Shield 重试不会重复写入（`normalized_shield_key` UNIQUE 幂等）；
68. 复合游标 crash 后无跳过、无重复（cursor_lane + cursor_start_json 续跑，禁止 offset）；
69. stage deadletter 后 Shield 仍为 `active`（不被 superseded/删除，reconciler 可恢复执行）；
70. `shielded_deadletter` Operation 可由 reconciler 恢复 `shielded` 继续 cascade/rebuild。

---

## X. 回滚与降级方案（Phase 6A）

- **Migration 回滚**：删除旧表迁移的 `downgrade()` 会重建空的 `forget_requests` 结构，但不会恢复已删除数据；继续回滚 baseline 才会删除新 Saga 表。
- **Saga 中途失败**：任何阶段失败 → Outbox 重试（fencing）→ 最终 deadletter（原子）；`forget_operations.status='shielded_deadletter'`、`forget_shields.status` 保持 `active`，内容已 shielded（fail-closed 不可见），不泄漏；可由 reconciler 恢复 `shielded` 继续执行。
- **Verifier 失败**：不进入 `purged`；内容保持 scrubbed + shielded，可人工/定时重试 verify（legacy_unverifiable 需先粗粒度清理）。
- **重建 LLM 失败**：旧 Summary/Checkpoint 保持不可检索（`is_searchable=False` + tombstone），不影响 fail-closed；下次 `forget_rebuild` 重试。
- **降级**：若 `ForgetVisibilityService` 异常 → fail-closed（默认不可见），绝不 fail-open。
- **无 Supervisor LLM**：重建复用 P3 compaction LLM；不新增任何判断“是否该删”的 LLM。
- **无关键词硬编码路由**：模式由主 Agent tool call 显式 `mode` 指定。
- **上下文持续有界**：所有重建/scrub 不向上下文注入额外内容；forget 不增加 token。

---

## 附录：已交叉核对文件

- `backend/aiive/db/models.py`（全部 ORM，含 `ForgetRequest` drift）
- `backend/aiive/alembic/versions/9ce8eac83d4b_v18_forget_requests.py`
- `backend/aiive/memory/memory_write_service.py`（`forget()` 真实行为）
- `backend/aiive/memory/memory_lifecycle_service.py`、`memory_mutation.py`、`memory_store.py`、`memory_maintenance.py`、`core_memory_projection.py`
- `backend/aiive/runtime/context_assembler.py`、`epoch_manager.py`
- `backend/aiive/retrieval/indexing_service.py`、`unified_retriever.py`、`raw_history_expander.py`、`retrieval_types.py`
- `backend/aiive/worker/outbox_worker.py`、`outbox_handlers.py`、`outbox_dto.py`、`scheduler_daemon.py`
- `backend/aiive/memory/recall_config.py`（`ENABLED_OUTBOX_JOB_TYPES`）
- `backend/aiive/api/routes_memories.py`、`tools/builtin_tools.py`、`tools/registry.py`
- `backend/aiive/compaction/*`（P3 摘要 LLM 纯生成组件路径）

---

## 下一步

> Phase 6A 方案级审查通过，可以开始实施。

实施顺序：

```text
migration/models
→ HMAC fingerprint + selector normalization
→ VisibilityService/Phase A Shield
→ Target/Dependency/Batch materialization
→ StageRun/Outbox fencing
→ scrub/rebuild/evidence recompute
→ verifier
→ tools/API
→ integration tests
```

Phase 6B（Retention / vacuum / 长期混沌）见 `phase_6B.md`。
