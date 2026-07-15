# AIive 长期连续个人代理：有界上下文、长期记忆、选择性遗忘与冷历史架构

> 版本：实施基线 v1  
> 适用范围：单用户、本地优先、长期持续运行的个人 Agent  
> 核心目标：用户始终面对同一个连续代理，但模型上下文、活跃记忆、热索引和后台维护成本长期保持有界。

---

## 0. 最终结论

AIive 不应依赖“创建新对话”控制上下文，也不能把一条逻辑会话实现成无限增长的消息数组。

最终采用：

```text
一个永久 ConversationStream
+ 自动滚动的 Epoch / Segment
+ 固定预算的 Working Context
+ 完整但隔离的 Raw Event Archive
+ 独立的长期记忆真相源
+ 分层检索
+ 空闲压缩
+ 每日增量 Dream 整理
+ 类型相关的衰减、沉睡、归档和遗忘
```

在正常运行中：

- 每轮模型输入规模不随总历史长度增长；
- 默认召回只访问有界的活跃记忆和近期摘要；
- 完整原始历史可以永久保留，但不参与每轮上下文和全局竞争；
- 长期记忆不能只增不减；
- 长期不用、价值低、可恢复的内容会逐级退出活跃范围；
- 用户明确固定的记忆不会被自动删除；
- 所有后台任务必须增量、幂等、有预算、有水位线，禁止每日全库扫描。

---

# 一、产品视角

## 1.1 用户看到什么

用户只看到一个长期存在的个人代理：

- 不需要手动创建新对话；
- 可以继续几天、几个月前的任务；
- 可以搜索原始历史；
- 可以查看、修改、固定和删除长期记忆；
- 长时间不用后再次回来，代理仍能恢复当前任务；
- 日常响应不会因为历史积累而越来越慢。

用户不需要理解 Epoch、Segment、Checkpoint、Dream 或索引分层。

## 1.2 用户不能感受到什么

内部维护不能表现为：

- 突然失忆；
- 历史越多，正常回复越慢；
- 每次对话都额外等待一次压缩和一次记忆模型；
- 临时要求被长期保存；
- 旧项目的内容串到新项目；
- 多年前的旧偏好持续覆盖当前明确要求；
- 系统静默删除用户固定的重要记忆；
- 系统声称“记得”，但用户无法查看来源和状态。

---

# 二、非目标与硬性禁令

## 2.1 非目标

本架构不尝试实现：

- 真正无限的模型上下文；
- 每轮读取全部历史；
- 每日让模型重新理解用户的一生；
- 用一个额外 LLM 监督主 Agent 是否调用工具；
- 用关键词、正则或固定短语硬编码自然语言意图；
- 通过扩写系统提示词解决架构正确性；
- 让所有原始历史永久处于热索引。

## 2.2 硬性禁令

禁止：

1. 一个永远 append 的 message list；
2. 每轮加载全部长期记忆；
3. 每轮保存完整 Prompt 副本且永久保留；
4. 每条 RawEvent 永久生成向量；
5. Daily Dream 全库扫描；
6. 摘要无限套摘要；
7. Candidate Memory 永久存在；
8. 所有 MemoryRecord 永久 active；
9. 只归档、不清理、不删除；
10. 后台维护模型直接修改真相源；
11. 派生索引成为唯一数据源；
12. 忘记操作只删除向量、不删除其他投影；
13. 第二个 LLM 作为通用 Tool Supervisor；
14. 为测试用例增加关键词分支或特殊逻辑。

---

# 三、不可破坏的系统不变量

## 3.1 上下文不变量

```text
每轮输入 token <= configured_context_budget
```

该预算由以下独立上限组成：

- Stable Contract；
- Core Memory；
- WorkingState；
- Recent Messages；
- Retrieved Memory；
- Tool Results；
- Reserved Output；
- Safety Margin。

任何分区不能无限借用其他分区的剩余空间。

## 3.2 记忆不变量

每条长期记忆必须具有：

```text
memory_id
canonical_key
scope
value
status
retention_policy
valid_from / valid_to
source_event_ids
evidence_type
confidence
last_confirmed_at
last_used_at
version
```

没有原始证据或合法推断链的内容，不得成为 active memory。

## 3.3 维护不变量

所有后台维护任务必须：

- 使用 watermark 或明确事件范围；
- 幂等；
- 支持重试；
- 有最大输入量；
- 有最大输出量；
- 有最大运行时间；
- 不覆盖尚未完成的结果；
- 失败时不破坏当前有效状态。

## 3.4 派生层不变量

以下全部是可重建投影：

- Qdrant/向量索引；
- FTS；
- Temporal KG；
- Core Memory Projection；
- Markdown Projection；
- Segment Summary Index；
- Epoch Timeline Index。

长期记忆真相源和 RawEvent 真相源不能依赖这些投影才能恢复。

---

# 四、核心对象

## 4.1 ConversationStream

代表用户与 AIive 的永久逻辑会话。

```text
conversation_stream_id
user_id
current_epoch_id
created_at
last_activity_at
```

一个用户可以只有一个默认 Stream，也可以为特殊隔离场景创建多个，但用户不需要通过“新建对话”管理上下文容量。

## 4.2 Epoch

后端内部的受控运行阶段。

```text
epoch_id
conversation_stream_id
status: active | sealing | sealed | archived
start_event_id
end_event_id
checkpoint_id
created_at
sealed_at
```

Epoch 会自动轮换，但不改变用户看到的连续聊天体验。

## 4.3 Segment

Epoch 内一个可独立密封的完整工作片段，例如：

- 一次代码问题排查；
- 一组连续工具调用；
- 一个论文讨论阶段；
- 一次提醒创建和确认；
- 一段普通闲聊。

```text
segment_id
epoch_id
start_event_id
end_event_id
source_hash
status: open | sealing | sealed | failed
created_at
sealed_at
```

Segment 不得切断尚未完成的工具链、审批、文件写入或模型流。

## 4.4 RawEvent

RawEvent 是历史证据真相源。

事件类型包括：

- user_message；
- assistant_message；
- tool_call；
- tool_result；
- operation_state；
- memory_proposal；
- memory_write；
- user_confirmation；
- artifact_reference；
- checkpoint_created；
- segment_sealed。

大体积正文写入对象存储或压缩文件，RawEvent 保存引用、摘要、hash 和元数据。

## 4.5 WorkingState

WorkingState 只保存当前仍有操作价值的信息：

```text
current_goal
active_subtasks
open_loops
active_constraints
current_project
active_entities
referenced_artifacts
latest_verified_tool_states
pending_approvals
```

WorkingState 必须有字段数量、单字段长度和总 token 上限。

已完成步骤不得永远留在 WorkingState，只进入 Checkpoint 或历史。

## 4.6 SegmentSummary

用于定位历史，不是长期记忆。

```text
segment_id
goal
outcome
decisions
open_loops
entities
artifacts
important_tool_results
source_event_range
source_hash
summary_version
model_id
```

## 4.7 EpochCheckpoint

用于恢复当前连续工作：

```text
epoch_id
current_goal
completed_milestones
open_loops
active_constraints
current_decisions
referenced_artifacts
relevant_entities
latest_verified_tool_states
source_segment_ids
version
```

同一 Epoch 只保留一个当前有效 Checkpoint，少量旧版本用于回滚。

## 4.8 MemoryRecord

MemoryRecord 是跨 Segment/Epoch 复用的长期事实。

建议状态：

```text
candidate
active
dormant
archived
expired
forgotten
```

保留策略：

```text
normal
pinned
ephemeral
legal_hold
```

个人版本主要使用 normal、pinned、ephemeral。

---

# 五、存储分层

## 5.1 热层

包含：

- 当前 Epoch；
- WorkingState；
- 最近消息；
- Core Memory；
- active MemoryRecord；
- 当前任务索引；
- 最近 SegmentSummary。

热层必须设置严格容量上限。

## 5.2 温层

包含：

- 已密封 Epoch；
- SegmentSummary；
- dormant MemoryRecord；
- 中期 RawEvent；
- 常用历史索引。

温层默认不进入每轮上下文。

## 5.3 冷层

包含：

- 全部或按策略保留的原始历史；
- archived MemoryRecord；
- 大体积工具日志；
- 文件和网页快照；
- 旧 Epoch；
- 低频审计数据。

冷层可以持续增长，但不能参与默认自动召回。

## 5.4 可重建层

包含：

- embedding；
- FTS projection；
- KG projection；
- 缓存；
- ContextSnapshot；
- rerank cache；
- Markdown projection。

可重建层必须随源数据删除或版本失效同步清理。

---

# 六、每轮在线执行流程

```text
收到用户消息
↓
写入 RawEvent
↓
更新 last_activity_at
↓
并发启动 Memory Ingestion Job
↓
Context Assembler 构建固定预算上下文
↓
主 Agent 原生工具循环
↓
所有工具结果写入 RawEvent / Operation
↓
完成 assistant turn
↓
更新 WorkingState
↓
写入最终 RawEvent
↓
判断是否需要同步压缩或创建后台 sealing job
```

## 6.1 Context Assembler

输入：

- Stable Contract；
- Core Memory；
- 当前 WorkingState；
- 当前 EpochCheckpoint；
- 最近必要消息；
- 当前请求相关 active memory；
- 当前任务所需工具结果。

禁止输入：

- 全部 active memory；
- 全部 SegmentSummary；
- 全部 Epoch；
- 全部 RawEvent；
- 无关项目历史；
- expired/forgotten memory；
- 大体积工具原文。

## 6.2 固定预算

必须配置：

```text
max_contract_tokens
max_core_memory_tokens
max_working_state_tokens
max_recent_message_tokens
max_retrieved_memory_tokens
max_tool_result_tokens
reserved_output_tokens
safety_margin_tokens
```

当某层超限时，执行该层自己的裁剪策略，不能让整个 Prompt 无限制增长。

---

# 七、Segment 密封与上下文压缩

## 7.1 触发方式

### 容量软阈值

接近上下文预算时，后台准备压缩，不阻塞当前回复。

### 容量硬阈值

下一次模型调用可能超限时，必须先完成压缩。

### 用户空闲

满足全部条件时触发：

- 无模型流；
- 无运行中工具；
- 无 pending approval；
- 无未提交副作用；
- 距离最后活动超过可配置 idle_window；
- 存在足够未密封事件、可完成 Segment 或已达到软阈值。

不得因为用户发一句话后短暂离开就无条件调用压缩模型。

### 任务边界

大型任务阶段完成后可以主动密封。

## 7.2 密封过程

```text
锁定 source_event_range
↓
计算 source_hash
↓
创建 immutable Segment
↓
并行：
  Context Compaction
  Memory Extraction
  Search Indexing
↓
生成或更新 EpochCheckpoint
↓
必要时密封 Epoch 并开启新 Epoch
```

## 7.3 压缩输出

Compactor 必须保留：

- 当前目标；
- 已完成里程碑；
- 未完成事项；
- 当前约束；
- 用户最近修正；
- 关键实体；
- 文件和 artifact 引用；
- 真实工具执行状态。

不能把 pending 写成 completed，不能把模型推测写成已验证事实。

## 7.4 防止摘要漂移

禁止：

```text
summary_n = summarize(summary_n-1)
```

SegmentSummary 必须基于原始 Segment 事件。

EpochCheckpoint 可以基于多个 SegmentSummary，但必须保存 source_segment_ids 和 source hashes。

定期 Rebase 时从一级 Summary 或仍保留的 RawEvent 重建高层状态。

---

# 八、长期记忆提取

## 8.1 不依赖主 Agent 主动调用记忆工具

主 Agent 可以主动调用显式记忆能力，但长期记忆系统不能把“主 Agent 是否想起工具”作为唯一入口。

每个完整用户 turn 都创建 Memory Ingestion Job：

```text
source_turn_id
source_event_ids
user_id
project_scope
status
attempt
watermark
```

该任务是记忆子系统，不是第二个 LLM Tool Supervisor：

- 不判断其他工具；
- 不修改主 Agent 决策；
- 不参与普通最终回复；
- 只从已完成的原始事件提取 MemoryProposal。

## 8.2 并发提取

Memory Ingestion 可以在主 Agent 处理当前 turn 时并发运行。

输出：

```text
MemoryProposal[]
```

建议字段：

```text
canonical_key
value
scope
evidence_source
persistence_requirement
memory_type
confidence
retention_hint
source_event_ids
```

## 8.3 显式与隐式

### 显式长期要求

如果提取器确认用户明确要求持久化：

```text
persistence_requirement = user_required
```

最终响应完成前，应等待正式 Memory Write Pipeline 的提交结果，或明确报告未完成。

### 隐式长期事实

例如稳定偏好、项目决策、工作方式：

```text
persistence_requirement = system_best_effort
```

异步写入，不阻塞普通回复，不在当前轮声称已完成。

## 8.4 写入流程

```text
MemoryProposal
→ Normalizer
→ Scope Validator
→ Value Gate
→ Duplicate Resolver
→ Conflict Resolver
→ MemoryWriteService
→ Event / Outbox
→ Projection
```

禁止 `_finalize()` 直接绕过正式入口写 MemoryRecord。

## 8.5 去重

需要区分：

- 同一 source_turn 的重复处理；
- 不同时间重复表达同一事实。

同一 source_turn + 同一 canonical fact：

```text
already_handled
```

不同 source_turn 的同一事实：

```text
independent evidence / reinforce
```

---

# 九、记忆生命周期与选择性遗忘

## 9.1 状态转换

```text
candidate
→ active
→ dormant
→ archived
→ expired
→ forgotten
```

允许：

- 新证据使 dormant/archived 恢复 active；
- 新事实 supersede 旧事实；
- 用户主动固定或取消固定；
- 用户主动删除直接进入 Forget Saga。

## 9.2 不是单一遗忘公式

禁止仅按“30 天未使用”直接删除。

维护判断至少考虑：

- memory_type；
- retention_policy；
- 最近使用时间；
- 最近确认时间；
- 独立证据数量；
- 被成功使用次数；
- 是否可从 RawEvent 恢复；
- 是否已被 supersede；
- 是否属于结束项目；
- 当前存储成本；
- 用户显式重要性；
- 是否存在冲突。

时间衰减只用于召回权重和维护候选排序，不直接作为物理删除命令。

## 9.3 类型相关策略

### Identity

- 自动遗忘慢；
- 明确修订时 supersede；
- 可 pinned。

### Preference

- 长期不用可 dormant；
- 再次表达或成功使用可恢复；
- 当前用户要求始终优先于历史偏好。

### Project Decision

- 项目活跃时 active；
- 项目结束后 archived；
- 项目恢复时按需唤醒；
- 不得串到其他项目。

### Episodic

- 衰减较快；
- 默认保留摘要和证据引用；
- 重要结果或反复引用的 episode 可以长期保存。

### Procedural / Agent Lesson

- 必须有成功或失败证据；
- 被后续测试推翻时 expired；
- 长期无应用可 dormant。

### Environment

- 默认有 TTL；
- 过期后需要重新观察；
- 不能永久当作当前事实。

### Knowledge

- 优先保存来源引用和摘要；
- 不把整份文档复制成个人记忆；
- 版本变化后旧知识过期。

## 9.4 Pinned

用户可以说：

```text
这条不要自动忘记
```

Pinned 的含义：

- 不自动进入 forgotten；
- 不参与普通容量淘汰；
- Dream 不能自动删除；
- 仍然可以因新事实变为 superseded/invalid；
- 用户可以主动取消固定或删除。

Pinned 超过配额时必须提示用户，不得静默删除。

---

# 十、每日 Dream Consolidation

## 10.1 触发

约每 24 小时调度一次，但先检查是否有变化。

无变化时：

```text
skipped_no_changes
```

不调用模型。

## 10.2 输入范围

只处理：

- last_dream_watermark 后新增/修改的 MemoryRecord；
- 最近 sealed Segment；
- dirty memory set；
- candidate queue；
- conflict queue；
- stale queue；
- 已结束项目相关记录。

禁止读取全库。

## 10.3 输入和输出预算

必须配置：

```text
max_input_memories
max_input_segments
max_source_events
max_output_proposals
max_model_tokens
max_runtime_seconds
```

## 10.4 Dream 只能生成维护建议

输出：

```text
MemoryMaintenanceProposal[]
```

允许类型：

- merge；
- revise；
- supersede；
- candidate_promote；
- candidate_reject；
- active_to_dormant；
- dormant_to_archived；
- expire；
- deletion_candidate；
- inferred_pattern_candidate。

Dream 不得直接修改 MemoryRecord。

所有建议必须经过正式 Memory Write Pipeline。

## 10.5 Dream 必须既整理又减少

每次报告：

```text
added
merged
revised
demoted
archived
expired
deleted
net_growth
```

如果连续多次：

```text
added >> merged + archived + deleted
```

则产生增长失控告警。

## 10.6 模式发现

跨 Segment 模式默认进入 candidate：

- 必须带 supporting evidence；
- 标记 inferred；
- 不能伪装成用户明确陈述；
- 达到多次独立证据后才可晋升 active。

---

# 十一、候选、归档和物理删除

## 11.1 Candidate TTL

Candidate 必须有：

```text
candidate_expires_at
```

到期前无新证据：

- archived；
- rejected；
- 或物理删除。

不能无限堆积。

## 11.2 Archived

Archived 不参与普通自动召回。

只有以下情况访问：

- 用户明确询问历史；
- Agent 调用深度历史搜索；
- Dream 对冲突或模式进行整理。

## 11.3 Expired

Expired 表示已知不再有效：

- 不参与当前决策；
- 可保留在历史时间线；
- 超过宽限期后进入删除候选。

## 11.4 Forget Saga

物理删除必须覆盖：

- memory_records；
- evidence links；
- candidate/proposal；
- vector index；
- FTS；
- Temporal KG；
- Core Memory Projection；
- Context cache；
- Markdown projection；
- object payload；
- derived summaries 中的直接暴露内容。

状态：

```text
planned
→ hidden_from_retrieval
→ projections_removed
→ payload_removed
→ completed
```

部分失败时进入 partial 并重试。

---

# 十二、冷历史永久保留但不拖慢 Agent

## 12.1 关键原则

冷存储增长不等于模型上下文增长。

永久保留 RawEvent 时，日常性能只在以下条件成立时稳定：

1. RawEvent 不进入默认 Prompt；
2. RawEvent 不参与每轮全局检索；
3. 不为所有 RawEvent 建永久向量；
4. 默认搜索 active memory 和近期摘要；
5. 原始历史采用分层钻取；
6. 查询有固定预算；
7. Dream 和维护只做增量。

## 12.2 三级检索

### 第一层：Active Memory Index

严格有界，仅包含当前有效长期记忆。

### 第二层：Epoch/Segment Summary Index

用于定位相关历史阶段。

### 第三层：Raw Event Archive

只在选中 Epoch/Segment 后，在小范围内检索原文。

流程：

```text
当前查询
→ active memory / current scope
→ summary index 找候选 epoch
→ segment index 找候选 segment
→ raw event drill-down
```

禁止：

```text
每轮直接在十年全部消息中做全局向量 top_k
```

## 12.3 原始历史索引策略

默认：

- 用户消息：时间/用户/project/epoch/FTS；
- Agent 关键回复：时间/实体/FTS；
- 大日志：摘要、错误码、文件名、时间索引；
- 网页/PDF：artifact 引用和摘要；
- 不默认生成每条事件 embedding。

## 12.4 查询预算

必须限制：

```text
max_candidate_epochs
max_candidate_segments
max_raw_events_scanned
max_vector_candidates
max_rerank_candidates
max_retrieval_tokens
max_retrieval_latency
```

普通自动召回不得超预算。

深度历史检索可以较慢，但必须由用户明确请求或 Agent 显式调用。

---

# 十三、热、温、冷数据保留策略

| 数据 | 默认策略 |
|---|---|
| Stable Contract | 版本化，保留少量版本 |
| Core Memory | 固定 token 上限 |
| WorkingState | 固定字段与 token 上限 |
| Recent Messages | 固定 token 上限 |
| Active Memory | 类型配额 + 生命周期维护 |
| Candidate Memory | TTL |
| Dormant Memory | 温层，不默认召回 |
| Archived Memory | 冷层，历史搜索 |
| Pinned Memory | 不自动删除 |
| User Raw Message | 用户可配置永久或期限保留 |
| Assistant 普通回复 | 长期保留后转冷层 |
| 大型 Tool Output | 短 TTL，保留摘要/hash/ref |
| Operation Audit | 长期保留，参数脱敏 |
| Debug Trace | 短 TTL |
| Full Context Snapshot | 失败/采样时保留，短 TTL |
| Embedding | 可重建，随源记录删除 |
| Dream 详细 payload | 有 TTL，保留审计摘要 |
| Segment Summary | 近期保留，旧内容可合并/淘汰 |
| Checkpoint 历史版本 | 当前 + 少量可回滚版本 |

永久保留全部 RawEvent 时，冷存储总量仍会增长，这是用户选择的物理代价；但热数据、索引和上下文必须保持有界。

---

# 十四、调度和任务系统

## 14.1 任务类型

```text
memory_ingestion
segment_sealing
context_compaction
segment_indexing
daily_dream
weekly_housekeeping
monthly_rebase
forget_saga
projection_rebuild
```

## 14.2 调度原则

- 每个任务使用 idempotency key；
- 同一 Segment 同一版本只压缩一次；
- 同一用户同一时间最多一个 Dream；
- Dream 只读取 sealed segment 和 committed memory；
- 用户回来时不读取半成品 Checkpoint；
- 任务失败不删除原始证据；
- 超过重试上限进入 dead letter。

## 14.3 空闲调度

`idle_window` 配置化。

空闲检测只创建 job，不直接在请求线程执行大型压缩。

## 14.4 24 小时调度

Daily Dream 使用水位线：

```text
last_successful_dream_watermark
```

失败后从上次成功水位继续，不能推进水位。

---

# 十五、一致性和事务

## 15.1 RawEvent

用户消息、Agent 回复和关键工具状态先持久化，再触发派生任务。

## 15.2 Transactional Outbox

同一业务事务中写入：

- 真相源变化；
- Domain Event；
- Outbox message。

Worker 负责投影、索引、异步提取和维护。

## 15.3 Memory Write

MemoryRecord、版本关系、Evidence 和 Event 必须在明确事务边界中提交。

## 15.4 Projection

投影失败不回滚真相源，通过 Outbox 重试。

## 15.5 Checkpoint 替换

新 Checkpoint 完整生成并验证后，以原子指针切换为 current。

不得让请求读取正在写入的半成品。

---

# 十六、可观察性

至少记录：

## 上下文

- 每轮总 token；
- 各分区 token；
- 压缩次数；
- 硬阈值阻塞次数；
- WorkingState 大小。

## 记忆

- active/candidate/dormant/archived 数量；
- 每日净增长；
- 重复率；
- 冲突率；
- 临时信息误写率；
- stale memory 使用率；
- pinned 大小。

## 检索

- 默认召回延迟；
- 深度检索延迟；
- candidate epoch 数；
- raw events scanned；
- retrieval token；
- 无关召回率；
- expired memory 误召回次数。

## 后台维护

- Dream 输入规模；
- Dream 输出 proposal 数；
- merge/archive/delete 数；
- net_growth；
- sealing backlog；
- dead letter；
- Forget Saga partial 数量。

## 存储

- 热/温/冷层大小；
- embedding 大小；
- 对象存储增长；
- 派生层与真相源大小比。

---

# 十七、硬性告警

以下情况必须告警：

- Context P99 随总历史持续增长；
- active memory 连续增长且几乎无降级；
- candidate 超过 TTL 未处理；
- Daily Dream 连续失败；
- Dream 连续净增长失控；
- archived/expired 仍进入普通召回；
- Forget Saga 长期 partial；
- 热索引超过配额；
- 派生索引大于源数据异常倍数；
- 单用户 pinned 数据超过配额；
- 普通检索开始扫描大量 RawEvent；
- 用户回来时频繁等待同步压缩。

---

# 十八、长期稳定性测试

## 18.1 模拟规模

至少模拟一年等效负载：

- 数千轮对话；
- 多个项目；
- 大量工具输出；
- 多次项目结束和恢复；
- 偏好修订；
- 重复事实；
- 长期不访问；
- pinned memory；
- 显式删除；
- 每日 Dream；
- 每周清理；
- 每月 Rebase。

## 18.2 上下文验收

```text
P99 输入 token 不随总历史长度增长
WorkingState 始终小于配置上限
Core Memory 始终小于配置上限
Retrieved Memory 始终小于配置上限
```

## 18.3 记忆验收

```text
active memory 在长期运行后趋于稳定区间
candidate 不无限增长
长期不用记忆能进入 dormant/archived
pinned 不被自动删除
superseded/expired 不参与当前决策
同一 source turn 不重复强化
```

## 18.4 检索验收

```text
普通检索延迟与冷历史总量弱相关
普通检索不扫描全量 RawEvent
跨项目不串线
旧失效事实不进入默认结果
深度搜索可以回到原始证据
```

## 18.5 存储验收

```text
热层有固定配额
温层有固定配额
冷层遵循用户保留策略
工具日志按 TTL 清理
派生索引可完整重建
```

---

# 十九、分阶段实施顺序

## Phase 0：真实代码审计与不变量确认

只分析，不改代码。

输出：

- 当前调用链；
- Session 和事务边界；
- 当前上下文组装；
- RawEvent 真相源；
- 当前记忆写入路径；
- 当前索引；
- 当前定时任务；
- 所有无限增长点；
- 迁移风险；
- 实施文件清单。

## Phase 1：有界上下文和内部 Epoch/Segment

实现：

- Context Budget；
- WorkingState 上限；
- Segment/Epoch 数据模型；
- soft/hard threshold；
- 密封状态机；
- 有界 Context Assembler。

此阶段先不改 Dream。

## Phase 2：统一 Memory Ingestion

实现：

- 每个完整 turn 的 Memory Ingestion Job；
- MemoryProposal；
- 去重、scope、冲突；
- 显式同步、隐式异步；
- 禁止绕过正式 Memory Write Pipeline。

## Phase 3：Idle Compaction

实现：

- last_activity_at；
- idle scheduler；
- segment sealing；
- compaction；
- checkpoint 原子替换；
- 用户返回时的竞态处理。

## Phase 4：记忆生命周期和 Daily Dream

实现：

- candidate/active/dormant/archived/expired/forgotten；
- retention_policy；
- 24 小时增量 Dream；
- dirty set/watermark；
- merge/revise/demote/archive；
- Candidate TTL；
- 增长告警。

## Phase 5：冷历史与分层检索

实现：

- 热/温/冷层；
- Summary Index；
- Raw Archive；
- two-stage retrieval；
- 固定检索预算；
- 不为所有 RawEvent 默认建向量。

## Phase 6：Forget Saga、清理和长期压测

实现：

- 完整 Forget Saga；
- weekly/monthly maintenance；
- 索引清理；
- 配额；
- 指标；
- 一年等效负载测试；
- 回滚与迁移验证。

---

# 二十、最终架构图

```text
Permanent ConversationStream
            │
            ▼
      Active Epoch
            │
   WorkingState + Recent Turns
            │
            ▼
      Main Agent Loop
            │
            ▼
       Raw Event Log
            │
      Transactional Outbox
            │
    ┌───────┼───────────┬──────────────┐
    ▼       ▼           ▼              ▼
Memory   Segment     Search         Operation
Ingest   Sealing     Indexing       Projections
    │       │           │
    │       ▼           │
    │   Compaction      │
    │       │           │
    │   Checkpoint      │
    │                   │
    ▼                   ▼
Memory Records     Summary Index
    │                   │
    ├── Active Index    ├── Epoch Index
    ├── Dormant Store   └── Raw Archive Drill-down
    └── Archive
            │
            ▼
   Daily Dream + Forgetting
            │
   merge / revise / decay /
   dormant / archive / expire /
   deletion candidate
```

---

# 二十一、作为个人用户是否接受

接受条件：

1. 不需要手动创建新对话；
2. 历史增加不导致日常回复持续变慢；
3. 当前任务不会在压缩后丢失；
4. 可以搜索原始历史；
5. 记忆可查看、修改、固定和删除；
6. 普通旧记忆会沉睡，不永久干扰；
7. 固定记忆不会被自动删除；
8. 失效事实不再作为当前事实；
9. 后台整理不频繁打扰；
10. 可以选择永久保存全部原始历史，并明确知道磁盘会增长；
11. 即使永久保存历史，默认上下文和检索仍保持有界；
12. Agent 不确定旧细节时会检索证据，而不是依赖多代摘要猜测。

不接受：

- 所有内容永久 active；
- 所有历史永久热索引；
- Daily Dream 全库扫描；
- 每轮全历史向量搜索；
- 只有新增，没有降级、归档和删除；
- 静默删除 pinned；
- 只删除数据库记录但残留在向量、KG 或缓存；
- 用第二个通用 LLM 监督主 Agent。

---

# 二十二、最终增长模型

```text
Model Context              O(1)
WorkingState               O(1)
Core Memory                O(1)
Default Retrieval Package  O(1)
Active Memory              bounded by policy/quota
Candidate Memory           bounded by TTL
Hot Index                  bounded by quota
Warm Index                 bounded by retention
Raw Cold Archive           grows only if user chooses retention
Daily Dream Cost           proportional to new/dirty data
Normal Retrieval Cost      independent of total cold history
```

这套架构的目标不是让所有数据绝对不增长，而是：

> 让所有参与日常推理、召回和维护竞争的数据保持有界；只有用户明确选择永久保留的冷证据允许持续增长，而且它不会自动进入模型上下文或每轮检索。
