# Phase 2：Memory Ingestion 差距审计（终版 · 已实施）

> **状态**：✅ 实施完成，34/34 核心测试通过
> **实施日期**：2026-07-15
> **前置**：Phase 0.5A、Phase 0.5B（Outbox claim/lease/fencing、MemoryIngestionRun、异步 memory_extraction）、Phase 1（有界 ContextAssembler、Epoch/Segment、WorkingState、Artifact 引用化）
> **约束**：不重做 Phase 0.5B；不新增仅为了名称匹配的 ScopeValidator/DuplicateResolver/ValueGate 类；不为显式路径创建 MemoryIngestionRun

---

## A. 显式记忆真实调用链

**入口**：`remember_or_update` 工具 → `builtin_tools.py:_handle_remember_or_update`

```
用户："记住 XXX"
  → AgentGraph LLM → tool_call: remember_or_update(content, memory_type, memory_key)
  → _handle_remember_or_update(db, ctx, content, ...)
     1. 构造 EvidenceItem(trust_level=TRUSTED, source_type="user_message")
     2. ProposalNormalizer.normalize(...)
     3. MemoryWriteService.write(proposal, run_context)
        3a. MemoryGate.decide(proposal)
        3b. ConflictResolver.resolve(proposal, existing)
        3c. 执行：create / reinforce / supersede / revise / promote / merge / ignore
        3d. MemoryStore.create_record()
```

**结论**：核心链完整。不缺少 ScopeValidator/DuplicateResolver/ValueGate 类——功能已在 MemoryGate + ConflictResolver + validate_scope() 中。

---

## B. 隐式记忆真实调用链

```
Turn 完成
  → TurnExecutionService._finalize_turn()
    → enqueue OutboxJob(job_type="memory_extraction",
        payload={
          user_message, reply, thread_id,
          source_turn_record_id: turn.id,
          source_turn_id: turn.turn_id,
          source_event_ids: [user_message_event.id, assistant_reply_event.id],
        })
  → OutboxWorker
    → handle_memory_extraction(claimed)
      Phase A: _validate_source_turn() + _resolve_ingestion_run()
      Phase B: UnifiedMemoryExtractor.extract() → LLM + ProposalNormalizer
      Phase C: fencing + MemoryWriteService.write_batch()
```

**结论**：三步幂等 + fencing，复用 ProposalNormalizer + MemoryWriteService。Outbox payload 记录完整的 `source_event_ids`（用户消息 + Assistant 回复的真实 Event.id）。

---

## C. 共享组件

两个路径必须共享：

| 组件 | 显式 | 隐式 |
|------|------|------|
| `ProposalNormalizer` | ✅ | ✅ |
| `MemoryGate` | ✅ | ✅ |
| `ConflictResolver` | ✅ | ✅ |
| `MemoryWriteService` | ✅ | ✅ |
| `MemoryStore` | ✅ | ✅ |

显式路径**不创建** `MemoryIngestionRun`——该表仅用于异步 Extractor 批次。

---

## D. POST `/api/memories` 旁路修复

**保留端点**（前端记忆管理 + 手动新增入口），完全删除业务旁路：

```
POST /api/memories
  → ProposalNormalizer
  → MemoryWriteService.write()
    → MemoryGate
    → ConflictResolver
    → MemoryStore
```

设置：
```python
execution_mode = "user_required"
created_by = "manual_memory_api"
```

### Idempotency-Key（强制）

`Idempotency-Key` header **必须持久化并受唯一约束保护**。重复请求返回原 WriteResult，不得产生第二次 create/reinforce：

```sql
UNIQUE(memory_proposals.idempotency_key)
```

生产代码**不得保留可条件启用的旧旁路**（不保留 `MemoryStore.create_record()` 直接调用路径）。

---

## E. execution_mode 传播方案

`execution_mode` 放入 `RunContext`，写入 proposal 审计记录时必须**显式传播**：

```python
memory_proposals.execution_mode = run_context.execution_mode
# 值："user_required" | "system_best_effort"
```

`MemoryWriteService` 对真正失败**统一抛出异常**，不根据 `execution_mode` 选择吞掉还是传播。调用方负责失败语义：

| 模式 | 调用方 | 失败行为 |
|------|--------|----------|
| `user_required` | `remember_or_update` 工具、POST `/api/memories` | 工具返回 `{"ok": false}`；Turn 不得声称已记住（见 §E.1 postcondition） |
| `system_best_effort` | `handle_memory_extraction` Outbox handler | 映射为 `RETRYABLE_ERROR` → retry/deadletter，不阻断原 Turn |

**不新增** `user_required_failed` WriteOutcome。

### E.1 user_required 防假成功（代码级 postcondition）

禁止只匹配回复是否包含"已记住"。必须使用结构化 enforcement：

```text
required memory operation 未成功
→ 结构化标记 memory_write_failed
→ 不允许普通 success completion 覆盖
→ 向用户明确说明未保存成功
```

Turn 中其他任务可以完成（如 list_tasks），但不得把记忆操作报告为成功。

---

## F. provenance 字段

### F.1 术语区分（强制）

| 字段 | 含义 | 来源 |
|------|------|------|
| `source_turn_record_id` | **TurnRecord 数据库主键**（UUID），用于关联查询 | `turn.id`（PK） |
| `source_turn_id` | **稳定业务 turn_id**，仅用于审计追踪 | `turn.turn_id`（业务 ID） |

**禁止混用**。两者在不同场景各有用处：
- `ingestion_run.source_turn_record_id` → FK 关联 TurnRecord 行
- `proposal.source_turn_id` → 审计日志中的业务标识

### F.2 显式路径

```python
# RunContext 携带
ctx.turn_record_id       # TurnRecord.id（数据库主键）
ctx.source_event_ids     # [user_message_event.id]（真实 Event.id）

# Proposal 中写入
proposal.source_turn_record_id = ctx.turn_record_id
proposal.source_turn_id = RunContext.turn_id      # 业务 turn_id
proposal.source_event_ids = [request_event_id]    # 真实 Event.id
```

幂等键：
```text
turn_id + tool_call_id + canonical_key + scope_type + scope_id
```

`ingestion_run_id` **允许为空**（显式路径不使用 MemoryIngestionRun）。

### F.3 隐式路径

在 `_finalize_turn()` 持久化 Event 和 OutboxJob 的**同一事务**中，写入完整 `source_event_ids`：

```python
payload = {
    "user_message": message,
    "reply": reply,
    "thread_id": thread.id,
    "source_turn_record_id": turn.id,               # TurnRecord PK
    "source_turn_id": turn.turn_id,                  # 业务 turn_id
    "source_event_ids": [
        user_message_event.id,                       # 用户消息真实 Event.id
        assistant_reply_event.id,                    # Assistant 回复真实 Event.id
    ],
}
```

禁止使用 `trace_id` 代替 `Event.id`。

### F.4 Assistant reply 证据约束

Gate 必须要求至少**一条可信 user_message evidence**，Assistant 事件不能单独生成用户记忆。

---

## G. DB 迁移前预检

实施前必须核对以下字段是否已存在于数据库中：

| 表 | 字段 | 检查 |
|----|------|------|
| `memory_proposals` | `execution_mode` | 若已存在 → 复用；否则新增 |
| `memory_proposals` | `retention_policy` | 若已存在 → 复用；否则新增 |
| `memory_proposals` | `valid_to` | 若已存在 → 复用；否则新增 |
| `memory_proposals` | `durable` | 若已存在 → 复用；否则新增 |
| `memory_proposals` | `idempotency_key` | 若已存在 → 加固 UNIQUE 约束；否则新增 |
| `memory_proposals` | `source_turn_record_id` | 若已存在 → 复用；否则新增 |
| `memory_evidence` | `source_event_id` | 确认类型（应为 UUID/FK，非 trace_id） |
| `memory_records` | `retention_policy` | 若已存在 → 复用；否则新增 |
| `memory_records` | `valid_to` | 若已存在 → 复用；否则新增 |

已有等价字段时复用，不重复建列；没有时加入本阶段 Alembic migration。

**不得只添加 Pydantic 字段而不持久化到数据库。**

---

## H. durability、confidence、retention 和 lifecycle 分离

### H.1 新增字段（ORM + Pydantic）

```python
# MemoryProposal + MemoryRecord ORM
retention_policy: Literal["ephemeral", "normal", "pinned"] = "normal"
valid_to: datetime | None = None
durable: bool = True
```

### H.2 规则

| 场景 | Gate 行为 | 写入 |
|------|-----------|------|
| `durable=False` 且无明显未来价值 | **reject** | — |
| 临时但有作用（如一次性任务事实） | active | `retention_policy = "ephemeral"` + 设置 `valid_to` |
| 长期信息 | active | `retention_policy = "normal"` |
| 用户明确要求不可自动遗忘 | active | `retention_policy = "pinned"`（仅 `user_required`） |
| 证据不确定但可能长期有效 → 置信度低 | **candidate** | — |

**禁止** `durable=False → candidate`。Candidate 表示**不确定**，不表示**临时**。

### H.3 ephemeral 强制不变量

```text
retention_policy = "ephemeral"
→ valid_to IS NOT NULL
→ valid_to > valid_from
```

时间不明确且没有配置 TTL 时，Gate **不得写入无期限 ephemeral 记录**——返回 reject 或要求调用方提供明确过期时间。

---

## I. 冲突和 cardinality

### 操作矩阵（不变）

`create / reinforce / supersede / revise / promote / merge / ignore`

### 新增约束

- **scalar / latest_value_wins key**：整条 `supersede`，不深度合并
- **revise_allowed key**：按 `KeyPolicy` 执行类型明确的字段级更新，**不**对所有 `structured_value` 默认 deep merge
- `canonical_key + scope` 层面语义重复 → 正确选择 reinforce / ignore / supersede

---

## J. 精确修改文件

| 文件 | 修改内容 |
|------|---------|
| `context/run_context.py` | 新增 `execution_mode`、`turn_record_id`、`source_event_ids` 字段 |
| `memory/memory_types.py` | `MemoryProposal` 新增 `retention_policy`、`valid_to`、`durable`、`source_turn_record_id`、`execution_mode` |
| `memory/memory_gate.py` | H.2 规则：retention_policy 判定 + `durable=False` → reject + ephemeral 不变量校验 |
| `memory/memory_write_service.py` | 写入时传递 `retention_policy` → `MemoryRecord`；对真正失败统一抛异常；传播 `execution_mode` 到 proposal 审计记录 |
| `db/models.py` | `MemoryRecord` 新增 `retention_policy`、`valid_to`；`MemoryProposal` ORM 新增 `execution_mode`、`retention_policy`、`valid_to`、`durable`、`source_turn_record_id`、`idempotency_key` UNIQUE |
| `tools/builtin_tools.py` | 构造 `RunContext` 带 `execution_mode="user_required"`；设置 `source_event_ids`、`source_turn_record_id`、`source_turn_id`；失败时返回 `{"ok": false}` + 结构化 postcondition |
| `memory/memory_extractor.py` | `RunContext` 传入 `execution_mode="system_best_effort"`；传入 `durable` 字段 |
| `worker/outbox_handlers.py` | Phase C 注入 `source_event_ids`（来自 payload）；Gate 拒绝 assistant-only evidence；失败映射 RETRYABLE_ERROR |
| `runtime/turn_execution.py` | `_finalize_turn()` 同一事务中写入完整 `source_event_ids` 到 Outbox payload（user_message + assistant_reply） |
| `api/routes_memories.py` | 完全删除旁路；走 ProposalNormalizer → MemoryWriteService；`execution_mode="user_required"`；`Idempotency-Key` 持久化 + UNIQUE 约束 |
| `alembic/versions/` | 新增 migration：所有 G 节缺失字段 + UNIQUE 约束 |

---

## K. 测试矩阵

| 测试 | 类型 | 覆盖 |
|------|------|------|
| A. 显式 read-after-write | 端到端 | Turn A 记住偏好 → Turn B 召回并使用 |
| B. 更新 supersede | 端到端 | 两次记住不同值 → 仅最新生效 |
| C. 隐式记忆 | 端到端 | 自然表达 → memory_extraction → Turn B 召回 |
| D. user_required 写入失败 → 结构化 postcondition | 单元 | 工具返回 `{"ok": false}`，不允许 success completion 覆盖 |
| E. system_best_effort 失败 → retry/deadletter | 集成 | 失败不阻断 Turn，Outbox 重试 |
| F. POST /api/memories 统一链路 + 幂等键 | 集成 | 经过完整链；重复请求返回原结果不重复写入 |
| G. 临时偏好 → active + ephemeral + valid_to | 单元 | `retention_policy=ephemeral`，`valid_to > valid_from` |
| H. `durable=False` 一次性事实 → reject | 单元 | Gate 返回 reject，不进入 candidate |
| I. 不确定长期偏好 → candidate | 单元 | confidence < 阈值，`retention_policy=normal` |
| J. `pinned` 只能由 `user_required` 创建 | 单元 | `system_best_effort` 设置 pinned → Gate reject |
| K. assistant reply 不能单独成为用户记忆证据 | 单元 | 仅 assistant evidence → Gate reject |
| L. `source_event_ids` 全部可在 Event 表查证 | 集成 | ID 匹配真实 Event 行；禁止 trace_id |
| M. 显式路径不创建 MemoryIngestionRun | 单元 | 写入后 DB 查询 → 0 行 |
| N. `retention_policy` 写入后可读回 | 集成 | `MemoryRecord.retention_policy` 与写入值一致 |
| O. ephemeral 无 valid_to → Gate reject | 单元 | `retention_policy=ephemeral` 且 `valid_to` 为空 → reject |
| P. `source_turn_record_id` ≠ `source_turn_id` | 单元 | PK 和业务 ID 分别正确填充，禁止混用 |
| Q. Idempotency-Key 持久化 + UNIQUE | 集成 | 重复请求返回原 WriteResult |
| R. migration upgrade/downgrade 成功 | CI | `alembic upgrade head && alembic downgrade -1` |

---

## L. 回滚方案

- `execution_mode` 默认 `system_best_effort`（与当前行为等价），仅显式路径设为 `user_required`
- `ConflictResolver` 不受行为影响——仅新增 `retention_policy` 传入
- `routes_memories.py`：不留旧旁路条件开关，直接替换为统一写入链路
- Migration 支持 `downgrade`
- 新增列均有安全默认值（`normal`），不影响现有数据
