# Memory System Final Architecture Refactor - Implementation Plan

## 零、三套独立状态定义

### lifecycle_state (MemoryRecord 生命周期)
candidate → active → sleeping → archived → forgotten
(candidate 不可直接变 sleeping)

### validity_state (数据效力)
valid / superseded / contradicted / expired

### lineage_operation (谱系操作类型)
create / reinforce / revise / supersede / merge / promote / sleep / wake / archive / forget
(reject/ignore/discard 属于 proposal 决策，不是 MemoryRecord 生命周期)

---

## 一、数据库完整 Schema

### memory_records 完整列
```
id VARCHAR(36) PK
canonical_key VARCHAR(256)        -- 规范化后的 memory_key
cardinality VARCHAR(16)           -- single / multi
memory_type VARCHAR(64)           -- canonical enum
scope_type VARCHAR(32) NOT NULL DEFAULT 'global'
scope_id VARCHAR(128)             -- global 时必须为 NULL
content TEXT
structured_value JSONB
lifecycle_state VARCHAR(32)       -- candidate/active/sleeping/archived/forgotten
validity_state VARCHAR(32)        -- valid/superseded/contradicted/expired
trust_level VARCHAR(32)           -- trusted/semi_trusted/untrusted/untrusted_derived
stability VARCHAR(32)             -- stable/contextual/volatile
stability_score FLOAT             -- 连续评分（可选）
confidence FLOAT
importance FLOAT
record_version INT DEFAULT 1
reinforce_count INT DEFAULT 0
source_event_id VARCHAR(36)
lineage VARCHAR(128)              -- 快捷字段
created_from VARCHAR(36)          -- proposal_id
revision_of VARCHAR(36)
supersedes VARCHAR(36)
superseded_by VARCHAR(36)
merged_from JSONB                 -- 快捷字段
valid_from TIMESTAMPTZ
valid_to TIMESTAMPTZ
observed_at TIMESTAMPTZ
last_reinforced_at TIMESTAMPTZ
pinned BOOLEAN DEFAULT FALSE
created_at TIMESTAMPTZ
updated_at TIMESTAMPTZ

-- Partial unique index: 仅 single cardinality + active 才唯一
CREATE UNIQUE INDEX ix_memory_records_single_active
  ON memory_records (canonical_key, scope_type, COALESCE(scope_id, ''))
  WHERE lifecycle_state = 'active' AND cardinality = 'single';
```

### memory_lineage 表
```
id VARCHAR(36) PK
predecessor_id VARCHAR(36) FK → memory_records(id)
successor_id VARCHAR(36) FK → memory_records(id)
operation VARCHAR(32)             -- revise/supersede/merge/derived_from
reason TEXT
proposal_id VARCHAR(36)
created_at TIMESTAMPTZ
```

### memory_evidence 表
```
id VARCHAR(36) PK
memory_id VARCHAR(36) FK → memory_records(id) ON DELETE CASCADE
source_event_id VARCHAR(36)
source_type VARCHAR(32)           -- user_message/tool_observation/webpage/pdf/email/code_comment/llm_reply
trust_level VARCHAR(32)           -- trusted/semi_trusted/untrusted/untrusted_derived
relation VARCHAR(32)              -- supports/contradicts/confirms/corrects/derived_from
content_span TEXT
created_at TIMESTAMPTZ
```

### memory_proposals 表
```
id VARCHAR(36) PK
proposal_id VARCHAR(128) UNIQUE
source_event_ids JSONB
memory_type VARCHAR(64)
canonical_key VARCHAR(256)
scope_type VARCHAR(32)
scope_id VARCHAR(128)
content TEXT
structured_value JSONB
evidence JSONB
trust_level VARCHAR(32)
confidence FLOAT
importance FLOAT
stability VARCHAR(32)
stability_score FLOAT
proposed_operation VARCHAR(32)
gate_decision VARCHAR(32)         -- reject/candidate/active
gate_reason TEXT
blocked_reason VARCHAR(64)
final_operation VARCHAR(32)       -- create/reinforce/revise/supersede/merge/ignore
final_memory_id VARCHAR(36)
requires_confirmation BOOL
extractor_name VARCHAR(64)
extractor_version VARCHAR(32)
idempotency_key VARCHAR(128) UNIQUE
raw_payload JSONB
normalized_payload JSONB
created_at TIMESTAMPTZ
```

---

## 二、类型映射表（旧 → 新）

| 旧 type | 旧 key | 新 type | 新 canonical_key | card | scope |
|---|---|---|---|---|---|
| user_profile | 无或 user.name | user_profile | user.name | single | global |
| user_profile | user.display_name | user_profile | user.display_name | single | global |
| name | 任意 | user_profile | user.name 或 user.display_name | single | global |
| preference | user.preference.* | user_profile | user.preference.<topic> | 由 key spec | global |
| routine | 任意 | user_profile | user.routine.<desc> | multi | global |
| habit | 任意 | user_profile | user.habit.<desc> | multi | global |
| schedule | 任意 | user_profile | user.schedule.<desc> | multi | global |
| project | project.* | project | project.<name>.<topic> | multi | project |
| agent_self | agent.* | agent_self | agent.* | 由 key spec | global |
| policy | 任意 | policy | policy.<topic> | multi | global/workspace |
| environment | 任意 | environment | environment.<topic> | multi | environment |
| fact | 任意 | knowledge/episodic | 自动推导 | multi | global/thread |
| 无法判定 | 无法判定 | - | - | - | legacy_unresolved |

### routine/habit/schedule 修正
全部映射为 user_profile 类型，key 使用 user.routine.* / user.habit.* / user.schedule.*
procedural 仅用于工作流、执行方法和经验教训

---

## 三、MemoryKeyRegistry

### 固定 canonical key（exact match，优先级高于 pattern）
```
user.display_name → user_profile, single, global, runtime_identity
user.name → user_profile, single, global, runtime_identity
user.preference.response_style → user_profile, single, global, runtime_identity
agent.display_name → agent_self, single, global, runtime_identity
agent.persona.tone → agent_self, single, global, runtime_identity
agent.persona.relationship → agent_self, single, global, runtime_identity
```

### 动态 pattern（longest-match priority）
```
user.preference.* → user_profile, multi (默认), global
user.routine.* → user_profile, multi, global
user.habit.* → user_profile, multi, global
user.schedule.* → user_profile, multi, global
project.* → project, multi, project
policy.* → policy, multi, global/workspace
environment.* → environment, multi, environment
knowledge.* → knowledge, multi, global/thread
```

**规则**：cardinality 最终由 key spec 决定，不由 memory_type 决定。exact key 优先于 pattern，pattern 使用 longest-match。

---

## 四、Scope 定义

| scope_type | scope_id | 说明 |
|---|---|---|
| global | NULL（必须为空） | 全局范围 |
| project | 项目名 | 特定项目 |
| thread | thread_id | 特定会话线程 |
| workspace | 工作空间标识 | 特定工作空间 |
| capability | capability_id | 特定能力 |
| environment | 环境标识 | 特定环境配置 |

约束：global 的 scope_id 必须为 NULL；其他 scope 的 scope_id 必须非空。

---

## 五、Operation 判定规则

| 条件 | Operation | 行为 |
|---|---|---|
| 无同 canonical_key + scope 的 active | create | 新建 MemoryRecord |
| 同 key active + 同 content_hash 或语义相同 | reinforce | 不新建，增加 confidence + reinforce_count + evidence |
| 同 key (single) + 内容不同 | supersede | 新建 record (active) + 旧 → superseded + lineage |
| 同 key + 内容部分更新 | revise | 新建 record + 旧 → superseded + lineage + revision_of |
| 多 proposal 合并为一个 | merge | 新建 record + 旧 → merged + lineage |
| 同 key (multi) + structured_value/content_hash 相同 | ignore | 重复值，不写入 |
| 同 key (multi) + 语义近重复 | ignore | 去重 |
| 同 key (multi) + 矛盾 | 可能 supersede 或创建为 valid + 标记旧为 contradicted | - |
| 置信度过低 | ignore | - |

**multi 去重**：比较 normalized structured_value、content_hash、语义近重复。multi 允许多个不同值，不允许多个重复值。

---

## 六、并发策略

1. 写入前获取 pg_advisory_lock(canonical_key_hash + scope_hash)
2. 查同 key active 时 SELECT FOR UPDATE
3. Partial unique index 作为最后防线
4. record_version 用于 Outbox projection 乱序检测

---

## 七、强一致 / 最终一致边界

### 强一致（同步写入，同 PostgreSQL 事务）
- 显式 remember_or_update 工具调用
- 用户纠正 (forget/supersede)
- 关键 policy 创建
- 明确的稳定偏好/身份变更
- 事务内：memory_record + evidence + lineage + proposal + event + outbox → COMMIT
- **不要求返回前 KG/Qdrant/Markdown 已完成**（这些是异步 projection）

### 最终一致（异步 + Thread Pending Overlay）
- 普通自动提取（聊天后）
- 仅 enqueue extraction outbox job
- **连续对话短暂失忆解决方案**：
  - Thread-level pending memory overlay：当前 thread 的 extraction job 产生的 proposal
    在 outbox 完成前，暂存在 thread_state.pending_memories
  - 下次同一 thread 对话时，pending_memories 作为临时 overlay 注入 context
  - Outbox 完成后清理 pending_memories，转为正式 memory_records

### Outbox Job 分类
**extraction job**（interaction → proposal）：
- job_type: "memory_extraction"
- 幂等键: `{thread_id}:{turn_index}:extraction`
- 失败: 重试 3 次 → deadletter

**projection job**（memory commit → KG/vector/Markdown/cache）：
- job_type: "memory_vector_upsert" / "memory_kg_project" / "memory_markdown_project" / "memory_cache_invalidate"
- 幂等键: `{memory_id}:{record_version}:{job_type}`
- 携带 record_version，旧版本任务晚到时比对跳过
- 失败: 重试 3 次 → deadletter（不回溯 memory_records）

---

## 八、Forget Saga

覆盖层：
1. memory_records: content → tombstone, structured_value → NULL, lifecycle → forgotten
2. memory_evidence: CASCADE DELETE
3. memory_lineage: 保留（不可逆审计），标记 operation = "forget_cleanup"
4. events: 不删 event，payload 中涉及 memory 内容的部分脱敏
5. outbox: 创建 memory.forgotten event → 触发 projection 层清理
6. KG relations: 异步删除
7. vector index: 异步删除
8. Markdown Projection: 异步重建（不含该 memory）
9. context cache: 异步失效
10. object evidence: 异步删除引用

部分失败 → partially_completed 状态 + Outbox 重试

---

## 九、MemoryStore 定位

MemoryStore 是内部 repository，仅供 MemoryWriteService 调用。
禁止其他业务模块直接执行生产写入。WriteService 不散落 SQL，
所有 DB 操作通过 MemoryStore 的方法完成。

---

## 十、职责拆分

```
ProposalNormalizer
├── 标准化 memory_type (legacy → canonical mapping)
├── 解析 memory_key → canonical_key
├── 推断 scope
├── 校验 evidence 逐条
├── 计算 idempotency_key
└── 输出 normalized MemoryProposal

MemoryGate (不依赖 execution_mode)
├── 信任准入检查 (trust boundary)
├── 来源校验
├── 意图校验
└── 输出: reject / candidate / active

ConflictResolver
├── SELECT FOR UPDATE 同 scope+key active records
├── cardinality 检查
├── content_hash / structured_value 比较
├── 语义近重复检测
└── 输出: create / reinforce / revise / supersede / merge / ignore

MemoryWriteService (事务边界)
├── BEGIN
├── 获取 advisory lock
├── 调用 ConflictResolver
├── 写入 memory_records (via MemoryStore)
├── 写入 memory_evidence
├── 写入 memory_lineage
├── 写入 memory_proposals (持久化)
├── 写入 Event (via EventLogger)
├── 写入 OutboxJob (extraction completion → projection jobs)
├── 释放 advisory lock
└── COMMIT
```

---

## 十一、测试覆盖（追加）

1. 普通 chat 明确长期偏好 → active (sync fast-path)
2. 寒暄不写记忆 (extraction policy skip)
3. 同 source event 重复消费不重复写 (idempotency_key)
4. 显式工具和自动提取写入行为一致
5. single cardinality key 不产生两个 active
6. multi cardinality key 保留多个不同事实/值，不重复
7. revise/supersede lineage 双向正确
8. candidate 因新增证据晋升
9. 过期 candidate 不变 sleeping → archived
10. 外部网页不能写入 user_profile/policy
11. assistant reply 不能单独强化用户画像
12. 非 canonical type 被拒绝
13. Runtime Identity 不全量读取
14. 同事务写 event 和 outbox
15. Outbox 乱序：旧 record_version 跳过
16. Forget 部分失败可恢复
17. 旧 type 数据 migration 后可读取
18. 两个并发同 key proposal 串行化 (advisory lock)
19. pending-memory overlay 连续对话可用
20. exact key 优先级高于 pattern
21. multi 同值去重
22. scope DB constraint (global→NULL, 其他→非NULL)
23. maintenance 不可创建 user_profile/policy
24. 事务失败回滚 (不残留在 memory_records)
25. migration unresolved 标记
