# AIive 记忆系统架构设计文档

> 版本: 1.0 | 日期: 2026-07-10 | 基于终局架构重构

---

## 目录

1. [架构全景](#一架构全景)
2. [完整调用链](#二完整调用链)
3. [类型系统](#三类型系统)
4. [三套独立状态](#四三套独立状态)
5. [写入管线的五个组件](#五写入管线的五个组件)
6. [数据库 Schema](#六数据库-schema)
7. [并发策略与事务边界](#七并发策略与事务边界)
8. [三个实例：机制与效果](#八三个实例机制与效果)
9. [信任边界](#九信任边界)
10. [废弃清单](#十废弃清单)
11. [已知遗留问题](#十一已知遗留问题)

---

## 一、架构全景

```
                           ┌──────────────────────┐
                           │    用户聊天请求        │
                           └──────────┬───────────┘
                                      │
                            ┌─────────▼──────────┐
                            │   AgentGraph.run()  │
                            │  ┌────────────────┐ │
                            │  │ MemoryReadModel │ │ ← 上下文读取（精确key+type）
                            │  │  (R. Identity + │ │
                            │  │   Policy +       │ │
                            │  │   User Memories) │ │
                            │  └────────────────┘ │
                            │  ┌────────────────┐ │
                            │  │  LangGraph LLM  │ │ ← 主对话
                            │  │  + tool_calls   │ │
                            │  └────────────────┘ │
                            │  ┌────────────────┐ │
                            │  │  _finalize()    │ │
                            │  │  ├ classify()   │ │ ← 模型分类（非关键词）
                            │  │  ├ SKIP/ASYNC/  │ │
                            │  │  │   SYNC 路由   │ │
                            │  │  └ commit()     │ │
                            │  └────────────────┘ │
                            └─────────┬──────────┘
                                      │
              ┌───────────────────────┼───────────────────────┐
              │                       │                       │
    ┌─────────▼────────┐   ┌─────────▼────────┐   ┌─────────▼────────┐
    │ 显式工具调用        │   │ 模型分类=SKIP     │   │ 模型分类=EXTRACT  │
    │ remember_or_update│   │                  │   │                  │
    │ forget_memory    │   │ 不做任何提取       │   │ Outbox enqueue   │
    └────────┬─────────┘   └──────────────────┘   └────────┬─────────┘
             │                                              │
    ┌────────▼──────────────────────────────────────────────▼─────────┐
    │                    MemoryWriteService (唯一写入入口)              │
    │  ┌───────────┐  ┌──────────┐  ┌────────────────┐               │
    │  │ Proposal  │→│ Memory   │→│ Conflict       │               │
    │  │ Normalizer│ │ Gate     │ │ Resolver       │               │
    │  └───────────┘  └──────────┘  └───────┬────────┘               │
    │                                       │                        │
    │  ┌────────────────────────────────────▼───────────────────────┐│
    │  │  PostgreSQL 事务 (强一致边界)                                ││
    │  │  ├─ advisory lock (pg_advisory_xact_lock)                   ││
    │  │  ├─ memory_records (INSERT/UPDATE)                          ││
    │  │  ├─ memory_evidence (INSERT)                                ││
    │  │  ├─ memory_lineage (INSERT)                                 ││
    │  │  ├─ memory_proposals (INSERT)                               ││
    │  │  ├─ events (INSERT)                                         ││
    │  │  └─ outbox_jobs (INSERT - projection tasks)                 ││
    │  └─────────────────────────────────────────────────────────────┘│
    └─────────────────────────────────────────────────────────────────┘
                                      │
                              ┌───────▼────────┐
                              │  Outbox Worker  │ ← 独立进程 (未来)
                              │  ├ vector_upsert│   (Qdrant)
                              │  ├ markdown     │   (Projection)
                              │  └ cache_inv    │   (Context Cache)
                              └────────────────┘
```

---

## 二、完整调用链

### 2.1 主聊天流程

```
POST /api/chat                                                          [routes_chat.py]
  └─ invoke_chat(message, thread_id)                                    [graph.py]
       └─ db = SessionLocal()
       └─ AgentGraph(llm_client, db).run(message, thread_id)            [agent_graph.py]
            │
            ├─ 1. 上下文构建
            │   _resolve_memories_for_context()
            │     └─ MemoryReadModel(store).build_context()
            │          ├─ resolve_identity()
            │          │   └─ store.get_by_context_roles(["runtime_identity"])
            │          │       精确 key 查询: agent.display_name, user.display_name,
            │          │       agent.persona.relationship 等 (6个固定 key)
            │          ├─ policy_records
            │          │   └─ 精确key查 → 失败则 get_active_by_types(["policy"])
            │          └─ user_memories
            │              └─ 精确key查 → 失败则 get_active_by_types(
            │                   ["user_profile","knowledge","episodic","procedural"])
            │
            │   _get_runtime_identity()
            │     └─ MemoryReadModel(store).resolve_identity()
            │         返回: { agent_display_name, user_display_name,
            │                  relationship_style, response_style }
            │
            │   → 构建 SystemMessage:
            │     ## Runtime Identity
            │     ## User Memory (Evidence)
            │     ## Policy
            │     ## Due Tasks
            │
            ├─ 2. LangGraph 执行 (LLM + tool_calls + policy_check)
            │    assistant → policy_check → [tools → assistant] → END
            │
            └─ 3. _finalize() 终结处理
                 ├─ 记录 event + tool_result
                 │
                 ├─ classify_memory_signal(user_message, reply)
                 │   └─ ActionPlanner 用便宜模型做结构化分类
                 │       返回 MemorySignalDecision { action, confidence, reason }
                 │
                 ├─ SKIP: 不做任何提取，不 enqueue
                 ├─ EXTRACT_SYNC: 同事务内完成提取+写入
                 │   ├─ UnifiedMemoryExtractor.extract() → proposals
                 │   └─ MemoryWriteService.write() (每个 proposal)
                 └─ EXTRACT_ASYNC: enqueue outbox job
                     └─ process_all() 同步处理 (当前实现)
                         └─ handle_memory_extraction()
                             ├─ should_skip_system_message() 结构性检查
                             ├─ UnifiedMemoryExtractor.extract() LLM提取
                             └─ MemoryWriteService.write() 事务写入
```

### 2.2 显式工具调用流程

```
LLM tool_call "remember_or_update"                                [builtin_tools.py]
  └─ _handle_remember_or_update(db, ctx, content, type, key)
       │
       ├─ ProposalNormalizer.normalize()                          [proposal_normalizer.py]
       │    ├─ 类型映射: legacy type → canonical type
       │    ├─ key 解析: MemoryKeyRegistry.resolve(key)
       │    ├─ scope 推断: global/project/thread/...
       │    ├─ evidence 构建
       │    └─ 输出: MemoryProposal (含 idempotency_key)
       │
       └─ MemoryWriteService.write(proposal, sync_projection=True)
            │                                                [memory_write_service.py]
            ├─ MemoryGate.decide(proposal)                    [memory_gate.py]
            │    ├─ 类型验证 (必须为 canonical 8 种之一)
            │    ├─ 信任边界: 敏感类型需要 trusted 来源
            │    ├─ 外部内容不能写 user_profile/policy/agent_self
            │    ├─ assistant reply 不能独立强化用户画像
            │    ├─ 置信度 < 0.7 → candidate
            │    └─ 无 evidence → candidate
            │
            ├─ 获取 advisory lock (canonical_key + scope hash)
            ├─ SELECT FOR UPDATE 已有 active records
            ├─ ConflictResolver.resolve()                      [conflict_resolver.py]
            │    ├─ 无已有: create
            │    ├─ single + 同hash: reinforce
            │    ├─ single + 不同: supersede
            │    ├─ multi + 同hash: reinforce (去重)
            │    └─ multi + 不同: create
            │
            ├─ 原子写入:
            │    ├─ memory_records (INSERT/UPDATE)
            │    ├─ memory_evidence (INSERT)
            │    ├─ memory_lineage (INSERT)
            │    └─ memory_proposals (INSERT, 持久化审计)
            │
            ├─ EventLogger.log_event()
            ├─ OutboxJob enqueue (vector + markdown + cache)
            └─ _db_handler 提交事务
```

### 2.3 遗忘流程

```
LLM tool_call "forget_memory"                                    [builtin_tools.py]
  └─ _handle_forget_memory(db, ctx, memory_id, scope, target)
       └─ MemoryWriteService.forget(memory_id)                  [memory_write_service.py]
            ├─ content → tombstone (不可逆，格式: "forgotten:{id[:8]}:{ISO8601}")
            ├─ structured_value → NULL
            ├─ lifecycle_state → forgotten
            ├─ memory_evidence → DELETE (按 memory_id)
            ├─ Event → "memory.forgotten"
            ├─ OutboxJob → projection cleanup (vector_delete + cache_invalidate)
            └─ ForgetRequest → 审计记录
```

---

## 三、类型系统

### 3.1 Canonical MemoryType (8种)

| 类型 | 值 | 说明 | 示例 key |
|------|---|------|----------|
| `user_profile` | user_profile | 用户身份、偏好、习惯、日程、规律 | user.display_name, user.preference.*, user.routine.* |
| `agent_self` | agent_self | Agent 名称、人格、关系风格 | agent.display_name, agent.persona.* |
| `project` | project | 项目决策、技术栈、架构 | project.aiive.backend_stack |
| `policy` | policy | 规则、约束、禁止事项 | policy.no_external_tools |
| `procedural` | procedural | 工作流、执行方法、经验教训 | procedural.git_workflow |
| `episodic` | episodic | 值得记住的一次性事件 | episodic.meeting_20260710 |
| `knowledge` | knowledge | 通用事实和知识 | knowledge.weather_hk |
| `environment` | environment | 环境配置 | environment.db_host |

### 3.2 旧类型映射 (Legacy → Canonical)

| 旧 type | 新 type | 新 canonical_key 推导 |
|---------|---------|----------------------|
| `user_profile` | user_profile | 保留或从 content 推导 |
| `name` | user_profile | user.name 或 user.display_name |
| `preference` | user_profile | user.preference.{topic} |
| `routine` | user_profile | user.routine.{desc} |
| `habit` | user_profile | user.habit.{desc} |
| `schedule` | user_profile | user.schedule.{desc} |
| `agent_self` | agent_self | 保留 |
| `project` | project | project.{name}.{topic} |
| `policy` | policy | policy.{topic} |
| `environment` | environment | environment.{topic} |
| `fact` | knowledge 或 episodic | 从内容推导 |
| 无法判定 | - | 迁移标记为 knowledge, 人工 review |

### 3.3 Scope 合法值

| scope_type | scope_id | 约束 |
|-----------|----------|------|
| `global` | NULL (必须为空) | 全局范围 |
| `project` | 项目名 (必填) | 项目范围 |
| `thread` | thread_id (必填) | 会话线程范围 |
| `workspace` | 工作空间标识 (必填) | 工作空间范围 |
| `capability` | capability_id (必填) | 能力范围 |
| `environment` | 环境标识 (必填) | 环境配置范围 |

**上下文召回优先级**: thread > project > global

---

## 四、三套独立状态

### 4.1 lifecycle_state (生命周期)

```
candidate ──promote──▶ active ──sleep──▶ sleeping ──wake──▶ active
    │                    │                    │
    │ (过期 7d)          │ (forget)           │ (过期 30d)
    ▼                    ▼                    ▼
archived            forgotten            archived

candidate ──X──▶ sleeping  ← 禁止！sleeping 仅用于曾经 active 的记录
```

### 4.2 validity_state (数据效力)

```
valid → superseded → contradicted → expired
```

一个被 supersede 的记录: lifecycle_state 保持原值, validity_state = "superseded"

### 4.3 lineage_operation (谱系操作)

```
create / reinforce / revise / supersede / merge / promote / sleep / wake / archive / forget
```

**关键区分**:
- `reject` / `ignore` / `discard` 属于 proposal 决策，不是 MemoryRecord 生命周期
- `superseded` 是 validity_state，不是 lineage_operation

---

## 五、写入管线的五个组件

### 5.1 ProposalNormalizer

**职责**: 将原始输入标准化为 `MemoryProposal`

```
输入: (content, memory_type_hint, memory_key_hint, confidence, ...)
  │
  ├─ 类型映射: legacy type → canonical type (LEGACY_TYPE_MAP)
  ├─ key 解析: memory_key_hint → canonical_key (MemoryKeyRegistry)
  ├─ scope 推断: default_scope + key prefix → scope_type + scope_id
  ├─ evidence 构建: { source_type, trust_level, relation, content_span }
  ├─ idempotency_key 计算: sha256(canonical_key|scope_type|scope_id|content_hash)
  └─ 输出: MemoryProposal
```

**关键规则**:
- 未知 canonical type → 拒绝
- scope 非法组合 → 拒绝
- key 无法解析 → 从 type + content 推导 fallback

### 5.2 MemoryGate

**职责**: 信任准入 → reject/candidate/active

规则（按优先级）:
1. **类型验证**: 非 canonical type → reject
2. **信任边界**: user_profile/policy/agent_self 需要 trusted user evidence → 否则 reject
3. **外部内容限制**: 全 external evidence → 不能写 user_profile/policy/agent_self → reject
4. **assistant reply 限制**: 全 llm_reply evidence → 不能创建 user_profile → reject
5. **置信度阈值**: < 0.7 → candidate
6. **无 evidence**: → candidate
7. **默认**: active

**不再依赖**: execution_mode, should_execute, intent_type

### 5.3 ConflictResolver

**职责**: 确定 lineage operation (create/reinforce/supersede)

**规则（纯确定性，无启发式）**:

| 条件 | Operation | 新 ID? | Lineage |
|------|-----------|--------|---------|
| 无同 key + scope 的 active | create | Yes | create |
| single + 同 content_hash | reinforce | **No** | 更新 existing |
| single + 内容不同 | supersede | Yes | predecessor→successor |
| multi + 同 content_hash | reinforce | **No** | 更新 existing (去重) |
| multi + 同 structured_value_hash | reinforce | **No** | 更新 existing (去重) |
| multi + 所有都不同 | create | Yes | - |

**reinforce**: 不创建新 record，仅增加 `confidence + 0.1`, `reinforce_count + 1`, `last_reinforced_at`, 追加 evidence
**supersede**: 创建新 record，旧 record 的 `validity_state → superseded`, `superseded_by → new_id`

### 5.4 MemoryWriteService

**职责**: 事务编排 — lock → resolve → persist → event → outbox

事务内操作顺序:
```
BEGIN
  │
  ├─ pg_advisory_xact_lock(canonical_key_hash + scope_hash)
  ├─ SELECT FOR UPDATE (同 key+scope 的 active records)
  ├─ ConflictResolver.resolve()
  ├─ INSERT/UPDATE memory_records
  ├─ INSERT memory_evidence
  ├─ INSERT memory_lineage (revise/supersede/merge)
  ├─ INSERT memory_proposals (持久化审计)
  ├─ INSERT events
  ├─ INSERT outbox_jobs (projection: vector + markdown + cache)
  │
COMMIT → 释放 advisory lock
```

**candidate 记录**: 不 enqueue vector_upsert (仅在 active 时可搜索)

**forget**: Saga 覆盖 memory_records + evidence + event + outbox + ForgetRequest

### 5.5 MemoryReadModel / MemoryRetriever

**MemoryReadModel**: 确定性 key + type 查询

```
build_context()
  ├─ resolve_identity()
  │   └─ SELECT WHERE canonical_key IN (精确 runtime_identity keys)
  ├─ policies
  │   └─ 精确 key 查 → 失败则 SELECT WHERE memory_type='policy'
  └─ user_memories
      └─ 精确 key 查 → 失败则 SELECT WHERE memory_type IN
           ('user_profile','knowledge','episodic','procedural')
           ORDER BY importance DESC LIMIT 30
```

**MemoryRetriever**: scope-aware + relevance-ranked

```
retrieve(scope_type, scope_id, memory_types, query, max_results, max_tokens)
  ├─ 过滤: lifecycle='active' AND validity='valid'
  ├─ scope: (scope_type, scope_id) OR global
  ├─ 排序: scope DESC, importance DESC, observed_at DESC
  └─ 截断: max_results + token budget
```

### 5.6 MemoryExtractionPolicy

**职责**: 纯结构性守卫，不做自然语言判断

```
should_skip_system_message(msg)
  ├─ 空消息 → true
  └─ 以 "[runtime event" / "[system command" 开头 → true

resolve_action(signal_action, user_message)
  ├─ 系统消息 → SKIP
  └─ 否则 → signal_action (模型决定)
```

**所有语义判断由 `classify_memory_signal()` 完成**，基于模型调用：
- `"你好"` → 模型返回 SKIP（而非关键词 `"你好" in msg`）
- `"以后叫我B"` → 模型返回 EXTRACT_SYNC（而非关键词 `"以后叫我" in msg`）
- `"我喜欢Python但今天代码报错了"` → 模型判断有价值 → EXTRACT_ASYNC（而非被 `"报错了"` 误杀）

---

## 六、数据库 Schema

### 6.1 memory_records (核心表，31列)

| 列名 | 类型 | 说明 |
|------|------|------|
| id | VARCHAR(36) PK | UUID |
| memory_type | VARCHAR(64) | canonical type |
| canonical_key | VARCHAR(256) | 规范化 key |
| cardinality | VARCHAR(16) | single / multi |
| scope_type | VARCHAR(32) | global/project/thread/... |
| scope_id | VARCHAR(128) | NULL for global |
| content | TEXT | 记忆内容 |
| structured_value | JSONB | 结构化值 |
| lifecycle_state | VARCHAR(32) | candidate/active/sleeping/archived/forgotten |
| validity_state | VARCHAR(32) | valid/superseded/contradicted/expired |
| trust_level | VARCHAR(32) | trusted/semi_trusted/untrusted/untrusted_derived |
| stability | VARCHAR(32) | stable/contextual/volatile |
| stability_score | FLOAT | 连续评分 |
| confidence | FLOAT | 置信度 0-1 |
| importance | FLOAT | 重要性 0-1 |
| record_version | INT | 投影乱序检测 |
| reinforce_count | INT | 强化次数 |
| source_event_id | VARCHAR(36) | 源事件 |
| lineage | VARCHAR(128) | 快捷字段 |
| pinned | BOOLEAN | 固定(不受自动清理) |
| memory_key | VARCHAR(128) | 去重键(兼容旧) |
| revision_num | INT | 修订版本号 |
| supersedes | VARCHAR(36) | 替代目标 |
| superseded_by | VARCHAR(36) | 被替代为 |
| created_from | VARCHAR(36) | proposal_id |
| revision_of | VARCHAR(36) | 修订自 |
| merged_from | JSONB | 合并自(快捷) |
| valid_from | TIMESTAMPTZ | 有效期始 |
| valid_to | TIMESTAMPTZ | 有效期止 |
| observed_at | TIMESTAMPTZ | 最近观测时间 |
| last_reinforced_at | TIMESTAMPTZ | 最近强化时间 |
| created_at | TIMESTAMPTZ | 创建时间 |
| updated_at | TIMESTAMPTZ | 更新时间 |

**Partial Unique Index**:
```sql
CREATE UNIQUE INDEX ix_memory_records_single_active
  ON memory_records (canonical_key, scope_type, COALESCE(scope_id, ''))
  WHERE lifecycle_state = 'active' AND cardinality = 'single';
```

### 6.2 memory_proposals (审计表)

| 列名 | 类型 | 说明 |
|------|------|------|
| id | VARCHAR(36) PK | UUID |
| proposal_id | VARCHAR(128) UNIQUE | 提案唯一ID |
| source_event_ids | JSONB | 源事件ID列表 |
| memory_type/canonical_key/scope_*/content/structured_value | - | 同 memory_records |
| evidence | JSONB | 证据列表 |
| trust_level/confidence/importance/stability/stability_score | - | 评分 |
| proposed_operation | VARCHAR(32) | 提取器建议 |
| gate_decision | VARCHAR(32) | Gate 决策 (reject/candidate/active) |
| gate_reason/blocked_reason | TEXT | 拒绝/阻止原因 |
| final_operation | VARCHAR(32) | 最终执行的操作 |
| final_memory_id | VARCHAR(36) | 关联的 memory_records.id |
| requires_confirmation | BOOLEAN | 需确认 |
| extractor_name/version | VARCHAR | 提取器标识 |
| idempotency_key | VARCHAR(128) UNIQUE | 幂等键 |
| raw_payload/normalized_payload | JSONB | 原始/标准化负载 |
| created_at | TIMESTAMPTZ | 创建时间 |

### 6.3 memory_evidence (证据表)

| 列名 | 类型 | 说明 |
|------|------|------|
| id | VARCHAR(36) PK | UUID |
| memory_id | VARCHAR(36) FK | → memory_records.id |
| source_event_id | VARCHAR(36) | 源事件 |
| source_type | VARCHAR(32) | user_message/webpage/pdf/... |
| trust_level | VARCHAR(32) | trusted/semi_trusted/untrusted |
| relation | VARCHAR(32) | supports/contradicts/confirms/corrects/derived_from |
| content_span | TEXT | 原文片段 |
| created_at | TIMESTAMPTZ | 创建时间 |

### 6.4 memory_lineage (谱系表)

| 列名 | 类型 | 说明 |
|------|------|------|
| id | VARCHAR(36) PK | UUID |
| predecessor_id | VARCHAR(36) FK | 前驱 → memory_records.id |
| successor_id | VARCHAR(36) FK | 后继 → memory_records.id |
| operation | VARCHAR(32) | revise/supersede/merge/promote |
| reason | TEXT | 操作原因 |
| proposal_id | VARCHAR(36) | 关联提案 |
| created_at | TIMESTAMPTZ | 创建时间 |

---

## 七、并发策略与事务边界

### 7.1 并发控制

```
1. pg_advisory_xact_lock(canonical_key_hash + scope_hash)
   → 相同 key+scope 的写入串行化，事务结束时自动释放

2. SELECT ... FOR UPDATE
   → 锁定已有 active 行，防止并发更新

3. partial unique index
   → 最后防线: cardinality=single 的 active 唯一
```

### 7.2 强一致 vs 最终一致

| 场景 | 一致性 | 实现 |
|------|--------|------|
| 显式 `remember_or_update` | 强一致 | 同步写入，返回前 commit |
| 用户纠正 (forget/supersede) | 强一致 | 同步写入 |
| 关键 policy/identity | 强一致 | EXTRACT_SYNC 路径 |
| EXTRACT_SYNC 的 MemorySignal | 强一致 | 同事务 extract + write |
| 普通自动提取 | 最终一致 | enqueue → worker 消费 |
| KG/向量/投影 | 最终一致 | outbox 异步 |
| Markdown Projection | 最终一致 | outbox 异步 |
| Context Cache | 最终一致 | outbox 异步 |

**强一致边界**: PostgreSQL 事务 (含 memory_record + evidence + lineage + proposal + event + outbox)
**不要求**: 返回前 KG/Qdrant/Markdown 已完成

### 7.3 投影乱序处理

Outbox projection job 携带 `(memory_id, record_version)`:
- 旧版本晚到时: 查 DB 当前 `record_version`，若 `current > task` → 跳过
- 同版本到达: 执行投影更新

---

## 八、三个实例：机制与效果

### 例 1: 用户说"我喜欢简洁的回答" → 自动长期偏好

**数据流**:
```
用户输入 "我喜欢简洁的回答"
  → LLM 生成回复 "好的，我会保持简洁"
  → classify_memory_signal("我喜欢简洁的回答", "好的，我会保持简洁")
  → 模型返回: { action: "extract_async", confidence: 0.9, reason: "clear preference" }
  → enqueue → process_all()
    → UnifiedMemoryExtractor.extract()
    → LLM 提取: [{ content: "偏好简洁回复", memory_type: "user_profile",
                    memory_key: "user.preference.response_style", confidence: 0.9 }]
    → ProposalNormalizer:
        canonical_key = "user.preference.response_style" (registry 精确 key)
        cardinality = "single" (registry 注册)
        scope = global
    → MemoryWriteService.write():
        Gate: trusted user_message, confidence 0.9 → active
        ConflictResolver: 如已有旧 preference → reinforce
        DB: memory_records + evidence + lineage + proposal
        Event: memory.created/reinforced
        Outbox: vector_upsert + markdown_project + cache_invalidate
```

**效果**: 下次对话时，`MemoryReadModel.resolve_identity()` 精确查询 `user.preference.response_style`，将"偏好简洁回复"注入 LLM 系统提示的 Runtime Identity 块。Agent 自主调整为简洁回复风格。

**不依赖**: 任何关键词表 (`"我喜欢" in msg` 之类的判断不存在于系统中)。

---

### 例 2: 用户说"你好" → 零提取成本

**数据流**:
```
用户输入 "你好"
  → LLM 生成回复 "你好！有什么可以帮你的？"
  → classify_memory_signal("你好", "你好！有什么可以帮你的？")
  → 模型返回: { action: "skip", confidence: 0.95, reason: "simple greeting" }
  → 不 enqueue outbox job
  → 不进 MemoryExtractionPolicy (policy 只处理结构性检查)
```

**效果**: 一次便宜的 classify 调用（~100ms），无提取 LLM 调用，无记忆写入。不会像旧架构那样被关键词表误判或漏判。

**对比旧架构**: 旧方案用 `"你好" in msg_lower` → 会误杀 `"你好请问一下我喜欢什么颜色"` (包含"你好"和偏好)。新方案由模型理解整句语义 → 正确判断为 EXTRACT_ASYNC。

---

### 例 3: 用户说"以后叫我 B" → 显式工具 + 自动提取双重保护

**数据流**:
```
用户输入 "以后叫我 B"
  → LLM 判断需要工具 → tool_call "remember_or_update"
    params: { content: "B", memory_type: "user_profile",
              memory_key: "user.display_name" }
  → _handle_remember_or_update():
    → ProposalNormalizer: canonical_key="user.display_name", cardinality="single"
    → MemoryWriteService.write():
      Gate: trusted → active
      ConflictResolver: single key, 如之前叫"A"→内容不同→supersede
        (旧 record: validity→superseded, superseded_by→new_id)
        (新 record: lifecycle→active, content="B")
      memory_lineage: predecessor=A_id, successor=B_id, op="supersede"
      DB commit (强一致)

同时 classify_memory_signal("以后叫我 B", "好的，以后叫你 B")
  → 模型返回: { action: "extract_async" } (可能)
  → 但 enqueue 后的 extract 可能也会提取到 user.display_name = "B"
  → MemoryWriteService: ConflictResolver 发现 hash 相同 → reinforce (不发散)
```

**效果**: 
- 显式工具: 立即生效，强一致
- 自动提取: 即使也跑，hash 去重确保不发散
- 下次对话: `resolve_identity()` 返回 `user_display_name = "B"`

---

## 九、信任边界

### 9.1 逐条 Evidence 标记

每个 MemoryProposal 携带多条 evidence，每条独立标记:

```python
EvidenceItem(
    source_event_id="evt-123",
    source_type="user_message",      # user_message | webpage | pdf | email | llm_reply | tool_observation
    trust_level="trusted",           # trusted | semi_trusted | untrusted | untrusted_derived
    relation="supports",             # supports | contradicts | confirms | corrects | derived_from
    content_span="以后叫我B",
)
```

### 9.2 信任准入规则

| 来源 | 可写类型 | 信任级别 |
|------|---------|---------|
| user_message (当前用户原话) | 全部 | trusted |
| tool_observation | knowledge, project, environment | semi_trusted |
| webpage / pdf / email | knowledge (candidate only) | untrusted |
| llm_reply (assistant 生成) | 不可创建 user_profile/policy/agent_self | untrusted_derived |

### 9.3 Gate 自动拒绝的场景

- 外部网页内容试图写 user_profile → **reject**
- assistant 的生成文本试图强化用户画像 → **reject**
- 全外部 evidence 写 policy → **reject**

---

## 十、废弃清单

| 废弃项 | 替代 |
|--------|------|
| `MemoryGate.decide_str()` | `MemoryGate.decide(MemoryProposal)` |
| `MemoryGateInput` dataclass | `MemoryProposal` Pydantic model |
| `StewardSignalExtractor` 独立 LLM 调用 | `UnifiedMemoryExtractor` 单次提取 |
| `MemoryMaintenance.forget/sleep/archive()` 直接操作 DB | `MemoryWriteService.forget/execute_maintenance()` |
| `AgentGraph._resolve_memories_for_context()` 全量扫描 | `MemoryReadModel.build_context()` 精确 key + type |
| `AgentGraph._get_runtime_identity()` 全量扫描 | `MemoryReadModel.resolve_identity()` |
| `agent.runtime_id` 作为 memory key | 属于 runtime config |
| `steward_extraction` outbox job | 合并为 `memory_extraction` |
| `PERSONAL_SIGNAL_TYPES` | 替换为 canonical type 集合 |
| `_SKIP_PATTERNS` / `_EXPLICIT_MEMORY_MARKERS` 关键词表 | `classify_memory_signal()` 模型分类 |
| `ConflictResolver._is_near_duplicate()` 词重叠率 | 仅 content_hash 精确匹配 |
| `ConflictResolver._find_contradictions()` 否定词匹配 | 移除, 矛盾检测留待模型层 |
| `ConflictResolver._is_major_change()` 长度比/前缀比较 | 内容不同 → supersede |

---

## 十一、已知遗留问题

| 问题 | 严重性 | 说明 |
|------|--------|------|
| `process_all()` 在 `_finalize()` 中同步执行 ASYNC job | 中 | ASYNC 退化为同步，增加聊天响应延迟。应改为仅 enqueue，由独立 worker 消费 |
| `classify_memory_signal()` 每次聊天额外调用一次便宜 LLM | 中 | 每次 ~100ms。未来可缓存最近 N 轮结果，或与主 LLM 合并输出 |
| EXTRACT_SYNC 路径复用 `self._llm_client` | 低 | 同一实例，需确认并发安全 |
| Qdrant/KG/向量投影为 stub | 低 | Outbox handler 接口已预留 |
| Thread pending memory overlay 未实现 | 低 | 连续对话短暂失忆的解决方案已设计 |
| `LLMClient` 无 public `base_url`/`api_key` 属性 | 低 | 导致 EXTRACT_SYNC 路径复用实例而非创建独立实例 |
