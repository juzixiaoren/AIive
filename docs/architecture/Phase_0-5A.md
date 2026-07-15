# Phase 0.5A：Turn 幂等、消息去重、事件顺序与历史修复（v5 最终实施版）

> **状态**：方案（待确认） | **禁止**：修改任何代码
>
> **前置**：无。独立部署。

---

## A. Turn 原子抢占状态机

### 状态

```
not_started  → 创建后未抢占
running      → 已抢占，AgentGraph 执行中
completed    → 正常完成
interrupted_unknown → 进入 AgentGraph 后任何异常（不再有 failed 状态）
```

### 原子抢占

```sql
-- 短事务内唯一抢占操作
UPDATE turn_records
SET status = 'running',
    execution_id = :execution_id,
    lease_expires_at = :lease,
    updated_at = NOW()
WHERE id = :turn_record_id
  AND status = 'not_started';
```

返回 `affected_rows == 1` 才进入 AgentGraph。`affected_rows == 0` 表示已被其他请求抢占或状态已变。

### 幂等解析

```
POST /api/chat { turn_id, message }
  │
  ├─ 查询: SELECT FROM turn_records WHERE thread_id=? AND turn_id=?
  │
  ├─ 不存在 → INSERT (not_started, fingerprint)
  │          → COMMIT
  │          → 原子抢占 UPDATE (not_started → running)
  │          │   ├─ affected=1 → proceed (进入 AgentGraph)
  │          │   └─ affected=0 → 重新读取状态
  │          │       ├─ completed + fingerprint 匹配 → 返回缓存
  │          │       ├─ completed + fingerprint 不匹配 → 409
  │          │       └─ running / interrupted_unknown → 返回对应状态
  │
  ├─ 存在 + status=not_started
  │          ├─ fingerprint 不匹配 → 409 (禁止覆盖 fingerprint)
  │          └─ fingerprint 匹配 → 原子抢占
  │              ├─ affected=1 → proceed
  │              └─ affected=0 → 被抢 → 读取新状态
  │
  ├─ 存在 + status=completed + fingerprint 匹配 → 返回缓存
  ├─ 存在 + status=completed + fingerprint 不匹配 → 409
  ├─ 存在 + status=running + lease 有效 → 202
  ├─ 存在 + status=running + lease 过期 → 返回 "turn_interrupted"
  └─ 存在 + status=interrupted_unknown → 返回 "turn_interrupted"
```

### 失败规则

一旦 `not_started → running` 抢占成功并进入 AgentGraph，**任何异常均标记为 `interrupted_unknown`**。不存在可自动重试的 `failed` 状态。不检查 tool Event 是否存在。

---

## B. 无长事务的执行时序

```
┌──────────────────────────────────────────────────────────────────┐
│ Phase 1: 幂等解析 (短事务 1a + 1b)                                │
│                                                                    │
│  db1: INSERT TurnRecord(not_started) + user_message Event          │
│       COMMIT                                                       │
│  db1: UPDATE running (原子抢占, affected_rows 检查)                │
│       COMMIT                                                       │
│  both sessions closed                                              │
└──────────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│ Phase 2: 上下文加载 (短事务 2, 只读 + recall audit 写入)           │
│                                                                    │
│  db2: Thread + identity + policies + core_memory + recall          │
│       + _persist_recall_run() → 短事务内写入审计数据               │
│  COMMIT (recall audit 持久化)                                      │
│  将上下文转换为纯数据 (ContextBundle)                               │
│  db2.close()  ← Session 已关闭，无长连接                            │
└──────────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│ Phase 3: LLM + 工具执行 (无 DB Session)                            │
│                                                                    │
│  langchain_llm.invoke() / astream_events()                        │
│  ToolNode.invoke() → 工具 handler 使用独立 Session (已有)           │
│  返回: reply, tool_records (含 tool_call_id), action_cards         │
│  无任何 db session 打开                                            │
└──────────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│ Phase 4: 记忆信号 + 提取 (无 DB Session)                           │
│                                                                    │
│  ActionPlanner.classify_memory_signal() → LLM                     │
│  if SYNC: UnifiedMemoryExtractor.extract() → LLM                  │
│  sync_proposals 保存在内存中                                       │
│  无任何 db session 打开                                            │
└──────────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│ Phase 5: 最终事务 (短事务 3, 仅 DB 写入)                           │
│                                                                    │
│  db3: UPDATE turn_records SET status='completed',                  │
│           response_payload=...                                     │
│        WHERE id=? AND execution_id=? AND status='running'          │
│        → affected 必须 == 1                                        │
│                                                                    │
│  写入 tool_call / tool_result / llm_response / chat_ended Event    │
│  写入 ContextSnapshot                                              │
│  写入 sync_proposals (DB only, 事务外已生成)                        │
│  enqueue OutboxJob (async memory_extraction)                       │
│                                                                    │
│  COMMIT                                                           │
│  db3.close()                                                      │
└──────────────────────────────────────────────────────────────────┘
```

### 关键保证

1. **Phase 2 之后无长 Session**：LLM 和工具执行期间不持有 DB 会话。
2. **Recall audit 写入**：`_persist_recall_run()` 移至 Phase 2 的短事务中。与 read Session 为同一事务，读取完成后一起 commit。不会因 Session 关闭而丢失。
3. **Phase 5 仅 DB 写入**：任何 LLM/HTTP/工具 mock 被调用即视为测试失败。

---

## C. AgentGraphResult 最终字段

```python
@dataclass
class ToolRecord:
    """单个工具调用的完整记录。"""
    tool_call_id: str          # LLM 生成的 tool_call ID (如 "call_abc123")
    batch_index: int           # 属于第几个 AIMessage(tool_calls=[...])
    name: str
    params: dict[str, Any]
    result: dict[str, Any]     # {"ok": bool, "result": str}
    status: str                # "completed" | "failed"
    order_index: int           # 全局执行顺序 (跨 batch)


@dataclass 
class AgentGraphResult:
    """AgentGraph 执行完成后返回的纯数据结构。不含任何 DB 句柄。"""

    reply: str
    trace_id: str                        # ← 真实 trace_id，非空字符串
    tool_records: list[ToolRecord]       # 含 tool_call_id，支持多工具并行恢复
    action_cards: list[dict[str, Any]]
    context_snapshot_items: list[ContextItem]
    context_snapshot_meta: dict[str, Any]
    post_context_items: list[ContextItem]
    post_full_contents: dict[str, str]
    memory_signal: MemorySignalDecision  # ← 仅 signal，不含 proposals
    # sync_memory_proposals 由 TurnExecutionService 单独生成
```

---

## D. 多工具并行调用的恢复

### ToolRecord 的产生

```python
# AgentGraph._execute_graph 中
batch_index = 0
order_index = 0

for msg in result["messages"]:
    if isinstance(msg, AIMessage) and msg.tool_calls:
        for tc in msg.tool_calls:
            tool_records.append(ToolRecord(
                tool_call_id=tc["id"],
                batch_index=batch_index,
                name=tc["name"],
                params=tc.get("args", {}),
                result=...,      # 从后续 ToolMessage 匹配
                status=...,
                order_index=order_index,
            ))
            order_index += 1
        batch_index += 1
```

### _build_history_messages 恢复

```python
@staticmethod
def _build_history_messages(turn_events: list[Event]) -> list[BaseMessage]:
    """从 Event 表重建消息序列。支持多工具并行调用。

    Event.payload 中包含 tool_call_id 和 batch_index，
    按 batch 分组重建 AIMessage(tool_calls=[...])。
    """
    # 按 batch_index 分组 tool_call
    # 每组生成一个 AIMessage(tool_calls=[...])
    # 每个 tool_result 生成 ToolMessage(tool_call_id=...)
```

### Event payload 结构

```json
// tool_call
{"name": "search_memory", "params": {...}, "tool_call_id": "call_abc", "batch_index": 0}

// tool_result
{"name": "search_memory", "result": {...}, "tool_call_id": "call_abc", "batch_index": 0}
```

---

## E. Heartbeat / Fencing

### Turn Heartbeat

```python
class TurnHeartbeat:
    def _loop(self):
        while not self._stop_event.wait(HEARTBEAT_INTERVAL):
            try:
                db = SessionLocal()
                affected = db.query(TurnRecord).filter(
                    TurnRecord.id == self._turn_record_id,
                    TurnRecord.execution_id == self._execution_id,
                    TurnRecord.status == "running",
                ).update({
                    TurnRecord.lease_expires_at: datetime.now(timezone.utc) + LEASE_DURATION,
                    TurnRecord.last_heartbeat_at: datetime.now(timezone.utc),
                })
                db.commit()
                if affected == 0:
                    self._lease_lost = True   # ← 标记租约丢失
                    logger.error("Turn heartbeat: LEASE LOST")
            except Exception:
                logger.exception("Heartbeat 异常，不退出线程")
            finally:
                db.close()
```

### 最终提交 fencing

```python
def _finalize_turn(self, turn_record_id, execution_id, ag_result, sync_proposals):
    db = SessionLocal()
    try:
        # 先更新 TurnRecord（fencing）
        affected = db.query(TurnRecord).filter(
            TurnRecord.id == turn_record_id,
            TurnRecord.execution_id == execution_id,
            TurnRecord.status == "running",
        ).update({
            TurnRecord.status: "completed",
            TurnRecord.response_payload: response_dict,
            ...
        })
        if affected != 1:
            db.rollback()
            raise FencingViolationError("TurnRecord fencing violation")

        # 然后写入 events / snapshot / memory / outbox
        ...
        db.commit()
    finally:
        db.close()
```

### _mark_interrupted fencing

```python
def _mark_interrupted(turn_record_id, execution_id):
    db = SessionLocal()
    try:
        affected = db.query(TurnRecord).filter(
            TurnRecord.id == turn_record_id,
            TurnRecord.execution_id == execution_id,
            TurnRecord.status == "running",
        ).update({
            TurnRecord.status: "interrupted_unknown",
            TurnRecord.lease_expires_at: None,
            TurnRecord.updated_at: datetime.now(timezone.utc),
        })
        db.commit()
    finally:
        db.close()
```

---

## F. History 合并（新旧 Turn）

```python
def get_recent_messages(self, thread_id: str, max_turns: int = 20):
    """合并新 Turn (turn_id IS NOT NULL) 和旧 Turn (turn_id IS NULL) 的事件。

    分组策略:
    - 新 Turn: 按 turn_id 分组
    - 旧 Turn: 按 user_message 边界分组

    排序策略:
    - 每组计算 sort_key = (earliest_created_at, turn_sequence 或 BIGINT_MAX)
    - 按 sort_key 升序排列各组
    - 组内按 (turn_event_index / created_at+id) 排序
    """

    # 1. 新 Turn 分组
    new_turns = (
        self._db.query(TurnRecord)
        .filter(TurnRecord.thread_id == thread_id, TurnRecord.status == "completed")
        .order_by(TurnRecord.turn_sequence.desc())
        .limit(max_turns)
        .all()
    )
    new_groups = {}  # turn_id → { events, sort_key }

    # 2. 旧 Turn 分组 (overscan by user_message boundary)
    legacy_events = self._query_legacy_events(thread_id, max_turns * 3)
    legacy_groups = self._group_by_user_boundary(legacy_events)
    # sort_key = (group.earliest_created_at, BIGINT_MAX)

    # 3. 合并排序
    all_groups = list(legacy_groups.values()) + list(new_groups.values())
    all_groups.sort(key=lambda g: g.sort_key)

    # 4. 取前 max_turns 组，展平事件
    result = []
    for group in all_groups[:max_turns]:
        result.extend(group.events_sorted)
    return result
```

**不依赖"旧一定在前"假设**：通过 `sort_key`（基于 `earliest_created_at`）合并，旧 Turn 和新 Turn 按实际时间顺序排列。

---

## G. 迁移

### PostgreSQL

```sql
-- Phase 1: TurnRecord 表
CREATE TABLE turn_records (
    id VARCHAR(36) PRIMARY KEY,
    thread_id VARCHAR(36) NOT NULL REFERENCES threads(id),
    turn_id VARCHAR(36) NOT NULL,
    turn_sequence BIGINT NOT NULL DEFAULT 0,
    status VARCHAR(32) NOT NULL DEFAULT 'not_started',
    execution_id VARCHAR(36),
    lease_expires_at TIMESTAMPTZ,
    attempt_no INTEGER DEFAULT 1,
    last_heartbeat_at TIMESTAMPTZ,
    request_event_id VARCHAR(36),
    request_fingerprint VARCHAR(64) NOT NULL DEFAULT '',
    response_payload JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE UNIQUE INDEX uq_turn_record_thread_turn ON turn_records (thread_id, turn_id);
CREATE INDEX ix_turn_records_thread_status ON turn_records (thread_id, status);
CREATE INDEX ix_turn_records_thread_sequence ON turn_records (thread_id, turn_sequence);

-- turn_sequence
CREATE SEQUENCE turn_sequence_seq START 1 INCREMENT 1;
ALTER TABLE turn_records ALTER COLUMN turn_sequence SET DEFAULT nextval('turn_sequence_seq');

-- Phase 2: Event 新列
ALTER TABLE events ADD COLUMN turn_id VARCHAR(36);
ALTER TABLE events ADD COLUMN turn_event_index INTEGER;

-- partial unique index
CREATE UNIQUE INDEX ix_events_turn_order_unique
    ON events (thread_id, turn_id, turn_event_index)
    WHERE turn_id IS NOT NULL AND turn_event_index IS NOT NULL;

-- non-unique index for queries
CREATE INDEX ix_events_turn_order ON events (thread_id, turn_id, turn_event_index);


-- Downgrade
DROP INDEX IF EXISTS ix_events_turn_order;
DROP INDEX IF EXISTS ix_events_turn_order_unique;
ALTER TABLE events DROP COLUMN turn_event_index;
ALTER TABLE events DROP COLUMN turn_id;
DROP TABLE turn_records;          -- ← 先删表
DROP SEQUENCE IF EXISTS turn_sequence_seq;  -- ← 再删 sequence
```

### SQLite 兼容

- `JSONB` → 模型使用 `JSON` 类型（SQLAlchemy 自动适配）
- Partial unique index → SQLite 不支持。创建普通 `UNIQUE(thread_id, turn_id, turn_event_index)`。SQLite 的 NULL 被视为 distinct，理论上不冲突。应用层额外保证。
- `Sequence` → SQLite 忽略。`turn_sequence` 使用应用层分配（见下方）。

### SQLite turn_sequence 并发

**测试环境**：SQLite 单写者。使用进程内 `threading.Lock` 保护 `SELECT MAX+1 → INSERT`。

**生产环境**：PostgreSQL SEQUENCE（`nextval('turn_sequence_seq')`），天然并发安全。

```python
# TurnExecutionService - 仅 SQLite 路径
_sqlite_seq_lock = threading.Lock()

def _assign_turn_sequence_sqlite(db: Session) -> int:
    with _sqlite_seq_lock:
        max_seq = db.query(func.max(TurnRecord.turn_sequence)).scalar() or 0
        return max_seq + 1
```

文档明确：SQLite 锁仅用于测试，生产走 PostgreSQL SEQUENCE。并发 Turn 创建测试禁用 SQLite，仅靠 PostgreSQL 集成测试覆盖。

### ORM 模型兼容

```python
# db/models.py
from sqlalchemy.dialects.postgresql import JSONB

turn_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)

response_payload: Mapped[dict | None] = mapped_column(
    JSON().with_variant(JSONB(), "postgresql"), nullable=True
)
```

---

## H. 测试矩阵

| # | 测试 | 断言 |
|---|------|------|
| **消息去重** | | |
| T1 | 第一轮 `execute_turn("hello")` | HumanMessage("hello") 恰好 1 次 |
| T2 | 第二轮 | 历史 + 当前各 1 次 |
| T3 | 相同文本连续两轮 | 历史保留两条 |
| **原子抢占** | | |
| T4 | 两个并发请求相同 turn_id → 只有一个抢占成功 | one affected=1, one affected=0 |
| T5 | 抢占失败 → 重新读取 → 返回正确状态 | completed/interrupted/conflict |
| T6 | not_started 已存在 + fingerprint 不同 → 409 | conflict |
| T7 | not_started 已存在 + fingerprint 相同 → 抢占 | proceed |
| T8 | 抢占成功后进入 AgentGraph → 任何异常 → interrupted_unknown | 无 failed 状态 |
| T9 | 抢占前 LLM 异常 → 不进入 AgentGraph → 可重试 | not_started 保留 |
| **无长 Session** | | |
| T10 | Phase 2 后 Session 已关闭 → LLM/工具执行期间无活跃 DB 连接 | mock 检测 |
| T11 | Recall audit 写入在 Phase 2 commit 后持久化 | MemoryRecallRun 存在 |
| T12 | 最终事务中 LLM/HTTP/工具 mock 被调用 → 测试失败 | assertion error |
| **单次同步提取** | | |
| T13 | TurnExecutionService 执行一次 memory_signal 分类 + 一次提取 | LLM mock 计数 = 2 (signal + extract) |
| T14 | AgentGraphResult 不含 sync_memory_proposals | 仅 memory_signal |
| **工具事件结构** | | |
| T15 | ToolRecord 含 tool_call_id | 匹配 LLM 返回的 ID |
| T16 | 多工具并行调用 → 按 batch_index 分组 | AIMessage(tool_calls=[t1,t2]) 正确重建 |
| T17 | _build_history_messages 恢复 tool_call/tool_result 配对 | ToolMessage.tool_call_id 匹配 |
| **trace_id** | | |
| T18 | AgentGraphResult.trace_id 非空 | 所有 Event/ContextSnapshot/Outbox/ChatResponse 一致 |
| **Heartbeat + Fencing** | | |
| T19 | Heartbeat 正常续租 | lease_expires_at 更新 |
| T20 | Heartbeat affected=0 → lease_lost | TurnExecutionService 检测 |
| T21 | lease 丢失后最终提交 → affected=0 → rollback | FencingViolationError |
| T22 | _mark_interrupted 同样 fencing | execution_id + running 匹配 |
| **History 合并** | | |
| T23 | 新 old 混合 → 按 earliest_created_at 排序 | 不依赖"旧在前"假设 |
| T24 | completed Turn 不交叉排列 | 每个 Turn 完整 |
| T25 | running/interrupted Turn 不在历史中 | 排除 |
| **迁移** | | |
| T26 | PostgreSQL partial unique index 生效 | 重复 (thread_id, turn_id, turn_event_index) 报错 |
| T27 | SQLite 兼容 (JSON, 普通 unique index) | 表创建成功 |
| T28 | downgrade 先删表再删 sequence | 无依赖错误 |
| **端到端** | | |
| T29 | HTTP POST /api/chat → TurnExecutionService → 完成 | 完整 ChatResponse |
| T30 | 重复 turn_id → 返回缓存 | response_payload 一致 |
| T31 | Stream 重试 → completed → 合成 done event | 含完整数据 |

---

## I. 精确修改文件

| # | 文件 | 变更关键 |
|---|------|---------|
| 1 | `db/models.py` | TurnRecord 新表；Event +turn_id/+turn_event_index；JSONB/JSON variant；索引 |
| 2 | `runtime/turn_execution.py` | **新建**：TurnExecutionService（原子抢占、无长 Session、heartbeat/fencing、单次提取） |
| 3 | `runtime/agent_graph.py` | 拆分：`_load_context()` + `_execute_graph()` + `_classify_memory_signal()`；返回 AgentGraphResult（仅 memory_signal）；tool_records 含 tool_call_id/batch_index |
| 4 | `runtime/graph.py` | invoke_chat → 委托 TurnExecutionService |
| 5 | `runtime/event_logger.py` | log_event +turn_id/turn_event_index |
| 6 | `runtime/thread_state.py` | get_recent_messages → 新旧 Turn 合并排序 |
| 7 | `context/run_context.py` | +turn_id |
| 8 | `api/routes_chat.py` | ChatRequest +turn_id；路由委托 TurnExecutionService |
| 9 | `alembic/versions/xxxx_phase05a.py` | 完整迁移（含 PG sequence + partial index + SQLite fallback） |
| 10-15 | 测试文件 | 新建 test_turn_execution.py；修改 test_agent_graph.py/test_thread_state.py/test_chat_api.py |

---

## J. 回滚

```bash
alembic downgrade -1
git revert <phase05a-commit>
```

无旧数据风险。仅新增列和表，回退即删除。
