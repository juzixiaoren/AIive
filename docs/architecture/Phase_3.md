# Phase 3：Segment Sealing、SegmentSummary、EpochCheckpoint 与 Idle Compaction

> **状态**：审计完成，待确认后实施
> **前置**：Phase 0.5A / 0.5B / Phase 1 / Phase 2
> **约束**：不新建 Supervisor LLM；不删除原始 Turn/Event；不修改 Phase 2 Memory Ingestion

---

## A. 当前真实数据模型

### A.1 Epoch（`db/models.py:660`，Phase 1 已建表）

```
epochs           | PK | 列
id               | ✅ | String(36)
thread_id        | ✅ | FK threads.id
epoch_no         | ✅ | BigInteger, UNIQUE(thread_id, epoch_no)
status           | ✅ | String(32), default="active"
start_turn_sequence | ✅ | BigInteger nullable
end_turn_sequence   | ✅ | BigInteger nullable
checkpoint_id    | ✅ | String(36) nullable（指向 EpochCheckpoint.id，当前为空）
sealed_at        | ✅ | DateTime nullable
```

现有 capability：`ensure_epoch_and_segment(db, thread_id, turn_sequence)` 创建 active Epoch + open Segment。

### A.2 Segment（`db/models.py:680`，Phase 1 已建表）

```
segments             | PK | 列
id                   | ✅ | String(36)
epoch_id             | ✅ | FK epochs.id
thread_id            | ✅ | FK threads.id
segment_no           | ✅ | BigInteger, UNIQUE(epoch_id, segment_no)
status               | ✅ | String(32), default="open"
start_turn_sequence  | ✅ | BigInteger NOT NULL, CK constraint
end_turn_sequence    | ✅ | BigInteger nullable, CK >= start_turn_sequence
source_hash          | ✅ | String(64) nullable（供 CompactionRun 校验）
summary_id           | ✅ | String(36) nullable（指向 SegmentSummary.id）
pending_seal_at      | ✅ | DateTime nullable（ContextAssembler 软阈值标记）
sealed_by_turn       | ✅ | BigInteger nullable（标记触发序列号）
created_at           | ✅ | DateTime
sealed_at            | ✅ | DateTime nullable
```

现有状态：`"open"` / `"sealed"`。缺少 `"sealing"` 过渡状态（需新增）。

**约束**：每个 Thread 最多 **1 个** `status="sealing"` Segment。

```sql
CREATE UNIQUE INDEX uq_segment_thread_sealing
ON segments(thread_id)
WHERE status = 'sealing';
```

**唯一合法状态转换**：
- `open → sealing`：仅 `begin_segment_sealing()` 执行
- `sealing → sealed`：仅 SegmentSummary Handler Phase C 在写入有效 Summary 的**同一事务**中执行
- 禁止直接设置 `Segment.status = "sealed"` 的旁路

```sql
CHECK (status <> 'sealed' OR summary_id IS NOT NULL)
```

迁移前审计已有 `sealed + summary_id IS NULL` 数据并处理。

### A.3 SegmentSummary（`db/models.py:709`，Phase 1 建表，从未写入）

```
segment_summaries      | PK | 列
id                     | ✅ | String(36)
segment_id             | ✅ | String(36)
goal                   | ✅ | Text nullable
outcome                | ✅ | Text nullable
decisions              | ✅ | JSON nullable
open_loops             | ✅ | JSON nullable
entities               | ✅ | JSON nullable
artifacts              | ✅ | JSON nullable
important_tool_results | ✅ | JSON nullable
source_turn_range      | ✅ | JSON nullable（`[start_seq, end_seq]`）
source_hash            | ✅ | String(64) nullable
summary_version        | ✅ | int, default=1
model_id               | ✅ | String(128) nullable
token_count            | ✅ | int, default=0
created_at             | ✅ | DateTime
```

**缺失字段**（需 migration）：`source_turn_ids`、`source_event_ids`、`active_constraints`、`unresolved_failures`。

**重要**：source 字段应以 JSON 数组形式追加到 `source_turn_range` 旁边，最小化 migration 增量。

### A.4 EpochCheckpoint（`db/models.py:730`，Phase 1 建表，从未写入）

```
epoch_checkpoints        | PK | 列
id                       | ✅ | String(36)
epoch_id                 | ✅ | String(36)
current_goal             | ✅ | Text nullable
completed_milestones     | ✅ | JSON nullable
open_loops               | ✅ | JSON nullable
active_constraints       | ✅ | JSON nullable
current_decisions        | ✅ | JSON nullable
referenced_artifacts     | ✅ | JSON nullable
relevant_entities        | ✅ | JSON nullable
latest_verified_tool_states | ✅ | JSON nullable
source_segment_ids       | ✅ | JSON nullable
version                  | ✅ | int, default=1
token_count              | ✅ | int, default=0
created_at               | ✅ | DateTime
```

**缺失字段**（需 migration）：`source_hashes`。

### A.5 WorkingState（`db/models.py:750`，Phase 1 已建表，已有 A+B 接线）

```
working_states            | 列
id                        | String(36)
thread_id                 | FK, UNIQUE 1:1
epoch_id                  | nullable
current_objective         | Text
open_loops                | JSON
active_constraints        | JSON
pending_approvals         | JSON（密封时检查）
artifact_refs             | JSON（密封时检查）
verified_tool_states      | JSON（密封时检查）
uncommitted_side_effects  | JSON（密封时检查）
running_tool_state        | JSON（密封时检查）
applied_idempotency_keys  | JSON
token_count               | int
version                   | int（每次写入递增）
```

### A.6 OutboxJob（`db/models.py`，Phase 0.5B 已完整）

已有完整的 claim/lease/status/heartbeat 字段。`job_type` 当前仅有 `"memory_extraction"` 和 `"core_memory_refresh"`。

### A.7 尚无 CompactionRun 表

当前无 CompactionRun 模型——Phase 4 新建。

---

## B. Segment 分配和 pending_seal 链路

### B.1 Turn 创建时的 Segment 分配

`TurnExecutionService._resolve_and_preempt()` → `EpochManager.ensure_epoch_and_segment(db, thread_id, next_seq)`：

1. 查找 active Epoch → 不存在则创建新 Epoch(epoch_no+1, status="active")
2. 查找 status="open" 的 Segment → 不存在则创建新 Segment(segment_no+1, start_turn_sequence=next_seq)
3. Turn.segment_id = segment.id

**关键**：Segment 分配在 Thread 行锁保护下，不存在竞争。

### B.2 pending_seal 触发

`ContextAssembler.assemble()` 中：

```python
# 第 201-209 行
soft_exceeded = final_safe > self._budget.soft_input_limit
if soft_exceeded and attempt == 0:
    epoch_mgr.mark_pending_seal(db, thread.id, assembly_started_at_seq or 0)
```

`EpochManager.mark_pending_seal()` 设置 `Segment.pending_seal_at = datetime.now()` 和 `Segment.sealed_by_turn = turn_sequence`。

**已存在但未自动化**：只标记，不触发任何 sealing 动作。Idle scanner 需读取此字段。

### B.3 移除直接 sealed 旁路

`EpochManager.try_seal_segment()` 直接执行 `Segment.status = "sealed"`，**必须移除**。唯一合法 sealed 路径是 `sealing → Handler Phase C` 在写入 Summary 的同一事务中设置。

迁移前审计并处理已有 `sealed + summary_id IS NULL` 的 Segment（见 L.4）。

### B.4 Turn sequence 分配规则（peek vs allocate）

**审计结论**：Phase 1 **无** `Thread.next_turn_sequence` 计数器列。当前 `turn_execution._next_turn_sequence(db, thread_id)` 在 Thread 行锁保护下计算 `max(TurnRecord.turn_sequence) + 1`，本质是**纯读取**——不 INSERT TurnRecord 就不会推进序列。据此明确区分两个操作：

```text
peek_next_turn_sequence(db, thread_id):
    在 Thread 行锁内读取，返回 max(TurnRecord.turn_sequence)+1，无副作用，不消费

allocate_turn_sequence(db, thread_id):
    创建 TurnRecord 时使用 peek 值作为该 Turn 的 turn_sequence；
    TurnRecord 落库后 max 自然推进，即“消费”发生在此刻
```

**规则**：
- 删除任何 `successor.start_turn_sequence = end_turn_sequence + 1` 的写法。
- 在 Thread 行锁内**读取但不消费**：`successor.start_turn_sequence = peek_next_turn_sequence(db, thread_id)`（概念上等于 `thread.next_turn_sequence`；因无计数器列，实际由 `max+1` 派生）。
- **创建空 successor Segment 不得消耗 Turn sequence**（不 INSERT TurnRecord）。
- 只有创建 TurnRecord 时才调用 `allocate_turn_sequence` 并推进序列。
- 下一个真实 Turn 会 allocate 到与该空 Segment `start_turn_sequence` 相同的值，从而首个 Turn 归属新 Segment。

`begin_epoch_sealing` 中新 Epoch 的 open Segment 起点同样使用 `peek_next_turn_sequence`。

---

## C. Outbox 复用方案

当前基础设施：

| 组件 | 位置 | 状态 |
|------|------|------|
| `OutboxWorker` + `claim` | `worker/outbox_worker.py` | ✅ 完整 |
| `OutboxHeartbeat` + `ActiveClaimRegistry` | `worker/outbox_heartbeat.py` | ✅ 已运行 |
| `HandlerRegistry` + schema version | `worker/handler_registry.py` | ✅ 已注册 2 个 handler |
| `ClaimedJob` / `HandlerResult` / `HandlerOutcome` | `worker/outbox_dto.py` | ✅ 完整 |
| APScheduler 周期 poll | `scheduler_daemon.py` | ✅ `schedule_outbox_poll()` |

**Phase 3 新增**：

```python
registry.register("segment_sealing", handle_segment_sealing,
                   supported_schema_versions=frozenset({1}))
registry.register("epoch_rollover", handle_epoch_rollover,
                   supported_schema_versions=frozenset({1}))
registry.register("epoch_checkpoint", handle_epoch_checkpoint,
                   supported_schema_versions=frozenset({1}))
```

> 三个 job_type **必须**全部进入 `recall_config.ENABLED_OUTBOX_JOB_TYPES`，否则 `OutboxWorker` 拒绝入队/处理（见 I.1 注意）。

Handler 复用三阶段模式（与 `handle_memory_extraction` 相同）：
- Phase A：验证 Outbox claim → 锁定 Segment → 创建 CompactionRun
- Phase B：无 DB Session，事务外 LLM 生成 SegmentSummary
- Phase C：双重 fencing → 原子写入 Summary + 更新 Segment → 标记 CompactionRun

OutboxJob payload 结构：

```json
{
  "schema_version": 1,
  "segment_id": "...",
  "compaction_input_id": "...",
  "source_hash": "...",
  "summary_version": 1
}
```

禁止重复保存 `source_turn_ids`/`source_event_ids`——这些由 CompactionInput 的 `turn_manifest` / `event_manifest` 派生，避免两份不可变来源漂移。

---

## D. Run / Input 模型（真实 FK + 非空约束）

**新建 4 张表**，均使用真实 FK，不复用 MemoryIngestionRun。所有 FK 列 `nullable=False`。

### D.1 CompactionInput（不可变快照）

```python
class CompactionInput(Base):
    __tablename__ = "compaction_inputs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    segment_id: Mapped[str] = mapped_column(String(36), ForeignKey("segments.id"), nullable=False, index=True)
    start_turn_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    end_turn_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    turn_manifest: Mapped[list[Any]] = mapped_column(JSON, default=list)   # [{turn_record_id, turn_id, turn_sequence, status}]
    event_manifest: Mapped[list[Any]] = mapped_column(JSON, default=list)  # [{event_id, turn_record_id, turn_event_index, event_type, content_hash}]
    working_state_version: Mapped[int] = mapped_column(nullable=False)
    working_state_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    summary_version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)

    __table_args__ = (
        UniqueConstraint("segment_id", "source_hash", "summary_version",
                         name="uq_compaction_input_segment_hash_version"),
    )
```

### D.2 CompactionRun（Segment 压缩运行记录）

```python
class CompactionRun(Base):
    __tablename__ = "compaction_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    segment_id: Mapped[str] = mapped_column(String(36), ForeignKey("segments.id"), nullable=False, index=True)
    compaction_input_id: Mapped[str] = mapped_column(String(36), ForeignKey("compaction_inputs.id"), nullable=False)
    outbox_job_id: Mapped[str] = mapped_column(String(36), ForeignKey("outbox_jobs.id"), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    summary_version: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    execution_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempt_count: Mapped[int] = mapped_column(default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        UniqueConstraint("segment_id", "source_hash", "summary_version",
                         name="uq_compaction_segment_hash_version"),
        UniqueConstraint("outbox_job_id", name="uq_compaction_run_outbox_job"),
        Index("ix_compaction_segment_status", "segment_id", "status"),
    )
```

> 修正：`source_hash` / `summary_version` 现为显式列，与 `uq_compaction_segment_hash_version` 唯一约束一致（原方案在约束中引用了不存在的列）。

### D.3 EpochCompactionInput（不可变 Epoch 边界快照）

```python
class EpochCompactionInput(Base):
    __tablename__ = "epoch_compaction_inputs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    epoch_id: Mapped[str] = mapped_column(String(36), ForeignKey("epochs.id"), nullable=False, index=True)
    boundary_turn_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    working_state_version: Mapped[int] = mapped_column(nullable=False)
    current_objective: Mapped[str | None] = mapped_column(Text, nullable=True)
    open_loops: Mapped[list[Any]] = mapped_column(JSON, default=list)
    active_constraints: Mapped[list[Any]] = mapped_column(JSON, default=list)
    artifact_refs: Mapped[list[Any]] = mapped_column(JSON, default=list)
    verified_tool_states: Mapped[list[Any]] = mapped_column(JSON, default=list)
    source_segment_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    source_hashes: Mapped[list[Any]] = mapped_column(JSON, default=list)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    checkpoint_version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)

    __table_args__ = (
        UniqueConstraint("epoch_id", "snapshot_hash", "checkpoint_version",
                         name="uq_epoch_input_epoch_hash_version"),
    )
```

### D.4 CheckpointRun（EpochCheckpoint 运行记录）

```python
class CheckpointRun(Base):
    __tablename__ = "checkpoint_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    epoch_id: Mapped[str] = mapped_column(String(36), ForeignKey("epochs.id"), nullable=False, index=True)
    epoch_compaction_input_id: Mapped[str] = mapped_column(String(36), ForeignKey("epoch_compaction_inputs.id"), nullable=False)
    outbox_job_id: Mapped[str] = mapped_column(String(36), ForeignKey("outbox_jobs.id"), nullable=False)
    boundary_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    checkpoint_version: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    execution_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempt_count: Mapped[int] = mapped_column(default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        UniqueConstraint("epoch_id", "boundary_hash", "checkpoint_version",
                         name="uq_checkpoint_epoch_hash_version"),
        UniqueConstraint("outbox_job_id", name="uq_checkpoint_run_outbox_job"),
        Index("ix_checkpoint_epoch_status", "epoch_id", "status"),
    )
```

### D.5 重试与重放语义（Run ↔ OutboxJob 关系）

**一个逻辑操作固定对应**：

```text
一个 operation_id
一个 OutboxJob
一个 CompactionRun（或 CheckpointRun）
```

- **普通重试**（lease 到期、Worker 接管）与 **deadletter 后人工重放**：均重新启用**同一 OutboxJob 行**（同一 `outbox_job_id`），生成新 `claim_token`（OutboxJob 侧）/ `execution_token`（Run 侧），`attempt_count += 1`；`Run` **不新建**、`outbox_job_id` 不变。
- **删除**“新 OutboxJob 重放创建新 Run”的设计——它与 `UNIQUE(segment_id, source_hash, summary_version)` / `UNIQUE(epoch_id, boundary_hash, checkpoint_version)` 冲突，且 `UNIQUE(compaction_runs.outbox_job_id)` / `UNIQUE(checkpoint_runs.outbox_job_id)` 已保证一 Job 一 Run。
- **约束**（见 L.3）：
  - `UNIQUE(compaction_runs.outbox_job_id)`
  - `UNIQUE(checkpoint_runs.outbox_job_id)`
- **与其他模型的复用关系**：
  - OutboxJob：`claim_token`、`worker_id`、`lease_expires_at` → 唯一 fencing 来源
  - MemoryIngestionRun：status 语义相似但不复用表
  - 唯一约束防止相同 segment/source_hash/version（或 epoch/boundary_hash/version）重复创建
- CheckpointRun 复用 CompactionRun 的 claim/takeover/deadletter 全部逻辑。

---

## E. Segment 边界切换事务

### E.1 `begin_segment_sealing()` 接口

```python
def begin_segment_sealing(
    db: Session,
    thread_id: str,
    expected_segment_id: str,
    expected_idle_cutoff: datetime | None = None,
) -> SegmentSealingResult:
```

**必须接收** `expected_segment_id`。事务内若当前 open Segment 已变化（ID 不匹配或 status ≠ "open"）→ stale/no-op。禁止根据 thread_id 盲目密封另一个 Segment。

### E.2 事务流程

```text
BEGIN
  1. SELECT FOR UPDATE Thread
  2. SELECT FOR UPDATE Segment WHERE id = expected_segment_id AND status="open"
     → 不匹配 → abort (stale/no-op)
  3. 检查不可密封条件：
     a. pending_approvals len > 0 → abort
     b. uncommitted_side_effects len > 0 → abort
     c. running_tool_state len > 0 → abort
     d. 存在任意非终态 Turn（not_started / running / interrupted_unknown）→ abort
     e. turn_record_count 为 0 → abort（空 Segment）
        （注意：failed / cancelled / preempted 均属终态，仅含这些 Turn 的 Segment 视为非空，允许密封）
     f. 已存在另一个 status="sealing" Segment → compaction_in_progress
  4. 固定 end_turn_sequence = Segment 内所有 TurnRecord 的最大 turn_sequence
  5. 收集 Turn manifest（turn_record_id, turn_id, turn_sequence, status）
  6. 计算每个 Event 的 content_hash
  7. 冻结 WS boundary snapshot
  8. 计算 source_hash；持久化 CompactionInput
  9. UPDATE Segment: status="sealing", end_turn_sequence, source_hash
  10. successor Segment（`create_successor=true` 时）：
        start_turn_sequence = peek_next_turn_sequence(thread_id)
        只读不消费，创建 TurnRecord 时才 allocate_turn_sequence 递增
        INSERT Segment(epoch_id, segment_no=old+1, start_turn_sequence=peeked)
  11. INSERT OutboxJob
COMMIT

区分：
  peek_next_turn_sequence → Thread 行锁内只读，不递增
  allocate_turn_sequence  → 创建 TurnRecord 时递增+返回
  创建空 successor Segment 不消耗 Turn sequence
```

> 步骤 4–11 由 `freeze_segment_for_sealing(segment, create_successor=true, boundary_working_state)`（见 E.3）统一执行；`begin_segment_sealing` 只负责行锁、stale/no-op 判断与不可密封条件（步骤 1–3）后调用该函数。

### E.3 `freeze_segment_for_sealing(segment, create_successor, boundary_working_state)`

**唯一权威冻结逻辑**。`begin_segment_sealing`（普通 sealing）与 `begin_epoch_sealing`（Epoch rollover）都必须复用它，禁止各自实现一套冻结流程。

```text
freeze_segment_for_sealing(segment, create_successor, boundary_working_state):
  1. 非终态 Turn 检查（not_started / running / interrupted_unknown 全部阻断）
  2. range 固定：end_turn_sequence = Segment 内 max(TurnRecord.turn_sequence)
  3. Turn/Event manifest：
     turn_manifest = [{turn_record_id, turn_id, turn_sequence, status}, ...]
     event_manifest = [{event_id, turn_record_id, turn_event_index, event_type, content_hash}, ...]
     （含 tool_call / tool_result event；content_hash 在步骤 4 计算）
  4. 每个 Event 的 content_hash（写入 event_manifest）
  5. CompactionInput 持久化（含 turn_manifest / event_manifest + boundary_working_state 快照 + working_state_version）
  6. source_hash = sha256(canonical manifest)（见 E.3.1）
  7. UPDATE Segment: status="sealing", end_turn_sequence, source_hash
  8. segment_sealing OutboxJob（operation_id 幂等）
  9. if create_successor:
        successor.start_turn_sequence = peek_next_turn_sequence(thread_id)   # 只读不消费
        INSERT Segment(epoch_id, segment_no=old+1, start_turn_sequence=peeked)

普通 Segment sealing:  create_successor=true
Epoch rollover:        create_successor=false（不在旧 Epoch 造 successor；
                       之后单独创建新 active Epoch + 它的 open Segment）
```

### E.3.1 source_hash：Canonical Manifest

**禁止** `sha256(thread_id | start_seq | end_seq | turn_count)`。

`source_hash = sha256(canonical_manifest)`，manifest 直接由以下结构化来源计算：

```text
thread_id
epoch_id
segment_id
start_turn_sequence
end_turn_sequence
turn_manifest（按 (turn_sequence, turn_record_id) 有序序列化；每行含 turn_record_id/turn_id/turn_sequence/status）
event_manifest（按 (turn_record_id, turn_event_index) 有序序列化；每行含 event_id/turn_record_id/turn_event_index/event_type/content_hash）
working_state boundary snapshot hash（sha256(canonical JSON)）
summary_version
```

### E.4 CompactionInput（不可变记录）

`begin_segment_sealing()` 同一事务中持久化：

```text
segment_id
start_turn_sequence
end_turn_sequence
turn_manifest: JSON（[{turn_record_id, turn_id, turn_sequence, status}]）
event_manifest: JSON（[{event_id, turn_record_id, turn_event_index, event_type, content_hash}]）
working_state_version: int
working_state_snapshot: JSON（boundary 快照）
source_hash: str
summary_version: int
created_at: datetime
```

`turn_manifest` / `event_manifest` 为不可变完整清单，是 Phase B 内容校验与 H 节覆盖校验的唯一权威来源（`source_turn_ids` / `source_event_ids` 由之派生，不再单独存储）。

Phase B **不得读取实时 WorkingState**。Phase B 用短只读 Session 加载 CompactionInput 后立即关闭，LLM 调用期间不持有 DB Session。

### E.5 Sealing 状态下新 Turn

- 查找 status="open" 的 Segment → 新建的 Segment
- Turn.segment_id = 新 Segment.id
- **旧 Segment 永不接收新 Turn；失败的 sealing Segment 禁止 reset 为 open**（否则产生两个 open Segment + 范围重叠）

### E.6 `ContextBudget.soft_input_limit` 触发

同 Phase 1：`soft_exceeded` → `mark_pending_seal`。

---

## F. 三阶段 Handler

### Handler 职责边界

Handler **只提交业务数据**（SegmentSummary, Segment.summary_id/status, CompactionRun.status），**不直接 finalize OutboxJob**。

Handler 返回 `HandlerResult(COMPLETED|RETRYABLE_ERROR|...)`，由 **OutboxWorker** 使用 claim fencing 完成 OutboxJob 的 status/lease 更新。Handler 不得自行实现另一套 retry/deadletter。

### `handle_segment_sealing(claimed: ClaimedJob) -> HandlerResult`

**Phase A（短事务）**：

```python
db_a = SessionLocal()
try:
    1. 验证 OutboxJob claim（lease/status/worker_id）
    2. SELECT FOR UPDATE Segment WHERE id = payload.segment_id
    3. 验证 Segment.status == "sealing"
    4. 验证 Segment.source_hash == payload.source_hash
    5. ON CONFLICT (segment_id, source_hash, summary_version) DO：
       - 已 succeeded → return ALREADY_SUCCEEDED
       - 否则：新 execution_token, attempt_count+1, status="running",
         completed_at=NULL, error_message=NULL
    6. COMMIT
finally:
    db_a.close()
```

**Phase B（无 DB Session，事务外 LLM）**：

1. 短只读 Session 加载 CompactionInput（含 turn_manifest / event_manifest / boundary snapshot）→ 立即关闭 Session
2. 加载原始 Turn/Event 内容（独立短只读 Session），**逐条比对 `event_manifest[].content_hash`**：任一 Event 当前 content_hash 与冻结值不一致 → `source_hash` 失效，RETRYABLE_ERROR（不进入 LLM）
3. 生成摘要 prompt → 为工具结果与失败生成局部 `item_ref`（`tool_1..` / `failure_1..`），附 `item_ref → tool_call_id` 映射 → LLM → schema validate（仅语义字段，见 G.4）
4. 校验失败 → RETRYABLE_ERROR
5. **代码确定性合并**：从 CompactionInput boundary snapshot 生成 `open_loops` / `active_constraints` / `artifacts` / `omitted_artifact_refs` / `important_tool_results` / `unresolved_failures` / source manifest，**通过 `item_ref → tool_call_id` 映射表**（非数组顺序）将 LLM 的 `result_summary` / `error` 回填到对应确定性条目；与 LLM 输出顺序无关；LLM 遗漏任何 boundary 内容不影响完整性
6. LLM 调用期间**不持有任何 DB Session**

**Phase C（短事务 + 双重 fencing）**：

```python
db_c = SessionLocal()
try:
    1. SELECT FOR UPDATE OutboxJob → 验证 claim_token/worker_id/status/lease
    2. SELECT FOR UPDATE CompactionRun → 验证 execution_token == 当前
    3. SELECT FOR UPDATE Segment → 确认仍为 sealing + source_hash 一致
    4. 执行确定性覆盖校验（见 H 节，对所有 boundary snapshot 执行）
    5. INSERT SegmentSummary(...)（合并后的完整载荷）
    6. UPDATE Segment.summary_id, status="sealed", sealed_at
    7. UPDATE CompactionRun.status="succeeded"
    8. 条件性 Outbox enqueue（同一事务，只创建 Job，不执行 rollover/checkpoint 本体）：
       a. 若该 active Epoch 的 sealed Segment 数达 max_segments_per_epoch 阈值 →
          INSERT OutboxJob(job_type="epoch_rollover",
            operation_id=f"epoch_rollover:{epoch_id}") ON CONFLICT (operation_id) DO NOTHING
       b. 若该 Epoch 处于 status="sealing" 且其所有非空 Segment 均已 sealed →
          INSERT OutboxJob(job_type="epoch_checkpoint",
            operation_id=f"epoch_checkpoint:{epoch_id}") ON CONFLICT (operation_id) DO NOTHING
    9. COMMIT
    # ← Handler 不设置 OutboxJob.status=completed
except:
    db_c.rollback()
    raise
finally:
    db_c.close()
```

**消除崩溃空窗**：Summary、`Segment.sealed` 与 rollover/checkpoint 的 enqueue 处于**同一事务**。Phase C 只创建事务性 OutboxJob，绝不在此内联执行 rollover/checkpoint 本体，因此“已 sealed 但未 enqueue 下游 Job”的原子空窗被消除。

Handler 返回 `HandlerResult(COMPLETED)` → **OutboxWorker 负责 finalize OutboxJob**。

### `handle_epoch_rollover(claimed: ClaimedJob) -> HandlerResult`

**Phase A（短事务）**：

```python
db_a = SessionLocal()
try:
    1. 验证 OutboxJob claim（lease/status/worker_id）
    2. SELECT FOR UPDATE Thread + active Epoch
    3. 该 Epoch 已不在 active（已被 rollover 或 status≠"active"）
       → return ALREADY_SUCCEEDED（幂等，不重复执行）
    4. 调用 begin_epoch_sealing()
    5. COMMIT
finally:
    db_a.close()
```

**语义**：
- stale / 已完成 → 返回幂等成功（`ALREADY_SUCCEEDED`）
- Handler **不 finalize OutboxJob**；由 **OutboxWorker** 负责 finalize / retry / deadletter
- `begin_epoch_sealing()` 复用 `freeze_segment_for_sealing`（见 E.3 / I.2）

`handle_epoch_checkpoint()` 沿用同一三阶段模式（见 I.2 闭合执行链），同样不 finalize OutboxJob。

### 失败映射

| 场景 | HandlerResult |
|------|---------------|
| Phase A fencing 失败 | CLAIM_LOST |
| Phase B LLM 失败 | RETRYABLE_ERROR |
| Phase B schema validation 失败 | RETRYABLE_ERROR |
| Phase C fencing 失败 | CLAIM_LOST |
| source_hash 不匹配 | NON_RETRYABLE（deadletter） |
| 达到 max_retries | Worker 终端 deadletter callback：验证 Outbox claim fencing → 锁定 CompactionRun + 验证 execution_token → CompactionRun→deadletter + OutboxJob→deadletter，同一事务提交。旧 claim 不得 deadletter 新 claim 接管后的 Run |

### CompactionRun 接管

每次新 claim：

```text
新 execution_token
attempt_count = old.attempt_count + 1
status = "running"
completed_at = NULL
error_message = NULL
```

Phase C 严格验证 execution_token。

---

## G. Summary schema 和 Prompt

### G.1 拆分 LLM 输出 Schema 与最终 SegmentSummary

严格分离两类字段，互不重叠：

**(a) LLM 语义输出 Schema（LLM 唯一职责，不含任何稳定 ID / artifact ref / constraint / open-loop 内容）**：

```json
{
  "goal": "...",
  "outcome": "...",
  "decisions": [{"what": "", "why": "", "by": "user"|"assistant"|"tool"}],
  "entities": [{"name": "", "type": "", "relation": ""}],
  "tool_result_summaries": [{"item_ref": "tool_1", "result_summary": ""}],
  "failure_explanations": [{"item_ref": "failure_1", "error": ""}]
}
```

**关键**：LLM **不得**看到、生成或修改任何真实稳定 ID（如 `tool_call_id`、artifact ref、`open_loops[].id`）。
代码为摘要输入生成**局部引用** `item_ref`（`tool_1..tool_N`、`failure_1..failure_M`），并保存 `item_ref → tool_call_id` 映射；
LLM 仅回传 `item_ref` 与语义文本，代码据此回填到对应的确定性条目。`item_ref` 与 `tool_call_id` 的对应关系对 LLM 不可见。

**(b) 代码从 CompactionInput boundary snapshot 确定性合并的字段**（LLM 不生成）：

```text
open_loops
active_constraints
artifacts
omitted_artifact_refs
important_tool_results（含稳定键；由 verified_tool_states 合并，LLM 的 tool_result_summaries 按稳定键回填 result_summary）
unresolved_failures（含稳定键；由 failed/cancelled Turn records + 工具错误合并，回填 LLM 的 failure_explanations）
failed/cancelled Turn records
source manifest（由 `turn_manifest` / `event_manifest` 派生的 source_turn_ids / source_event_ids / source_hash）
```

最终 SegmentSummary = (a) 语义字段 ∪ (b) 确定性字段，由代码在 Phase C 合并后单行写入。即使 LLM 遗漏任何 boundary 内容，(b) 仍由代码完整补齐。

### G.1.1 稳定键审计与决定

**审计**：Phase 1 `WorkingState.verified_tool_states` 每项当前结构为
`{"tool_name": ..., "state": {"ok": ..., "tool_call_id": ...}, "verified_at": ...}`，
按 `tool_name` 去重（每工具保留最新一项），`tool_call_id` **嵌套在 `state` 内**，**无 `state_key` 字段**。

**决定**：唯一稳定键统一采用 **`tool_call_id`**。理由：`tool_call_id` 是全流程唯一身份标识（`Artifact` 唯一约束 `(turn_record_id, tool_call_id)`、Event payload、`ToolRecord` 均以其为准），且 `unresolved_failures` 是“具体某次调用”的失败，天然以 `tool_call_id` 为键；`tool_name` 无法唯一标识多次失败。

**要求的最小 Phase 1 对齐**（写入 `docs` 与实现时统一）：`working_state.update_verified_tool_state` 将 `tool_call_id` **提升为条目顶层字段**（保留 `tool_name` 去重语义，条目变为 `{"tool_name", "tool_call_id", "state", "verified_at"}`）。此后 Prompt（不再要求 LLM 输出 ID）、Pydantic Schema、ORM（`important_tool_results` / `unresolved_failures` JSON）、checker 全部以 `tool_call_id` 为准。

> 该 Phase 1 对齐属受确认项（见文末），不触及 Phase 2 Memory Ingestion。

### G.2 UNIQUE 约束

```sql
UNIQUE(segment_id, summary_version)
```

并保证：`Segment.status="sealed" → summary_id IS NOT NULL`。

### G.3 摘要 Prompt（仅要求 LLM 输出语义字段）

Prompt **不再要求** LLM 复制稳定 ID、artifact ref、constraint / open-loop 内容。LLM 只做语义概括：

```
你是一个结构化摘要生成器。只根据提供的源 Turn/Event 内容输出 JSON。

规则：
1. 不推测用户属性
2. 只做语义概括，不要复制或编造任何 ID、artifact ref、约束或未完成任务列表
   （这些由系统确定性合并，不属于你的职责）
3. 区分：用户陈述 | 工具验证结果 | Assistant 建议
4. 输出严格结构化 JSON，键名必须与以下 Schema 完全一致，不得增删顶层键

输入：
- source_turns: [{turn_id, user_message, assistant_reply, tool_calls, tool_results}]
（boundary WorkingState 仅供你理解上下文，不要在输出中复制其结构化字段）
- 工具摘要引用（由系统生成，仅局部引用，非真实 ID）：
    tool_1: <工具调用上下文>
    tool_2: <工具调用上下文>
    failure_1: <失败调用上下文>
（每条引用对应一个工具结果或失败；不要臆测其背后真实 ID）

输出 JSON 格式（只输出以下字段）：
{
  "goal": "...",
  "outcome": "...",
  "decisions": [{"what": "", "why": "", "by": "user"|"assistant"|"tool"}],
  "entities": [{"name": "", "type": "", "relation": ""}],
  "tool_result_summaries": [{"item_ref": "tool_1", "result_summary": ""}],
  "failure_explanations": [{"item_ref": "failure_1", "error": ""}]
}
（item_ref 必须从上方引用列表中选择；禁止自造或编造 item_ref，禁止输出真实 ID）
```

### G.4 Schema Validation（仅校验 LLM 语义字段）

- 必填键：`goal` / `outcome` / `decisions` / `entities` / `tool_result_summaries` / `failure_explanations`
- **禁止**出现 `open_loops` / `active_constraints` / `artifacts` / `omitted_artifact_refs` / `important_tool_results` / `unresolved_failures` / 任何稳定 ID —— 这些由代码从 boundary snapshot 合并；若 LLM 输出这些键 → RETRYABLE_ERROR（或剥离并告警）
- `json.loads()` 解析；必填键存在；数组不超过上限；失败 → RETRYABLE_ERROR
- `tool_result_summaries[].item_ref` / `failure_explanations[].item_ref` **必须**属于代码预计算的 item_ref 集合，且**禁止**出现真实稳定 ID：
  - 未知 item_ref → 告警并丢弃该条，不写入确定性工具状态（不覆盖 `important_tool_results` / `unresolved_failures`）
  - 重复 item_ref → 告警，仅保留首条
  - LLM 漏回某 item_ref → 该工具的 `result_summary` / `error` 留空，确定性条目仍保留
- 以下字段由**代码**从 CompactionInput boundary snapshot 确定性填充，不由 LLM 生成：
  - `open_loops` / `active_constraints` / `artifacts` / `omitted_artifact_refs`
  - `important_tool_results` / `unresolved_failures`（稳定键均为 `tool_call_id`）
  - `source_turn_ids` / `source_event_ids`（由 `turn_manifest` / `event_manifest` 派生）/ `source_hash` / `token_count`
- `token_count` 使用 Phase 1 `TokenCounter` 计算，不使用 LLM 自报
- 回填**禁止按数组顺序映射**：一律通过 `item_ref → tool_call_id` 映射表将 LLM 的 `result_summary` / `error` 回填到对应确定性条目，与 LLM 输出顺序无关（见 Phase B 步骤 5）

---

## H. 确定性覆盖校验

**提交前（Phase C）全部 checker 通过后方可写入**。禁止使用第二个 LLM 做校验。

校验**仅针对 begin_segment_sealing 时的 boundary WorkingState snapshot**，不读取未来 Turn 已更新的实时 WorkingState。

**覆盖校验使用稳定标识符**：按 `ref`（Artifact）、`tool_call_id`（工具结果/失败）、`open_loops[].id`、`active_constraints[].id`/`.source` 执行精确匹配，不仅比较自然语言。Prompt、Pydantic Schema、ORM 和 checker 必须使用同一套标识符。

| # | 检查项 | 来源 |
|---|--------|------|
| 1 | `open_loops` 全部出现在 summary 中（按 ref/key 比对） | Boundary snapshot vs Summary |
| 2 | `active_constraints` 被保留 | Boundary snapshot vs Summary |
| 3 | `pending_approvals` 为空（sealability 已确保） | Boundary snapshot |
| 4 | `uncommitted_side_effects` 为空（sealability 已确保） | Boundary snapshot |
| 5 | `artifact_refs` 全部出现在 summary 或记录 omitted（按 ref 比对） | Boundary snapshot vs Summary |
| 6 | `verified_tool_states` 关键结果被保留（按 tool_call_id 比对） | Boundary snapshot vs Summary |
| 7 | source_turn_ids（由 `turn_manifest` 派生）与 manifest 一致 | CompactionInput vs 数据库 |
| 8 | source_event_ids（由 `event_manifest` 派生，含 tool events）与 manifest 一致 | CompactionInput vs 数据库 |
| 9 | source_hash 重新计算并一致（基于 turn_manifest / event_manifest canonical） | 重新计算 vs Segment |

---

## I. EpochCheckpoint 策略

### I.1 `epoch_rollover` OutboxJob + `EpochRolloverCoordinator`

**自动触发链路**（enqueue 在 SegmentSummary Phase C 同一事务，见 F 节步骤 8a）：

```
SegmentSummary Handler Phase C（写 Summary + Segment.sealed 的同一事务）
→ 条件检查 sealed_segment_count >= max_segments_per_epoch
→ 满足阈值时 INSERT OutboxJob(job_type="epoch_rollover", schema_version=1)
   operation_id=f"epoch_rollover:{epoch_id}" ON CONFLICT DO NOTHING
→ 稳定 operation_id + 数据库 UNIQUE 幂等
→ Handler 调用 begin_epoch_sealing()
→ Phase C 只创建 Job，绝不内联执行 rollover 本体
```

显式 API 与自动触发复用相同服务。

**注意**：`epoch_rollover` / `epoch_checkpoint` / `segment_sealing` 三个 job_type 必须加入 `recall_config.ENABLED_OUTBOX_JOB_TYPES` allowlist，否则 `OutboxWorker` 拒绝入队/处理。

### I.1.1 Enqueue Reconciler（补发缺失 Job）

Phase C 已消除“sealed 与 enqueue 之间”的原子空窗；但为兜底历史异常或跨版本状态，新增周期 reconciler：

```text
周期扫描（低频，独立短事务）：
  - active Epoch 满足 sealed_segment_count >= 阈值 但无 pending/处理中的 epoch_rollover Job
    → INSERT epoch_rollover Job ON CONFLICT DO NOTHING
  - sealing Epoch 所有非空 Segment 已 sealed 但无 epoch_checkpoint Job
    → INSERT epoch_checkpoint Job ON CONFLICT DO NOTHING
  - open Segment 满足 idle/pending_seal 条件 但无 segment_sealing Job（复用 J 节判断）
只补发“条件已满足但缺 Job”的情形；operation_id UNIQUE 保证不重复。
```

### I.2 `begin_epoch_sealing()`

```text
BEGIN
  1. SELECT FOR UPDATE Thread + active Epoch
  2. 存在未完成 sealing Segment 时 → epoch_compaction_in_progress
  3. 冻结旧 Epoch 当前 open Segment：
     a. 若非空 → freeze_segment_for_sealing(
            segment, create_successor=false, boundary_working_state)
        （复用 E.3 完整逻辑：非终态 Turn 检查 / range 固定 / Turn·Event manifest /
         Event hash / CompactionInput / source_hash / segment_sealing OutboxJob；
         create_successor=false → 不在旧 Epoch 造 successor open Segment）
     b. 若为空 → 关闭/删除空 Segment（不消耗 Turn sequence）
  4. 冻结 Epoch boundary WorkingState snapshot → EpochCompactionInput（含 snapshot_hash）
  5. Epoch.status = "sealing"
  6. new_start = peek_next_turn_sequence(thread_id)   # 只读不消费
  7. INSERT 新 active Epoch + 它的 open Segment(start_turn_sequence=new_start)
  8. UPDATE WorkingState.epoch_id = new_epoch.id（同一事务）
COMMIT
```

**复用**：旧 Epoch 当前 open Segment 的冻结与普通 sealing **共用** `freeze_segment_for_sealing`，仅以 `create_successor=false` 区分；随后再单独创建新 active Epoch + open Segment。

**sequence 规则**：新 Epoch.open Segment 的 `start_turn_sequence = peek_next_turn_sequence`（只读不消费），首个真实 Turn allocate 时归属新 Segment。

**防止多余 open Segment**：不在旧 Epoch 中创建 successor open Segment。

### I.2 闭合的执行链：`epoch_checkpoint` OutboxJob + CheckpointRun

```
旧 Epoch 最后一个 Segment sealed
→ 原子检查 Epoch.status=sealing 且所有非空 Segment sealed
→ enqueue OutboxJob(job_type="epoch_checkpoint", schema_version=1)
```

Handler 复用 P0.5B 三阶段模式：

```text
Phase A: 验证 Outbox claim + 锁定 Epoch + CheckpointRun 幂等解析
Phase B: 确定性聚合 → 只读 EpochCompactionInput + SegmentSummary
Phase C: 双重 fencing → INSERT EpochCheckpoint,
  UPDATE Epoch.checkpoint_id/status=sealed/sealed_at,
  UPDATE CheckpointRun.status=succeeded,
  → Handler 返回 COMPLETED，OutboxWorker finalize OutboxJob
```

### I.3 EpochCompactionInput（不可变）

begin_epoch_sealing 持久化：

```text
epoch_id
boundary_turn_sequence
working_state_version
current_objective
open_loops
active_constraints
artifact_refs
verified_tool_states
source_segment_ids
source_hashes
snapshot_hash = sha256(canonical JSON of above)
created_at
```

EpochCheckpoint **只能**根据该快照 + SegmentSummary 生成，不得读取新 active Epoch 已更新的实时 WS。

### I.4 CheckpointRun

```text
id
epoch_id
outbox_job_id
status: running/succeeded/failed_retryable/deadletter
execution_token
attempt_count
boundary_hash (来自 EpochCompactionInput.snapshot_hash)
checkpoint_version
error_message
created_at, completed_at

UNIQUE(epoch_id, boundary_hash, checkpoint_version)
```

复用 CompactionRun 的 claim/takeover/deadletter 全部逻辑。

### I.5 不变量

- **始终只有一个 active Epoch**
- Epoch rollover 后新 Epoch 不在旧 Epoch 创建多余 open Segment
- 新 Epoch.open Segment.start_turn_sequence 来自真实 next sequence
- Epoch rollover 时用户立即进入新 Epoch，不等待旧 Checkpoint
- 旧 Epoch Checkpoint 不读取新 Epoch WS
- 不基于 LLM 判断"话题结束"

---

## J. Idle Scanner

### J.1 候选条件

```python
Thread.last_activity_at <= now - idle_threshold  # 默认 15 分钟
AND Segment.status = "open"
AND (
    Segment.pending_seal_at IS NOT NULL
    OR turn_record_count_in_segment >= configured_minimum  # 默认 3
)
```

### J.2 实现

```text
周期性扫描（IDLE_CHECK_INTERVAL，默认 30 秒）

FOR UPDATE SKIP LOCKED 短事务：
  SELECT segments WHERE thread_id IN (
    SELECT thread_id FROM threads
    WHERE last_activity_at <= now - idle_threshold
  ) AND status = "open" AND (pending_seal_at IS NOT NULL OR ...)
  ORDER BY pending_seal_at ASC LIMIT MAX_SCAN_BATCH

对每个候选：
  → begin_segment_sealing(thread_id, expected_segment_id, expected_idle_cutoff)
  → 事务内原子创建 OutboxJob(operation_id="segment_sealing:{seg_id}:{hash}:{ver}")
  → 数据库唯一 dedupe key 保证幂等，删除"先查询 JSON payload 是否存在"的竞态方案
```

### J.3 Idle 重验证

Scanner 查询候选后，`begin_segment_sealing` 事务内重新验证：

```text
Thread.last_activity_at <= idle_cutoff（或验证 expected_last_activity_at）
```

用户已重新活跃时返回 stale/no-op，不创建 OutboxJob。

### J.4 约束

- 不重复创建 OutboxJob（`operation_id` UNIQUE dedupe）
- `FOR UPDATE SKIP LOCKED` 并发友好
- 批次上限 `MAX_SCAN_BATCH`（默认 10）
- 不在扫描 Session 中执行 LLM 或长事务
- 空 Segment 不密封
- running/interrupted Turn 不密封
- idle threshold 未达到时不密封

---

## K. ContextAssembler 接入

### K.1 组装顺序（含 sealing bridge）

```
Partition 1: Stable Contract（不变）
Partition 2: Core Memory（不变）
Partition 3: 最近有效 EpochCheckpoint
Partition 4: 最近 N 个 SegmentSummary（N = budget.max_segment_summaries）
Partition 5: sealing Segment bridge（最多 1 个）：
             • 若有 Summary → 加载 Summary
             • 若无 Summary → 加载 CompactionInput 冻结范围的 bounded raw tail
Partition 6: WorkingState（不变）
Partition 7: 当前 open Segment 有界原始 Turn（Phase 1 keyset pagination）
Partition 8: Retrieved Memory（不变）
Partition 9: Current User Message（不变）
```

### K.2 sealing bridge 规则

```
Sealing Segment：
  - 不属于当前 open Segment（sealing ≠ open）
  - Summary 成功前 → 无有效 SegmentSummary
  - 此时加载 CompactionInput 中冻结范围的 bounded raw tail
  - Summary 成功后 → 停止读取 raw bridge，加载 Summary 替代
  - 最多 1 个 sealing Segment 作为 bridge
  - bridge 有独立 token/数量上限
  - 禁止因 bridge 缺失而加载全部旧历史
```

### K.3 Token 预算分区（通过 ContextBudget，不硬编码）

```python
# Phase 3 在 Phase 1 ContextBudget 上新增三个分区
ContextBudget:
    # Phase 1 已有
    stable_contract:      2000
    core_memory:          1000
    working_state:         500
    recent_messages:      4000
    retrieval:            1500
    current_message:       500
    tools:                1000
    # Phase 3 新增
    epoch_checkpoint:      500
    segment_summaries:    1500
    sealing_bridge:       2000   # 新分区
```

启动时重新验证所有 hard limits + reserved output ≤ ModelProfile.context_window。文档中数字仅为示例。

### K.4 关键约束

- sealed Segment 只加载 Summary
- sealing + 无 Summary → bounded raw bridge（最多 1 个，有 token 上限）
- Summary 成功 → bridge 退化（不再加载 raw）
- Summary 缺失 → degraded state，不静默加载全部历史
- 当前 open Segment 仍走 Phase 1 keyset reader
- 最终请求经过完整 LiteLLM/fallback hard gate

---

## L. Migration

### L.1 新增表

| 表 | 说明 |
|----|------|
| `compaction_runs` | Segment 压缩运行记录 |
| `compaction_inputs` | 不可变 CompactionInput 快照 |
| `checkpoint_runs` | EpochCheckpoint 运行记录 |
| `epoch_compaction_inputs` | 不可变 Epoch boundary WS 快照 |

### L.2 新增/修改列

| 表 | 列 | 类型 | 默认 | 说明 |
|----|-----|------|------|------|
| `segment_summaries` | `source_turn_ids` | JSON | NULL | |
| `segment_summaries` | `source_event_ids` | JSON | NULL | 含 tool events |
| `segment_summaries` | `active_constraints` | JSON | NULL | |
| `segment_summaries` | `unresolved_failures` | JSON | NULL | |
| `segment_summaries` | `omitted_artifact_refs` | JSON | NULL | 因预算省略 |
| `epoch_checkpoints` | `source_hashes` | JSON | NULL | |

### L.3 新增约束（含外键）

| 表 | 约束 |
|----|------|
| `compaction_runs` | UNIQUE(segment_id, source_hash, summary_version); UNIQUE(outbox_job_id); FK → compaction_inputs(id); FK → outbox_jobs(id) |
| `compaction_inputs` | UNIQUE(segment_id, source_hash, summary_version) |
| `checkpoint_runs` | UNIQUE(epoch_id, boundary_hash, checkpoint_version); UNIQUE(outbox_job_id); FK → epoch_compaction_inputs(id); FK → outbox_jobs(id) |
| `epoch_compaction_inputs` | UNIQUE(epoch_id, snapshot_hash, checkpoint_version) |
| `epoch_checkpoints` | UNIQUE(epoch_id, version) |
| `segment_summaries` | UNIQUE(segment_id, summary_version) |
| `segments` | PARTIAL UNIQUE INDEX(thread_id) WHERE status='sealing'; CHECK(status≠'sealed' OR summary_id IS NOT NULL) |

### L.4 历史异常数据迁移（fail-closed）

添加 `CHECK(status<>'sealed' OR summary_id IS NOT NULL)` 之前，**必须**先处理历史异常，禁止直接加约束导致 migration 崩溃。

**步骤 1：审计（约束 migration 内）**

```sql
SELECT id FROM segments WHERE status = 'sealed' AND summary_id IS NULL;
```

- 若结果非空 → **migration fail-closed**（抛错中止），并将异常 Segment IDs 打印到日志/输出。
- **不**在约束 migration 内自动修复。

**步骤 2：独立修复命令**（人工触发，非 Alembic migration）

对每个 `sealed + summary_id IS NULL` 的 Segment，**仅当同时满足以下全部条件才允许恢复为 open**：

```text
- 属于当前 active Epoch
- 是 Thread 最新 Segment（segment_no / created_at 最大）
- 不存在更新（更高 epoch_no）的 Epoch
- 不存在其他 status="open" / "sealing" 的 Segment
- 不存在比该 Segment 更大 turn_sequence 的 Turn（即无更新 Turns）
```

其余异常 Segment **一律转为 `status="sealing"`**，并构建完整 CompactionInput（含 `turn_manifest` / `event_manifest` / Event hash）+ 补发 `segment_sealing` OutboxJob，由正常 Handler 生成 Summary。

```text
统一约束：
- 不调用 LLM
- 不伪造空 Summary
- 不删除原始 Segment / Turn / Event
- 禁止把旧 sealed / archived Epoch 的 Segment 重新打开（不符合上述 open 条件的旧 Epoch Segment 只转 sealing，不回退 open）
- 转 sealing 时若缺失 CompactionInput / segment_sealing Job 则补建补发
```

**步骤 3**：修复命令执行完毕、审计查询返回空后，再执行约束 migration。

> `try_seal_segment` 旁路移除后不再产生新的 `sealed+NULL` 数据；L.4 仅处理历史遗留。

---

## M. 精确修改文件

| 文件 | 修改 |
|------|------|
| `db/models.py` | 新增 `CompactionInput` + `CompactionRun` + `EpochCompactionInput` + `CheckpointRun` ORM（真实 FK，均 NOT NULL；`CompactionRun` 补 `source_hash`/`summary_version` 列） |
| `runtime/working_state.py` | `update_verified_tool_state` 将 `tool_call_id` 提升为条目顶层字段（保留 tool_name 去重）——受确认项 |
| `memory/recall_config.py` | `ENABLED_OUTBOX_JOB_TYPES` 新增 `segment_sealing` / `epoch_rollover` / `epoch_checkpoint` |
| `memory/` (其余) | 不修改（不触及 Phase 2 Memory Ingestion） |
| `runtime/epoch_manager.py` | `begin_segment_sealing(...)`；抽取 `freeze_segment_for_sealing(segment, create_successor, boundary_working_state)`；`begin_epoch_sealing()` 复用 freeze；`peek_next_turn_sequence()` / `allocate_turn_sequence()` 明确化；移除 `try_seal_segment` sealed 旁路 |
| `runtime/compaction.py` (新建) | `turn_manifest` / `event_manifest` 冻结 + canonical source_hash；CompactionInput/EpochCompactionInput 构造；Phase B 逐条 Event content_hash 校验；`item_ref → tool_call_id` 映射表生成与回填；LLM 语义输出与 boundary snapshot 的确定性合并；覆盖校验 checker（按 `tool_call_id`/ref/`open_loops[].id`/`active_constraints[].id` 比对，顺序无关） |
| `runtime/summary_schema.py` (新建/或并入 compaction) | LLM 语义输出 Pydantic Schema（仅 goal/outcome/decisions/entities/tool_result_summaries[failure_explanations]，每项带 `item_ref` 局部引用，无真实稳定 ID） |
| `worker/outbox_handlers.py` | `handle_segment_sealing()`（Phase C 内条件性 enqueue rollover/checkpoint）+ `handle_epoch_rollover()` + `handle_epoch_checkpoint()` + `register_all()`（三 handler 均注册 v1） |
| `worker/outbox_worker.py` | 终端 deadletter callback（通用 fenced callback，非 MemoryIngestionRun 专用） |
| `runtime/context_assembler.py` | `_load_sealing_bridge()` + `_load_epoch_checkpoint()`；修改 `assemble()` |
| `runtime/context_budget.py` | 新增 `epoch_checkpoint`、`segment_summaries`、`sealing_bridge` 分区 |
| `worker/scheduler_daemon.py` | `idle_scanner()` + 事务内 idle 重验证；enqueue reconciler（I.1.1） |
| `api/routes_epochs.py` (新建) | 显式 Epoch sealing API |
| `scripts/repair_sealed_null_summary.py` (新建) | L.4 历史异常修复命令（不调用 LLM、不伪造 Summary、不删除原始数据） |
| `alembic/versions/` | migration：4 新表 + 新列 + 约束；加 `sealed→summary_id NOT NULL` CHECK 前 fail-closed 审计 |
| `tests/` | `test_phase3_segment_sealing.py` 等 |

## N. 测试矩阵

| 编号 | 测试 | 类型 |
|------|------|------|
| 1 | soft threshold 标记 pending_seal | 单元 |
| 2 | begin_segment_sealing 原子固定范围 + CompactionInput + 新 Segment | 集成 |
| 3 | sealing 期间新 Turn 进入新 Segment | 集成 |
| 4 | running Turn 阻止密封 | 单元 |
| 5 | interrupted_unknown Turn 阻止密封 | 单元 |
| 6 | pending approval 阻止密封 | 单元 |
| 7 | uncommitted side effect 阻止密封 | 单元 |
| 8 | running tool 阻止密封 | 单元 |
| 9 | 空 Segment（turn_record_count=0）阻止密封 | 单元 |
| 10 | expected_segment_id 过期 → stale/no-op | 单元 |
| 11 | CompactionInput 冻结后 WS 新变化不影响旧 Summary | 单元 |
| 12 | Event 内容变化 → source_hash 不一致 → 无法提交 | 单元 |
| 13 | tool events 被纳入 source manifest | 单元 |
| 14 | old Worker execution_token 无法提交 | 单元 |
| 15 | schema validation 失败 → RETRYABLE_ERROR | 单元 |
| 16 | Summary + Segment sealed 原子提交 | 集成 |
| 17 | Handler Phase C 不 finalize OutboxJob | 单元 |
| 18 | OutboxWorker finalize OutboxJob | 集成 |
| 19 | 原始 Turn/Event 未删除 | 集成 |
| 20 | boundary snapshot 覆盖校验按稳定 ID/ref/key 执行 | 单元 |
| 21 | ContextAssembler 使用 Summary 而非 sealed raw history | 集成 |
| 22 | sealing bridge：无 Summary 时加载 bounded raw tail | 集成 |
| 23 | sealing bridge：Summary 成功后不再加载 raw | 集成 |
| 24 | N 个 sealed Segment 下 token 保持上限（含 bridge） | 集成 |
| 25 | Summary 缺失 → degraded state，不静默加载全部历史 | 单元 |
| 26 | idle scanner 幂等（operation_id dedupe key） | 单元 |
| 27 | 多 scanner 下 dedupe key 只产生一个 Job | 集成 |
| 28 | idle threshold 未达到时不密封 | 单元 |
| 29 | 用户在 scanner 查询后重新活跃 → sealing stale/no-op | 单元 |
| 30 | FOR UPDATE SKIP LOCKED 并发扫描 | 集成 |
| 31 | Epoch rollover 时新 Turn 立即进入新 Epoch | 集成 |
| 32 | active Epoch 始终只有一个 | 单元 |
| 33 | Epoch rollover 不在旧 Epoch 创建多余 open Segment | 单元 |
| 34 | 新 Epoch.open Segment 使用真实 next sequence | 单元 |
| 35 | Epoch rollover 后新 WS 变化不污染旧 Checkpoint | 单元 |
| 36 | 最后一个 Segment sealed 后只 enqueue 一个 epoch_checkpoint Job | 集成 |
| 37 | CheckpointRun Worker takeover + 旧 claim fencing | 集成 |
| 38 | 最近有效 EpochCheckpoint 被 ContextAssembler 读取 | 集成 |
| 39 | failed Segment 不被 reset 为 open | 单元 |
| 40 | Summary Prompt 与 item_ref 局部引用 Schema 完全匹配；LLM 不输出真实稳定 ID | 单元 |
| 41 | Outbox payload 只引用 compaction_input_id（不重复 source 字段） | 单元 |
| 42 | Outbox/CompactionRun 原子 deadletter（旧 claim 不得 deadletter 新 Run） | 集成 |
| 43 | CompactionRun 首次 execution attempt_count=1 | 单元 |
| 44 | 第二个 Segment 在已有 sealing Segment 时无法开始 sealing | 单元 |
| 45 | 存在任意非终态 Turn 时禁止密封（含 not_started） | 单元 |
| 46 | failed/cancelled Turn 不会造成 Segment range 重叠 | 单元 |
| 47 | successor Segment 使用真实 sequence allocator | 单元 |
| 48 | 旧 try_seal_segment 路径不可产生 sealed+NULL summary | 迁移 |
| 49 | migration 处理或拒绝历史 sealed+NULL summary 数据 | 迁移 |
| 50 | sealed_segment_count 达阈值只产生一个 epoch_rollover Job | 集成 |
| 51 | rollover 后 WorkingState.epoch_id 指向新 Epoch | 集成 |
| 52 | omitted_artifact_refs 可持久化并参与 checker | 单元 |
| 53 | LLM 漏掉 boundary open_loop 时代码确定性合并仍完整 | 单元 |
| 54 | checker 不依赖源 WS 中不存在的 tool_call_id | 单元 |
| 55 | Phase 0.5A/0.5B/P1/P2 回归 | 全量 |
| 56 | PostgreSQL migration upgrade/downgrade | CI |
| 57 | 创建 successor Segment 不消耗 Turn sequence（max(TurnRecord) 不变） | 单元 |
| 58 | 下一个真实 Turn allocate 到 successor.start_turn_sequence 并归属新 Segment | 集成 |
| 59 | LLM 输出不含 boundary 稳定字段；LLM 遗漏 open_loop/artifact 时最终 Summary 仍由代码完整合并 | 单元 |
| 60 | checker 只使用真实 verified-tool 稳定键（tool_call_id），不依赖源 WS 中不存在的 ID | 单元 |
| 61 | CompactionRun/CheckpointRun 的 FK 与 NOT NULL 约束生效（缺失 FK 目标插入失败） | 单元 |
| 62 | Segment Phase C 在写 Summary+sealed 同一事务内已事务性写入 rollover/checkpoint Job（enqueue 后崩溃点前 Job 已存在） | 集成 |
| 63 | reconciler 补发“条件满足但缺 Job”的情形且 operation_id 幂等不重复 | 集成 |
| 64 | Epoch rollover 复用 freeze_segment_for_sealing 生成完整 CompactionInput（含 Event hash/manifest） | 集成 |
| 65 | 历史 sealed+summary_id IS NULL 导致约束 migration fail-closed 并输出 Segment IDs | 迁移 |
| 66 | 修复命令：仅当属 active Epoch/最新 Segment/无更新 Epoch/无其他 open·sealing/无更大 turn_sequence 时才恢复 open；其余转 sealing 并补建 CompactionInput+补发 Job；旧 Epoch Segment 不回退 open；不调用 LLM/不删数据 | 迁移 |
| 67 | 同一 OutboxJob 重试与 deadletter 后人工重放均复用同一 OutboxJob 与同一 Run（outbox_job_id 不变，attempt_count+1）；UNIQUE(outbox_job_id) 保证一 Job 一 Run | 集成 |
| 68 | verified_tool_states 条目 tool_call_id 提升为顶层字段且 checker 按其比对 | 单元 |
| 69 | 仅含 failed/cancelled/preempted 终态 Turn 的 Segment 可密封，记录确定性写入 unresolved_failures | 单元 |
| 70 | turn_manifest / event_manifest 被完整冻结（含 content_hash、event_type、turn_event_index） | 单元 |
| 71 | LLM 故意打乱 tool_result_summaries 顺序，回填仍按 item_ref→tool_call_id 正确映射 | 单元 |
| 72 | LLM 回传未知/编造 item_ref 不覆盖确定性工具状态，仅告警丢弃 | 单元 |
| 73 | epoch_rollover Handler 已在 HandlerRegistry 注册 v1 且对已完成 Epoch 幂等返回成功 | 集成 |
| 74 | 历史旧 sealed/archived Epoch 的 Segment 修复命令不错误恢复为 open（只转 sealing） | 迁移 |
| 75 | UNIQUE(compaction_runs.outbox_job_id) / UNIQUE(checkpoint_runs.outbox_job_id) 约束生效 | 单元 |

## O. 回滚方案

- `begin_segment_sealing()` 失败 → Segment 保持 open，不影响新 Turn
- `Segment.status="sealing"` 下崩溃 → **不 reset 为 open**，Outbox retry 或人工处理
- CompactionRun/CheckpointRun 达 max_retries → Worker 原子 deadletter（OutboxJob + Run 同一事务），旧 claim 不得 deadletter 新 Run
- ContextAssembler Summary 加载失败 → degraded state + bridge fallback；bridge 缺失时不静默加载全部历史
- Epoch rollover 失败 → 旧 Epoch 保持 active，新 Epoch 未创建
- Migration downgrade → `DROP TABLE compaction_runs, compaction_inputs, checkpoint_runs, epoch_compaction_inputs` + `ALTER TABLE segment_summaries DROP COLUMN ...`
