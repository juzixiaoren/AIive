# AIive 长期个人代理架构：分阶段编码提示词

> 使用方式：按 Phase 0 → Phase 6 顺序执行。  
> 每个阶段第一次提交给编码 Agent 时，只允许分析、定位、提出实施方案和测试计划，不允许直接修改。用户确认后，再让其执行同一阶段。  
> 不要同时并行修改相互依赖的阶段。

---

# 全阶段通用约束

将以下约束附在每个阶段提示词中：

```text
你正在修改一个长期运行的单用户个人 Agent 项目 AIive。

核心原则：
1. 用户看到一个永久连续的个人代理，但模型上下文必须严格有界。
2. 禁止通过新建第二个 LLM Supervisor/Reviewer 判断主 Agent 是否应调用工具。
3. 禁止使用关键词、正则、固定短语或测试专用分支判断自然语言意图。
4. 禁止把扩写系统提示词作为主要修复。
5. 优先阅读项目文档、真实调用链、数据模型和测试，再定位代码。
6. 不要根据方案文档臆造项目中不存在的文件、类、表或接口。
7. 所有结论必须引用真实文件、类、函数和行号。
8. 第一次回复只做分析，不修改任何代码、配置、迁移、Prompt 或测试。
9. 第一次回复必须给出：现状、问题、修改范围、数据迁移、风险、测试和回滚方案。
10. 等待我明确确认后才能实施。
11. 实施时不得顺手重构无关模块。
12. 所有后台任务必须幂等、增量、有预算、有重试上限。
13. 所有热数据结构必须有显式上限，禁止无界 list、无界缓存、无界索引。
14. RawEvent 和 MemoryRecord 是真相源；向量、FTS、KG、Markdown、摘要均为可重建投影。
15. 不得为了通过测试降低验收标准。
```

---

# Phase 0：代码库真实审计

## 目标

在不修改代码的前提下，确认当前项目能否承载长期连续代理架构，并找出所有无限增长点和绕过路径。

## 提示词

```text
请对 AIive 当前代码库执行“长期连续个人代理架构 Phase 0 审计”。

本阶段严格禁止修改任何代码、配置、数据库迁移、系统提示词或测试。

请优先阅读：
- 项目架构文档；
- AgentGraph/LangGraph 图构建；
- 上下文构建和消息加载；
- RawEvent/Event/Message/Thread 数据模型；
- MemoryWriteService、记忆分类、记忆召回；
- ToolRegistry、ToolNode、工具 handler；
- Outbox/Worker/Scheduler；
- Qdrant/FTS/KG/Markdown 投影；
- ContextSnapshot；
- 数据清理和 TTL；
- 现有测试。

必须回答：

1. 当前用户会话、thread、message、event、checkpoint 的真实关系是什么？
2. 每轮发送给模型的上下文从哪些表或状态加载？哪些集合可能无界增长？
3. 当前是否把完整 thread history 交给模型？裁剪、摘要、压缩分别在哪里？
4. 当前 WorkingState 或 LangGraph State 是否永久累积 messages/tool results？
5. 当前 RawEvent/Event 是否是真相源？哪些数据只有派生层有？
6. 当前显式记忆和隐式记忆分别通过什么路径写入？是否存在绕过正式入口？
7. 当前长期记忆有哪些状态、scope、版本、有效期和证据字段？
8. 当前哪些表、索引、日志、ContextSnapshot、embedding 会只增不减？
9. 当前是否有 scheduler、Outbox、Worker、任务幂等和水位线？
10. 当前是否有历史搜索、向量召回、FTS、KG？每次查询范围是否可能扫描全量历史？
11. 当前删除记忆时会清理哪些投影？是否存在残留？
12. 当前测试是否覆盖长会话、上下文超限、记忆重复、遗忘和索引清理？

请输出：

A. 真实调用链图；
B. 真实数据模型图；
C. 无限增长风险清单，按 P0/P1/P2 分级；
D. 绕过真相源和事务边界清单；
E. 与目标架构的差距矩阵；
F. Phase 1～6 推荐修改的真实文件清单；
G. 数据迁移和兼容风险；
H. 每阶段可独立验收的测试建议；
I. 明确指出哪些方案假设与当前代码不匹配。

不要给泛泛建议。所有结论必须引用真实文件、函数、类和行号。
完成分析后停止，等待我确认。
```

---

# Phase 1：有界上下文、Epoch 与 Segment

## 目标

让用户继续使用同一个逻辑会话，但每次模型输入、WorkingState 和近期消息规模严格有界。

## 提示词

```text
请实施 AIive 长期连续个人代理架构 Phase 1：有界上下文、Epoch 与 Segment。

开始前先复核 Phase 0 审计。第一次回复只提交实施计划，不修改代码，等待我确认。

本阶段范围：

1. 引入或完善永久 ConversationStream 与内部 Epoch/Segment 的关系。
2. 为模型上下文建立明确的 ContextBudget。
3. 为 Stable Contract、Core Memory、WorkingState、Recent Messages、Retrieved Memory、Tool Results、Reserved Output 设置独立上限。
4. 实现 soft threshold 和 hard threshold。
5. 实现 Segment 的 open/sealing/sealed/failed 状态机。
6. 实现 Epoch 的 active/sealing/sealed/archived 状态机。
7. 实现有界 ContextAssembler。
8. 工具大输出只进入上下文摘要，原文保存为引用。
9. 当前任务的 open loops、active constraints、artifact refs 和 verified tool states 不得在压缩边界丢失。
10. 保持用户侧一个连续会话，不要求用户新建对话。

本阶段不做：
- Daily Dream；
- 记忆自动遗忘；
- 全量冷历史迁移；
- 第二个监督 LLM；
- 系统提示词扩写；
- 关键词路由。

设计要求：

A. 所有预算配置化，不写死在业务逻辑中。
B. ContextAssembler 必须输出各分区 token 统计。
C. 达到硬阈值时先压缩或裁剪，再调用模型。
D. 不得切断正在运行的工具链、审批或未提交副作用。
E. 新 Checkpoint 完整生成后原子替换 current 指针。
F. 旧 Checkpoint 只保留当前、前一可回滚版本和必要审计版本。
G. 不得无限保存完整 ContextSnapshot。
H. 流式和非流式路径必须共享同一套预算与状态逻辑。

实施前请提交：

1. 将新增或修改的真实文件；
2. 数据模型和迁移；
3. ContextBudget 结构；
4. Segment/Epoch 状态机；
5. 软硬阈值时序；
6. 兼容旧 thread/message 数据的方法；
7. 流式竞态处理；
8. 回滚方案；
9. 测试矩阵。

确认后实施，并完成：

- 单元测试；
- 数据库迁移测试；
- 长 thread 模拟；
- P99 上下文 token 不随总历史增长的测试；
- 正在执行工具时不会密封的测试；
- 用户连续对话体验不被 Epoch 切换影响的测试。

禁止用固定消息数作为唯一切分规则；允许 token、任务边界、空闲和容量共同决定。
```

---

# Phase 2：统一长期记忆摄取

## 目标

长期记忆提取不再只依赖主 Agent 是否主动调用记忆工具；显式和隐式记忆共享正式写入管线。

## 提示词

```text
请实施 AIive 长期连续个人代理架构 Phase 2：统一 Memory Ingestion。

第一次回复只分析 Phase 1 完成后的真实代码和实施方案，不修改，等待确认。

本阶段范围：

1. 每个完整用户 turn 创建幂等 MemoryIngestionJob。
2. Memory Ingestion 可以与主 Agent 并发，但只能读取完整、已提交的 RawEvent。
3. 它只负责长期记忆提取，不监督主 Agent 的其他工具决策。
4. 输出结构化 MemoryProposal。
5. 区分 user_required 与 system_best_effort。
6. 显式记忆要求同步提交或明确失败。
7. 隐式长期事实异步处理，不阻塞普通回复。
8. 统一经过 Normalizer、ScopeValidator、ValueGate、DuplicateResolver、ConflictResolver、MemoryWriteService。
9. 移除或改造任何 `_finalize()` 直接调用 MemoryWriteService 的绕过路径。
10. 同一 source_turn 不得重复写入或重复强化。
11. 不同 source_turn 的同一事实可作为独立证据。
12. 临时偏好、一次性格式要求、暂态情绪不得进入 active long-term memory。

MemoryProposal 至少包含：

- canonical_key；
- value；
- scope；
- memory_type；
- evidence_source；
- persistence_requirement；
- confidence；
- retention_hint；
- source_event_ids。

设计约束：

A. 不新增通用 Supervisor/Reviewer LLM。
B. 不用关键词或正则判断“记住、以后、偏好”等意图。
C. 不修改系统提示词作为主要方案。
D. Job 必须有 source_turn_id 幂等键、attempt、状态和错误。
E. 异步任务失败不能影响当前聊天，但必须可重试和审计。
F. 显式记忆提交成功前不能生成正式已保存状态。
G. MemoryRecord 必须保留 provenance、scope、版本和有效期。
H. 所有投影通过 Outbox 更新。

实施前请提交：

1. 当前显式/隐式记忆真实路径；
2. 将删除或统一的绕过点；
3. Job 状态机；
4. Proposal schema；
5. 去重和 source_turn 语义；
6. 同步与异步边界；
7. 事务和 Outbox；
8. 失败恢复；
9. 测试矩阵。

验收必须包含真实 LLM E2E：

- 显式长期记忆；
- 隐式稳定偏好；
- 项目决策；
- 长期工作方式；
- 临时偏好；
- 否定记忆；
- 冲突更新；
- 同一 turn 重放；
- 不同 turn 独立强化；
- 提取失败重试。

测试不能只检查回复文本，必须 read-after-write 并检查 MemoryRecord、Evidence、scope 和状态。
```

---

# Phase 3：用户空闲压缩

## 目标

用户长时间不对话时，自动密封稳定历史段并准备下一次无缝恢复。

## 提示词

```text
请实施 AIive 长期连续个人代理架构 Phase 3：Idle Compaction。

第一次回复只提交代码定位、状态机和实施计划，不修改，等待确认。

本阶段范围：

1. 维护 ConversationStream.last_activity_at。
2. 增加配置化 idle_window。
3. 用户活动时重新计算空闲调度。
4. 只有满足安全条件时创建 SegmentSealingJob：
   - 无模型流；
   - 无运行中工具；
   - 无 pending approval；
   - 无未提交副作用；
   - 存在足够未压缩事件、可完成 Segment 或已接近软阈值。
5. 锁定不可变 source_event_range 和 source_hash。
6. 并行触发：
   - Context Compaction；
   - Memory Extraction；
   - Search Indexing。
7. 生成 SegmentSummary 和 EpochCheckpoint。
8. 新 Checkpoint 完整验证后原子切换。
9. 用户重新回来时，不得读取半成品摘要。
10. 压缩失败时保留原始事件和旧 Checkpoint。
11. 同一 Segment 同一 compaction_version 只能成功执行一次。

禁止：

- 用户离开几分钟就无条件调用模型；
- 正在执行工具时切断 Segment；
- 按固定消息数粗暴切分；
- 删除尚未成功压缩的原始事件；
- 摘要基于上一代摘要无限递归；
- 让空闲压缩阻塞普通用户请求很长时间。

实施前请提交：

1. 当前 scheduler/worker 的真实实现；
2. Idle 检测和取消/重排机制；
3. sealing lock；
4. source range 的确定方式；
5. compaction version 和幂等键；
6. 用户返回时的竞态；
7. 原子 Checkpoint 替换；
8. 失败恢复；
9. 测试矩阵。

验收测试：

- 用户持续活跃时不启动；
- 空闲但无可压缩内容时跳过；
- 达到 idle_window 且有稳定 Segment 时启动；
- 工具运行中不密封；
- 同一 Segment 不重复压缩；
- 用户回来时可继续任务；
- open loops、constraints、artifact refs、verified tool states 不丢失；
- 压缩失败不破坏旧状态；
- Context P99 不因历史增加而增长。
```

---

# Phase 4：Daily Dream 与选择性遗忘

## 目标

每 24 小时增量整理长期记忆，使记忆能够合并、修订、沉睡、归档、过期和删除候选，而不是只增不减。

## 提示词

```text
请实施 AIive 长期连续个人代理架构 Phase 4：Daily Dream、生命周期和选择性遗忘。

第一次回复只分析现有 MemoryRecord 模型、调度器和修改方案，不修改，等待确认。

本阶段范围：

1. 引入或完善状态：
   candidate / active / dormant / archived / expired / forgotten。
2. 引入 retention_policy：
   normal / pinned / ephemeral（如项目已有 legal_hold 可保留）。
3. 增加 last_used_at、last_confirmed_at、candidate_expires_at、valid_from/valid_to。
4. 每约 24 小时调度 DreamConsolidationJob。
5. 无新变化时 skipped_no_changes，不调用模型。
6. 使用 last_successful_dream_watermark、dirty set、candidate queue、conflict queue、stale queue。
7. Dream 只读取 sealed Segment 和 committed MemoryRecord。
8. Dream 输出 MemoryMaintenanceProposal，不得直接修改真相源。
9. 支持：
   - merge；
   - revise；
   - supersede；
   - candidate promote/reject；
   - active→dormant；
   - dormant→archived；
   - expire；
   - deletion candidate；
   - inferred pattern candidate。
10. 模式推断必须保留 supporting evidence，并默认 candidate。
11. Candidate 必须有 TTL。
12. Pinned 不得自动删除，但可以 superseded/invalid。
13. 所有 proposal 经正式 Memory Write Pipeline。
14. 每次 Dream 报告 added/merged/revised/demoted/archived/expired/deleted/net_growth。
15. 连续净增长失控时告警。

禁止：

- 每日全库扫描；
- 单一“多少天没用就删除”公式；
- 模型原地重写 Memory Store；
- 无证据创建用户偏好；
- 静默覆盖冲突；
- 自动删除 pinned；
- 只新增 pattern、不合并和降级。

实施前请提交：

1. 数据模型迁移；
2. 类型相关 retention 规则；
3. Dream 输入水位线；
4. 输入/输出预算；
5. Proposal schema；
6. Pinned 和 superseded 的关系；
7. Candidate TTL；
8. 数据库规则与模型判断的边界；
9. 回滚；
10. 测试矩阵。

验收：

- 无变化跳过模型；
- 只处理 watermark 后变化；
- 重复记忆合并；
- 新事实 supersede 旧事实；
- 长期不用偏好降为 dormant；
- 项目结束后 decision 归档；
- 环境事实过期；
- Candidate 到期清理；
- Pinned 不自动删除；
- 模式推断保留证据且默认 candidate；
- Dream 失败不影响当前有效记忆；
- active/candidate 数量在长时间模拟后趋于稳定；
- 每次 Dream 的输入规模不随全部历史线性增长。
```

---

# Phase 5：冷历史与分层检索

## 目标

允许用户永久保存全部 RawEvent，同时保证普通 Agent 上下文、检索延迟和召回竞争范围保持有界。

## 提示词

```text
请实施 AIive 长期连续个人代理架构 Phase 5：冷历史、分层索引和有界检索。

第一次回复只提交现有检索路径审计和实施计划，不修改，等待确认。

本阶段范围：

1. 明确热、温、冷数据层。
2. 默认自动召回只查询 active MemoryRecord 和当前 scope。
3. 为 Epoch/Segment Summary 建立定位索引。
4. RawEvent Archive 用于历史钻取，不参与每轮全局竞争。
5. 实现三级检索：
   active memory/current scope
   → epoch/segment summary
   → selected raw event range。
6. 为查询设置固定预算：
   max_candidate_epochs
   max_candidate_segments
   max_raw_events_scanned
   max_vector_candidates
   max_rerank_candidates
   max_retrieval_tokens
   max_retrieval_latency。
7. 不默认为所有 RawEvent 建永久 embedding。
8. 大型日志只索引摘要、错误码、文件名、时间和 artifact ref。
9. archived memory 只在显式历史搜索或深度工具中出现。
10. expired/forgotten 不得进入默认召回。
11. project/user/thread/epoch/time/entity 过滤字段必须有真实数据库或 payload index。
12. 索引是投影，可重建。

禁止：

- 每轮全历史向量 top_k；
- 全部历史 metadata 过滤后仍暴力扫描；
- archived/expired 与 active 同权竞争；
- 查询预算随历史总量增长；
- 冷存储和热向量集合混在一起；
- 将大型文件全文直接注入模型。

实施前请提交：

1. 当前 Qdrant/FTS/KG 查询链；
2. 当前 metadata index；
3. 热/温/冷划分；
4. 三级检索 API；
5. query budget；
6. 原始历史永久保留时的存储方案；
7. 索引迁移和重建；
8. 深度历史搜索工具；
9. 失败降级；
10. 测试矩阵。

验收：

- 冷历史从 1 万增长到 100 万事件时，普通 Context token 不变；
- 普通召回不扫描 RawEvent 全集；
- 普通检索 P99 在目标范围；
- 跨项目不串线；
- archived/expired 不进入默认召回；
- 用户明确追问历史时可钻取原文；
- 删除源记录后所有投影可清理；
- Qdrant/FTS/KG 可从真相源重建。
```

---

# Phase 6：Forget Saga、清理、指标与长期压测

## 目标

完成真正可维护的删除闭环、容量治理和一年等效运行验证。

## 提示词

```text
请实施 AIive 长期连续个人代理架构 Phase 6：Forget Saga、清理、指标和长期压测。

第一次回复只提交真实代码审计和实施计划，不修改，等待确认。

本阶段范围：

1. 实现完整 Forget Saga：
   planned
   → hidden_from_retrieval
   → projections_removed
   → payload_removed
   → completed。
2. 删除范围覆盖：
   MemoryRecord
   Evidence
   Proposal
   Vector
   FTS
   KG
   Core Memory Projection
   Context Cache
   Markdown Projection
   Object Payload
   可直接暴露该内容的派生摘要。
3. 部分失败进入 partial 并重试。
4. 增加 weekly housekeeping：
   - Candidate TTL；
   - 旧 embedding；
   - orphan projection；
   - debug trace；
   - 大型 tool output；
   - 旧 checkpoint version；
   - 无活动 Epoch 归档。
5. 增加 monthly rebase：
   - 高层 Checkpoint 重建；
   - 无来源记忆检查；
   - orphan lineage；
   - pinned 审计；
   - scope 增长统计；
   - 容量配额。
6. 增加热/温/冷配额和告警。
7. 增加关键指标和 dashboard 数据。
8. 实现一年等效负载测试。
9. 提供迁移、回滚和灾难恢复说明。

禁止：

- 只从向量库删除；
- 只把 status 改为 forgotten 但仍能从缓存/摘要召回；
- 静默删除 pinned；
- 清理任务无预算；
- 月度任务全库调用大型模型；
- 为压测写业务特殊分支。

实施前请提交：

1. 所有投影和缓存清单；
2. Forget Saga 状态机；
3. 任务幂等；
4. orphan detection；
5. TTL/容量配置；
6. metrics；
7. 一年等效数据生成方法；
8. 性能基线；
9. 回滚和恢复；
10. 测试矩阵。

最终验收至少包括：

A. Context P99 token 不随总历史增长；
B. WorkingState/Core Memory/Retrieval Package 有固定上限；
C. active memory 长期趋于稳定；
D. candidate 不无限增长；
E. 普通召回延迟与冷历史总量弱相关；
F. Daily Dream 成本与新增/dirty 数据相关；
G. Forget 后所有召回层不可见；
H. Pinned 不被自动删除；
I. 冷历史永久保留时日常 Agent 性能不明显退化；
J. 数据库、对象存储、向量、FTS、KG 可恢复和重建；
K. 所有 CI 测试通过；
L. 真实 LLM 长期连续对话回归通过。

最终报告必须给出修改文件、迁移、测试结果、指标对比、已知限制和后续风险，不得只说“已完成”。
```

---

# 推荐执行方式

1. 先执行 Phase 0，获得真实代码审计。
2. 根据审计修订后续提示词中的文件和模型假设。
3. Phase 1 完成并通过长上下文测试后，再做 Phase 2。
4. Phase 2 稳定后，再加入空闲压缩。
5. 记忆写入和来源稳定后，再做 Daily Dream。
6. 有稳定 Summary/Memory 后，再做冷历史分层检索。
7. 最后完成 Forget Saga 和一年等效压测。
8. 每阶段单独提交、单独迁移、单独回滚，不要一次性大改。
