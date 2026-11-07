# AIive Memory 系统专项架构审计

> **文档版本：** V1.1 源码复核修订版（2026-11-07；保留原文件名）\
> **补充日期：** 2026-11-07\
> **审计性质：** 源码复核 + 定向离线测试；第 22～24 章替换原执行计划\
> **审计基线：** `juzixiaoren/AIive`，`main` 分支\
> **基线 Commit：** `2d3233d2388019505131aa2ac17d79036151fb11`\
> **Commit 日期：** 2026-11-06\
> **对应项目阶段：** V62.6.1\
> **审计范围：** Memory
> 数据模型、提案规范化、准入、冲突解析、写入操作、生命周期、Evidence、Provenance、Lineage、读取、召回、投影、Graph、Forget、Identity
> Slot\
> **审计限制：** 本文依据该 Commit
> 的源码复核。原审计轮执行 86 项既有测试与 4 项临时 ORM 缺陷复现，当时仅修订文档；用户随后授权实施代码修复并集中测试。原审计证据与后续实施结果分列于第 24 章，当前工作区代码尚未提交。

------------------------------------------------------------------------

## 0. 执行摘要

### 0.1 核心结论

AIive 的 Memory 已经不是简单的 Key-Value
记忆存储，而是一套包含事实提案、事实记录、证据、来源追踪、生命周期、版本谱系、检索和派生投影的复杂系统。

目前系统的主要组成已经存在：

1.  `MemoryProposal`：记忆写入提案。
2.  `MemoryRecord`：正式记忆记录，是事实内容和生命周期的权威存储。
3.  `MemoryEvidence`：支持记忆的证据。
4.  `ContentProvenanceRef`：用于来源追踪和 Forget 传播的持久来源关系。
5.  `MemoryLineage`：记录记忆的演进关系。
6.  `MemoryKeyRegistry`：定义 Key 的类型、基数、作用域、冲突和维护策略。
7.  `MemoryGate`：准入和权威检查。
8.  `ConflictResolver`：比较新提案与已有记录，选择写入操作。
9.  `MemoryWriteService`：执行记忆写入及相关事务操作。
10. `MemoryLifecycleService`：执行维护型生命周期变化。
11. `ForgetVisibilityService`：统一判断内容是否因 Forget 而不可见。
12. Core Memory、Vector、Graph、Retrieval：从权威 Memory
    派生出的不同读取和检索表示。

**最高优先级问题是部分消费者与写入旧目标未接入已有保护，造成端到端覆盖缺口。先补入口与回归，再根据实际重复逻辑收敛接口。**

当前最需要解决的设计问题有：

-   普通读取入口并未全部强制执行 Forget 可见性检查。
-   写入冲突解析读取旧记录时，未统一过滤来源已被 Forget 屏蔽的记录。
-   `MemoryProposal`、`MemoryEvidence`、`ContentProvenanceRef` 对 Event
    和 Turn 的来源表达不完全一致。
-   `MemoryRecord` 的 Lifecycle、Validity、Forget Visibility、Lineage
    是不同维度，但部分调用方只检查其中一部分。
-   Graph 历史关系和 Sealing Bridge 等路径仍有绕过统一可见性判断的可能。
-   身份读取须过滤来源并稳定选择合法值；显式 Identity Slot 与占位符属于暂缓的产品设计。
-   Merge、Revise、Supersede 在不同执行入口中的语义并不完全一致。

### 0.1.1 本轮修订结论（优先阅读）

真实风险集中在**尚未接入现有保护的入口**，不需要先重建整套 Memory 架构。优先补齐 Runtime、Goal、API/Tool、Sealing/Graph 历史读取，以及写入旧目标可见性。现有 `ForgetVisibilityService`、`MemoryPolicyEngine`、Scope Resolver、Worker Guard 和写入/Forget 锁协议应继续复用。

原第 22～24 章已经重写：14 项 D 计划逐项给出保留、收缩、合并或暂缓结论，并归并为 8 个可验收工作包。D01/D11 不再作为大规模重构的 P0 前置；D10 暂缓；D07 收缩为来源字段修复与 Turn-only 明确策略；D12/D13/D14 在现有实现上增量补缺。

第 1～21 章中的设计示意不等于现状缺陷或已批准需求。特别是 Identity Slot、统一 Source 类型扩展和通用 AccessContext，均为可选设计；下文与新计划有冲突时，以本节和第 22～24 章为准。

### 0.2 最重要的审计判断

需要明确区分以下两个场景：

**场景 A：新提案的来源被遗忘**

``` text
E1 被 Forget
  ↓
后台重新处理 E1
  ↓
Proposal 来源仍然是 E1
  ↓
_is_reingestion_blocked()
  ↓
拒绝写入
```

V62.6.1 已经针对这一场景实现了防重抽保护。

**场景 B：旧 Memory 的来源被遗忘，但新提案来自正常的新 Event**

``` text
E1 → M1
E1 被 Forget

M1 的 Lifecycle 可能仍然是 ACTIVE
但 Forget Visibility = BLOCKED

E2 → 新 Proposal
E2 未被 Forget
  ↓
防重抽检查通过
  ↓
查询同 Key 的旧记录
  ↓
M1 可能仍然进入 ConflictResolver
```

第二个场景才是"Forget 后重新设置必须创建独立 Memory"的关键问题。

它不能只靠新 Proposal
的来源防重抽解决，必须在旧记录候选查询、冲突解析、强制决策、Promote、Wake、Merge、Provenance
继承和 Lineage 写入等环节共同保证。

### 0.3 审计结论分级

  --------------------------------------------------------------------------------
  领域               当前判断           主要问题
  ------------------ ------------------ ------------------------------------------
  Key Registry       已实现             缺少显式语义角色和 Identity Slot 定义

  Proposal 契约      已实现主体字段     Turn 来源持久化不完整

  Gate               已实现             Authority 与事实来源完整性仍需统一

  Conflict Resolver  已实现             依赖调用方提供的已有记录集合

  Write Service      已实现多种操作     旧记录可见性没有统一强制

  Lifecycle          已实现             直接生命周期动作与维护动作入口不同

  Evidence           已实现             证据内容与来源关系职责需要进一步明确

  Provenance         已实现 Event 闭包  Turn 来源没有完整成为独立持久来源边

  Lineage            已实现             被遗忘事实与新事实之间的谱系边界不够明确

  MemoryReadModel    已实现             Runtime Identity / Policy 查询缺少 Forget
                                        过滤

  ContextAssembler   已实现             Sealing Bridge 存在可见性缺口

  Core Memory        已实现             实时重建与持久投影需统一保证

  Vector Recall      已实现             召回后有 Forget 回源过滤

  Graph              已实现             历史关系读取只检查
                                        Lifecycle，未完整检查来源可见性

  Direct API         部分实现           Personal Signals、Timeline、Conflict
                                        等入口有旁路

  Forget             已实现复杂机制     不是所有消费者都强制使用

  Identity Slot      可选模型暂缓       现有空值契约可继续使用
  --------------------------------------------------------------------------------

------------------------------------------------------------------------

# 1. Memory 的整体定义

## 1.1 一条 Memory 到底是什么？

在 AIive 中，一条 Memory 不应该被理解成只有下面两个字段：

``` json
{
  "key": "user.display_name",
  "value": "小李"
}
```

完整的记忆至少包含以下信息：

``` text
Memory
├── Fact
│   ├── memory_type
│   ├── canonical_key
│   ├── content
│   ├── structured_value
│   └── scope
│
├── Trust
│   ├── confidence
│   ├── trust_level
│   └── sensitivity
│
├── Evidence
│   ├── supporting evidence
│   ├── source event
│   └── evidence relation
│
├── Provenance
│   ├── source Event
│   └── source Turn
│
├── Lifecycle
│   ├── candidate
│   ├── active
│   ├── sleeping
│   ├── archived
│   └── forgotten
│
├── Validity
│   ├── valid
│   ├── superseded
│   ├── contradicted
│   └── expired
│
├── Lineage
│   ├── create
│   ├── reinforce
│   ├── revise
│   ├── supersede
│   ├── merge
│   └── promote
│
└── Projection
    ├── Core Memory
    ├── Vector
    ├── Graph
    └── Retrieval Index
```

这几个部分分别回答不同问题：

  维度                回答的问题
  ------------------- --------------------------------
  Fact                系统记住了什么？
  Trust               这条事实有多可信？
  Evidence            有什么证据支持它？
  Provenance          内容最初来自哪里？
  Lifecycle           记录目前处于什么生命周期？
  Validity            事实当前是否有效？
  Lineage             这条事实经历过什么演进？
  Projection          如何让不同消费者读取和检索它？
  Forget Visibility   系统是否仍然被允许使用它？

**建议将 Forget Visibility 视为独立访问控制维度，而不是 Lifecycle
的一个状态。**

例如：

``` text
Memory M1
Lifecycle = ACTIVE
Validity = VALID
Forget Visibility = BLOCKED
```

这在当前系统中是可能出现的，也不应被视为数据矛盾。

## 1.2 单一事实源

当前设计中：

-   `memory_records` 是记忆事实和生命周期的权威存储。
-   Core Memory 是小规模稳定上下文投影。
-   Vector 是语义召回投影。
-   Graph 是结构化关系投影。
-   Retrieval Index 是检索加速和索引投影。
-   Markdown/File Projection 是可检查的文件化投影。

原则上，任何派生数据都不能成为独立事实源。

重建 Core Memory、Vector、Graph 时，应从权威 MemoryRecord
及其仍然有效的来源和事实字段重建，而不是从旧投影反向恢复事实。

------------------------------------------------------------------------

# 2. MemoryKeyRegistry

文件：

`backend/aiive/memory/memory_key_registry.py`

## 2.1 设计职责

MemoryKeyRegistry 是 Memory Key 的集中定义来源。

当前 `MemoryKeySpec` 主要包含：

  字段                      含义
  ------------------------- ----------------------
  canonical_key             规范化 Key
  memory_type               记忆类型
  cardinality               single / multi
  default_scope             默认作用域
  context_roles             可进入哪些上下文角色
  conflict_policy           冲突策略
  maintenance_policy        生命周期维护策略
  structured_value_schema   结构化值 Schema
  is_pattern                是否为 Pattern
  core_memory_role          是否进入 Core Memory

`KeyPattern` 提供动态前缀匹配。

解析优先级：

1.  Exact Key。
2.  Pattern 中最长前缀匹配。

因此，Exact Key 可以覆盖宽泛 Pattern。

## 2.2 当前 Exact Keys

当前注册的 Exact Key 包括：

  ------------------------------------------------------------------------------------------------------------------------------------------
  Key                                  Memory Type    Cardinality   Context Role       Conflict    Maintenance   Core Role
                                                                                       Policy
  ------------------------------------ -------------- ------------- ------------------ ----------- ------------- ---------------------------
  `user.display_name`                  user_profile   single        runtime_identity   supersede   persist       core.human_identity

  `user.name`                          user_profile   single        runtime_identity   supersede   persist       core.human_identity

  `user.preference.response_style`     user_profile   single        runtime_identity   supersede   persist       core.interaction_defaults

  `user.preference.default_language`   user_profile   single        runtime_identity   supersede   persist       core.interaction_defaults

  `agent.display_name`                 agent_self     single        runtime_identity   supersede   persist       core.agent_persona

  `agent.persona.tone`                 agent_self     single        runtime_identity   supersede   persist       core.agent_persona

  `agent.persona.relationship`         agent_self     single        runtime_identity   supersede   persist       core.agent_persona

  `core.human_identity`                user_profile   single        未声明             supersede   persist       core.human_identity

  `core.interaction_defaults`          user_profile   single        未声明             supersede   persist       core.interaction_defaults

  `core.agent_persona`                 agent_self     single        未声明             supersede   persist       core.agent_persona
  ------------------------------------------------------------------------------------------------------------------------------------------

注意：

`core.*` 这些显式投影 Key 与 `user.display_name`、`agent.display_name`
等身份 Key 并不是同一层语义。

它们允许 Agent 直接写入 Core
Memory，但当前系统需要避免同一身份事实同时通过多个 Key
产生互相不一致的值。

## 2.3 当前 Pattern

  --------------------------------------------------------------------------------------------------------
  Pattern               Memory Type    Cardinality   Scope         Context Role  Conflict    Maintenance
  --------------------- -------------- ------------- ------------- ------------- ----------- -------------
  `user.preference.*`   user_profile   multi         global        user_memory   append      persist

  `user.routine.*`      user_profile   multi         global        user_memory   append      decay

  `user.habit.*`        user_profile   multi         global        user_memory   append      decay

  `user.schedule.*`     user_profile   multi         global        user_memory   append      expire_7d

  `project.*`           project        multi         project       user_memory   append      persist

  `policy.*`            policy         multi         global        policy        supersede   persist

  `environment.*`       environment    multi         environment   无            append      persist

  `knowledge.*`         knowledge      multi         global        user_memory   append      persist

  `episodic.*`          episodic       multi         global        无            append      expire_30d
  --------------------------------------------------------------------------------------------------------

## 2.4 Registry 的关键问题

### 可选改进 A：语义角色分类（非安全修复前置）

当前 Registry 能判断：

``` text
user.display_name
cardinality = single
core_memory_role = core.human_identity
```

但不能明确表达：

``` text
semantic_role = identity_slot
```

现有 context_roles、cardinality 与 core_memory_role 已支撑当前消费者。新增 semantic_role 仅在出现无法由这些字段表达的实际需求后考虑，缺少该字段本身不证明存在错误。

建议未来显式区分：

-   identity_slot
-   preference
-   policy
-   project_fact
-   episodic_fact
-   agent_configuration
-   system_configuration

不建议把所有 Core Memory Key 都定义为 Identity Slot。

### 问题 B：Core Memory Key 与普通身份 Key 可能产生双重事实源

例如：

``` text
user.display_name = 小李
core.human_identity = 用户称呼是小李
```

这两条记录可能表达同一个事实，但系统目前没有通用的语义约束保证它们始终一致。

需要决定：

1.  `core.human_identity` 是否只允许保存综合描述，而不是单独身份值。
2.  是否禁止其与 `user.display_name` 重复表达同一个字段。
3.  是否由一个明确的 Identity Read Model 生成身份配置，再投影成 Core
    Memory。

### 问题 C：Pattern 无法表达所有动态 Key 的具体语义

`project.*` 的所有 Key 默认 multi、append
是合理的，但未来如果某个项目字段必须 single，需要显式 Exact Key 覆盖。

需要保持 Registry 是唯一真相源，不应在业务代码中再出现第二套 Key 规则。

------------------------------------------------------------------------

# 3. MemoryProposal：提案与来源契约

文件：

`backend/aiive/memory/memory_types.py`

## 3.1 Proposal 的职责

MemoryProposal 是"候选事实"，不是正式 Memory。

它表达：

-   系统发现了什么。
-   这个事实应该属于什么 Key。
-   来自哪些来源。
-   有什么 Evidence。
-   可信度和重要性如何。
-   提议采用什么操作。
-   最终写入是否需要用户确认。

Proposal 不能直接决定最终操作。

最终行为由：

``` text
ProposalNormalizer
    ↓
MemoryGate
    ↓
Forget Reingestion Guard
    ↓
ConflictResolver
    ↓
MemoryWriteService
```

共同决定。

## 3.2 MemoryProposal 字段分类

### 身份与规范化字段

  字段            作用
  --------------- --------------
  proposal_id     提案唯一身份
  memory_type     记忆类型
  canonical_key   规范 Key
  scope_type      作用域类型
  scope_id        作用域 ID

### 内容字段

  字段               作用
  ------------------ ------------------
  content            自然语言事实内容
  structured_value   结构化事实
  keywords           检索关键词
  graph_facts        结构化关系事实

### 来源字段

  字段                    作用
  ----------------------- -----------------------
  source_event_ids        来源 Event ID 集合
  source_turn_id          业务 Turn ID
  source_turn_record_id   TurnRecord 数据库主键
  evidence                EvidenceItem 列表
  ingestion_run_id        抽取批次 ID
  proposal_index          批次中的提案序号

### 信任和质量字段

  字段              作用
  ----------------- ------------------
  trust_level       提案总体信任级别
  confidence        置信度
  importance        重要性
  stability         稳定性分类
  stability_score   稳定性分数
  sensitivity       内容敏感级别

### 写入控制字段

  字段                    作用
  ----------------------- ------------------------------------
  proposed_operation      Extractor 提议操作
  requires_confirmation   是否要求确认
  execution_mode          user_required / system_best_effort
  retention_policy        ephemeral / normal / pinned
  valid_to                有效期截止时间
  durable                 是否持久化

### 幂等字段

  -----------------------------------------------------------------------------------
  字段                          作用
  ----------------------------- -----------------------------------------------------
  idempotency_key               请求级幂等身份

  content_hash                  内容哈希

  compute_request_idempotency   根据来源、Extractor、版本、序号、内容计算请求幂等键
  -----------------------------------------------------------------------------------

## 3.3 两种 Turn ID 必须区分

``` text
TurnRecord.id
    = source_turn_record_id

TurnRecord.turn_id
    = source_turn_id
```

它们不是同一个 ID。

-   `source_turn_record_id` 用于关联数据库中的 TurnRecord。
-   `source_turn_id` 表示业务 Turn 标识。

现有 Proposal 和 DB Model 都包含两者。

但当前 `MemoryWriteService._persist_proposal()` 持久化时只看到：

``` python
source_turn_record_id=proposal.source_turn_record_id or None
```

没有同时持久化 `source_turn_id`。

这会导致内存中的 Proposal 来源契约与持久化审计记录不完全一致。

## 3.4 Evidence 与 source_event_ids 的关系

Proposal 有两套 Event 来源表达：

``` text
proposal.source_event_ids
proposal.evidence[*].source_event_id
```

它们应该保持一致，但职责不同：

-   `source_event_ids`：提案整体来源集合。
-   `EvidenceItem.source_event_id`：具体证据条目的来源。

当前 `_collect_forget_sources()` 会将这些来源统一收集后做防重抽检查。

当前写入时 `_write_evidence_batch()` 又会把：

``` python
set(source_event_ids) | evidence.source_event_id
```

写入 ContentProvenanceRef。

因此，Provenance 是来源闭包的长期依据，不能仅依赖某一个 Evidence 字段。

## 3.5 需要明确的来源契约

建议未来建立以下规则：

1.  正常对话提取的 Proposal 必须具有可验证的 Event 来源，或具有明确的
    Turn 来源证明。
2.  Event ID 必须是真实 Event 主键，不能用 trace_id 伪装。
3.  TurnRecord ID 与业务 Turn ID 必须分别保存。
4.  Evidence 的来源必须属于 Proposal
    声明的来源集合，或在明确规则下扩展来源集合。
5.  来源缺失不能默认认为是人工创建。
6.  只有明确的独立人工创建路径，才能获得无 Event 来源的豁免。
7.  Proposal 持久化后应能完整重建其来源契约。
8.  Forget 必须能够沿来源关系反向找到所有受影响 Memory。

------------------------------------------------------------------------

# 4. ProposalNormalizer

文件：

`backend/aiive/memory/proposal_normalizer.py`

## 4.1 主要工作

Normalizer 接收提取结果，进行：

1.  Memory Type 规范化。
2.  Canonical Key 解析。
3.  Registry 查询。
4.  Cardinality 与 Scope 解析。
5.  EvidenceItem 构造。
6.  内容哈希计算。
7.  Proposal 构造。
8.  请求幂等身份计算。

Key 的定义不应由模型自由决定。

模型可以提供 hint，但最终应由 Registry 确认。

## 4.2 Evidence 构造

当调用方提供 Evidence 时，Normalizer 根据字段构造 EvidenceItem。

当调用方只提供 `source_event_ids` 时，会为来源创建 EvidenceItem。

其中：

-   用户消息可使用 `user_message` 来源。
-   Assistant 回复标记为 `llm_reply`。
-   Assistant 回复的 `derived_from` 关系只用于
    Provenance，不参与事实权威判定。

这一区分很重要：

**来源存在不代表来源有权威性。**

例如 Assistant
回复可以是用户消息的派生来源，但不能因此独立证明用户个人资料。

------------------------------------------------------------------------

# 5. MemoryGate：准入和权威规则

文件：

-   `backend/aiive/memory/memory_gate.py`
-   `backend/aiive/memory/memory_policy.py`
-   `backend/aiive/memory/memory_types.py`

## 5.1 Gate 的职责

MemoryGate 负责决定 Proposal 是否可以成为正式记忆。

当前结果主要有：

-   `active`
-   `candidate`
-   `reject`

其检查包括：

-   Key / Type 规范。
-   Evidence 完整性。
-   Evidence 来源权威性。
-   临时记忆期限。
-   置信度阈值。
-   无 Evidence 的降级处理。

## 5.2 Authority 与 Evidence Relation

当前 Gate 会过滤 `relation="derived_from"` 的 Evidence，再调用
MemoryPolicyEngine 判断权威来源。

原因：

Assistant 回复、派生内容等可以用于追踪来源，但不能独立证明用户事实。

这是正确的设计方向。

例如：

``` text
用户说：我住在深圳
    ↓
user_message Evidence
    ↓
有用户陈述权威
    ↓
允许作为用户事实候选
```

而：

``` text
Assistant 回复：你应该住在深圳
    ↓
llm_reply Evidence
    ↓
derived_from
    ↓
不具备独立用户事实权威
```

## 5.3 AUTHORITY_RULES

当前 `memory_types.py` 中存在按 Memory Type 区分的 EvidenceSourceType
权威规则。

其中 `agent_self` 可以接受：

-   test_result
-   kernel_result
-   tool_observation
-   selfdev 相关证据
-   user_assertion

这意味着 Agent
自我认知不能只依赖用户描述，也可以由测试和内核运行结果证明。

未注册 Authority Rule 的类型默认没有授权来源，采用 fail-closed。

## 5.4 Gate 的关键边界

Gate 解决的是：

> 新提案是否有资格进入 Memory。

Gate 不解决：

-   旧 Memory 是否仍然可见。
-   新提案应该 Reinforce 还是 Supersede。
-   Forget 后是否能复用旧记录。
-   Provenance 是否完整。
-   Lineage 是否应该建立。

这些必须由后续服务共同完成。

另外，Gate 的 `candidate` 结果不能被强制决策或普通 Resolver
绕过，尤其不能允许低置信 Proposal 直接覆盖已有有效事实。

------------------------------------------------------------------------

# 6. ConflictResolver：single / multi 冲突策略

文件：

`backend/aiive/memory/conflict_resolver.py`

## 6.1 Resolver 的输入

Resolver 本身不负责查询数据库。

它接收：

``` python
resolve(proposal, existing_records)
```

`existing_records` 由调用方提供。

因此 Resolver 的正确性依赖两个前提：

1.  查询到的记录集合完整。
2.  查询到的记录集合只包含允许参与冲突解析的记录。

第二个前提与 Forget 后重新设置直接相关。

## 6.2 Single Cardinality

当前规则：

``` text
single
├── 没有 Active / Candidate
│   └── create
│
├── 有 Active
│   ├── 内容相同
│   │   └── reinforce
│   │
│   └── 内容不同
│       ├── latest_value_wins
│       │   └── supersede
│       │
│       └── revise_allowed / merge
│           ├── minor change → revise
│           └── major change → supersede
│
└── 没有 Active，但有 Candidate
    ├── 找到相同 Candidate
    │   └── promote
    └── 找不到
        └── create
```

当前 Exact Key 的 `user.display_name` 使用：

``` text
cardinality = single
conflict_policy = supersede
```

因此：

``` text
小李 → 阿伟
```

通常应产生 Supersede。

## 6.3 Multi Cardinality

当前规则：

``` text
multi
├── 找到内容相同的 Active
│   └── reinforce
│
├── 找到内容相同的 Candidate
│   └── promote
│
└── 没有匹配
    └── create
```

Multi Key 允许同一个 Key 存在多个不同事实。

例如：

``` text
user.preference.food = 火锅
user.preference.food = 烧烤
user.preference.food = 粤菜
```

## 6.4 内容匹配

当前 Resolver 主要使用：

-   content_hash
-   structured_value_hash

判断内容是否相同。

Minor Change 判断会使用内容长度和词重叠等启发式规则。

它不是完整语义判断。

尤其是：

``` text
喜欢深圳
不喜欢深圳
```

两者可能长度相近，但语义完全相反。

当前源码注释也明确指出此类误判风险。

## 6.5 Merge 的实际触发

必须区分：

-   Resolver 的普通操作决策。
-   Conflict Confirmation 用户决策。
-   MemoryLifecycleService 的 exact duplicate merge。
-   MemoryWriteService 的 `_execute_merge()`。

普通 ConflictResolver 的 operation 类型主要是：

``` text
create / reinforce / revise / supersede / promote / ignore
```

当前普通 Resolver 并不常规返回 merge。

`MemoryWriteService._execute_merge()` 虽然存在，但不能据此认为普通
Resolver 会自然触发 Merge。

此外，用户冲突确认选择 merge 时，当前 `_execute_forced_decision()` 将
`merge` 映射到 `revise` 路径：

``` python
operation = "revise" if decision in ("revise", "merge") else "supersede"
```

因此当前至少存在三种不同语境：

1.  普通 Resolver 决策。
2.  用户确认中的 merge。
3.  生命周期维护中的 exact duplicate merge。

审计和后续设计必须分别描述，不能混为一个操作。

------------------------------------------------------------------------

# 7. MemoryWriteService：写入流程

文件：

`backend/aiive/memory/memory_write_service.py`

## 7.1 主写入调用链

``` text
UnifiedMemoryExtractor / Tool / REST API
             ↓
      ProposalNormalizer
             ↓
      MemoryWriteService.write()
             ↓
      获取 Forget 共享锁
             ↓
      Memory Key 并发锁
             ↓
      _write_single_in_transaction()
             ↓
      _is_reingestion_blocked()
             ↓
         MemoryGate
             ↓
      Conflict Confirmation
             ↓
      _load_existing_or_wake_sleeping()
             ↓
       ConflictResolver
             ↓
       Execute Operation
             ↓
  MemoryRecord / Evidence / Provenance
             ↓
      MemoryProposal 持久化
             ↓
       MemoryLineage / Event
             ↓
        Outbox Projection
```

## 7.2 Forget 与 Write 并发协议

V62.6.1 已有：

-   MemoryWrite 共享事务锁。
-   Forget Phase A 排他事务锁。
-   PostgreSQL advisory lock。
-   READ COMMITTED 前置检查。
-   锁序约束。
-   同一批次来源检查缓存。

核心目标是：

Forget Phase A 不能与在途 MemoryWrite 发生不可控竞争。

等待锁期间完成的写入必须在 Forget cutoff 语义下得到一致处理。

这是当前架构中重要且应该保留的保证。

## 7.3 防重抽检查

`_is_reingestion_blocked()` 收集：

-   Evidence Event ID。
-   Proposal source_event_ids。
-   source_turn_record_id。
-   source_turn_id。
-   RunContext 中的来源。

任一来源被 active Shield 或 block_reingestion Tombstone 屏蔽，整个
Proposal 被拒绝。

该检查发生在 Gate Reject 持久化和其他写入操作之前。

其目的不仅是阻止创建 Memory，还要阻止被遗忘原文通过
MemoryProposal、Evidence 或其他字段再次落库。

这部分设计正确。

但它只检查新 Proposal 的来源，不代表旧 Memory 的来源已经安全。

------------------------------------------------------------------------

# 8. 六类写入操作的真实实现

## 8.1 Create

流程：

``` text
没有可参与冲突解析的记录
    ↓
Gate 决定 active / candidate
    ↓
MemoryStore.create_record()
    ↓
写入 MemoryEvidence
    ↓
写入 ContentProvenanceRef
    ↓
持久化 MemoryProposal
    ↓
写入 memory.created Event
    ↓
Outbox Projection
```

结果：

-   新建 MemoryRecord。
-   新建 Evidence。
-   新建来源 Provenance。
-   产生 CREATE 类 Lineage / Event。

注意：具体 Lineage 是否通过 MemoryMutationExecutor
写入，应以实际执行路径为准；不能只凭 `memory.created` Event
就认定一定存在一条 `MemoryLineage(CREATE)`。需要分别核对各执行分支和
mutation executor 的真实调用。

## 8.2 Reinforce

流程：

``` text
找到内容相同的 Active Memory
    ↓
检查 Proposal 幂等键
    ↓
检查来源 Event 是否已计入 Evidence
    ↓
更新 confidence
    ↓
更新 reinforce_count
    ↓
更新 last_reinforced_at
    ↓
record_version + 1
    ↓
追加 Evidence
    ↓
追加 Provenance
    ↓
持久化 Proposal
    ↓
memory.reinforced Event
    ↓
刷新投影
```

Reinforce 不创建新的 MemoryRecord。

它是在原记录上追加支持证据并提高置信度。

重要风险：

如果被遗忘的旧 Memory 进入 Reinforce，它会把新来源混入旧记录，破坏
Forget 后独立重新摄取的承诺。

因此，Reinforce 必须只针对可见且允许参与写入的旧记录。

## 8.3 Supersede

流程：

``` text
旧 M1
    ↓
validity_state = superseded
record_version + 1
    ↓
创建新 M2
    ↓
M2.supersedes = M1.id
    ↓
M1.superseded_by = M2.id
    ↓
写 MemoryLineage(M1 → M2)
    ↓
新 M2 写入 Evidence / Provenance
    ↓
刷新 M2 投影
    ↓
删除 / 更新 M1 的旧投影
```

Supersede 主要修改旧记录的 Validity，不一定改变 Lifecycle。

例如：

``` text
M1.lifecycle = active
M1.validity = superseded
```

这是合法的状态组合。

关键风险：

如果 M1 的来源已被 Forget 屏蔽，新 M2 仍然建立：

``` text
M1 → M2
```

就会让被遗忘事实进入新记忆的版本谱系。

对于 Forget 后重新设置，这种关系不应建立。

## 8.4 Revise

当前 `_execute_supersede_or_revise()` 中，Revise 和 Supersede
共用主要执行流程：

-   旧记录 validity 变为 superseded。
-   创建新记录。
-   设置 supersedes / revision_of。
-   更新 revision_num。
-   创建 Lineage。
-   写新来源。

差异主要在 operation 和 revision 字段语义。

因此 Revise 不是原地修改内容，而是创建新版本记录。

这点必须在文档中明确。

## 8.5 Merge

`_execute_merge()` 当前行为：

1.  收集所有旧记录 ID。
2.  查询旧记录的 ContentProvenanceRef。
3.  将旧 Event 来源合并进新 Proposal。
4.  将所有旧记录 Validity 改为 superseded。
5.  创建新的 MemoryRecord。
6.  为每个旧记录建立 superseded_by。
7.  为每个旧记录建立 MERGE Lineage。
8.  写新 Evidence / Provenance。
9.  刷新新旧投影。

当前实现存在一个重要问题：

``` python
inherited = {
    ref.source_id
    for ref in old_provenance
}
proposal.source_event_ids = proposal.source_event_ids | inherited
```

它会继承所有旧 Event Provenance。

如果旧记录集合中存在来源已被 Forget
屏蔽的记录，就可能把被遗忘来源重新带入新 Memory。

所以 Merge 的正确性不仅依赖"新记录 ID 是新的"，还依赖参与 Merge
的所有旧记录均可见、来源有效。

## 8.6 Promote

普通 Resolver 找到相同 Candidate 后可以 Promote。

当前 `_execute_promote_candidate()`：

-   对 Candidate 自身创建 PROMOTE Lineage。
-   Candidate lifecycle 改为 active。
-   更新 confidence。
-   更新 record_version。
-   追加新 Evidence。
-   持久化 Proposal。
-   刷新投影。

Promote 不创建新 MemoryRecord。

这意味着：

如果被 Forget 屏蔽的 Candidate 仍然参与 Resolver，它可能被新 Proposal
重新激活。

这是 Forget 后重新设置必须覆盖的路径。

------------------------------------------------------------------------

# 9. MemoryLifecycleService

文件：

`backend/aiive/memory/memory_lifecycle_service.py`

它负责维护型状态变化，与 MemoryWriteService 的事实写入不是同一个入口。

主要动作：

-   promote
-   sleep
-   wake
-   archive
-   merge_exact_duplicate
-   supersede
-   no_op

## 9.1 维护动作执行前检查

当前 `apply_maintenance_action()`：

1.  获取 Forget 共享锁。
2.  检查 Action 来源是否完整。
3.  检查来源是否被 Forget 屏蔽。
4.  检查 Action 的版本和前置条件。
5.  决定是否执行。
6.  执行生命周期变化。
7.  写 Lineage / Event。
8.  刷新投影。

这一条维护路径已经具有来源可见性保护。

## 9.2 Wake

Wake：

``` text
sleeping → active
```

同时：

-   record_version + 1
-   updated_at 更新
-   observed_at 更新
-   写 WAKE Lineage
-   写 memory.wake Event
-   刷新投影

Wake 与普通 AccessTracker touch 不同。

## 9.3 Promote

Promote：

``` text
candidate → active
```

同时增加 confidence，更新版本，写 Lineage 和 Event。

## 9.4 Sleep / Archive

Sleep 和 Archive 有状态机前置条件。

Forgotten 不允许通过：

``` text
forgotten → sleeping → active
```

被复活。

这是正确的防御。

## 9.5 维护型 Merge 与写入型 Merge

`merge_exact_duplicate` 是生命周期维护动作。

它与 `MemoryWriteService._execute_merge()` 不是同一个业务操作。

前者由 Maintenance Planner 选择 winner，服务执行 duplicate
合并；后者是新 Proposal 与多个已有记录合并。

必须分别审计其：

-   来源继承。
-   Evidence 处理。
-   Provenance 处理。
-   Lineage。
-   Forget 可见性。
-   新记录是否创建。

------------------------------------------------------------------------

# 10. MemoryRecord 的状态机

文件：

`backend/aiive/memory/memory_types.py`

## 10.1 LifecycleState

当前状态：

``` text
candidate
active
sleeping
archived
forgotten
```

大致含义：

  状态        含义
  ----------- ----------------------------------
  candidate   候选事实，尚未正式确认或提升
  active      当前可参与正常读取的生命周期状态
  sleeping    暂时休眠，可在满足条件时唤醒
  archived    归档，不参与普通当前事实读取
  forgotten   已遗忘，不得正常使用

## 10.2 ValidityState

当前状态：

``` text
valid
superseded
contradicted
expired
```

含义：

  状态           含义
  -------------- ----------------------
  valid          当前有效
  superseded     已被新事实替代
  contradicted   被其他事实否定或冲突
  expired        超过有效期限

## 10.3 LineageOperation

当前枚举包含：

-   create
-   reinforce
-   revise
-   supersede
-   merge
-   promote
-   sleep
-   wake
-   archive
-   forget

LineageOperation 不是 LifecycleState。

## 10.4 状态正交性

以下组合可以成立：

  Lifecycle   Validity     Forget Visibility   解释
  ----------- ------------ ------------------- --------------------------------
  active      valid        visible             正常可用
  active      valid        blocked             来源被 Forget 屏蔽
  active      superseded   visible             生命周期未归档，但事实已被替代
  sleeping    valid        visible             可唤醒的休眠事实
  archived    expired      visible             已归档且过期
  forgotten   valid        blocked             被遗忘，不允许使用

因此，任何消费者只检查 Lifecycle 或 Validity 都不够。

建议未来定义统一的 MemoryAccessPolicy，至少接收：

``` text
MemoryRecord
Forget Visibility
Read Purpose
Execution Authority
```

并给出明确的允许 / 拒绝结果。

------------------------------------------------------------------------

# 11. Evidence、Provenance、Lineage 的关系

这是整个 Memory 设计中最重要的部分之一。

## 11.1 MemoryEvidence

表：`memory_evidence`

主要字段：

-   memory_id
-   source_event_id
-   source_type
-   trust_level
-   relation
-   content_span
-   created_at

它回答：

> 哪些证据支持这条 Memory？

Evidence 是业务证据，不等于全部来源关系。

## 11.2 ContentProvenanceRef

表定义位于 `backend/aiive/db/forget_models.py`。

当前 Memory 来源写入主要是：

``` text
owner_type = memory_record
owner_id = MemoryRecord.id
source_type = event
source_id = Event.id
```

它回答：

> 这条 Memory 的内容来自哪些原始来源？

它必须在 MemoryEvidence 被 Forget Cascade
清理后仍然保留，供后续可见性判断和防重抽使用。

因此 Evidence 与 Provenance 不是同一个东西：

  维度                       Evidence     Provenance
  -------------------------- ------------ ------------------
  目的                       支持事实     追踪内容来源
  是否包含证据关系           是           否
  是否保存证据文本片段       可以         不以此为职责
  Forget 后是否必须保留      依清理策略   来源闭包需要保留
  是否作为可信度依据         是           不直接决定
  是否作为 Forget 闭包依据   辅助         核心

## 11.3 MemoryLineage

表：`memory_lineage`

它回答：

> 这条记忆如何演进？

例如：

``` text
M1 --SUPERSEDE--> M2
```

它不是来源关系。

M2 的来源应该通过自己的 Provenance 表达，而不是仅依靠 predecessor M1
推断。

## 11.4 三种关系不能混为一谈

``` text
Evidence:
E1 ──supports──> M1

Provenance:
M1 ──derived_from──> E1

Lineage:
M1 ──superseded_by──> M2
```

它们回答不同的问题。

## 11.5 来源契约缺口：Turn 持久化与 Turn-only 可用性

当前持久 Provenance 的主要实现是：

``` text
MemoryRecord → Event
```

但 Proposal 有：

``` text
source_turn_id
source_turn_record_id
```

`_persist_proposal()` 没有完整持久化两种 Turn 字段。

当前 Forget 可见性主要通过 Turn 对应 Event 闭包处理。

这意味着 Turn-only 来源的持久可用性不足，而不是当前默认放行：`_source_event_blocked_memory_ids()` 对无 Event 来源且非严格手工创建证明的 Memory 关闭可见性，对非 event 类型的 Provenance Ref 也关闭。纯 Turn 的防重抽检查已经存在。不能仅新增 `source_type="turn_record"` 的边而不更新 reader、cascade、scrub 和 verifier。见 P1-A。

建议未来将来源契约抽象为统一 Provenance Source：

``` text
MemoryRecord
  ├── Event Source
  ├── TurnRecord Source
  ├── External Document Source
  └── Explicit Manual Creation Proof
```

每种来源需要明确类型、ID、验证方式和 Forget 传播规则。

------------------------------------------------------------------------

# 12. MemoryReadModel 与 ContextAssembler

## 12.1 MemoryReadModel

文件：

`backend/aiive/memory/memory_read_model.py`

主要提供：

-   RuntimeIdentity
-   Policies

调用：

``` text
MemoryReadModel.resolve_identity()
MemoryReadModel.resolve_policies()
```

底层使用：

``` text
MemoryStore.get_by_context_roles()
```

## 12.2 当前读取缺口

`MemoryStore.get_by_context_roles()` 查询条件主要是：

``` python
lifecycle_state == "active"
validity_state == "valid"
```

没有在该方法中调用 `ForgetVisibilityService.filter_memory_ids()`。

因此：

来源 Event 被 Forget 屏蔽，但 MemoryRecord 仍然 active + valid
时，可能通过 RuntimeIdentity 或 Policies 读取。

## 12.3 Identity 读取规则

当前 RuntimeIdentity 读取：

-   agent.display_name
-   user.display_name
-   user.name
-   agent.persona.relationship
-   agent.persona.tone
-   user.preference.response_style

其中存在：

``` python
if not identity.user_display_name:
    identity.user_display_name = content
```

即 `user.name` 可以 fallback 到 `user.display_name`。

此处已确认的安全缺陷是候选未经过 Forget 过滤；fallback 本身是否保留属于身份展示语义。

例如：

``` text
user.display_name = 小李
user.name = 罗伟
```

如果 `user.name` 也来自被屏蔽来源，必须过滤；如果它有独立可见的合法来源，Forget display_name 不自动撤销 name。是否允许真实姓名作为称呼应单独明确，不把它当作已经证实的遗忘泄漏。

## 12.4 ContextAssembler 的组装结构

`_load_agent_context()` 主要执行：

``` text
MemoryReadModel
    ├── resolve_identity()
    └── resolve_policies()
          ↓
      Stable Contract

Core Memory Projection
          ↓

Unified Recall
    ├── Memory Recall
    ├── Segment Summary
    └── Epoch Checkpoint
          ↓

Graph Expansion
          ↓

assemble_system_content()
```

除此之外，完整 ContextAssembler 还会组合：

-   Recent Hot History
-   WorkingState
-   Segment Summary
-   Epoch Checkpoint
-   Sealing Bridge
-   当前 Turn

## 12.5 已经有保护的路径

  路径                    当前保护
  ----------------------- ------------------------------------------
  普通热历史              `_filter_forgotten_events()`
  Segment Summary         `blocked_target_ids("segment_summary")`
  Epoch Checkpoint        `blocked_target_ids("epoch_checkpoint")`
  Unified Recall Memory   UnifiedRetriever / AutomaticRecall
  Core Memory             CoreMemoryProjection / load_core_memory

## 12.6 尚有缺口的路径

### Sealing Bridge

`_load_sealing_bridge()`：

-   有 Summary 时直接加载 SegmentSummary。
-   没有 Summary 时根据 CompactionInput.event_manifest 加载 Event。

当前没有看到该函数对 Summary 或 Event 执行统一 Forget 过滤。

### Stable Contract

RuntimeIdentity 和 Policies 由 MemoryReadModel 返回，而底层 Context Role
查询缺少 Forget 过滤。

### Graph Expansion 历史关系

当前关系与历史关系的可见性检查不同，见后文 Graph。

------------------------------------------------------------------------

# 13. Core Memory、Vector、Graph、Retrieval 的派生关系

## 13.1 Core Memory

文件：

`backend/aiive/memory/core_memory_projection.py`

Core Memory 是少量稳定事实的投影。

流程：

``` text
MemoryRecord
    ↓
MemoryKeyRegistry.core_memory_role
    ↓
build_blocks()
    ↓
按 Role 分桶
    ↓
MemoryPolicyEngine
    ↓
Token Budget
    ↓
CoreMemoryBlock
```

读取时：

-   如果持久投影有效，则读取持久块。
-   如果来源 Memory 不可见，则回退实时 rebuild。
-   如果没有持久块，也会实时 rebuild。

当前 `build_blocks()` 已经对 Memory ID 执行 Forget 可见性过滤。

这条路径比 MemoryReadModel 更完整。

## 13.2 Memory Vector

文件：

`backend/aiive/memory/vector_projection.py`

向量是 MemoryRecord 的派生投影。

当前模型：

-   512 维向量。
-   pgvector。
-   HNSW。
-   cosine distance。
-   embedding_model identity。
-   record_version。

Refresh：

1.  读取 MemoryRecord。
2.  检查 record_version。
3.  检查 Lifecycle / Validity。
4.  检查 ForgetVisibility。
5.  构造 Embedding Text。
6.  生成向量。
7.  更新 MemoryVectorProjection。

Search：

1.  对 Query 生成 Embedding。
2.  pgvector 近邻检索。
3.  回源 MemoryRecord。
4.  检查 record_version 和模型身份。
5.  调用 ForgetVisibilityService 过滤 ID。

因此 Vector 不是权威事实源，召回结果必须回源验证。

## 13.3 Automatic Recall

文件：

`backend/aiive/memory/automatic_recall.py`

当前主要召回路由：

1.  Exact Lookup
2.  Dense Vector
3.  Sparse BM25
4.  Lexical Fallback

其中 BM25 使用 ParadeDB pg_search。

Dense 使用 pgvector HNSW。

各路候选在融合前统一通过 ForgetVisibilityService 批量过滤。

## 13.4 RRF 与排序

文件：

-   `memory/recall_fusion.py`
-   `memory/recall_ranking.py`

当前融合考虑：

``` text
fused_score =
    w_rrf       * rrf_norm
  + w_relevance * relevance
  + w_recency   * recency
  + w_importance* importance
  + w_trust     * trust
  + w_scope     * scope
```

不同召回通道的原始分数不直接相加。

RRF 使用各路排名，避免 cosine 和 BM25 原始分数不在同一量纲的问题。

当前没有发现独立 Cross-Encoder Reranker 阶段。

## 13.5 Graph Projection

文件：

`backend/aiive/memory/graph_projection.py`

Graph 是 MemoryRecord.graph_facts 的派生投影。

主要内容：

-   GraphEntity
-   GraphRelation
-   时间有效性
-   关系置信度
-   来源 Memory ID
-   Source Span
-   冲突关系
-   Superseded 关系

Graph Projection Refresh 会检查来源 Memory 的 Forget 可见性。

## 13.6 Graph Expansion

文件：

`backend/aiive/memory/graph_expansion.py`

当前流程：

``` text
Recall Memory IDs
    ↓
Seed Entities
    ↓
Query Entity Match
    ↓
多跳 GraphRelation 查询
    ↓
回源 MemoryRecord
    ↓
Forget Visibility
    ↓
Lifecycle / Validity
    ↓
置信度排序
    ↓
附加历史关系
    ↓
Context 注入
```

当前当前关系会检查 ForgetVisibility。

但 `_attach_history()` 对历史关系只检查：

``` python
record.lifecycle_state != forgotten
```

没有对历史来源 Memory 执行完整 ForgetVisibility 检查。

这可能导致：

``` text
M1 来源 E1
M2 来源 E2
M1 → M2 Supersede
Forget E1
```

当前关系 M2 正常，但历史关系 M1 仍然被读取。

这是明确需要补齐的读取缺口。

## 13.7 Knowledge RAG 与 Memory Recall 的区别

Knowledge RAG 使用：

-   Document
-   Chunk
-   KnowledgeChunkVector
-   Lexical Search
-   Vector Search
-   RRF
-   Neighbor Expansion

Memory Recall 使用：

-   MemoryRecord
-   MemoryVectorProjection
-   BM25
-   Exact Lookup
-   Graph
-   Recency / Trust / Importance / Scope

两者共享部分向量基础设施，但数据模型、事实来源、生命周期和 Forget
语义不同。

不能将 Knowledge RAG 的 Chunk 来源清理与 Memory Provenance 混为一谈。

------------------------------------------------------------------------

# 14. Forget 对 Memory 的影响

## 14.1 Forget 的基本设计

当前 Forget 是一个分阶段 Saga：

``` text
Forget Request
    ↓
Phase A Shield
    ↓
Tombstone / Immediate Visibility Block
    ↓
Outbox Cascade
    ↓
Target Discovery / Content Scrub
    ↓
Projection Cleanup
    ↓
Verification
    ↓
Shield Retirement
```

Phase A 的目标是立即阻止正常读取，不等待后续 Cascade 清理完成。

## 14.2 Forget Visibility

核心文件：

`backend/aiive/forget/visibility_service.py`

主要能力：

-   检查 active ForgetShield。
-   检查 ForgetTombstone。
-   支持 memory / event / turn / thread / scope / time_range /
    canonical_key / all_user_data。
-   沿 Event 与 Turn 关系扩展屏蔽。
-   沿 Memory Provenance 追踪来源。
-   Fail-closed。
-   提供批量过滤方法。

重要接口：

-   `is_shielded()`
-   `blocked_target_ids()`
-   `filter_blocked_ids()`
-   `filter_events()`
-   `filter_memory_ids()`
-   `is_tombstone_blocked_reingestion()`
-   `can_audit_read()`

## 14.3 Event Forget

例如：

``` text
Forget(history_only, event_ids=[E1])
```

Phase A 会创建 Event Shield / Tombstone。

但不会因为 Event 被 Forget 就必然同步将所有来源 MemoryRecord 的
Lifecycle 改成 forgotten。

因此：

``` text
M1.lifecycle_state = active
M1.validity_state = valid
M1.source_event_id = E1
E1 = blocked
```

是可能存在的状态。

普通业务必须使用 ForgetVisibilityService，而不能只检查 MemoryRecord
状态。

## 14.4 Memory Forget

显式 `memory_ids` Forget：

-   创建 Memory Shield。
-   创建 Memory Tombstone。
-   将指定 MemoryRecord.lifecycle_state 设置为 forgotten。
-   删除对应 Vector Projection。
-   删除 CoreMemoryBlock 或触发相关投影更新。
-   屏蔽其来源 Event。
-   后续 Cascade 清理派生内容。

## 14.5 宽选择器

canonical_key、scope、thread、time_range、all_user_data 等宽选择器不能在
Phase A 无界枚举全部数据。

因此采用：

-   选择器级 Shield。
-   读取侧根据目标元数据即时判断。
-   Cascade 后续分批物化和清理。

这意味着所有读取消费者都必须执行统一可见性检查。

## 14.6 防重抽

`MemoryWriteService._is_reingestion_blocked()` 会检查新 Proposal
的来源。

它解决：

``` text
被遗忘来源 → 再次进入写入链路
```

不解决：

``` text
新来源 → 复用来源已被遗忘的旧 Memory
```

这两个问题必须分开。

## 14.7 当前 Forget 覆盖矩阵

  -----------------------------------------------------------------------
  消费者                  当前实现                审计判断
  ----------------------- ----------------------- -----------------------
  Core Memory build       filter_memory_ids       已有保护

  Core Memory persisted   检查 source_memory_ids  已有保护
  read

  Automatic Recall        统一批量过滤            已有保护

  UnifiedRetriever        回源检查                已有保护

  MemoryReadModel         Context Role 查询       缺少过滤

  GoalDecisionEngine      Active + Valid 查询     缺少过滤
  Memory

  GoalDecisionEngine      Turn / Event 读取       缺少过滤
  Conversation

  GoalDecisionEngine      TaskEvidence 摘要读取   缺少来源闭包
  Evidence

  Personal Signals API    直接 MemoryRecord 查询  缺少过滤

  Memory Timeline         ID / Key 查询           缺少完整过滤

  Memory Event Log        Episodic 查询           缺少过滤

  Graph 当前关系          filter_memory_ids       已有保护

  Graph 历史关系          仅 Lifecycle 判断       缺少完整过滤

  Sealing Bridge Summary  直接读取                缺少过滤

  Sealing Bridge Event    直接读取                缺少过滤

  普通热历史              filter_events           已有保护

  Segment Summary         blocked_target_ids      原缺来源闭包

  Epoch Checkpoint        blocked_target_ids      原缺来源闭包

  Memory Search           UnifiedRetriever        已有保护

  Personal Signals        直接 API                缺少过滤

  Memory Relations API    计算 visible            存在旁路
                          但仍返回正文

  Memory Conflicts API    读取 Conflict Case      需要即时屏蔽
                          Snapshot

  Memory Create 幂等重放  直接读取                需要再次检查可见性
                          final_memory_id
  -----------------------------------------------------------------------

注意：此表是本次已检查代码路径的结果，不代表已经穷举所有
API、Tool、Worker 和内部调用。

------------------------------------------------------------------------

# 15. Forget 后重新设置：正确语义

## 15.1 目标场景

``` text
E1：用户说“以后叫我小李”
  ↓
M1：user.display_name = 小李

Forget E1
  ↓
M1 不再可见
  ↓
Identity Slot = UNSET

E2：用户说“以后叫我阿伟”
  ↓
P2：来源只有 E2
  ↓
Create M2
```

最终：

``` text
M1：被遗忘，不参与正常读取和冲突解析

M2：
  id = 新 UUID
  content = 阿伟
  source = E2
  evidence = E2 的新 Evidence
  provenance = E2 的新来源关系
  lineage = CREATE
```

## 15.2 为什么只创建新 UUID 不够？

假设：

``` text
M1 → E1（已 Forget）
E2 → 新 Proposal
```

如果 M1 参与 Supersede：

``` text
M1 ──SUPERSEDE──> M2
```

M2 虽然有新 UUID，但仍然建立了旧记忆与新记忆之间的谱系关系。

如果 M1 参与 Reinforce：

``` text
M1 + E2 → M1
```

则根本没有新记录。

如果 M1 参与 Merge：

``` text
M1 + M2 → M3
```

还可能继承 E1 的 Provenance。

所以独立性至少包含：

1.  新 Memory ID。
2.  新 Evidence。
3.  新 Provenance。
4.  不继承被遗忘来源。
5.  不继承被遗忘记录的 Graph Facts。
6.  不建立指向被遗忘记录的有效 predecessor。
7.  不复用旧 Candidate。
8.  不唤醒旧 Sleeping。
9.  不 Reinforce 旧记录。
10. 不让旧记录参与 Resolver。

## 15.3 重新设置的操作原则

当某个 Key 的历史记录都因 Forget 而不可见时：

``` text
existing_visible_records = []
```

Resolver 应当看到空集合。

因此：

``` text
operation = create
```

而不是：

-   reinforce
-   revise
-   supersede
-   merge
-   promote

除非另有未被遗忘、可见且业务上有效的独立记录。

------------------------------------------------------------------------

# 16. Identity Slot：身份记忆可选设计（暂缓实施）

> **复核修正：** 本章是产品可选方案，不是 Forget 正确性的前提。`RuntimeIdentity` 已以空字符串/省略字段表达无值。只要所有候选均通过可见性检查，独立合法的 `user.name` 回退不构成泄漏。暂不要求数据库 Slot、Registry 全量 semantic_role 或强制 Prompt 占位符；若将来实施，结构化返回 `value=null`，`[暂未设置]` 仅由展示层生成。重新设置仅在没有可见合法旧目标时创建新记录。

## 16.1 为什么身份记忆需要特殊语义？

以下 Key 不只是普通事实：

-   user.display_name
-   user.name
-   agent.display_name
-   agent.persona.relationship
-   agent.persona.tone

它们会影响 Agent 的稳定自我认知、用户称呼和交互行为。

用户要求遗忘它们时，不仅要阻止读取旧 Memory，还需要让 Agent 知道：

> 这个身份字段目前没有设置。

否则模型可能从历史对话、摘要、其他字段或语义召回中重新推断旧值。

## 16.2 Identity Slot 与 MemoryRecord

建议：

Identity Slot 是逻辑配置槽，不一定需要独立数据库表。

MemoryRecord 是其当前事实值。

``` text
IdentitySlot: user.display_name

状态：
SET
  ↓
MemoryRecord M1
  ↓
“小李”

Forget M1 来源
  ↓
UNSET
  ↓
[暂未设置]
```

若未来采用 Slot，Read Model 用 `state=unset,value=null` 表达，`[暂未设置]` 仅由展示层生成，不是 MemoryRecord 内容。

不能把它写成：

``` text
MemoryRecord.content = "[暂未设置]"
```

否则可能被召回、强化或再次抽取。

## 16.3 建议的 Identity Read Contract

例如：

``` json
{
  "slot": "user.display_name",
  "state": "unset",
  "value": null,
  "source_memory_id": null
}
```

当存在可见有效记录：

``` json
{
  "slot": "user.display_name",
  "state": "set",
  "value": "阿伟",
  "source_memory_id": "M2"
}
```

## 16.4 Identity Slot 规则

建议明确：

1.  每个 Slot 具有独立 Key。
2.  Slot 状态由可见有效 Memory 决定。
3.  Forget 后，如果没有其他可见有效值，则 UNSET。
4.  UNSET 不等于空字符串事实。
5.  UNSET 不写入 MemoryRecord。
6.  不允许未经定义的跨 Slot fallback。
7.  当没有可见有效旧值时，新设置使用新来源创建独立 Memory；存在合法可见目标时仍按正常冲突规则执行。
8.  Identity Slot 不允许从历史摘要或模型推断自动恢复。
9.  Agent 自己的名称和 Persona 也应使用相同机制。
10. Prompt 必须明确告诉模型 UNSET 的含义。

------------------------------------------------------------------------

# 17. 当前实现与理想设计差距

  领域               当前实现                       理想设计                     差距
  ------------------ ------------------------------ ---------------------------- --------
  Key Registry       Key / Pattern / Policy         按需求增量扩展               暂缓
  Proposal           字段较完整                     来源契约完整持久化           中
  Gate               权威和准入                     权威、来源、可见性职责清晰   中
  Resolver           single / multi                 输入集合必须是可见记录       高
  Create             创建新记录                     来源完整且独立               低至中
  Reinforce          原记录追加证据                 只允许强化可见记录           高
  Supersede          新建版本并链接旧记录           不允许链接被遗忘记录         高
  Revise             新建版本                       明确版本和来源边界           中
  Merge              继承所有旧来源                 只合并有效可见来源           高
  Promote            Candidate 原地升级             只允许提升可见 Candidate     高
  Lifecycle          多状态机                       统一访问策略                 中
  Evidence           证据记录                       与来源契约一致               中
  Provenance         Event 闭包与严格关闭           补 Turn 字段及输入策略       中
  Lineage            记录演进                       Forget 后隔离谱系            高
  MemoryReadModel    Key 查询                       强制可见性                   高
  ContextAssembler   多分区组装                     所有路径统一可见性           高
  Core Memory        实时 / 持久投影                单一 Access Contract         低至中
  Vector             HNSW + 回源过滤                统一 Access Contract         低
  Graph              当前关系过滤                   历史关系也完整过滤           高
  Retrieval          多路融合过滤                   全部消费者统一过滤           中
  Forget             Shield / Tombstone / Cascade   所有消费者强制遵守           高
  Identity Slot      依赖 Key 和 ReadModel          可选 SET / UNSET             暂缓

------------------------------------------------------------------------

# 18. 完整操作矩阵

以下为目标操作约束，非逐项已实现现状。Supersede/Revise 的新来源是否独立取决于内容是否实际依赖旧值；Merge 必须保留全部实际来源。具体执行与当前缺口以第 22～23 章为准。

  ----------------------------------------------------------------------------------------------------------------
  操作         旧记录            新记录         Evidence       Provenance             Lineage
  ------------ ----------------- -------------- -------------- ---------------------- ----------------------------
  Create       无                新建 M1        新建           新来源                 CREATE（需核实各分支写入）

  Reinforce    保留并更新        不新建         追加新证据     追加新来源             REINFORCE

  Supersede    Validity →        新建 M2        新建           新来源                 M1 → M2
               superseded

  Revise       旧版本失效        新建修订版本   新建           新来源                 REVISION

  Merge        多条旧记录失效    新建 M3        合法证据       全部实际依赖           多条旧记录 → M3

  Promote      Candidate         不新建         追加证据       追加来源               自身 PROMOTE
               原地升级

  Wake         Sleeping 原地升级 不新建         通常不追加     不应伪造新来源         自身 WAKE

  Sleep        Active 原地变化   不新建         不追加         不追加                 自身 SLEEP

  Archive      原记录归档        不新建         不追加         不追加                 自身 ARCHIVE

  Forget       目标记录不可用    不创建         按 Forget      来源关系保留用于闭包   FORGET / Forget Operation
  Memory                                        清理策略

  Forget Event 旧记录可能仍      不创建         可能被清理     Event 来源仍保留       不应自动生成新版本
               ACTIVE +
               VALID，但不可见

  Forget       旧记录不可参与    必须新建 M2    仅新证据       仅新来源               独立
  后重新设置                                                                          CREATE，不继承被遗忘谱系
  ----------------------------------------------------------------------------------------------------------------

## 18.1 关键矩阵约束

### Create

-   新 UUID。
-   新 Evidence。
-   新 Provenance。
-   不从无关旧记录继承事实来源。

### Reinforce

-   只针对可见有效记录。
-   只追加新证据。
-   同一来源不能重复计数。
-   如果来源已经被 Forget，不能追加。

### Supersede / Revise

-   旧记录必须可见。
-   新记录来源必须真实完整；独立新陈述可只用新来源，依赖旧内容的改写保留该实际依赖。
-   不得将被遗忘记录作为 predecessor。
-   不得继承被遗忘 Evidence。

### Merge

-   所有参与者必须可见。
-   所有参与者来源必须通过完整性校验。
-   只合并有效 Evidence 和 Provenance。
-   任一来源不安全时，不得部分继承。
-   不得通过 Graph Facts 或结构化值重新带入被遗忘内容。

### Promote

-   Candidate 必须可见。
-   Candidate 来源必须可验证。
-   不允许从 Forget 屏蔽状态恢复。

### Forget 后重新设置

-   旧记录完全退出 Resolver。
-   新 Proposal 使用新 Event / Turn 来源。
-   没有其他可见、合法目标时才 Create；Gate 仍可拒绝或将其作为 Candidate。
-   不继承旧 Evidence。
-   不继承旧 Provenance。
-   不继承旧 Lineage。
-   不继承旧 Graph Facts。
-   Identity Slot 从 UNSET 变为 SET。

------------------------------------------------------------------------

# 19. 必须建立的回归测试矩阵

## 19.1 Write 独立性测试

下列 Create 预期限定为：旧目标均不可见、没有其他合法独立目标、新提案通过 Gate；Candidate/拒绝仍遵守 Gate。

  ---------------------------------------------------------------------
  场景                               预期结果
  ---------------------------------- ----------------------------------
  M1 来源 E1，Forget E1，E2 同内容   Create M2

  M1 来源 E1，Forget E1，E2 不同内容 Create M2

  M1 是 Candidate，来源被 Forget     不 Promote M1

  M1 是 Sleeping，来源被 Forget      不 Wake M1

  强制 decision_target_id 指向被屏蔽 返回过期冲突，不能静默换目标
  M1

  多条 Merge 参与者中有一条被屏蔽    整次拒绝，不删来源边后放行

  M1 被 Forget 后重新设置            不建立 M1 → M2 Lineage

  同一 E2 重复提交                   幂等，不重复创建

  E2 是新来源但内容与 M1 一致        仍然独立 Create
  ---------------------------------------------------------------------

## 19.2 Read Visibility 测试

  读取入口                           预期
  ---------------------------------- --------------------------
  MemoryReadModel.resolve_identity   不返回被屏蔽记录
  MemoryReadModel.resolve_policies   不返回被屏蔽记录
  Core Memory                        不注入
  Automatic Recall                   不召回
  UnifiedRetriever                   不召回
  Graph 当前关系                     不注入
  Graph 历史关系                     不注入
  Memory Timeline by ID              不返回内容
  Memory Timeline by Key             不返回被屏蔽版本
  Memory Event Log                   不返回被屏蔽记录
  Personal Signals                   不返回被屏蔽记录
  GoalDecisionEngine Memory          不进入 Prompt
  GoalDecisionEngine Event           不进入 Prompt
  GoalDecisionEngine TaskEvidence    不进入 Prompt
  Sealing Bridge Summary             不注入
  Sealing Bridge Event               不注入
  Memory Relations API               不返回关系正文
  Conflict Case API                  不返回被屏蔽快照
  Idempotency Replay                 不返回被屏蔽 Memory 内容

## 19.3 来源闭包测试

必须覆盖：

-   Event-only。
-   Turn-only。
-   Event + Turn。
-   Event + Evidence。
-   多来源混合。
-   来源 Event 不存在。
-   TurnRecord 不存在。
-   trace_id 被误当 Event ID。
-   来源 Event 被 Forget。
-   来源 Turn 被 Forget。
-   Memory 来源被 Forget。
-   Memory Evidence 已被物理删除，但 Provenance 仍存在。
-   Provenance 为空且不是明确人工创建。
-   Provenance 中存在未知 source_type。

## 19.4 Identity Slot 可选测试（暂不作为交付门槛）

``` text
初始：
user.display_name = 小李
Slot = SET

Forget：
user.display_name 的唯一来源被遗忘
Slot = UNSET
ReadModel = state: unset, value: null（展示层可显示 [暂未设置]）

重新设置：
用户说叫我阿伟
新 Event E2
Create M2
Slot = SET
ReadModel = 阿伟
```

同时测试：

-   `user.name` 不应未经允许填充 display_name。
-   `agent.display_name` 遗忘后也必须 UNSET。
-   Agent Persona 遗忘后不应从历史摘要推断恢复。
-   占位符不能进入 MemoryRecord。
-   占位符不能被 Recall。
-   用户重新设置后必须使用新 Memory ID。

------------------------------------------------------------------------

# 20. 架构收敛建议

应优先修复 MemoryWriteService 的旧候选与目标授权缺口。以下契约用于说明规则，可由现有 Store、Visibility、Policy 组合实现；不要求先完成抽象层重构。

## 20.1 Memory Access Contract

普通读取应组合以下规则；示意图中的 MemoryAccessPolicy 并非必须新增的类：

``` text
MemoryAccessPolicy
    ├── Lifecycle
    ├── Validity
    ├── Forget Visibility
    ├── Scope
    ├── Sensitivity
    └── Read Purpose
```

不同访问目的必须明确区分：

-   Runtime Context
-   Recall
-   API
-   Tool
-   Conflict Resolution
-   Maintenance
-   Forget Cascade
-   Authorized Audit

不能把 Forget 内部清理读取和普通业务读取混成同一规则。

## 20.2 Memory Source Contract

当前以 Event 来源和严格手工证明为基线；下图是可扩展方向，Turn-only 与 External Document 不在本轮强制实施范围：

``` text
MemorySource
    ├── Event
    ├── TurnRecord
    ├── External Document
    └── Explicit Manual Creation
```

每种来源需要：

-   Source Type。
-   Source ID。
-   Source Identity Validation。
-   Source Visibility。
-   Forget Propagation。
-   Reingestion Guard。

## 20.3 Memory Operation Contract

为所有操作定义统一的前置条件和副作用：

``` text
MemoryOperation
    ├── Operation Preconditions
    ├── Allowed Existing Records
    ├── New Record Policy
    ├── Evidence Policy
    ├── Provenance Policy
    ├── Lineage Policy
    ├── Projection Policy
    └── Forget Policy
```

这样可以避免不同入口各自实现相似但不一致的逻辑。

## 20.4 Identity Slot Contract（可选、暂缓）

身份槽应该是 Memory 的特殊消费者，不是另一套独立事实数据库。

其值仍来自 MemoryRecord。

ReadModel 负责：

-   解析 Slot。
-   判断可见性。
-   返回 SET / UNSET。
-   生成稳定提示词表达。
-   禁止不受控的跨 Key fallback。

------------------------------------------------------------------------

# 21. 最终结论

AIive 当前 Memory 的基础设施已经比较完整：

-   有统一 Key Registry。
-   有 Proposal Normalizer。
-   有 Gate 和 Authority。
-   有 Conflict Resolver。
-   有 MemoryWriteService。
-   有 Lifecycle Service。
-   有 Evidence、Provenance 和 Lineage。
-   有 Core Memory、Vector、Graph 和 Retrieval。
-   有 Forget Shield、Tombstone、Cascade 和 Verification。
-   有较完整的自动化测试体系。

但当前系统还没有完全实现一个真正统一的 Memory 访问和演进契约。

后续按第 23 章优先修复具体入口；下面四类规则用于约束实现与验收：

### 1. 一条记忆如何被定义？

事实内容、来源、证据、版本、状态和访问权限必须有明确边界。

### 2. 一条记忆如何被证明？

必须知道它来自哪个 Event、Turn 或独立人工来源；不能把
trace_id、模型推断和真实来源混为一谈。

### 3. 一条记忆如何演进？

Create、Reinforce、Revise、Supersede、Merge、Promote
必须有统一操作契约。

### 4. 一条记忆如何被遗忘？

Forget 不仅要让当前读取失效，还必须确保旧事实不会通过
Evidence、Provenance、Lineage、Graph、Summary、Vector、Proposal、TaskEvidence
或其他派生内容重新进入正常上下文。

**最终原则：被遗忘的事实不能通过任何旧版本、旧证据、旧来源、旧谱系或派生副本重新成为系统可使用的事实。**

当旧事实均不可见时，重新设置以新来源建立事实，不复活旧 Memory；存在其他合法独立目标时仍遵循正常冲突规则。

后续以第 22～24 章的源码复核、范围裁决和验收计划为执行基线，保留已有有效机制，避免将可选设计误当作安全必需条件。

------------------------------------------------------------------------

# 附录 A：核心源码索引

  ---------------------------------------------------------------------------------
  模块                         文件
  ---------------------------- ----------------------------------------------------
  Memory 类型与 Proposal       `backend/aiive/memory/memory_types.py`

  Key Registry                 `backend/aiive/memory/memory_key_registry.py`

  Proposal Normalizer          `backend/aiive/memory/proposal_normalizer.py`

  Gate                         `backend/aiive/memory/memory_gate.py`

  Authority Policy             `backend/aiive/memory/memory_policy.py`

  Conflict Resolver            `backend/aiive/memory/conflict_resolver.py`

  Store                        `backend/aiive/memory/memory_store.py`

  Write Service                `backend/aiive/memory/memory_write_service.py`

  Lifecycle Service            `backend/aiive/memory/memory_lifecycle_service.py`

  Mutation Executor            `backend/aiive/memory/memory_mutation.py`

  Read Model                   `backend/aiive/memory/memory_read_model.py`

  Core Memory                  `backend/aiive/memory/core_memory_projection.py`

  Automatic Recall             `backend/aiive/memory/automatic_recall.py`

  Recall Fusion                `backend/aiive/memory/recall_fusion.py`

  Vector Projection            `backend/aiive/memory/vector_projection.py`

  BM25                         `backend/aiive/memory/bm25_search.py`

  Graph Projection             `backend/aiive/memory/graph_projection.py`

  Graph Expansion              `backend/aiive/memory/graph_expansion.py`

  Forget Visibility            `backend/aiive/forget/visibility_service.py`

  Forget Phase A               `backend/aiive/forget/phase_a_shield.py`

  Forget Selector              `backend/aiive/forget/selector_normalizer.py`

  Conflict Confirmation        `backend/aiive/memory/conflict_confirmation.py`

  Context Assembly             `backend/aiive/runtime/context_assembler.py`

  Goal Decision                `backend/aiive/task_runtime/goal_decision.py`

  Personal Signals             `backend/aiive/api/routes_personal.py`

  Memory APIs                  `backend/aiive/api/routes_memories.py`

  Memory Recall Tools          `backend/aiive/tools/builtin_memory_recall.py`

  Memory Write Tool            `backend/aiive/tools/builtin_memory_write.py`

  DB Models                    `backend/aiive/db/models.py`

  Forget DB Models             `backend/aiive/db/forget_models.py`
  ---------------------------------------------------------------------------------

# 附录 B：主要测试索引

  -------------------------------------------------------------------------------------------
  测试文件                                                           关注点
  ------------------------------------------------------------------ ------------------------
  `tests/unit/backend/test_memory_write_service.py`                  基本写入与冲突操作

  `tests/unit/backend/test_memory_read_model.py`                     Read Model

  `tests/unit/backend/test_identity_memory.py`                       身份记忆

  `tests/integration/backend/test_forget_write_privacy.py`           Forget × Write 防重抽

  `tests/integration/backend/test_forget_v6261.py`                   来源闭包与派生投影

  `tests/integration/backend/test_forget_content_coverage.py`        Forget 内容覆盖

  `tests/integration/backend/test_memory_conflict_confirmation.py`   冲突确认

  `tests/integration/pg/test_pg_forget_write_concurrency.py`         PostgreSQL 并发锁
  -------------------------------------------------------------------------------------------

------------------------------------------------------------------------

**审计结束。**

本报告记录的是指定 Commit 的静态代码审计结论。后续若 `main`
已前进，应先对比 Commit 和相关文件差异，再将本报告作为当前实现基线使用。

------------------------------------------------------------------------

# 22. 源码复核：哪些值得做，哪些应调整或取消

复核日期：2026-11-07。当前 HEAD 与原报告基线相同，均为 `2d3233d2388019505131aa2ac17d79036151fb11`；下列差异来自重新核查，不是版本漂移。源码路径均相对于仓库根目录；行号对应此基线。

## 22.1 原 D01～D14 逐项裁决

P0 表示直接影响遗忘承诺、应优先修复的缺陷；P1 表示确定性、来源完整性、并发验证和维护性增量；暂缓项不进入开发队列。“有已有实现”不代表“该项全部通过验收”。本轮只修改报告，不将待修缺陷标为完成。

| 原项 | 复核裁决 | 新工作包 | 依据与范围修正 |
| --- | --- | --- | --- |
| D01 统一访问边界 | 值得收敛，但收缩；从 P0 大重构降为 P1 增量 | P1-B | 已有 Store、Visibility、Policy；新增通用 Domain→Repository→Policy 层未证明比扩展已有边界更有效，不阻塞补洞 |
| D02 Identity / Policy | 保留 P0 | P0-A | ReadModel 只处理敏感度；Store 的 context-role 查询只有 ACTIVE+VALID，没有 Forget 过滤 |
| D03 写入旧候选 | 保留 P0 | P0-B | `_load_existing_or_wake_sleeping()` 直接采用 Store 返回值；防重抽只保护新提案来源 |
| D04 Goal 上下文 | 保留 P0，扩展检查范围 | P0-C | Memory、Conversation、TaskEvidence 确有缺口；还须检查 plan/step/trigger 等直接进入 prompt 的字段 |
| D05 API / Tool / 重放 | 保留 P0 | P0-D | Personal Signals、Timeline、Event Log、Relations、Conflict Snapshot、重放均有未授权正文路径 |
| D06 Sealing / Graph 历史 | 保留 P0，保留历史专用状态规则 | P0-A | `_attach_history()` 不能直接复用“仅 ACTIVE+VALID”，否则合法版本历史全部消失 |
| D07 Event / Turn | 拆分：字段修复 P1；纯 Turn 支持暂缓 | P1-A | DB 已有两种 Turn 字段，无需为字段补迁移；纯 Turn 缺 Event 时通常被 fail-closed 隐藏，并非已证实的读取放行 |
| D08 三种关系职责 | 值得做，合并到实际操作修复 | P0-B / P1-A | 增加操作规则和测试即可；不另建关系框架，不因区分职责而删除现有来源边 |
| D09 操作独立性 | 保留 P0，与 D03 合并 | P0-B | Forced target、直接 Wake/Promote 和 Resolver Promote 都要校验；Merge 不可选择性丢弃来源后继续使用旧合成内容 |
| D10 Identity Slot | 暂缓，取消强制实现 | 可选需求 | 无值已有表示；无证据证明必须增加 SET/UNSET 模型、semantic_role 或稳定 Prompt 占位符 |
| D11 访问决策 | 收缩为状态矩阵与已有策略组合，P1 | P1-B | `MemoryPolicyEngine` 已集中处理敏感度；Scope 已有解析器，不能新造另一份权限规则 |
| D12 投影统一 | 保留增量 P1，取消重写已有保护 | P1-C | Core build/load、Graph refresh、召回回源、Worker Guard 已有实现；重点补 repair 入口及状态/敏感度新鲜性 |
| D13 防旁路测试 | 保留，但缩窄静态检查，P1；各漏洞回归随 P0 交付 | P1-B | 仅扫描 `db.query(MemoryRecord)` 会漏 `get`、`select`、别名、快照和原生 SQL；全库 ORM 禁令成本高且误报多 |
| D14 并发快照 | 保留增量 P1 | P1-C | 已有 READ COMMITTED 校验、共享/排他锁及 PG 测试；增加读取发送边界与直接动作覆盖，不从零搭建协议 |

## 22.2 原分析中需要纠正的判断

| 原判断或隐含结论 | 纠正后的判断 | 源码证据 |
| --- | --- | --- |
| 必须先完成 AccessContext / Repository 全迁移再修入口 | 不成立。可在已有 Store、服务入口补批量过滤，立即获得安全收益；抽象应在调用共性出现后收敛 | `memory/memory_store.py:202`、`:378` 已有按角色读取和维护可见性边界 |
| Turn 来源缺失等于 Forget 防护缺失 | 混淆写入防重抽、持久来源和读取可用性。Turn 防重抽已实现；Turn-only 持久记录反而可能被隐藏 | `forget/visibility_service.py:372`、`:390`、`:418`、`:740`；`memory/memory_write_service.py:1219` |
| 任意 Event 遗忘都应让所有共享来源 Memory 失效 | 必须区分模式。memory_only 不应反射到保留的兄弟 Memory；history_only/everywhere 的来源闭包仍要保护 | `visibility_service.py:431` 的 `_ignore_memory_only=True`；`test_forget_v6261.py:72` |
| Merge 只继承可见 Evidence / Provenance 即可 | 若正文已依赖不可见输入，删边会洗掉来源。应整次拒绝/过期；另起新提案从合法输入重新生成内容 | `_execute_merge():1110` 保留所有参与者 Event 来源是正确机制；`tests/integration/backend/test_forget_v6261.py:431` 验证后续任一来源屏蔽即不可见 |
| Supersede/Revise 都必须无条件“来源独立” | 应区分新事实替换与依赖旧内容的改写。完全独立的新陈述可只用 E2；改写若使用旧文本/结构值，须保留实际依赖。Lineage 不自动等于来源边 | `memory_write_service.py:1013`、`:1102`；维护 clone 的来源已有专门实现与测试 `test_forget_v6261.py:408` |
| Forget 后重新设置一律强制 Create | 仅当没有可见合法候选且 Gate 接受时成立。合法独立的同 Key 记录可正常参与；显式失效 target 应报冲突，不暗中换目标 | `_load_existing_or_wake_sleeping():237`、`_execute_forced_decision():858` |
| `user.name` fallback 会恢复已遗忘称呼，所以必须删除 | 只能说未过滤的 fallback 可能泄漏；若该 name 是独立合法来源，允许回退属于产品语义。应先过滤，再显式定义优先级 | `memory_read_model.py:96` 附近；`memory_store.py:230` 返回更新时间倒序，但循环仍覆盖字段，存在确定性问题 |
| Graph 历史和当前关系应使用完全相同状态规则 | 共享 Forget、Scope、敏感度检查，状态矩阵必须不同：历史可以展示 superseded 的合法旧事实，标为非当前 | `graph_expansion.py:245` 与 `:378`；后者仅排除 forgotten，缺少其余检查 |
| 缺少统一访问决策，需新建一套敏感度/Scope 权限引擎 | 集成已有策略即可；新接口可以组合规则，不复制规则。Forget 拒绝应先于正文脱敏，不能用占位符掩盖其余字段泄漏 | `memory_policy.py:104`、`:125`；`memory/scope_resolver.py` |
| 未来所有旁路都能由 ORM AST 白名单发现 | 这是无法保证的验收目标。静态扫描只是补充，必须靠真实 API、Tool、Prompt 和操作结果测试 | 旁路不仅是 ORM 查询，还包括 Conflict Snapshot、GraphEntity aliases、TaskEvidence summary 和幂等 Proposal 副本 |
| 本章所有机制都未完成 | 不准确。核心保护和并发测试已存在，剩余缺口未验收。应按入口列“已有/缺失/本轮验证” | `test_forget_v6261.py`、`test_forget_write_privacy.py`、`tests/integration/pg/test_pg_forget_write_concurrency.py` |

## 22.3 本轮新增发现与边界

1. **写入 Store 的方法名与实际条件不符。** `memory_store.py:142` 声称 active+valid+candidates，但 SQL 未筛 Validity；sleeping 查询也未筛。真实 ORM 复现确认 superseded 的 active 记录仍在候选集合。Resolver `conflict_resolver.py:63` 会再次排除非法 active，故不能据此断言普通路径必然重用；candidate 分组没有同等 Validity 检查，forced target 和提前 wake 也不能依赖该防线。纳入 P0-B。
2. **加锁失败降级有风险。** `memory_store.py:172`、`:198` 捕获任意异常后重试非锁定查询；PG 事务失败时还可能已经无法继续。后续应使生产 PG 锁错误终止事务，不以“兼容 SQLite”为理由降级。纳入 P0-B。
3. **Runtime 字段选择不确定。** Store 按 `updated_at DESC`，ReadModel 循环赋值可能让较旧同 Key 值覆盖较新值；response_style 还同时来自 tone 与 user preference。属于确定性/优先级问题，不应夸大成单独的已证实 Forget 泄漏。纳入 P1-B。
4. **敏感度保护有字段旁路。** Personal Signals 直接返回 content；Timeline 的 Evidence span 未经渲染；Relations 只对 source_span 脱敏，entity 名称、aliases、subject/object 仍可能暴露。纳入 P0-D。
5. **投影存在增量检查点。** Core 持久读取只验证来源可见性，不能据此保证状态或 sensitivity 刚变更时内容也新鲜；Graph 的 `repair_overclosed()` 可直接 reopen，未显式调用可见性检查。完整 rebuild 随后会经过受保护的 refresh，故不能把 repair 的局部风险描述为整个重建流程完全无保护。纳入 P1-C。
6. **Goal 风险不止三张表。** context() 也直接放入 plan summary、step note、trigger event_payload。已确认这些字段进入 prompt；是否含遗忘内容取决于实际来源，是需要追踪的风险，不是本轮已逐一复现的泄漏。纳入 P0-C。

------------------------------------------------------------------------

# 23. 修订后的执行计划（只实施有证据和收益的部分）

## 23.1 顺序与共用约束

建议顺序：P0-A → P0-B → P0-C → P0-D → P1-A → P1-B → P1-C → P1-D。P0-C/P0-D 不依赖 P1 的重构或来源类型扩展，可在 P0-A/B 后分别交付。每包以漏洞回归和既有合法行为共同验收，不能只验证“全部返回空”。

所有读取共用已有 `ForgetVisibilityService.blocked_target_ids/filter_memory_ids`，注意前者返回**不可见 ID**，后者返回**可见 ID**。先过滤再序列化，再按现有 Policy 的通道规则渲染；Scope 使用服务端上下文构造。过滤后的分页使用有界分批取数、稳定游标及扫描预算，不能先 SQL limit 再简单删行导致第一页全被屏蔽时永远看不到后面的合法数据。

普通读取、合法历史、写入候选和 Cascade 的状态要求不同；不对整个 Session 施加不可绕过的全局 ACTIVE+VALID 条件。Cascade/内部修复需要读原始行，但不能因此允许普通调用者传 `audit=True` 或 `purpose=AUDIT` 即读取遗忘正文。审计能力须由服务端授权，并满足所有相关 Tombstone 的审计与未清理条件。

以下为修复设计基线；当前实施与测试状态见 24.4。计划文字不代表代码或并发验收已经通过。

## 23.2 P0-A：补齐 Runtime、Sealing Bridge、Graph 历史读取

**映射：D02、D06；无需数据库迁移。**

执行步骤：

1. 在 `MemoryStore` 增加明确的安全 context-role 读取入口，复用 `_ACTIVE_VALID` 与批量 `filter_memory_ids`；原始入口仅保留给明确的内部场景并命名说明。让 `MemoryReadModel.resolve_identity/resolve_policies` 使用安全入口，再继续调用现有 `MemoryPolicyEngine`。不要只修身份、漏掉 Policy。
2. `_load_sealing_bridge()` 的 summary 路径先用既有 summary 可见性规则验证对象及来源，再取 goal/outcome；raw tail 路径对 manifest 中 Event 批量授权，并覆盖 Turn 屏蔽语义。任一混合来源 summary 不可信时丢弃整个 summary；只有可独立验证的 bounded raw tail 才能回退。
3. Graph `_attach_history()` 批量加载旧关系的来源 Memory，执行 Forget、Scope、敏感度检查后再加载/输出实体内容。将调用链的 ScopeContext 显式传到历史分支；复用当前关系的基础校验函数，传入历史用途的合法状态集合。
4. 保留合法 superseded 历史及 `is_current=False`。不要简单调用当前分支的 ACTIVE+VALID 条件来“修好”历史。

验收：来源 Event-only Shield 生效但 Memory 仍 ACTIVE 时，identity、policy、summary bridge、raw bridge 和 history 全都不含标记秘密；实体级及选择器级 Shield、查询异常也关闭。对照组验证合法身份、Policy、bounded bridge 和 superseded 历史仍可用；Scope 外历史不可见，secret 不进入 LLM。

测试位置：扩展 `test_memory_read_model.py`、`test_memory_graph.py`、`test_context_assembler_graph_expansion.py`；增加 Sealing Bridge 真 ORM 测试，覆盖 summary/raw 两条分支。

## 23.3 P0-B：旧目标授权、状态前置条件与事务闭环

**映射：D03、D09、D08 操作部分；无需数据库迁移。**

执行步骤：

1. 修正 Store active/candidate/sleeping 查询的 Validity 契约（通常 valid；旧 NULL 兼容需与 Resolver 现有逻辑统一），保留 key+scope 和稳定排序。在持有 Forget 共享事务锁、key 锁、目标行锁后批量过滤候选；无可见 active/candidate 才尝试可见合法 sleeping，不能先 wake 再过滤。
2. write 与 write_batch 共用这一逻辑。Resolver 不负责访问数据库，但其输入必须为可用候选；可加纯状态断言，避免把安全责任转给 Resolver。
3. 强制决策的 target_id 重新加载并加锁，检查 key、scope、生命周期、Validity、Forget 可见性及确认时版本；显式目标失效返回稳定冲突/过期结果，禁止回退到列表第一条。未显式 target 的普通提案可在安全候选为空时走 Create。
4. 给直接 `MemoryLifecycleService.wake/promote` 补共享 Forget 锁与来源校验，统一锁顺序；同时覆盖 `_execute_promote_candidate`。已有 `apply_maintenance_action` 保留其来源和版本前置校验，不重复建设。
5. 在写入操作发生状态、Evidence、Lineage、Graph Facts、Outbox 变更之前做整组前置检查；Merge 任何参与者或依赖不可用就整次拒绝，不删边后继续。Supersede/Revise 根据实际内容依赖决定来源，不能仅因新建 UUID 就宣称独立。
6. PG 加锁失败直接上抛/回滚；SQLite 使用其已有方言行为，不捕获生产异常后降级。写路径保持锁序：Forget shared → 按序 key locks → 按序 row locks；与现有维护/Cascade 检查死锁关系。
7. 验证无目标时的 Create 只使用 E2 的内容、结构值、Evidence、Provenance 和 Graph Facts，无指向 M1 的新 Lineage。M1 的历史不需要为此删除；新事实也不能继承 E1。

验收矩阵：M1 分别为 active/candidate/sleeping；E1 被 Shield 或 Tombstone 屏蔽；E2 独立且合法；相同/不同内容；single/batch；普通/forced；直接 wake/promote。断言 M1 版本和状态不变，失败路径不新增副本或 Outbox。普通合法重新设置可创建 M2；显式失效 target 报冲突。再覆盖一个仍有合法 M3 的场景，防止把所有情况强制 Create。

测试位置：扩展 `test_forget_write_privacy.py`（现有测试多检查新来源被拦，必须另加旧来源被拦、E2 正常的用例）、`test_memory_write_service.py`、`test_phase4_lifecycle.py`；锁失败与直接动作并发在 P1-C 增加 PG 验证。

## 23.4 P0-C：GoalDecisionEngine 的完整 Prompt 来源过滤

**映射：D04；优先复用已有来源解析，不预先设计新通用表。**

执行步骤：

1. 列出 `GoalDecisionEngine.context/messages` 的全部文本入口：Memory、Turn/Event、TaskEvidence、task title、plan summary/acceptance、step note、trigger payload。记录每项可解析的 source ID 与当前生产者，不能只按 task.created_at 判断所有派生文本。
2. `_memories()` 复用 scope、ACTIVE+VALID、Forget 与 LLM_CONTEXT 敏感度规则；`_conversation()` 在取正文前过滤 Turn/Event。保留当前 turn.source 规则，禁止把系统触发文本伪装成用户话语。
3. TaskEvidence 先追踪 run/action/inline_payload 及现有持久来源关系，按已有来源解析能力授权。没有来源就不能仅用 summary 假定合法；旧混合摘要不能通过删除单个 ID 保留全文。来源无法证明时从 LLM context 移除，诊断仅记录 ID 与原因码。
4. 对计划、步骤、trigger 的派生内容采用相同来源原则；独立系统配置可单独登记来源类型和允许依据，不能将所有旧记录统一认作可信手工输入。若确需新来源持久字段，先给生产者列迁移清单，再独立做增量迁移。
5. 过滤后证据不足时返回明确的信息不足/等待新证据结果，不能把隐藏证据当作任务成功依据。消息构建保留来源清单，供 P1-C 的发送前检查使用。

验收：用唯一秘密标记分别放入六类输入，Forget 后捕获**最终发给假 LLM 的 messages**，断言 marker 不存在、也不产生依赖该证据的动作。覆盖合法独立证据对照组、查询失败、旧快照和 trigger payload。复用现有 `forget/content_scope.py` 与 `worker/job_source_guard.py` 的来源规则，不宣称它们已经保护所有直接 Goal 读取。

测试位置：新增 Goal Forget 集成测试，复用 `test_goal_decision_serialization.py` 校验既有序列化兼容；外部模型使用本地 fake，实际执行路径不要把 Visibility 全部 mock 掉。

## 23.5 P0-D：API / Tool / Conflict Snapshot / 幂等响应

**映射：D05，并补齐现有敏感度字段旁路；通常无需数据库迁移。**

执行步骤：

1. `api/routes_personal.py:list_personal_signals` 先明确页面所需状态集合，排除 forgotten 与来源被屏蔽行，再按 API 通道渲染。历史管理页可返回合法历史状态，不能为了统一而随意只留 active。
2. `builtin_memory_recall.py:handle_memory_timeline` 的 ID 和 key 两条分支都加入 Scope 与 Forget 检查；revisions 和总数仅统计授权行。Evidence span 同时受来源和父记录敏感度约束。历史对象可合法存在，但 current 仍须 ACTIVE+VALID。`handle_memory_event_log` 保留既有 scope chain 并补 Forget 过滤。
3. Relations 在不可见时直接返回约定的无正文响应（建议与不存在一致），不能只设置 `visible=false`。可见但 secret 的记录还须处理实体名、aliases、subject/object/source_span；peer 冲突标记也只基于授权同 scope 数据计算，防止通过冲突标记泄露隐藏记录。
4. Conflict Cases 列表与确认执行都重新验证 old_memory、new proposal sources、resolved_memory 与版本；question、verdict、old/new_content、decision 都是派生快照，不是独立合法来源。来源失效时整条隐藏或仅返回明确约定的无正文过期状态；屏蔽并不等于删除内部操作历史。
5. create_memory 的所有幂等分支都重新授权：包括 final_memory_id，以及无 final_memory_id 但回放 Proposal.content/request.content 的 ignore 分支。隐藏结果返回无内容 409 等稳定语义；原幂等键不产生新写入。用户用新来源重新设置使用新请求身份，不能沿旧重放复活内容。
6. 对列表采用过滤后有界补页，明确分页游标和扫描上限；禁止对异常 fail-open。

验收：实际 API/Tool 结果整体序列化后搜索秘密标记，覆盖正文、实体别名、证据、快照和重放 reason；同时验证拒绝请求零新记录、零来源边、零 Outbox。状态标记、布尔 visible、敏感度占位不能替代无正文断言。测试合法历史可读、跨 scope 不可读、secret 按现有通道契约脱敏，避免兼容性回归。

测试位置：扩展 API/Tool 集成、`test_memory_conflict_confirmation.py`、`test_memory_sensitivity_paths.py`，并补 personal/relations/replay 的真实 route 测试。

## 23.6 P1-A：修正来源持久化，明确 Turn-only 策略

**映射：D07、D08；默认不新增来源类型。**

1. `_persist_proposal()` 同时持久化 `source_turn_id` 与 `source_turn_record_id`，验证分别为业务 ID 与 DB PK，若两者同时存在则必须对应同一 Turn。DB 已有字段，无需新增列。
2. normalizer、write/write_batch 与 RunContext 的来源合并明确所有实际依赖；Evidence 的 Event 来源与提案来源求并集，按当前持久 Provenance 规则写入。来源列表不代表所有来源都能参与 authority 加权，继续使用现有 relation/authority 策略。
3. 先做生产者盘点。若纯 Turn 并非正式支持的输入：在写前返回 `source_unresolved` 等稳定原因，禁止写成功后读取时才隐藏；若有对应 Event 则规范化成可验证的 Event 来源，不能凭空生成事件 ID。
4. 只有确认需要“无 Event 的 Turn 也是正式事实来源”时，另立迁移：writer、Visibility（目前非 event ref 会关闭）、Phase A/Cascade、source scrub、verifier、Job Guard、回填与备份恢复共同支持 `turn_record`。不能把此扩展夹进字段补写的小修复。
5. 整理操作表：Create 使用新依赖；Reinforce 追加合法证据/来源；Revise 区分独立替换和旧内容改写；Merge 保留所有真实依赖；Promote 不改变原事实来源；Lineage 记录演进但不自动赋予来源或读取权限。
6. 旧数据 dry-run 统计缺 Turn 字段、孤立 ref、缺 Event 和 manual proof。只从能确证对应关系的数据回填，不把历史数据批量标成 manual 来绕过检查。

验收：两种 ID round-trip 正确；异常 ID/错配来源被拒；已 Forget Turn 继续被防重抽拦截；合法手工无 Event 创建仍可读；Evidence 清理后持久来源闭包仍生效；memory_only 兄弟 Memory 例外保留。来源模型扩展若暂缓，文档明确“不支持 Turn-only”，不标成完整支持。

## 23.7 P1-B：小范围访问边界、确定性与防回归

**映射：D01、D11、D13；在 P0 修复后收敛，不阻塞安全交付。**

1. 从 P0 已修调用点提取少量安全方法，优先扩展现有 Store/服务；新增方法必须有实际消费者。批量授权、Scope、状态筛选与敏感度渲染可组合，但仍分别维护于已有模块。
2. 固化用途矩阵：Runtime/Recall 通常 active+valid；历史允许合法 superseded；候选允许有效 active/candidate；wake 只允许有效 sleeping；维护动作按前置条件；Cascade 可读原始数据。所有普通用途均不能绕过 Forget。
3. Identity 按每 Key 选择最新合法值，增加稳定 tie-break（例如 updated_at、id）；明确 user display_name 对 name 的优先级，以及 response_style 的用户偏好/agent tone 优先级。保留回退兼容，除非产品明确更改。空值继续沿现有契约，不引入强制占位文本。
4. 盘点 API/Tool/Runtime/Goal 的受保护入口，建立检查清单：对象、来源、Scope、状态、敏感度、快照、测试名。内部原始访问注明理由，不能由调用者任意选择审计权限。
5. CI 先约束已迁移的目录/模块：禁止新增直读受保护内容的已知模式；覆盖 query/get/select 与别名的常见写法，异常名单精确到用途。另加一条真实新增旁路的扫描器自测。不要宣称扫描可证明所有数据流安全。
6. P0 的真实 ORM/API/Prompt 回归是必需防线；AST、import 边界和扫描报告是补充。扫描只报存量清单的变化，避免一次性把整库内部维护代码禁掉。

验收：两个消费者在相同用途和 Scope 下结论一致；历史与 Runtime 在不同用途下可有明确差异；最新值和优先级稳定；扫描能抓到已承诺覆盖的新增绕行，同时 Cascade 和内部迁移正常运行。无新增重复敏感度/Forget 规则库。

## 23.8 P1-C：投影修补、发送边界与 PostgreSQL 并发

**映射：D12、D14；已有保护保留。**

1. 保留 Core `build_blocks/load_core_memory` 的来源回查、Graph refresh 的版本与可见性检查、Vector/Retrieval 回源以及 Job Guard。以已有测试为基线，逐个列出缺口，避免重写所有 projection builder。
2. 对 `GraphRebuildService.repair_overclosed()` 的独立入口加来源检查，禁止 reopen 来源不可见的关系；对 rebuild 的 repair→refresh 组合验证最终状态。历史读取另由 P0-A 保证，不能靠异步清理阻止即时泄漏。
3. Core 持久块增加状态/敏感度新鲜性判定：若来源状态、有效性或敏感度改变，就从权威记录重建。优先采用读取时批量比对；确有需要再持久化 source version/fingerprint，并单独提交可回滚迁移。不能仅以 Forget 未屏蔽推断投影正文仍可用。
4. 写明读取发送语义：本轮建议以“模型发送前最后一次授权通过”为线性化边界。Context Snapshot 保留所有文本依赖 ID；来源失效则重建或放弃本次调用。在检查之后才提交的 Forget 不追溯撤回已发送请求；更强语义需单独产品决策。不要在整个 LLM 网络请求期间持有数据库锁。
5. 普通新读取不得复用上个事务的可见性 memo；PG 已要求 READ COMMITTED，不另造 REPEATABLE READ 快照协议。Worker 在副作用执行前继续做实际来源检查，而非仅依赖入队时检查。
6. 扩展既有 PG 文件，使用两会话与 barrier/锁等待观测：Forget commit 后新读取；writer 在 Forget 后候选检查；直接 wake/promote 与 Forget；上下文构建后、发送前提交 Forget；投影过期任务和回滚释放锁。使用唯一测试实体和实际 PG 隔离，不能用 SQLite 的 no-op 锁作验收。

验收：新读取看不到已提交 Shield；发送前已失效的 snapshot 不进入模型；不安全来源零 embedding/LLM/索引副作用；合法旧历史仍能展示；既有写入/Forget 并发测试不回归。PG 环境未运行时标“未验证”，不能因为默认跳过显示绿色就标完成。

## 23.9 P1-D：验收收口与旧数据检查

**映射：跨工作包验收，不另建架构框架。**

1. 对 Memory、Evidence、Provenance、Lineage、Conflict Snapshot、Core 和 Graph 产出只读一致性报告，只含 ID、计数、原因码。特别核对 source 缺失、非法 candidate、重复 single Key、跨 scope、已屏蔽来源的残留投影。
2. 用 P0 新回归与第 24 章现有测试运行定向套件；再运行仓库标准离线全套。PG 专项单列执行/跳过原因，不混算。
3. 涉及存量回填时先 dry-run 并备份，按可验证来源幂等补齐。无法证明的数据保持关闭/隔离，禁止猜测来源或清空 ref 后放行。
4. 更新每个入口及计划状态，记录修改文件、迁移、实际命令、测试结果和剩余限制。普通诊断不保存被遗忘正文，生产日志只记录拒绝原因和对象 ID。

交付完成条件：所有 P0 均有失败于修复前、通过于修复后的真实入口回归；独立来源、合法历史、manual creation、memory_only 例外和无副作用断言通过。P1 的完成状态按实际证据单独维护，不要求为结束 P0 而实施可选 Identity Slot。

------------------------------------------------------------------------

# 24. 本轮证据、未验证项与后续约束

## 24.1 实际运行的测试

在仓库根目录使用现有 `.venv`，没有安装依赖、部署数据库或调用外部模型：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/backend/test_forget_v6261.py tests/integration/backend/test_forget_write_privacy.py tests/unit/backend/test_memory_read_model.py tests/unit/backend/test_memory_graph.py -q -o addopts= --disable-warnings
```

结果：**86 passed，1 warning，10.57 秒**。这些用例证明已有 Event 来源闭包、防重抽、投影/维护 guard、手工创建例外、Merge 全来源继承等行为在本地测试环境成立；不能据此证明原报告指出的未覆盖入口安全。该 warning 未在本轮展开诊断，不将其解释为无风险。

另外使用仓库 SQLite fixture 和真实 ORM 路径执行四项临时诊断：**4 passed，3.36 秒**。这些断言刻意确认当前缺陷存在，后续修复应将其反转为保护性回归，不能将“复现通过”标成“缺陷修复通过”。诊断过程先修正了测试脚本的 RunContext import 和构造参数，最终四项均执行成功；临时文件已移除，未加入永久 CI。

| 诊断 | 构造与调用 | 实际观察 |
| --- | --- | --- |
| Runtime Identity 来源泄漏 | 建真实 Event→Memory ref，设 `user.display_name`，仅对 Event 执行 history_only Phase A；调用 resolve_identity | 仍返回该 Memory 的正文 |
| 不可见旧记录进入写路径 | 同上，旧记录 ACTIVE；新建无旧来源的 Proposal，调用 `_load_existing_or_wake_sleeping` | 返回集合仍包含旧 Memory ID（验证候选缺口，不声称已端到端完成错误写入） |
| 直接 Wake 缺前置授权 | 旧 Memory 先设 sleeping，对来源 Event 屏蔽，直接调用 lifecycle.wake | 返回 True，状态变为 active |
| Store Validity 契约错误 | Memory 设 active+superseded，调用 get_active_by_key_scope_locked | 仍返回该 ID；Resolver 普通 active 分组有二次过滤，风险边界见 22.3 |

复现采用 `tests/integration/backend/test_forget_v6261.py` 的 `source/memory/shield` 构造函数；其中 shield 对真实 Phase A 提交，不依赖假 Visibility 返回值。复现 Memory 使用合法 Event 来源边，避免把“无来源 fixture 被 fail-closed”误当作 Forget 效果。

## 24.2 证据等级与未验证项

- **运行确认：** 上述 86 项既有测试与 4 项诊断；SQLite 不提供 PostgreSQL advisory lock 语义。
- **静态确认：** API、Goal、Sealing、Graph history、Turn persistence 的调用链及缺失检查。尚未逐个执行其漏洞复现，不能描述成所有端点均已实测泄漏。
- **设计建议：** 发送前授权边界、访问用途矩阵、旧数据处理和来源扩展策略，须在实施时以具体代码与测试验证。
- **未执行：** 全量套件、PG 专项、真实数据库存量统计、性能测试、生产部署；没有证据证明现网已经出现数据泄漏。

核心定位：`memory/memory_read_model.py:83`；`memory/memory_store.py:142`、`:179`、`:202`；`memory/memory_write_service.py:237`、`:809`、`:881`、`:1102`、`:1219`；`memory/memory_lifecycle_service.py:46`、`:236`、`:264`；`forget/visibility_service.py:372`；`task_runtime/goal_decision.py:118`；`runtime/context_assembler.py:1030`；`memory/graph_expansion.py:378`；`api/routes_memories.py:114`、`:226`、`:296`；`api/routes_personal.py:22`；`tools/builtin_memory_recall.py:193`、`:293`；`memory/core_memory_projection.py:115`；`memory/graph_rebuild.py:89`；`forget/concurrency.py:24`。均位于 `backend/aiive/`。

## 24.3 暂不投入的工作

不安排完整 Identity Slot 状态机、所有 Registry Key 的 semantic_role 分类、外部文档来源体系、全库 ORM 一刀切白名单、第二套敏感度/Scope 引擎，也不将统一 Repository 全迁移作为修漏洞的前置条件。它们尚无与当前成本相称的需求或安全收益证据；将来若有明确消费者和验收目标，再单独立项。

保留现有 Lineage 历史不等于允许读取遗忘正文；新增事实也不因存在同 Key 历史就必须绑定旧谱系。安全修复优先保证“不可用的来源不再被消费”，而非用大量新抽象取代已经有效的机制。

原审计轮交付为文档复核。用户随后授权按优先级实施修复，实施记录与验证状态如下；不再将下述已修改代码描述为“未实施”。


## 24.4 修复实施记录（2026-11-07）

按用户要求，先集中完成代码改动再统一测试，未逐功能运行全量套件。没有修改数据库 Schema，新增来源记录复用 ContentProvenanceRef；没有自动回填无法证明的旧来源或清理生产数据。

| 工作包 | 已实施内容 | 验证状态 |
| --- | --- | --- |
| P0-A | Identity/Policy 来源与 Scope 过滤、确定性选值；Sealing 两条分支过滤；Graph 历史及敏感度过滤 | 代码已实现；最终相关离线回归通过 |
| P0-B | 旧 active/candidate/sleeping 筛选；直接 Wake/Promote 授权；forced target 重新加锁；执行器前置检查；锁失败不降级；请求幂等结果不可见时也禁止重写 | 代码已实现；最终相关离线回归通过 |
| P0-C | Goal Memory/Turn/Event 过滤；TaskEvidence 持久来源与读前复核；未知证据不进入模型；trigger 引用检查；发送前 Task/证据/来源及 Forget watermark 复核 | 代码已实现；最终相关离线回归通过 |
| P0-D | Personal、Timeline、Event Log、Relations、Conflict Case、幂等重放保护；Evidence span 与实体派生字段保护；普通列表有界补页 | 代码已实现；最终相关离线回归通过 |
| P1-A | 两类 Turn 字段落库与可解析 ID 归一化；Turn-only 解析为实际 Event，否则拒绝；保留实际 Merge 依赖；Evidence 来源参加既有 Cascade 范围发现 | 代码已实现；最终相关离线回归通过 |
| P1-B | 复用 Store/Visibility/Policy/Scope；身份优先级；只增少量批量读取与持久来源工具；29 个现存直读模块的增量 AST 基线 | 代码已实现；最终相关离线回归通过 |
| P1-C | Core 持久正文与权威重建比对；Graph repair 校验；前台/Goal 模型发送前检测 Forget 修订变化；补 PG 快照/直接 Wake 回归 | 离线相关回归通过；PG 并发验收未完成 |
| P1-D | 只读一致性检查函数，输出 ID/原因；新回归、旧来源 fixture 修正；测试命令与环境限制记录 | 代码已实现；最终相关离线回归通过 |

访问用途规则：Runtime 为可见有效记录；历史允许合法 superseded；写候选为有效 active/candidate；Wake 为有效 sleeping；内部 Cascade 保留原始访问。API 所有者管理视图可显示合法历史状态，LLM 不接收 secret。单独的 Direct Relations 在内容不可输出时返回无正文 404，避免实体字段穿透。

上下文并发规则：Snapshot 在组装开始记录追加式 ForgetOperation 数量，模型调用前比较。新 Forget 提交则保守地使整个在途 Snapshot 失效（包括与本次内容无关的 Forget）；前台停止本轮，Goal 返回 no_action。最后一次检查之后才提交的 Forget 不追溯撤销已发送请求。无数据库锁跨越模型网络调用。此 watermark 不替代逐来源授权，不应作为持久跨事务授权缓存；依赖 ForgetOperation 追加保留、不在运行中删除历史操作的约束。

兼容性：明确授权的跨 Key 语义冲突仍可执行，target 必须有合法 Scope、状态和来源；不把“key 必须相同”的旧计划文字应用到既有跨 Key 确认功能。显式失效目标不静默换到其他记录。同请求的旧结果不可见时返回无正文结果，重新设置须用新请求身份。

旧 TaskEvidence 不伪造来源；缺少持久证明时不参与 Goal 上下文。新证据保存已知的 Task 来源、Action 参数及结果中的类型化引用；源查询异常或引用不存在不能凭摘要放行。增量 AST 基线只约束已识别 query/get/select 模式的新增直读数量，不承诺发现原生 SQL 或任意数据流。

首轮全量离线测试：1661 passed、63 failed、123 skipped，319.45 秒；失败包含两个实现问题、旧测试缺少真实 Event/Provenance，以及本机默认 Bash 解析到无可用 Linux 的 WSL 入口。修复实现问题、完善真实来源 fixture，并在测试进程中指定已安装的 `E:/git/Git/bin/bash.exe` 后集中复验：123 passed、1 failed，33.50 秒。剩余失败揭示隐藏旧结果的请求幂等缺口，随后已在 single/batch 写入事务中补齐，最终全量曾得到 1731 passed、1 failed、119 skipped（300.99 秒）；该领域事件误判已修复，后续相关用例复验通过，完整收口结果见下段。

PostgreSQL：本机有 Docker CLI，但原先引擎未运行。尝试隐藏启动 Docker Desktop 后，Docker info 在 10 秒内仍未响应。不得将默认跳过的 PG 用例记为通过；PG 专项及真实存量审计尚无运行证据。只读审计入口为 `aiive.memory.integrity_audit.audit_memory_integrity(session)`，本轮不连接生产库执行。


### 24.4.1 最终离线收口结果

最终相关回归：**364 passed，0 failed，5237 warnings，41.36 秒**。范围包含全部 Forget 集成文件、上下文组装/快照、Memory 审计新回归、统一检索、Epoch、Memory Store/ReadModel/WriteService、写入摄取、Graph、冲突确认/实体绑定、Goal 领域事件与决策序列化、Worker 证据回填，以及 AST 访问边界检查。警告数量按实际 pytest 输出记录，本轮未专项清理警告。

没有为得到绿色结果而把未知来源放行。旧的正向测试补齐真实 Event、Provenance、Epoch/Segment 关系；对于来源 Event 根本不存在的摘要，测试改为要求父摘要立即不可见，不能再以 DEEP 降级结果携带正文。

最后收口还修正了原审计对普通 Summary/Checkpoint 的判断：旧 Visibility 对这两类对象只处理对象级屏蔽，没有跟随来源展开。现已在 `ForgetVisibilityService` 统一验证 Summary 的声明/持久来源，以及 Checkpoint 的来源 Segment/Summary；缺失来源和查询异常关闭整批。Sealing、普通摘要、Checkpoint 和检索回源共同使用此边界。Turn 来源沿其子 Event 校验，复用既有 Worker 来源规则。新回归验证仅屏蔽来源 Event 时，Summary 和 Checkpoint 同时不可见。

模型调用的最后一次复核还复用领域事件来源解析：任务完成等领域 ID 不冒充 Event 表主键。Graph/Relations 的展示名从获准 Memory 的规范 GraphFact 生成，不直接输出共享 GraphEntity 的历史别名；找不到对应规范事实时不展示该关系。Conflict question 与 old/new content 一样经过通道敏感度渲染。

统一测试环境命令（仅对测试进程设置，不改用户持久配置）：

```powershell
$env:AIIVE_SHELL_BASH_PATH = 'E:\git\Git\bin\bash.exe'
$env:PATH = 'E:\git\Git\bin;' + $env:PATH
.\.venv\Scripts\python.exe -m pytest tests -q -o addopts= --disable-warnings
```

最后一次定向收口采用同一 pytest 参数，将以上列明的受影响测试文件一次性传入；未在收口后再次重复耗时全量。全量出现过的失败已在修复后相关回归中覆盖，不将“1731 passed、1 failed”的历史输出改写成全量零失败。

测试详细输出保留在本地忽略目录 `.pytest_cache/memory_audit_20261107_implementation/`；核心结果与限制已写入本文，不依赖缓存文件作为唯一证据。

**代码实现与离线相关回归已完成。PostgreSQL 并发专项仍未运行，不能标记为完整并发验收通过；真实存量数据审计/回填未执行。** Schema 迁移：无。Git 提交：未创建。
