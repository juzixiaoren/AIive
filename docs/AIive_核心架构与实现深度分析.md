# AIive 核心架构与关键实现深度分析

本文基于当前源码，对 AIive 的核心架构与关键实现做一次系统梳理：从一次普通对话的运行路径，到上下文组装、持久化压缩、记忆与知识检索、任务执行、遗忘与自进化等子系统，说明各模块的真实数据模型、触发条件、一致性边界与已知限制。

**仓库：** https://github.com/juzixiaoren/AIive

> 阅读约定：本文把“源码明确实现”“由源码可推导的行为”“静态审计风险”“未能证明的能力”分开。描述配置默认值时，不把“代码支持”混同为“当前部署已开启”；源码链接以符号名为准，行号会随提交变化。本文是源码静态分析结论，不是生产环境端到端验收报告。

## 阅读导航

- 第一至三部分：全局运行模型、上下文组装与裁剪、Segment/Epoch 持久化压缩。
- 第四至七部分：Memory Recall、Unified Retrieval、Knowledge 文档 RAG、时序知识图谱。
- 第八至十一部分：持久任务与 Goal、遗忘概览、配置与功能状态、关键问题与风险判断。
- 第十二至十七部分：Memory 写入与冲突、Forget Saga、Persistent Task Worker、MCP/Capability、SelfDev、跨模块可靠性语义。
- 第十八部分：优先级清单与未覆盖范围。
- 附录 A / B：核心源码导航与主要源码链接。

---

# 第一部分：全局运行模型

## 1.1 三类持久化对象：对话、任务、知识/记忆

AIive 不是一个只靠聊天记录驱动的 Agent。至少应区分三条状态主线：

| 主线 | 核心对象 | 用途 | 恢复依据 |
|---|---|---|---|
| 对话执行 | Thread、TurnRecord、Event、WorkingState | 处理一次用户消息及其工具循环 | Turn/Event + WorkingState + Segment/Epoch |
| 持久任务 | AgentTask、AgentRun、Action、Evidence、Artifact、Checkpoint | 跨 Turn、跨进程推进任务 | Task State、Checkpoint、Action/Evidence/Artifact |
| 知识与记忆 | MemoryRecord、Document、Chunk、GraphEntity/GraphRelation、各类索引投影 | 保存长期事实、文档和实体关系 | canonical Memory/Document/Chunk；向量/检索/图谱是派生数据 |

一个重要原则是：**聊天历史不是真正的唯一状态源**。例如任务恢复不应重放整个 Thread；记忆向量可由 MemoryRecord 重建；图谱关系可由 `memory_records.graph_facts` 重建；知识向量可由 Chunk 内容重建。

## 1.2 一次普通聊天 Turn 的主路径

```text
HTTP / WebSocket 请求
  -> TurnExecutionService 创建/领取 TurnRecord
  -> 写入用户 Event（并确定 turn_sequence / segment 归属）
  -> ContextAssembler.assemble()
       -> 稳定系统内容 / Core Memory
       -> 待回答 Goal Question（仅用户消息且确有 open 问题）
       -> Attention / WorkingState
       -> Segment/Epoch/历史消息
       -> Memory Recall + Graph Expansion
       -> token 分区裁剪与全量 hard gate
       -> 构建工具 schema（主 Agent 固定任务元工具）
  -> AgentGraph assistant 节点
       -> LLM 输出文本或 tool call
       -> policy_check
       -> tools 执行
       -> 工具结果规范化/有界回填
       -> assistant 再次决策
  -> Turn 收尾、事件持久化
  -> Outbox 异步工作（记忆提取、索引刷新、压缩扫描等）
```

这里存在两个循环：

- **Turn 内循环**：AgentGraph 的 assistant/tool 往返，服务于当前用户消息。
- **跨 Turn 循环**：Outbox Worker、Task Worker、Goal Trigger、Segment Sealing 等持久化后台流程，服务于长期任务和系统维护。

模型产生 tool call 只是提议，不等于工具必然执行。执行还要经过注册表、参数校验、Policy、权限/审批等服务端边界。

## 1.3 主对话 Agent 与 Task Worker 的边界

主对话 Agent 不直接拥有全部文件、桌面、自进化能力。源码注释明确：`ContextAssembler` 为 Conversation Agent 固定构建 `build_main_agent_registry()`，只绑定任务元工具；具体执行能力由 Persistent Task Worker 经 CapabilityBroker 获取。

因此：

- 主 Agent 负责理解用户意图、委派任务、查询/管理任务。
- Task Worker 负责在受限 TaskScope 中发现能力、提出一个 Action、经 Broker 校验并执行。
- Capability 的“可发现”不等于“可执行”；发现结果不能扩大任务授权。

源码入口：[`runtime/context_assembler.py`](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/runtime/context_assembler.py)、[`task_runtime/tools.py`](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/task_runtime/tools.py)、[`task_runtime/tool_injection.py`](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/task_runtime/tool_injection.py)、[`control/capability_broker.py`](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/control/capability_broker.py)。

---

# 第二部分：上下文组装与压缩

## 2.1 ContextAssembler 是唯一的聊天上下文组装入口

核心文件：

- `backend/aiive/runtime/context_assembler.py`
- `backend/aiive/runtime/context_budget.py`
- `backend/aiive/runtime/thread_state.py`
- `backend/aiive/runtime/token_counter.py`
- `backend/aiive/runtime/token_models.py`
- `backend/aiive/runtime/working_state.py`
- `backend/aiive/runtime/attention_manager.py`

`ContextAssembler.assemble(db, message, thread, _source, requested_output_tokens, upper_bound_sequence, trace_id, current_turn_id)` 返回 `AssembledContext`，包含：

- `messages`：最终发给模型的消息列表；
- `tools_schema`：本次组装可见的工具定义；
- `partition_reports`：分区 token 使用情况；
- `total_token_count`：整体估算/安全 token 统计；
- `snapshot`：输入快照（预览、来源、信任等级、token estimate、注入记忆 ID 等）；
- `agent_ctx`：召回包、待回答问题、图谱扩展等运行时上下文；
- `tool_registry`：与当前 schema 对应的不可变工具视图。

这不是一个“拼字符串”的函数。它要确保工具 schema 与执行时 Registry 一致，并对消息和工具定义整体计数。

## 2.2 一轮上下文实际如何组装

源码 `_compose_prompt_messages()` 的真实顺序为：

1. `system_content`：稳定系统内容（Stable Contract + Core Memory 等）；
2. `epoch_checkpoint_text`（若有）；
3. `segment_summary_text`（若有）；
4. `history_messages`：按边界读取的原始聊天历史；
5. `attention_text`；
6. `working_state_text`；
7. `sealing_bridge_text`；
8. `history_summary_text`；
9. `recall_messages`：记忆召回块、图谱关系扩展块；
10. `questions_text`：当前线程待回答 Goal 问题（若有）；
11. 当前消息：`user` 或 `system` 角色，取决于 `_source`。

其中 Epoch/Segment 摘要被放在原始历史之前，是因为它们只在压缩边界改变，稳定性高；注意力、WorkingState、召回和问题随轮次变化，放在历史之后。这样既保留历史顺序，也尽量让 Prompt 前缀稳定。

**一个容易误解的点：** `system_content` 并不等于整个 system prompt。后续的摘要、WorkingState、Recall 等也会以 `system` role 消息追加。模型看到的是多条 system 消息与 history 交错后的完整列表。

### 2.2.1 待回答问题的触发条件

只有 `_source` 为 `None` 或 `user` 时，`ContextAssembler` 才调用 `list_open_questions(db, thread_id)`；系统命令/运行时事件不进入该用户回答关联流程。只有返回 open 问题时才生成 `questions_text`。加载失败会记录日志并跳过，不阻断正常聊天。

Goal Questions 对 open 的判定不只看“有没有 resolution / 是否过期”：还要读取 `task_state["goal"]` 当前 GoalPlan，检查目标是否 active、步骤是否仍存在且处于 `waiting_user`，以及同一步骤是否有更新问题取代旧问题。问题状态是读取时推导值，不需要独立调度器。

例如：

```text
步骤 interview 进入 waiting_user -> 产生 question Q1
Goal 暂停 -> Q1 推导为 stale，不注入当前上下文
Goal 恢复，步骤仍 waiting_user -> Q1 重新变为 open
步骤被 modify 重置 -> Q1 仍为 stale；之后新提问 Q2 会取代 Q1
```

回答工具按 `agent_ctx["open_questions"]` 条件绑定；即便绕过聊天工具直接调用服务层，`answer_step_question()` 也会复核 question_id、task/step 归属和 open 状态，防止旧问题修改计划。

## 2.3 Token 预算：分区限制不是总量保证

`ContextBudget` 将上下文拆成若干逻辑分区，例如 Stable Contract、Core Memory、WorkingState、Tool Definitions、Recent Messages、Retrieved Memory、Tool Results、Epoch Checkpoint、Segment Summaries、Sealing Bridge、Retrieved History Summary，以及输出预留。

要区分三个数：

- **soft limit**：希望某分区通常控制在的预算；
- **hard limit**：分区允许的上限/默认边界；
- **model context window**：模型端最终总窗口。

各分区限制用于局部装配和裁剪，最终是否能发给模型由完整 messages + tools 的 TokenCounter 重新计数决定。不能把分区预算简单相加后当成最终模型实际 token 数。

上述数字属于配置默认值/某个预算配置快照，不是所有模型、所有部署的恒定值。模型上下文窗口、最大输出 token、环境变量都可能改变弹性 Recent Messages 预算。实际部署必须读取 `ContextBudget.from_env()` 和 ModelProfile，而不是仅抄文档中的固定数字。

## 2.4 五轮裁剪到底怎么发生

`TrimPlan.from_budget()` 每轮生成独立裁剪计划，不修改共享 `ContextBudget`。最多执行 `MAX_TRIM_ROUNDS = 5` 次组装：

| attempt | Recent Messages / Tool Definitions 系数 | 额外限制 |
|---:|---:|---|
| 0 | 1.00 | 使用各分区 soft limit |
| 1 | 0.80 | Recent / Tool definitions 收紧 |
| 2 | 0.60 | Tool Results ≤ 2000 |
| 3 | 0.40 | Tool Results ≤ 1000；Retrieved Memory ≤ 400 |
| 4 | 0.25 | Tool Results ≤ 500；Retrieved Memory ≤ 200；WorkingState ≤ 500 |

每轮都会重新渲染 WorkingState、重新读取有界历史、重新装载摘要/桥接/Recall，再重新构建工具 schema。不是只对上一轮最终字符串做一次截断。

随后 `TokenCounter.count_messages(model, messages, tools)` 统计完整输入。若 `safe_tokens + requested_output <= context_window`，才返回 `is_safe=True` 的 `AssembledContext`。五轮仍不满足则抛 `ContextBudgetExceededError`，不把超限上下文硬塞给模型。

### 2.4.1 Soft Limit 的副作用

第一次组装后，若 `safe_tokens > budget.soft_input_limit`，会调用 `EpochManager.mark_pending_seal()` 标记当前 open Segment 的 `pending_seal_at` 和 `sealed_by_turn`，并显式 commit。此标记表达“上下文已达到软阈值，需要后台考虑密封”，不是“当前已经执行了压缩”。

这意味着：

- token 裁剪是当前 Turn 的即时措施；
- `pending_seal` 是后台 Segment 密封的提示信号；
- 真正密封还要经过调度扫描、状态检查、冻结和 Outbox Handler。

## 2.5 历史消息如何有界读取

`ThreadState.load_recent_messages_bounded()` 使用 Keyset/序列边界读取，而不是一次性加载全 Thread 全部 Event。源码中的默认约束包括按 Turn 分页、最大 Turn/Event 数和原始字节上限；具体值应以当前 `thread_state.py` 配置为准。

读取过程按较新的 Turn 向前扫描，并结合 token 预算决定是否继续纳入更旧 Turn。对大型工具输出，先尝试引用化/有界回填，再重新计数；如果完整某轮历史放不下，则停止继续向更旧方向扩展，避免只保留更早碎片而丢失近因。

`interrupted_unknown` 是特殊情况：工具调用可能已经发生副作用但执行结果未知，不能伪装成普通成功的 assistant/tool 消息重放。系统将其降级为说明性 System 内容，提示模型该调用状态未知，从而避免模型据此盲目重复执行。

## 2.6 工具输出如何进入上下文

工具输出有两层治理：

1. `ToolResultNormalizer`：大型工具结果超过阈值时，将完整结果持久化为 Artifact，返回引用和确定性预览；
2. `guard_tool_output()`：对进入聊天/任务 Evidence/历史回放的输出统一做有界处理，超长 shell 日志采用流式文件保存并提供分页读取。

因此工具输出可能出现三种形态：

- 小结果：直接作为 tool result 消息进入历史；
- 大结果：上下文中只看到摘要/引用，完整内容在 Artifact 或日志对象中；
- 超大或存储失败：返回有界错误预览，不将无限长内容重新塞进上下文。

工具输出被标为不可信数据。它是被模型分析的内容，不是可以覆盖 System/Policy 的指令。

---

# 第三部分：Segment / Epoch 持久化压缩

## 3.1 两种压缩不能混淆

| 类型 | 发生时机 | 改变什么 | 是否调用 LLM | 是否持久化 |
|---|---|---|---|---|
| Turn 内裁剪 | 每次上下文组装超出 soft/hard 预算时 | 当前请求的 prompt 内容 | 否 | 通常不保存压缩摘要；soft 超限会留下 pending_seal 标记 |
| Segment Sealing | 空闲扫描、达到 Segment 上限、soft-limit 标记等条件满足后 | 一段历史转成 SegmentSummary | 是（语义阶段） | 是 |
| Epoch Rollover / Checkpoint | Epoch 达到 Segment 数量/阶段边界 | 多个 Segment 归档成 EpochCheckpoint | Checkpoint 聚合为确定性过程 | 是 |

## 3.2 Segment 密封触发与阻塞条件

从配置说明和 `EpochManager` 源码可确认：系统有空闲扫描和 Segment 容量上限；Soft Limit 触发 pending seal；达到容量后也会产生待密封信号。空闲扫描的候选还必须在事务内复核 Thread 是否仍空闲，避免扫描后用户重新发消息仍密封旧边界。

密封入口 `begin_segment_sealing()` 会锁 Thread 行，核对传入的 `expected_segment_id` 仍是当前 open Segment，并检查 idle cutoff。以下情况会阻止密封：

- `running` Turn；
- 一小时窗口内的 `not_started` Turn；
- WorkingState 有 `uncommitted_side_effects`；
- 有 `pending_approvals`；
- 有 `running_tool_state`；
- Segment 没有 TurnRecord；
- 同一 Thread 已有另一个 Segment 处于 sealing。

注意：`interrupted_unknown` 被视为吸收态/终态，不阻塞密封；这是为了避免未知中断永久阻塞压缩。但其失败工具状态会进入 unresolved failure 派生信息，不能被摘要当作成功。

## 3.3 冻结输入：为什么不是“取最近 N 条消息做摘要”

`freeze_segment_for_sealing()` 先固定 Segment 的 turn range，然后通过 `freeze_compaction_input()` 生成不可变输入：

1. 按 `turn_sequence` 生成 Turn Manifest；
2. 按 `(TurnRecord.turn_sequence, Event.turn_event_index)` 生成 Event Manifest；
3. 每个 Event 计算内容 hash；
4. 生成边界 WorkingState Snapshot 和 Snapshot Hash；
5. 将 thread/epoch/segment/range/manifests/working-state hash/summary version 规范化 JSON；
6. SHA256 得到 `source_hash`；
7. 写入 `CompactionInput`，Segment 转为 `sealing`；
8. 同事务写入 `segment_sealing` Outbox Job；
9. 普通 Segment 密封会创建 successor open Segment，新的对话继续写入新 Segment。

`source_hash` 的价值是将“摘要对应的输入版本”固定下来。若 Event 内容或边界 WorkingState 改变，旧压缩结果就不能被当作同一份输入的有效结果。

## 3.4 三阶段 Segment Sealing

### Phase A：领取与验证

Handler 校验 Outbox Claim/claim token、Segment 当前状态、CompactionInput 和 source_hash，创建或恢复 CompactionRun。过期 Worker 不能凭旧 claim 提交结果。

### Phase B：语义摘要

系统从冻结 Manifest 重建源对话，核验 Event Hash；将对话脱敏，并把真实工具调用 ID 替换为本次摘要局部引用（如 `tool_1`、`failure_1`）。LLM 被要求输出语义字段：

- `goal`、`outcome`；
- `decisions`、`entities`；
- `tool_result_summaries`；
- `failure_explanations`。

模型不负责生成 open loops、active constraints、artifact refs 等确定性状态字段。校验器要求必填字段存在，拒绝模型输出越权的稳定字段，并校验每个 `item_ref` 都属于本次输入允许的集合；未知/重复引用被丢弃或产生警告。

### Phase C：确定性合并和提交

系统通过 `item_ref -> tool_call_id` 映射把模型对工具结果/失败的摘要关联回真实工具调用；同时从冻结的 WorkingState 和真实 Event 恢复：

- open loops；
- active constraints；
- artifacts；
- important tool results；
- unresolved failures；
- source turn/event IDs；
- source hash、summary version、model ID、token count。

最终执行结构和边界覆盖校验。只有校验成功才写入 SegmentSummary 并把 Segment 从 `sealing` 转为 `sealed`。LLM JSON 多次失败时有确定性抽取式降级路径，避免无限重试。

## 3.5 Epoch Rollover 与 Checkpoint

当 Epoch 达到 Segment 容量/阶段边界，`begin_epoch_sealing()` 锁 Thread 和 active Epoch，冻结当前 open Segment（空 Segment 则直接移除），记录 WorkingState 边界快照及其 hash，创建新的 active Epoch 和 open Segment，并创建 Epoch Compaction Input/Outbox 工作。

Epoch Checkpoint 与 Segment Summary 不同：Checkpoint 是对已完成 Segment Summary 的确定性聚合，不再让 LLM 对同一段历史重新做一轮自由摘要。聚合的内容受固定规则控制，例如当前目标、最近完成里程碑、open loops、约束、决策、实体、Artifact、验证过的工具状态等。

这种设计的目标是降低多层 LLM 摘要造成的语义漂移；代价是 Checkpoint 聚合质量受 SegmentSummary 字段质量和确定性聚合规则限制。

## 3.6 Sealing Bridge 的作用

Segment 进入 sealing 后，摘要可能尚未完成，而新 Segment 已经开始接收对话。Sealing Bridge 用来在压缩任务完成前暂时桥接最近边界信息，避免“旧 Segment 已不在 raw history 范围内，但新 Segment Summary 尚未生成”的信息空窗。

它属于短期过渡上下文，不等同于 SegmentSummary，也不应永久累积。其预算在上下文装配中单独裁剪。

---

# 第四部分：记忆系统与自动召回

## 4.1 MemoryRecord 是事实源，向量/索引/图谱是投影

记忆写入主链：

```text
Turn/Event
 -> Outbox memory_extraction
 -> UnifiedMemoryExtractor / Proposal Normalizer
 -> Memory Gate（准入、来源权威、敏感级别、Evidence、confidence）
 -> Conflict Candidate Discovery
 -> Conflict Adjudication（语义关系 + entity binding）
 -> MemoryWriteService.write_batch()
 -> MemoryMutationExecutor（锁、版本/hash 校验、lineage、事件）
 -> Outbox：Core Memory / Vector / Graph / Retrieval Index 等投影刷新
```

LLM 输出 MemoryProposal 不等于写入完成。候选状态、Evidence、confidence、source authority、memory type、sensitivity、durable 等规则决定它能否进入 active。低置信或证据不足可以停留在 candidate。

## 4.2 记忆冲突与实体绑定

冲突候选不是只靠字符串相似度。发现阶段并行获取：

- Exact：canonical key + scope 的精确候选；
- Retrieval：统一检索召回的语义/文本相关记忆；
- Graph：已有图谱关系证据；
- 待绑定实体候选及其关系证据。

裁决阶段让 LLM 判断 compatible、complementary、conditional、temporal_change、contradiction、uncertain 等关系，并同时提出实体复用/新建/未决建议。

服务端验证模型建议：reuse 的 Entity ID 必须在候选集合内；名称必须是本次待绑定名称；关系证据 ID 必须来自实际提供的候选证据；fact_index、subject/object 要对应真实事实位置；冲突建议不一致时降级 unresolved。模型不能直接写任意 Entity ID。

这使实体绑定发生在冲突裁决上下文中，而不是每次普通召回时再单独调用 LLM。图谱投影阶段仍会二次校验实体是否存在、Scope 是否一致；无效绑定回退按名称解析。

## 4.3 AutomaticRecallEngine：普通聊天每轮的记忆召回（BM25 架构）

核心：`memory/automatic_recall.py`、`memory/bm25_search.py`、`memory/recall_ranking.py`、`memory/recall_fusion.py`、`memory/recall_config.py`。

`ContextAssembler._load_agent_context()` 构建 agent context 时触发 Automatic Recall。Query 来自当前用户消息，使用 `ScopeContext.chain()` 限定可检索 Scope。这是普通聊天上下文装配的一部分，不依赖模型主动调用一个“RAG 工具”。

当前主路由是 **Exact + BM25 + Vector**。原先独立的 Recent Episode 路由已删除，其时间相关性进入统一排序；Lexical 仅作为 BM25 关闭或不可用时的替代/降级路由。

### 路由一：Exact Lookup

仅当 Query 符合 `_KEYISH = ^[A-Za-z0-9_.]+$` 时进入。按 `ScopeContext.chain()` 逐级查询 `canonical_key` 或 `scope_id`，严格匹配 `scope_type + scope_id`；每个 Scope 最多取 5 条，按 importance 降序，命中后停止向更宽 Scope 扫描。Exact 命中在最终排序时免于普通 relevance threshold 淘汰，保留确定性查询价值。

### 路由二：Sparse BM25（pg_search）

配置 `AIIVE_MEMORY_BM25_ENABLED` 开启且 BM25 服务可用时，调用 `Bm25SearchService.search()`。当前锁定 ParadeDB `pg_search 0.26.4`，在 PostgreSQL 的权威表 `memory_records` 上建立 BM25 索引，不复制第二份记忆事实表：

- `content`、`keywords` 使用 `pdb.jieba`，用于中文/中英混合文本；
- `canonical_key` 使用 `pdb.literal_normalized`，保留 `user.preference` 等标识符整串匹配；
- `id` 是 key field；BM25 返回 Memory ID、原始分数与排名。

查询按 Scope Chain、lifecycle、validity 限制候选。BM25 命中后只把 ID/分数视为检索信号，必须回源读取 `MemoryRecord`，再次校验状态和 Scope；随后所有候选统一经过 `ForgetVisibilityService` 的 Shield/Tombstone 过滤（fail-closed）。PostgreSQL 表事务负责维护 BM25 索引与源行更新/删除的一致性。

若 BM25 配置关闭，Lexical 作为显式选择的稀疏召回；若 BM25 已启用但扩展/索引/SQL 不可用，则回退到 Lexical，并在 `MemoryRecallPack.degraded/degraded_reason` 标记原因。不得把降级结果描述为 BM25 已生效。

### 路由三：Dense Vector（可选）

仅在 `AIIVE_MEMORY_VECTOR_ENABLED` 开启并构造 Vector Service 时加入。`MemoryVectorProjectionService.search()` 使用 Embedding Provider（`input_type=query`），在 pgvector 上按 cosine distance 检索；连接 `MemoryRecord` 并过滤 lifecycle、validity、Scope；校验 `record_version` 与当前记录一致、`embedding_model` 与当前 `embedding_model_identity` 一致；取 Top-K 后将 distance 转为 `1-distance` 并裁剪到 0..1。向量投影仅为 active + valid MemoryRecord 建立。Vector 异常会记录 degraded reason，不影响 Exact/BM25（或 Lexical fallback）候选继续参与融合。

### 候选安全过滤与融合

所有路由候选在融合前统一调用 `ForgetVisibilityService.filter_memory_ids()`。BM25 还会回源复核 canonical 记录；不能因新增加 BM25 绕过 Scope、Lifecycle、Validity 或 Forget。

`recall_ranking.py` 的排序信号为：RRF、relevance、recency、importance、trust、scope。默认权重来自 `RecallConfig.signal_weights`：

```text
fused_score =
  0.40 * rrf_norm
+ 0.20 * relevance
+ 0.20 * recency
+ 0.10 * importance
+ 0.05 * trust
+ 0.05 * scope
```

RRF 使用 `k=60`，只融合路由排名，不直接把 cosine similarity 与 BM25 原始分相加。BM25 relevance 按本批最高原始分归一化到 0..1；Vector relevance 使用 cosine 得分；Lexical 降级使用词法相关度。最终排序还结合 `automatic_recall_top_k=8`、`automatic_recall_token_budget=1200`（代码默认值，可由配置覆盖）进行打包。Exact 候选免于普通 relevance threshold。

### Recent Episode 的迁移

旧 Recent Episode 不再单独扫描最近 episodic MemoryRecord 并产生第三/第四份候选集。时间信号由 `recency_signal()` 统一计算：

- episodic/event 半衰期 3 天；task 7 天；knowledge 180 天；preference/user_profile/agent_self 365 天；未知类型默认 90 天；
- 衰减公式为 `0.5 ** (age_days / half_life_days)`；缺失时间为 0，未来时间按 age=0；
- 对 episodic/event，普通查询额外乘 0.35，显式时间敏感 Query（如最近、昨天、上次、today、yesterday）乘 1.0；
- 长期偏好和知识不因“最近发生”被无条件压过。

这不是一条额外召回路由，而是候选融合后的统一排序特征。当前没有独立 Cross-Encoder 或 LLM reranker。

## 4.4 记忆向量库怎么建立、何时刷新、怎么重建

### 表与向量生成

- canonical 表：`memory_records`；
- 派生表：`memory_vector_projections`；
- 每条投影记录关联 `memory_id`、`record_version`、`content_hash`、`embedding_model`、`embedding`；
- Embedding Provider 通过 OpenAI-compatible `/embeddings` HTTP API 调用，可接本地 TEI 或兼容远端服务；
- 文本为 `canonical_key + newline + content`（有 key 时），否则只用 content；
- 查询使用 `input_type=query`，文档使用 `input_type=document`；可配置 query prefix；响应条数、index、维度均校验。

### 增量刷新触发

Memory Mutation 写入/变更后，事务中 enqueue `memory_vector_refresh` Outbox Job。Handler 根据 memory_id + expected record_version 调 `refresh()`：

- 源记录不存在：删投影；
- 当前 record_version 不等于事件 expected_version：跳过 stale job；
- 记录不是 active+valid：删除投影；
- 否则重新生成 embedding，upsert 投影及版本/hash/model identity。

删除时只删除 `projection.record_version <= expected_version` 的行，避免旧删除任务误删新版本向量。

### 启动回填

启用向量开关后，`ensure_memory_vector_backfill()` 扫描 active+valid MemoryRecord，找出投影缺失、版本不匹配、content_hash 不匹配或 embedding model 不匹配的行，为每行按 embedding generation + memory_id + record_version 生成确定性 operation_id，幂等入队 Outbox。启动回填不是同步把所有向量算完，而是把工作交给 Worker。

### 全量重建边界

Memory 向量目前看到的是“按源版本回填/增量修复”的机制；不要把 RetrievalIndex 的 Generation 全量切换机制套用到 Memory Vector Projection。向量模型 revision、dimensions、provider、query prefix 会参与 embedding generation identity；配置变化后启动 backfill 会识别旧模型投影并刷新。若数据库投影表/索引结构需要重建，应以 Alembic migration 和运维脚本为准；源码未证明 Memory Vector 有独立双 Generation 原子切换。

## 4.5 召回结果如何注入 Prompt

AutomaticRecall 的 Pack 在 `_load_agent_context()` 被转换为 `recall_messages` / `recall_text`。`ContextAssembler` 将 Recall 文本与 Graph Expansion 共用 `retrieved_memory` 分区预算：先按 GraphConfig 的 `budget_ratio` 给图谱预留预算，再给 Memory 文本分配剩余预算；Memory 没用完的预算可回补给 Graph。图谱按关系价值顺序整条注入，预算不足时丢弃低价值关系，不把单条关系定义从中间截断。

注入形式是一个或多个 `role=system` 的上下文块，位于原始聊天历史之后、待回答问题和当前消息之前。只有最终通过 hard gate 后，才调用 `_touch_injected_memory(recall_pack)` 更新记忆访问记录；所以“检索候选”不等于“实际注入并计为访问”。

---

# 第五部分：Unified Retrieval 与历史检索

## 5.1 它不是 Automatic Recall 的另一个名字

`UnifiedRetriever` 是更通用的检索编排器，统一输出 `RetrievalHit`，主要处理：

- `memory_record`：复用 AutomaticRecallEngine；
- `segment_summary` / `epoch_checkpoint`：通过 RetrievalIndex 的 lexical/inverted index；
- Exact：按 canonical key / scope 从索引中精确命中；
- DEEP：在命中摘要后，通过 RawHistoryExpander 二阶段回溯原始 Turn/Event。

它的入口模式至少区分：

- `AUTO`：自动模式，按配置选择 memory + index 路由；
- `SEARCH`：显式搜索，允许扩大到 sleeping 等来源；
- `DEEP`：先找历史摘要/检查点，再展开到原始历史。

普通聊天的自动 Memory Recall 不应被误写成每轮都执行完整 DEEP History Search。

## 5.2 AUTO/SEARCH 的执行步骤

1. 依据 `source_types` 判断 Exact、Memory、Index 路由是否启用；
2. 每条路由超取 `max_results * 3`；
3. 各路分别计算 RRF + 归一化加权分数；
4. 合并候选并初步去重；
5. 排除已经由热分区加载的 SegmentSummary/EpochCheckpoint，避免同一摘要重复注入；
6. 批量回源读取 canonical source，校验 source version/hash、状态、scope、forgotten/archived/superseded 等；
7. fail-closed 过滤不合法候选；
8. 最终去重并按 token budget 打包。

若某一路失败，其他路由仍可工作，并在 `notes` 标记 degraded；若整个编排失败，则返回空且 `degraded=True`，不把未经校验的候选交给模型。

UnifiedRetriever 权重表中 Scope 使用 0.5 中性占位。这一点很关键：不能把权重配置项描述成已经完成真实动态 Scope Score。具体权重/归一化公式应以 `RetrievalConfig` 和 `_fuse_scores()` 当前实现为准。

## 5.3 RetrievalIndex 是什么索引

`RetrievalIndex` 面向 MemoryRecord、SegmentSummary、EpochCheckpoint 等可检索来源建立 lexical/inverted entry。每个 entry 保存来源类型、来源 ID、source version/hash、标题、search_text、snippet、scope、生命周期、retrieval tier、tokenized terms 等信息。

它不是 pgvector 向量库。当前可见 UnifiedRetriever 的 Segment/Epoch 路由使用倒排/词法索引；Memory 路由内部才可能用 pgvector。不要把“统一检索”理解成所有来源都做向量 embedding。

## 5.4 Generation 全量重建与并发刷新

Generation 用于 RetrievalIndex 的代次切换：

1. 创建 `building` generation；
2. 按 source type 分批扫描 canonical source（游标推进）；
3. 将每个来源构建为 index entry；
4. 在重建期间，增量 refresh 同时 upsert/tombstone active 与 building 两个 generation；
5. `source_version` fencing 防止旧 Outbox Job 覆盖新数据；
6. 重建完成执行验证；
7. 原子将 building 切为 active，旧 active 退役。

为什么刷新必须同时写 active + building？假设 generation G1 仍在服务，G2 正在重建；某条 Memory 在 G2 扫描它之后发生更新。如果刷新只写 G1，G2 最终激活时就带着旧内容。双代刷新保证 G2 在切换时包含重建期间的变更。

下线事件会写 tombstone：清空 token/text、标记不可搜索。覆盖 forgotten、superseded、merged、archived、source deleted 等情形。Tombstone 不等于删除 canonical 数据，而是保证检索层不能继续返回旧来源。

---

# 第六部分：Knowledge 文档 RAG

## 6.1 Knowledge 与 Memory 是两套不同的数据模型

| 维度 | Memory | Knowledge |
|---|---|---|
| 原始真相源 | MemoryRecord | Document 原文对象 + Chunk |
| 语义单位 | 一条结构化记忆 | 一个文本 Chunk |
| 向量投影 | MemoryVectorProjection | KnowledgeChunkVector |
| 词法检索 | AutomaticRecall Lexical | Knowledge Chunk lexical search |
| 向量启用开关 | `AIIVE_MEMORY_VECTOR_ENABLED` | `AIIVE_KNOWLEDGE_VECTOR_ENABLED` |
| 重建基础 | MemoryRecord | Document 原文/Chunk |
| 主要用途 | 个人事实、偏好、事件、决策 | 用户导入的文档知识 |

## 6.2 文档读取与格式边界

`document_reader.py` 将支持格式解析成纯文本和元数据。实际提取能力受文件格式、解析器和文本层限制：单文件原始 bytes 上限为 20 MiB，提取文本最多 2,000,000 字符，超出时截断并在 metadata 标记 `truncated=true`。支持 txt/md/html/json/csv/tsv、常见源码文本、docx 和 pdf 等后缀。

- PDF 使用 pypdf 文本层提取；扫描图片型 PDF 没有可提取文本层时不会凭空识别，未看到 OCR pipeline；
- DOCX / HTML / 表格等走各自解析器；
- 不执行文档宏或嵌入内容；
- 单文件和提取文本长度都有上限，超限会截断/报错而不是无限摄取。

`read_document` Tool 读取允许根目录内的原始文件，最多返回调用方请求的 max_chars（上限 200,000）；`ingest_document` 则把文件持久化到知识库，二者不是同一操作。

## 6.3 文档摄取与版本持久化

`KnowledgeIngestor.ingest(file_path)`：

1. 通过 `resolve_knowledge_path()` 检查文件位于允许知识库根目录、路径存在，并规范化为绝对路径；
2. 读取原始文件 bytes；
3. `extract_document_bytes()` 提取文本、MIME、doc type；
4. `put_content_addressed('knowledge-documents', bytes, metadata)` 以 SHA256 内容寻址方式写入 Object Store，得到 content_hash；
5. 按 content_hash 查重；
6. 若同一内容已有 Document，复用或复活该 Document；
7. 同一 normalized source_path 的其他 active 版本先 supersede 并 flush，再插入/激活新版本（遵守 active source path 部分唯一索引，避免同一事务 flush 时暂时出现两个 active 行）；
8. supersede 会立即清空旧 Chunk 与对应 Chunk Vector；
9. 新版本创建 Document，调用 `_replace_chunks()`：`parse_document_blocks()` 解析结构化 Block，`chunk_blocks()` 生成 ChunkDraft 并写入 Chunk（`chunk_text()` 仅保留为遗留兼容接口）；
10. 向量开关可用时调用 `ChunkVectorService.refresh_document()` 刷新 Chunk Vector；向量失败只告警，摄取仍可完成，检索退化为 lexical。

同一 source_path 内容不变时可复用 Document；内容回到历史版本时可以复活历史记录。文档删除是软删除并清空分块/释放对象引用；源码明确不会删除 `source_path` 指向的用户原文件。

## 6.4 Block 解析与递归切分

主摄取路径不再调用旧的 `chunk_text(text, chunk_size=500)`，而是：

```text
原始文件 bytes
  -> DocumentReader 提取/校验文本与元数据
  -> parse_document_blocks(data, filename)
  -> Block[]（类型、标题路径、行/页范围、Section ID）
  -> chunk_blocks(Block[])
  -> ChunkDraft[]（正文、结构元数据、来源片段 parts）
  -> Chunk 持久化
  -> Embedding 输入构造
  -> KnowledgeChunkVector 刷新
```

`chunk_text()` 仍存在，但它是旧接口：按 `\n` 行累积，超过字符数时冲刷，单行超长按固定字符窗口切分。不能再用它描述 KnowledgeIngestor 的当前主路径。

### 6.4.1 Block 解析

`knowledge/blocks.py::parse_document_blocks()` 将文档解析为 `Block`。核心字段包括 `content`、`block_type`、`heading_path`、`block_index`、`line_start/line_end`、`page_start/page_end`、`section_id` 与 `source_type` 等。

- Markdown：识别 H1-H6、段落、列表、表格、引用和 fenced code；标题只维护 heading_path，不作为正文 Chunk。
- DOCX：根据段落样式/outline level 识别标题，保留段落与表格结构。
- PDF：按页提取文本并保留页码；不把没有解析证据的结构伪造成标题。
- HTML：识别标题、段落、列表、代码和表格等块。
- CSV/TSV：按表格块处理。
- JSON 与常见源代码文本：作为代码/结构化文本块；无法可靠识别时退回通用文本块。
- TXT/通用文本：按段落/空行等边界形成 Block。

同一文档内重复出现的相同标题路径通过 `section_id_for(heading_path, occurrence)` 加入出现序号生成确定性 Section ID，避免两个同名章节被合并。相同原文重复解析时 ID 稳定。

### 6.4.2 递归切分与默认预算

`knowledge/chunker.py::chunk_blocks()` 先按 Section 分组，再在 Section 内切分。默认 `ChunkConfig`：

| 参数 | 默认值 | 含义 |
|---|---:|---|
| target_chunk_tokens | 450 | 目标预算 |
| max_chunk_tokens | 700 | 单 Chunk 最大估算预算 |
| min_chunk_tokens | 100 | 短尾合并参考下限 |
| overlap_tokens | 60 | 同 Section 文本块之间的上下文重叠上限 |

切分优先级为：Section/标题边界 → Block 边界 → 段落/行 → 句子 → 标点 → 字符窗口。短 Section 尽量保持完整，不为凑满目标长度强行拆分；超长 Block 才逐级降级。所有输出最终经过 `_enforce_budget()`，要求 `count_tokens(chunk.content) <= max_chunk_tokens`。

Token 计数来自 `knowledge/tokenization.py::count_tokens()`，是确定性启发式估算：CJK 字符按字符计数，非 CJK 连续字符按约每 4 字符一个估算 Token。它不是 Embedding 模型的真实 BPE Tokenizer，因此 700 是系统内部估算上限，不等于模型端严格 700 Token。

### 6.4.3 特殊结构处理

- **代码块**：尽量按顶层定义/函数边界拆分；超长函数/代码行再按行与字符窗口兜底。递归片段携带原始字符偏移，最终映射为来源行号。
- **表格**：超长表格按行/单元格边界拆分，续块保留表头上下文；超长单元格递归拆分；空单元格与列顺序必须保留；表头本身超预算时退化为文本切分，不能静默丢内容。
- **列表/引用**：尽量作为结构块保留，超长时才递归拆分。
- **Overlap**：仅在同一 Section 的普通文本块之间生成；代码/表格不使用普通文本 Overlap。Overlap 与重复表头被标记为 context，不计入本 Chunk 的来源行/页范围，`context_tokens` 单独记录。

### 6.4.4 来源定位与兜底拆分

内部 `_Segment(text, start, end)` 保存当前输入文本中的字符区间；`ChunkPart` 保存文本、line_start/line_end、page_start/page_end 与 `context` 标记；`ChunkDraft.parts` 保留组成该 Chunk 的来源片段，供最终预算兜底拆分继续使用。

`_enforce_budget()` 检查每个 Draft 是否超过 max；超限时 `_split_draft()` 按 ChunkPart 再拆、`_pack_parts()` 重新装箱、`_draft_from_parts()` 生成子 Draft。只有真实正文片段能组成输出 Chunk；若上下文不能与后续正文共存，优先舍弃上下文。`context_tokens` 只统计最终保留的 context；行号和页码只由真实正文片段汇总，缺失页码保持 None。

实现要点（当前行为）：

- 超长表格行/单元格预算、重复标题 Section ID、递归拆分行号来源映射、无首尾竖线 Markdown 表格识别均已处理。
- 代码超长行的局部偏移与 rebase 语义统一；兜底拆分保留 ChunkPart 来源与 context_tokens；表格空单元格不丢失。
- `ChunkPart` 携带 page_start/page_end 并贯穿兜底路径；`_pack_parts()` 只输出含真实正文的组，不产生纯 Overlap Chunk；`_draft_from_parts()` 只根据真实正文片段汇总行号与页码。
- `chunk_blocks()` 跳过 heading Block，按连续相同 Section ID 聚合，逐 Section 切分，最终 chunk_index 全局连续。
- 当前 `CHUNKER_VERSION=structure_recursive_v4`，`PARSER_VERSION=structure_v2`。

当前实现不包含 Parent-Child 双层 Chunk、LLM 语义切块、自动 Chunk 摘要、动态模型切块或 Cross-Encoder Chunk reranker。它是结构感知的确定性递归切分器，不应描述成语义模型切块。

## 6.5 Chunk 向量的生成与检索

`KnowledgeChunkVector` 是 Chunk 的派生投影。`ChunkVectorService.refresh_document(document_id)`：

1. 按 chunk_index 读取该文档全部 Chunk；
2. 若没有 Chunk，删除该文档向量；
3. 读取已有向量投影；
4. 构造包含文档标题 + heading_path + Chunk 正文的完整 Embedding 输入，比较完整输入 Hash、`input_version` 与 `embedding_model`，只对过期/缺失 Chunk 生成 embedding；
5. 使用 Embedding Provider 的 `embed_batch()`，batch size 取配置；
6. upsert vector、hash、model identity；
7. 删除不再属于当前文档 Chunk 集合的孤儿投影。

查询侧用 query embedding 对 pgvector 执行 cosine distance，并回源关联 Chunk/Document，过滤 inactive Document、模型身份不匹配及 `input_version` 过期投影；刷新/维护侧用完整 Embedding 输入 Hash 检查新鲜度。失效文档即使残留向量也不应被召回。

## 6.6 Embedding 输入与 HNSW 索引

两类向量投影均使用 512 维 pgvector Vector：

| 表 | 主键 | 内容哈希 | 模型身份 | 索引 |
|---|---|---|---|---|
| `memory_vector_projections` | memory_id | content_hash | embedding_model | HNSW + vector_cosine_ops |
| `knowledge_chunk_vectors` | chunk_id | content_hash | embedding_model | HNSW + vector_cosine_ops |

ORM `db/models.py` 中显式声明 `postgresql_using='hnsw'` 与 `postgresql_ops={'embedding':'vector_cosine_ops'}`；注释说明 Alembic migration 以裸 DDL 创建相同索引。SQLite 将 Vector 映射为 JSON，不具备 pgvector 向量索引语义。

索引类型确认为 HNSW；但没有看到在 ORM Index 声明中显式指定 m、ef_construction、ef_search 等参数，因此不应编造这些调优参数。

Embedding Provider 使用 OpenAI-compatible `/embeddings`：

- document 输入不加 query_prefix；
- query 输入可按 `AIIVE_EMBEDDING_QUERY_PREFIX` 添加 BGE 查询指令；
- 请求校验 data 条目数、index、向量维度；
- embedding_model identity 包含 model revision，避免模型升级后混用旧向量；
- local provider 默认可由 TEI 提供，远程 provider 走兼容 API。

## 6.7 Knowledge 混合检索与降级

知识库查询把 Lexical Chunk Search 与 Vector Chunk Search 合并，采用 RRF 融合、去重和 Top-K。向量通道仅当开关开启、数据库是 PostgreSQL 且向量表可用时工作；SQLite/未启用/服务失败时走纯 lexical。向量失败不应返回伪造的语义结果。

向量侧的具体 SQL 路径是：对 Query 生成 embedding，计算 `KnowledgeChunkVector.embedding.cosine_distance(query_vector)`，连接 `Chunk` 与 `Document`，过滤 `Document.status` 不属于 `superseded/deleted`，过滤 embedding model identity 不匹配的投影，按 cosine distance 升序取 limit。得分转换为 `1 - distance` 并裁剪到 0..1。它并没有在此处做 LLM 二次判断。

需要留意一个与 Memory Vector 不同的实现细节：Knowledge `search()` 当前的 SQL 过滤条件显式排除 inactive document 状态，并检查模型身份；它没有在这个查询片段中像 Memory Vector 那样对每条候选比较一个独立 `record_version`。内容新鲜度主要由 `content_hash` 在刷新/backfill 时维护，维护扫描负责发现缺失/孤儿向量。不能把两套投影的校验字段混为一谈。

**没有从已读代码确认 Cross-Encoder / LLM reranker。** 当前明确可见的是 Lexical + Vector + RRF，不是“混合检索后再经模型重排”。

## 6.8 混合召回的精确公式

`search_chunks(db, query, limit=5)`：

1. Query strip + casefold；
2. limit clamp 到 1..100；
3. 两路各自超取 `depth = min(limit * 4, 100)`；
4. Lexical route；
5. Vector route；
6. RRF 融合并按 chunk_id 去重；
7. 返回最终 limit 条。

### Lexical

- Query 按空白分词，去重，最多 12 个 token；
- 每个 token 生成 `Chunk.content ILIKE '%token%'`；
- 转义 `%`、`_` 和反斜杠；
- 先按 Document.created_at desc、Chunk.chunk_index asc 取候选，最多 `min(1000, depth*20)`；
- Python 排序优先级：整句包含 > 命中 token 数 > 较小 chunk_index。

这不是 BM25，也不是 PostgreSQL tsvector/tsquery；是可移植 ILIKE + 命中覆盖度排序。

### Vector

- `vector_table_ready()` 检查开关、PostgreSQL 方言和表存在；
- query 用 input_type=query 生成向量；
- pgvector 计算 cosine_distance；
- 只使用 embedding_model identity 匹配的投影；
- 取 `min(depth, AIIVE_KNOWLEDGE_VECTOR_TOP_K)`；
- 转换 score=`1 - cosine_distance` 并 clamp 到 0..1；
- 低于 `AIIVE_KNOWLEDGE_VECTOR_MIN_SIMILARITY` 的结果丢弃；
- 任何向量服务异常降级为空 Vector 候选，保留 Lexical 结果。

### RRF

常量：`_HYBRID_RRF_K = 60`，两路权重均为 0.5：

\[
RRF(chunk)=\frac{0.5}{60+rank_{lexical}}+\frac{0.5}{60+rank_{vector}}
\]

只在某一路出现的 Chunk 只有该路贡献；两路都出现会叠加。最终按 RRF 分数降序取 limit。没有 Cross-Encoder、LLM rerank 或基于 query-context 的第二阶段精排。

## 6.9 检索结果与进入 Agent 上下文

`_chunk_hit()` 返回：

- chunk_id；
- document_id；
- source_path；
- content_preview（Chunk.content 前 200 字符）；
- line_start / line_end；
- matched_terms；
- vector_score（若走 Vector）；
- hybrid_score（融合后）。

因此 `search_knowledge` 返回的是检索命中与 200 字符预览，不是完整 Chunk 正文。Worker 下一步如果需要完整上下文，必须通过 `read_document` 读取允许根目录中的原文，或使用其他明确读取路径；不能把 preview 当成完整 Chunk。

普通 Conversation Agent 固定只注入五个任务元工具，知识库能力由 Persistent Task Worker 的 Skill/Capability 工具视图按需使用。典型流程：

```text
主对话 Agent
  → delegate_task
      → Task Worker 获得 search_knowledge
          → search_chunks()
              → lexical + vector + RRF
                  → 200 字符 preview + source_path/line range
          → 需要全文时调用 read_document
          → EvidenceStore 将工具结果保存为 Evidence/Artifact
          → 下轮 Task Context 只注入有界摘要和引用
```

这与 `ContextAssembler` 自动注入 MemoryRecallPack 是两条不同链路。Knowledge RAG 不会无条件把整个知识库或全部 Chunk 自动注入每轮主对话 Prompt。

## 6.10 文档元数据/原文变化时如何更新

- 同一 source_path 内容变化：新 content_hash 产生新版本；旧 Document 被标记 `superseded`，旧 Chunk 与 Chunk Vector 被清空，新版本重新建 Chunk/Vector。
- 同一内容重新导入：复用 content_hash 对应 Document；若历史版本处于 inactive，则先使同路径其他活跃版本失效，再复活历史记录；Chunk 缺失则从提取出的文本重建。
- 原文对象缺失/Chunk 缺失/重复 Chunk/孤儿向量/孤儿对象：Knowledge Maintenance 扫描产生 issue；对可恢复文档可从持久原文重新分块，对孤儿派生数据执行清理。
- Chunk 内容、标题/heading_path、Embedding 输入模板版本或 embedding model identity 任一变化：`refresh_document()` 会重算完整输入 Hash 并刷新向量；维护扫描 `missing_vector_document_ids()` 还检查缺失投影、模型身份、input_version、历史空 Hash 与投影 Hash 不匹配。

这里要区分“元数据变化”和“正文变化”。`content_hash` 针对原始文件内容；Document 的 title/path/doc_type 等元数据变化不必然代表 Chunk 正文变化。是否触发全量 Chunk 重建，取决于实际调用 `ingest/reindex` 的路径，不能简单断言所有 metadata 字段变化都会自动重新 embedding。

### 手工 reindex

`KnowledgeIngestor.reindex(document_id)` 从 content-addressed object store 读取原始 bytes，并用 content_hash 校验；不依赖原 source_path 文件仍然存在。随后重新 extract、chunk、refresh vector。原文对象丢失或 hash 校验失败时进入 index_failed/错误状态，不会假装重建成功。

### 向量投影 freshness 的一个注意点

`ChunkVectorService.refresh_document()` 使用完整 Embedding 输入（文档标题 + heading_path + Chunk 正文）的 Hash，而不只是正文 Hash；新鲜度同时比较 `content_hash`、`input_version` 与 `embedding_model`。当前 `missing_vector_document_ids()` 已检查缺失投影、模型身份、输入模板版本、历史空 `Chunk.embedding_input_hash`，以及 `KnowledgeChunkVector.content_hash != Chunk.embedding_input_hash`。因此维护扫描能够发现同 ID Chunk 的输入 Hash 不一致。

## 6.11 知识库维护与重建边界

`knowledge/maintenance.py` 的扫描项包含：orphan chunks、duplicate chunks、inactive document stale chunks、missing chunks、missing object、orphan objects、expired superseded objects、missing chunk vectors、orphan chunk vectors。

维护不是无条件重建所有知识：先扫描、形成报告，再按 issue 类型执行可修复动作。Chunk 可由持久 Document 原文重建；向量可由 Chunk 重建；孤儿对象可按引用关系清理。若原文对象已经丢失，则不能凭空恢复原文内容，应标记不可恢复/需用户重新导入。

## 6.12 Knowledge 权限边界

`routes_knowledge.py` 的 ingest 通过 `resolve_knowledge_path()` 限制可读取的本地文件根目录，防止任意本地文件读取；但 `search_chunks()` 查询 Document/Chunk 时没有用户 ID、workspace_id、ACL 或 per-document permission 参数，检索条件主要是 Document.status 不属于 inactive 状态。

所以当前实现能证明“摄取路径有本地目录根限制、只搜活跃文档”，不能证明“多用户环境下每个用户只会召回有权限的文档”。如果部署为多租户系统，必须在 Document/Chunk 元数据和 search SQL 中加入授权 Scope，并在 Vector 与 Lexical 两路同时过滤，再在结果回源阶段复核。

---

# 第七部分：时序知识图谱

## 7.1 Graph 是 Memory 的派生投影，不是独立事实库

MemoryRecord 的 `graph_facts` 保存结构化事实候选，GraphEntity/GraphRelation 是读侧投影。关系行保存 source_memory_id、source_record_version、Scope、confidence、source_span、valid_from/valid_to、status 等，能追溯到支持它的原始 Memory。

投影入口由 Outbox `memory_graph_refresh` 驱动，Memory 创建、加强、取代、合并、晋升、睡眠、唤醒、归档等状态变更共用投影逻辑。

## 7.2 关系投影规则

- 关系 key 由来源 Memory + 三元组 + 事实起始时间确定；同一事实重复投影幂等更新；
- 新版本不再包含某事实时，只撤回该来源 Memory 自己的旧关系；
- 只有明确登记为单值的谓词才允许冲突关闭；
- 关闭旧关系还必须有 Memory 层权威 supersede 证据（旧 Memory validity 失效且 superseded_by 指向新 Memory）；
- 多值/未知谓词不会因新客体出现而关闭旧关系；
- 遗忘会删除关系内容，不只是把关系标记 inactive。

实体解析优先使用裁决阶段绑定的 Entity ID，但投影时再次检查实体存在和 Scope 一致。不满足则退回按规范化名称在相同 Scope 内 upsert；名称可作为 alias 保存。

## 7.3 图谱扩展何时发生、如何注入

普通聊天的 Memory Recall 得到 Memory ID 后，图谱扩展可从这些 Memory 的关系事实中抽取实体种子；也可从 Query 匹配已知实体名/别名。随后按有限 Hop 遍历 active 且有效关系，回查来源 Memory 状态、Forget 屏蔽、Scope 和时间有效性，再按置信度/价值排序。

它不是全图 BFS，也不是每轮让 LLM 判断扩展策略；扩展有最大 hop、候选和 token 预算，失败不应破坏普通 Memory Recall。最终 Graph Expansion 文本与记忆召回共用 `retrieved_memory` 分区，关系以完整结构块注入 system context。

## 7.4 图谱重建

`GraphRebuildService.rebuild()` 从 canonical MemoryRecord 的 `graph_facts` 重建派生图谱，使用与实时投影相同的 `GraphProjectionService`，不调用 LLM、不访问网络；relation_key 保证幂等。过程按 batch 扫描 MemoryRecord，刷新包含 graph_facts 或存在历史关系的来源，再清理孤儿 Relation/Entity，最后 verify。

`repair_overclosed()` 用于修复旧版本错误地关闭的多值关系：检查来源 Memory 是否 active、validity 是否 valid、canonical graph_facts 是否仍主张该关系；只有仍被源 Memory 明确支持的关系才恢复。可先 dry-run 统计。

---

# 第八部分：持久任务、Goal 与 Outbox

## 8.1 Persistent Task 的恢复模型

Task Worker 每次 Run 只提出一个 Action 或控制决策，不把一个长任务变成单次巨大工具调用。每轮先恢复 AgentTask/Checkpoint/Action/Evidence/Artifact 状态，检查预算、目标暂停状态和待执行 Action；有已持久化 Action 就优先恢复/结算，没有才构建 Task Context 调 LLM 决策。

Task Context 不是聊天历史副本。它由任务目标、Task Brief、Task State、最新 Checkpoint、最近 Action/Evidence/Artifact/重要事件、当前工具视图、Skill 和 Goal 当前步骤组成。它的恢复不依赖全量重放 Thread。

## 8.2 能力发现和执行边界

Task Worker 采用 L0 常驻工具 + 按需发现能力的分层注入。能力目录规模较小时可以全量 Schema 注入；目录较大时由 `search_capabilities` / `describe_capability` 查找和加载具体能力。执行时仍经过 CapabilityBroker：检查 Capability 状态、Descriptor Hash、TaskScope、参数、前置条件、资源锁、风险和审批。

审批请求绑定 Action ID、参数 hash、Descriptor hash、Checkpoint、风险快照、Preconditions、Effects 和过期时间。批准时必须重新核验工具描述/参数/前置条件；不是只看 approved=true 就执行。

## 8.3 Goal Plan 与问题闭环

GoalPlan 是任务内的结构化长期计划，包含 GoalStep、依赖关系、步骤类型和状态。依赖图需要无环，步骤类型可区分自主执行、用户交互和审批要求。

Goal Trigger 的处理链：确定性规则扫描（once_at/recurring/window_once/event_condition/opportunistic）→ Outbox 决策 Job → 检查已有决策/前台 Turn/Lease → LLM 产生结构化决策 → 持久化决策 → 服务端校验并应用 → 创建主动消息/问题 → 标记已应用。

“先持久化 LLM 决策，再应用”使 Outbox 重试时能复用原决策，不必重复调用 LLM；decision_id 和 fencing token 防止重复/过期执行者。

主动消息写入真实 TurnRecord/Event 时间线，不是 UI 临时通知。若前台 Turn 正在执行，后台主动消息让出，避免时间线乱序。

## 8.4 Outbox 的可靠性边界

Outbox 将业务变更与 Job 记录放在同一数据库事务中，避免“业务提交成功、消息入队失败”的双写窗口。Worker 使用 `SELECT FOR UPDATE SKIP LOCKED` 领取 pending 或 Lease 过期任务，写入 claim token、locked_by、lease_expires_at。

Handler 返回 COMPLETED / RETRY_LATER / RETRYABLE_ERROR / NON_RETRYABLE / CONTINUE / CLAIM_LOST 等结果；过期 Worker 必须通过 claim token/fencing 校验才能提交。这里提供的是至少一次投递 + 幂等业务处理，不应宣称任意外部副作用天然 exactly-once。外部系统若不支持幂等键，仍需专门处理不确定结果。

---

# 第九部分：遗忘与数据可见性

Forget 不是单条 `DELETE`，而是一个“先屏蔽、再发现依赖、重建派生内容、Scrub、验证”的持久化 Saga。Forget 实现涉及 `phase_a_shield.py`、`selector_normalizer.py`、`visibility_service.py`、`handlers_forget.py`、`reconcile_service.py` 和 10 张 Forget 表；详细实现见「Forget Saga」一节。

核心时序：

```text
用户请求
  -> Phase A：同一请求事务中冻结 Selector、建立 Shield/Tombstone、入队 Cascade
  -> Phase B：按批次物化 ForgetTarget / ForgetDependency
  -> Phase C：重算 Evidence、重建 SegmentSummary / EpochCheckpoint
  -> Phase D：Scrub 正文与派生数据
  -> Phase E：Verify；成功后退休临时 Shield
```

必须区分：

- **Shield** 是临时的即时可见性屏蔽层；**Tombstone** 是长期保留的可见性/重摄取阻断记录。
- Phase A 的立即屏蔽不代表物理删除已经完成；真正 Scrub 由后台 Outbox 阶段推进。
- `memory_only` 不等于“所有聊天原文一定完整保留”：目标 Memory 的独占来源 Event 可能在 `_scrub_batch()` 中被清空。
- `verified` / `verified_with_coarse_purge` 的验证范围有限，不能解释为所有内容承载表均已逐项扫描并证明无残留。
- `all_user_data` 当前宽清理 SQL 未按用户 ID 过滤；如果部署为多用户/多租户系统，这会构成跨用户数据影响风险。
- 还需注意：Task Runtime 的 Evidence/Artifact 等对象是否纳入 Forget 需要单独核实，不能只根据“all user data”名称推断已经清理。

「Forget Saga」一节给出 10 张表、阶段执行、游标、依赖关系、恢复逻辑及静态审计风险。

---

# 第十部分：配置、部署与“功能已实现”判断

## 10.1 向量功能默认关闭：区分代码默认值与示例环境文件

`backend/aiive/config.py` 与 `.env.example` 需要交叉核对，二者不能混为一谈。

| 配置项 | `config.py` 的代码默认值 | `.env.example` 示例值 |
|---|---|---|
| `AIIVE_MEMORY_VECTOR_ENABLED` | `false` | `false` |
| `AIIVE_KNOWLEDGE_VECTOR_ENABLED` | `false` | `false` |
| `AIIVE_EMBEDDING_PROVIDER` | `openai_compatible` | `local` |
| `AIIVE_EMBEDDING_MODEL` | `text-embedding-3-small` | `BAAI/bge-small-zh-v1.5` |
| `AIIVE_EMBEDDING_MODEL_REVISION` | 空字符串 | 固定 BGE revision |
| `AIIVE_EMBEDDING_DIMENSIONS` | `512` | `512` |
| `AIIVE_EMBEDDING_QUERY_PREFIX` | 空字符串 | BGE 中文检索前缀 |
| `AIIVE_EMBEDDING_BATCH_SIZE` | `4` | `4` |

因此，**没有实际 `.env` 覆盖时，代码默认 Provider 是 OpenAI-compatible，而不是 local BGE**；如果部署者把 `.env.example` 复制为 `.env` 并保留示例值，运行配置才会切到 local TEI + BGE。两个向量功能开关在代码默认值和示例文件中都关闭。

关闭时：

- Memory 的 Exact + BM25（或 BM25 关闭/故障时 Lexical）仍可运行，Vector 路由不加入召回；Recent Episode 已并入统一 Recency 排序，不再是独立路由。
- Knowledge 的 Lexical Chunk Search 仍可运行，Chunk Vector 不生成/不查询。
- 不能因为代码中存在 pgvector 表、HNSW 索引和 Vector Service，就声称当前部署实例正在使用向量 RAG。

启用时还需核验 PostgreSQL 方言、pgvector extension、迁移、Embedding 服务健康、512 维响应和模型身份。Memory Vector 有更严格的运行时校验；Knowledge Vector 的向量表不可用时会降级为纯词法检索。

仓库中的 `docs/reports/2026-10-26_V46_embedding_hybrid_rag_report.md` 记录了 BGE-small-zh + TEI + PostgreSQL/pgvector 的真实集成测试、检索评测和 2 GiB cgroup 内存测试结果。这些是**仓库已有报告中的证据**，不是在当前部署环境重新启动服务得到的实测结果。

## 10.2 功能状态矩阵

| 能力 | 源码状态 | 运行时默认/触发 | 本文核验边界 |
|---|---|---|---|
| 对话 AgentGraph / Tool Policy | 已实现 | 正常对话路径 | 未逐节点审计全部异常分支 |
| Context Assembler | 已实现 | 每次普通聊天组装上下文 | 预算值随模型和环境配置变化 |
| Turn 内多轮裁剪 | 已实现 | 超预算时最多 5 轮 | 不等于持久化压缩 |
| Segment Sealing / Epoch Checkpoint | 已实现 | Worker 扫描、阈值和边界触发 | 未重新测量生产调度频率 |
| Automatic Memory Recall | 已实现 | 普通对话上下文装配路径 | 受 Scope、生命周期和 Forget 过滤限制 |
| Memory Vector | 已实现、可选 | 默认关闭 | 开启依赖 PostgreSQL/pgvector + Embedding 服务 |
| Unified Retrieval AUTO/SEARCH/DEEP | 已实现 | 按显式检索请求/配置触发 | 不等同每轮自动执行 DEEP |
| RetrievalIndex Generation 重建 | 已实现 | 由索引重建任务/入口触发 | 与 Memory Vector 不同 |
| Knowledge 摄取 / Chunk | 已实现 | 用户导入、reindex 或维护触发 | 结构化 Block + Section 感知递归切分；Token 为启发式估算 |
| Knowledge Hybrid Lexical + Vector | 已实现、可选 | Vector 默认关闭 | 未发现 Cross-Encoder/LLM reranker |
| Knowledge 多用户 ACL | 未发现完整实现 | — | 搜索 SQL 未见 user/workspace ACL 过滤 |
| Graph Projection / Expansion / Rebuild | 已实现 | 受配置与召回路径影响 | Graph 是 Memory 派生投影 |
| Memory Extraction / Conflict Confirmation | 已实现 | Turn 后台 Outbox 提取；冲突时进入裁决/确认 | 本文已追踪主链；未逐一枚举所有 mutation 字段 |
| Forget Saga | 已实现，来源 Event 隔离闭环已补齐 | 用户请求触发 Phase A，后续 Outbox 推进 | 来源 Event 可见性/防重抽/摘要重建/memory_only 保留/Verify 已按 13.11 补齐；mode/selector 契约与 Turn ID 清理已按 13.12 补齐；宽范围 Scrub 与 Task/Knowledge 对象覆盖仍有审计缺口 |
| Persistent Task Runtime | 已实现 | 用户委派或事件唤醒 | 已追踪 Run/Action/Evidence/Checkpoint 主链 |
| Desktop Unknown Reconciliation | 已实现 | Node journal 回执/缺失报告触发 | 不代表普通 MCP/Shell 也具备同等对账能力 |
| General External Action Exactly-once | 未能证明 | — | 必须按 capability 单独定义幂等/回执 |
| Watcher / Goal Trigger | 已实现 | time/file/node Watch 与 Goal 事件触发 | Goal 全部触发规则未逐项复核 |
| Outbox | 已实现 | 后台 Worker | at-least-once + lease/fencing，不自动等于 exactly-once |
| MCP stdio | 已实现 | curated catalog + npm 白名单 + Bootstrap | 不具备已证明的 OS 级强隔离 |
| MCP SSE / Streamable HTTP / pip | 未发现实现 | — | Installer 明确只允许 stdio + npm |
| SelfDev Repair / Promotion | 已实现 | 显式修复/晋升流程 | 局部测试不等于全量回归；候选 OS 沙箱未证实 |
| Forget 覆盖 Task Evidence / Artifact / Knowledge 原文 | 未能证明 | — | 需要补充 provenance 与对象存储清理审计 |

---

# 第十一部分：关键问题与风险判断

## 11.1 当前 RAG 术语建议

为了后续团队沟通不混乱，建议文档和代码评审统一使用：

1. **Memory Recall**：普通对话上下文装配时对 MemoryRecord 做 Exact + BM25 + 可选 Vector；BM25 不可用时 Lexical 降级，Recent Episode 已并入 Recency 排序；
2. **History Retrieval**：UnifiedRetriever 对 SegmentSummary/EpochCheckpoint 做倒排检索，DEEP 模式可回溯原始 Turn/Event；
3. **Knowledge RAG**：对 Document/Chunk 做 Lexical + Vector 混合检索。

只有第三条明确是典型文档 RAG；第一条更准确叫长期记忆召回；第二条是对话历史检索。三者可以在架构总图里统称 Retrieval Subsystem，但不能用一个 RAG 流程描述全部。

## 11.2 Rerank 的准确结论

源码明确可见的排序包括：

- Memory Recall：RRF + relevance/scope/recency/importance/trust 加权；
- Unified Retrieval：多路 RRF/归一化评分 + 回源验证 + 去重；
- Knowledge：Lexical/Vector 候选融合（RRF）+ Top-K。

没有从这些实现看到独立 Cross-Encoder reranker 或 LLM rerank。后续如要确认全仓是否存在 reranker，应继续搜索 `rerank`、`cross_encoder`、`bge-reranker` 等调用和依赖；在此之前结论写为“当前审计路径未发现”，而不是断言全仓绝对不存在。

## 11.3 向量索引类型

已在 `backend/aiive/db/models.py` 中确认：`MemoryVectorProjection` 与 `KnowledgeChunkVector` 均声明 `postgresql_using="hnsw"`，并使用 `vector_cosine_ops`。模型注释说明 Alembic migration 以裸 DDL 创建相同索引。SQLite 的 Vector 字段是 JSON 变体，不具备 pgvector HNSW 语义。当前未看到显式配置 m、ef_construction、ef_search 等参数，因此只记录 HNSW 类型，不编造索引调优值。

## 11.4 关键一致性边界

- Memory Vector：record_version + content_hash + embedding_model identity；
- Knowledge Vector：chunk content hash + embedding model identity；
- RetrievalIndex：source_version/source_hash + generation + tombstone；
- Graph：relation_key + source_record_version + canonical graph_facts + authoritative supersede；
- Compaction：Turn/Event Manifest + content hash + WorkingState snapshot hash + source_hash + summary_version；
- Outbox：operation_id + claim_token + lease/fencing。

这些版本/hash 不是装饰字段，而是防止异步旧任务覆盖新状态的关键约束。

---

# 第十二部分：Memory 写入与冲突处理——从用户消息到 canonical MemoryRecord

这是 Memory 链路中需要完整说明的一条。Memory 不只是“LLM 提取后写数据库”，真实实现是：异步触发、来源校验、提案提取、候选发现、语义裁决、确定性 Gate、事务写入、派生投影。

## 12.1 完整执行链

```text
用户 Turn 完成
   │
   ├─ Turn/Event 持久化
   │
   └─ 入队 memory_extraction OutboxJob
          │
          ▼
Phase A：短事务
  1. 校验 source Turn / Thread / schema_version
  2. 创建或接管 MemoryIngestionRun
  3. 校验 Outbox claim_token + lease
          │ commit
          ▼
Phase B：事务外 LLM 提取
  UnifiedMemoryExtractor.extract()
  → MemoryProposal[]
          │
          ▼
Phase B2：冲突预判（仍在事务外）
  只读 DB 候选发现：Exact + Unified Retrieval + Graph + Entity
  → SemanticConflictAdjudicator 一次 LLM 裁决
  → validate_entity_bindings()
  → ProposalConflictPlan
          │
          ▼
Phase C：新短事务
  1. 再检查 Outbox claim_token / lease
  2. 再检查 MemoryIngestionRun.execution_token
  3. MemoryWriteService.write_batch()
  4. 写 MemoryRecord / Evidence / Proposal / Lineage / Event
  5. 同事务创建投影 Outbox Job
  6. IngestionRun 标记 succeeded
          │ commit
          ▼
异步投影消费者
  Core Memory / Graph / Vector / Retrieval Index / File Projection
```

源码入口：`backend/aiive/worker/outbox_handlers.py::handle_memory_extraction`。

## 12.2 Phase A：来源校验与 IngestionRun 幂等

Phase A 使用独立 Session，核心目的是避免同一 Turn 被重复提取或两个 Worker 同时写入。

1. `_validate_source_turn()` 检查来源 TurnRecord、Thread、Turn 标识和 Job payload 的 schema version。来源不合法会走不可重试错误，而不是拿任意消息作为事实来源。
2. `_resolve_ingestion_run()` 根据 source Turn 创建或读取 `MemoryIngestionRun`。已经成功则直接返回 `already_succeeded`；存在仍有效的执行租约则返回 `busy`；过期执行可被接管。
3. Phase A 把 Run 标记为 running 并提交，执行 token 使用当前 Outbox `claim_token`。
4. LLM 阶段不会持有这个 Session。

相关约束：Outbox Job 的 claim token 和 MemoryIngestionRun 的 execution token 是两道不同的 fencing。前者证明 Worker 仍拥有队列任务；后者证明 Worker 仍拥有这一次提取运行。

## 12.3 Phase B：LLM 提取不等于写入

`UnifiedMemoryExtractor.extract()` 接收：

- 用户消息；
- 助手回复；
- Thread ID、Trace ID；
- 真实 source Event ID；
- 助手 Event ID（如存在）。

它生成 `MemoryProposal`，其中可能包含：

- `canonical_key`、`memory_type`、`scope_type/scope_id`；
- 内容、结构化值、confidence、importance；
- trust、sensitivity、durable、pinned、validity 等字段；
- Evidence 列表；
- graph_facts（三元组、source_span、confidence、有效时间）；
- source_turn_id / source_turn_record_id / source_event_ids；
- proposal_id、idempotency_key、content_hash。

重要边界：模型只负责提出结构化候选。`UnifiedMemoryExtractor` 使用 JSON mode；解析/Schema 校验失败时最多进行一次带错误反馈的重试，仍失败则该次提取返回空候选。关系事实还要经过 `_verified_graph_facts()`：三元组必须完整，`source_span` 必须能在用户原文或记忆内容中定位（空白折叠后子串匹配），每条 Memory 最多保留配置上限内、按 confidence 排序的事实。是否成为 active MemoryRecord，要经过 Gate 和 ConflictResolver。`graph_facts` 也不是直接写入 GraphRelation，而是先作为 canonical MemoryRecord 的字段，随后由 GraphProjectionService 异步投影。

提取 Handler 先过滤系统消息来源。它不会因为一条 system/runtime 消息里包含用户描述，就自动把它当作新的用户事实来源。

## 12.4 Phase B2：冲突候选发现的三条路径

`MemoryConflictService.collect_candidates()` 为每个 proposal 创建 `ConflictCandidateFinder`。候选发现只读数据库，不写任何记忆表。

### 路径 A：Exact Key

查询条件的核心组合：

```text
MemoryRecord.canonical_key = proposal.canonical_key
AND MemoryRecord.scope_type = proposal.scope_type
AND MemoryRecord.scope_id = proposal.scope_id
AND lifecycle_state IN conflict_states
AND validity_state = valid
ORDER BY updated_at DESC
LIMIT candidate_limit
```

因此，Exact 是“同 Key + 同 Scope”的强候选，不是只要 Key 一样就跨 Scope 合并。

### 路径 B：Unified Retrieval

- Query 使用 proposal.content，最长 400 字符；
- RetrievalMode.AUTO；
- source_types 固定为 `memory_record`，不会把历史摘要误当成旧记忆；
- max_results 为 `candidate_limit * 2`；
- 仅保留 `final_score >= retrieval_min_score` 的 MemoryRecord。

该路径的作用是发现“canonical_key 不同但语义相近”的潜在冲突。例如新提案 Key 为 `user.work_location`，旧记录 Key 为 `user.office_city`，精确键查不到，但 Unified Retrieval 可能召回。

### 路径 C：Graph

对 proposal.graph_facts 的每个完整三元组：

1. 在提案 Scope Chain 中解析 subject Entity；
2. 计算 predicate family；
3. 查询 active GraphRelation，要求 subject_entity_id 相同、predicate 属于同一 family、Scope 匹配；
4. 每个 fact 最多超取 `limit * 4` 条关系；
5. 把旧关系来源 MemoryRecord 也加入候选；
6. 同时生成 graph_evidence，包含 relation_id、subject/object entity ID、旧客体、新客体、谓词基数、Scope、confidence、valid_from。

图谱路径的作用不是简单查字符串，而是判断“同一主体的同一关系族是否已经有其他客体”。

### 实体候选收集与确定性别名绑定

候选发现同时为 proposal.graph_facts 收集 Entity 候选：

- 先看本次 Graph Evidence 中已经出现的关系两端实体；
- 再在 proposal 自身 Scope 内扫描有界 Entity 集合；
- 匹配信号包括 exact name、alias、substring、token overlap；
- 只有唯一命中的已确认 alias 会直接生成 `resolved_entity_bindings`；
- 多个实体都可能匹配时，不按 SQL 返回顺序选一个，而是交给 LLM 做消歧。

最后所有候选统一做 ForgetVisibility 检查、真相源回查、相同 content_hash 排除和 Top-N 截断。内容完全相同的记录会作为 reinforce 处理，不会被当成冲突对象。

## 12.5 语义裁决：一次 LLM 调用同时判断冲突与实体绑定

`SemanticConflictAdjudicator.adjudicate()` 接收：

- 新 proposal；
- Exact/Unified Retrieval/Graph 召回的旧 MemoryCandidate；
- graph_evidence；
- entity_candidates；
- pending_entity_names。

模型同时输出两类结论：

1. 新旧记忆的关系：compatible、complementary、conditional、temporal_change、contradiction、uncertain 等；
2. Entity Binding：对具体名称给出 `reuse`、`create_new` 或 `unresolved`，必要时精确到 `fact_index + role(subject/object)`。

这不是“LLM 输出一个 Entity ID 就直接写数据库”。`validate_entity_bindings()` 会做服务端白名单校验：

- 名称必须来自本次真实提供的 pending name；
- fact_index 必须在 facts 范围内；
- role 只能是 subject/object；
- 对应事实位置的名称必须与绑定名称一致；
- reuse 的 Entity ID 必须属于本次候选集合；
- evidence_relation_id 必须属于本次实际提供的关系集合；
- 同一目标出现互相矛盾的绑定建议时，整个目标降级 unresolved；
- 名称级与事实级绑定冲突时，事实级优先。

通过的 reuse 绑定会写回 `proposal.graph_facts[*].subject_entity_id/object_entity_id`。`create_new` 与 `unresolved` 不会把模型提供的任意实体 ID 写回事实；后续 Graph Projection 再按事实名称解析/创建同 Scope 的实体。它是 canonical Proposal 的字段提示，不需要另建实体绑定表，也不需要第二次 LLM 调用。

## 12.6 裁决如何决定自动写入还是等待用户

`adjudication_to_plan()` 将语义结果变成确定性动作：

| 裁决结果 | 自动策略 |
|---|---|
| compatible / complementary | 继续原写入流程 |
| temporal_change | 继续写入，由 ConflictResolver 按时间变化语义 supersede |
| contradiction + 单值 Key/谓词 + 新旧双方 trusted | 允许自动纠正/取代 |
| contradiction + 多值/未知谓词 | defer，等待用户确认 |
| conditional + 多值 | 允许共存 |
| conditional + 单值 | defer，等待用户补充条件 |
| uncertain | defer |
| LLM 异常或解析失败（degraded） | 退回既有确定性写入流程，不把基础设施故障伪装成语义不确定 |

最后一行非常重要：冲突裁决 LLM 失败时，系统优先保持记忆提取主流程可用，可能继续走既有 MemoryWriteService/ConflictResolver。它不是“LLM 失败就 fail closed 不写入”。如果产品策略要求语义裁决故障时禁止覆盖旧事实，需要另行调整这一降级策略。

## 12.7 待确认案例状态机

当 plan.action = defer 且存在可比较旧记忆时，`MemoryConflictService.park()`：

- 创建 `MemoryConflictCase`；
- 保存 old_memory_id、old_record_version、old_content、new_content、proposal_payload、verdict、graph_evidence、candidate evidence；
- 用 `idempotency_key = conflict:{proposal_id}:{old_memory_id}` 防止重复建案；
- 在同一事务创建 `memory_conflict_question` Outbox Job；
- 不写 MemoryRecord，不写 GraphRelation。

状态：

```text
pending --问题生成成功--> asking
asking --用户回复--> resolved / rejected
asking --需要澄清--> asking（clarify_rounds + 1）
asking --旧记忆版本改变--> stale
pending/asking --TTL 到期--> expired
```

问题通知复用同一条 pending notification；重试或多轮澄清会更新通知正文，不重复生成待办通知。

用户回复处理：

1. 对 MemoryConflictCase 行加 `FOR UPDATE`，并检查 status/TTL；
2. LLM `ConflictReplyParser` 将自然语言回复解析成结构化决策；
3. 对需要实际写入/改动旧记忆的决策（例如 accept_new/revise/merge/keep_both/forget_old）重新检查旧记录存在性、record_version 和 lifecycle_state；clarify、keep_old、reject_new 不需要对旧记录执行同一套写入前版本校验；
4. 对需要修改旧记录的决策，若旧记录已变化或不再 active，则标记 stale，不执行基于旧快照的计划；
5. clarify 会递增轮次并入队下一轮问题；超过最大澄清轮数则 expired，保留旧记忆且不写新提案；
6. accept_new/revise/merge/keep_both 会转成 MemoryWriteService 的强制 decision；
7. forget_old 先调用 `MemoryWriteService.forget()`（进入 Forget Saga，同事务持排他锁），再将新提案按 create 语义写入（同会话再取共享锁，advisory lock 可重入，不阻塞）；
8. 用户回复作为 trusted Evidence 加入新 Proposal，confidence 至少提高到 0.95，execution_mode=user_required；
9. 成功后更新 Case、同步 Proposal 记录、关闭通知并提交。

这条链路保证“等待确认期间旧记忆不变”，而不是先覆盖再让用户确认。

## 12.8 MemoryWriteService 的事务与并发控制

`MemoryWriteService.write_batch()` 是提案批次写入入口。它不应被理解成“循环执行几个 INSERT”。

1. 先计算批次中所有 canonical_key + scope 的锁 ID；
2. 对锁 ID 排序后获取 PostgreSQL `pg_advisory_xact_lock`，避免两个批次以相反顺序锁多个 Key 造成死锁；
3. SQLite 测试环境该锁是 no-op，因此 SQLite 测试不能证明 PostgreSQL Advisory Lock 的并发行为；
4. 对每个 Proposal 执行 Gate、Forget Tombstone / Reingestion 检查、ConflictResolver；
5. 必要时对旧 MemoryRecord 行加 `SELECT ... FOR UPDATE`，并核验 record_version / state hash；
6. 创建或更新 MemoryRecord、MemoryEvidence、MemoryProposal、MemoryLineage；
7. 记录 lifecycle Event；
8. 调用 `MemoryMutationExecutor.enqueue_projection()`，把 Core Memory、Graph、Vector、Retrieval Index 等派生刷新任务写入 Outbox；
9. 外层 `handle_memory_extraction` 在同一 Phase C 事务里将 IngestionRun 标记 succeeded，最后一次性 commit。

Advisory Lock 使用 `pg_advisory_xact_lock`，事务结束自动释放。`_release_lock()` 是保留的空操作，不是依赖 Python finally 手工解锁。

除 Memory Key 锁之外，`MemoryWriteService` 必须在事务开头取全局 Forget 共享锁
（`forget/concurrency.py::acquire_forget_shared_lock`，双 int4 命名空间
`(0x41495645, 1)`，与 bigint Key 锁空间隔离），并在任何 Shield 查询之前持有到
COMMIT/ROLLBACK。Forget Phase A 的排他锁（`acquire_forget_exclusive_lock`）唯一入口是
`execute_phase_a_shield()`：它在计算 cutoff、创建 Operation / Shield / Tombstone 之前取锁，
因此 REST（`routes_forget` → `handle_forget`）、Agent Forget Tool、
`MemoryWriteService.forget` 与内部直接调用全部自动受保护，不存在绕过路径。
PostgreSQL 非 READ COMMITTED 时 fail-closed（避免等待锁期间沿用旧快照）；SQLite 为 no-op，
只用于逻辑测试。冲突确认的 `forget_old` 在同一事务先 forget（排他）再 write（共享）：
PostgreSQL advisory lock 在会话内可重入，不会自我阻塞，也不构成锁升级。

## 12.9 Memory Gate 的准确含义

Gate 是写入准入规则，不是 LLM 自己的置信度投票：

- memory_type / sensitivity 必须属于允许枚举；
- 来源必须具备对应 trust/authority；
- durable 必须为 true 才能进入 durable memory；
- pinned 仅允许 user_required 等受控来源；
- ephemeral 必须带有效期；
- confidence < 0.7 或 Evidence 缺失时通常只能进入 candidate，不等同于 active；
- Forget Tombstone 对 reingestion 的阻断优先于普通提取结果。

用户明确确认冲突后，Proposal 以 user_required 写入，confidence 至少 0.95；这是一条受控用户授权路径，不代表普通模型提取也能绕过 Gate。

## 12.10 Memory 投影的写入顺序

MemoryRecord 是 canonical 真相源。后续投影任务是可重建副本：

| 投影 | 输入 | 幂等/防旧任务机制 |
|---|---|---|
| Core Memory | 被 Key Registry 明确声明 core_memory_role 的记录 | 按当前 MemoryRecord 重算投影 |
| Graph | graph_facts | relation_key、source_record_version、Scope、authoritative supersede |
| Memory Vector | canonical_key + content | record_version + content_hash + embedding_model identity |
| Retrieval Index | memory/summary/checkpoint | source_version/source_hash + active/building generation + tombstone |
| Markdown File Projection | 允许导出的记忆视图 | Outbox 重建，Forget Shield 生效后再次刷新 |

不要直接修改 GraphRelation、MemoryVectorProjection 或 RetrievalIndexEntry 来“修复记忆”。应修改 canonical MemoryRecord，然后触发对应投影；否则下次刷新/重建可能覆盖手工修改。

---

# 第十三部分：Forget Saga——实际五阶段、表结构、恢复与审计缺口

Forget 是当前项目中最重要的隐私边界之一。本节按实际代码解释其真实运行方式。

## 13.1 真实阶段划分

实现上是 **Phase A 同步立即屏蔽 + 四个异步 Outbox Stage**，合计五个逻辑阶段：

| 逻辑阶段 | 实际入口 | 主要职责 |
|---|---|---|
| A | `execute_phase_a_shield()` | 同步冻结 Selector、创建 Shield/Tombstone、立即隐藏、入队 Cascade |
| B | `handle_forget_cascade()` | 物化 ForgetTarget、发现依赖、写 Tombstone、建立批次 |
| C | `handle_forget_rebuild()` | 重算 Evidence、重建 SegmentSummary、重建 EpochCheckpoint |
| D | `handle_forget_purge()` | Scrub 内容、删除敏感派生行、处理宽范围清理 |
| E | `handle_forget_verify()` | 校验关键不变量，成功后退休临时 Shield |

后台恢复由 `handle_forget_reconcile()` / `ForgetReconcileService` 完成，它不是第六个清理阶段，而是 Saga 的对账控制面。

## 13.2 Forget 的 10 张持久化表

`backend/aiive/db/forget_models.py` 明确列出 10 张表：

| 表 | 作用 |
|---|---|
| `forget_operations` | Saga 根记录：mode、selector_type/hash、状态、target_count、时间、错误 |
| `forget_selector_manifests` | 不可变 Selector JSON + hash + cutoff_created_at |
| `forget_shields` | 临时选择器级/实体级立即屏蔽 |
| `forget_targets` | 冻结后的目标清单，含 source_version/source_hash/Scope/Key/value_hash |
| `forget_dependencies` | 从 Target 和真实 provenance 推导的依赖 |
| `forget_batches` | 每阶段每类型的复合游标与批次状态 |
| `forget_actions` | 每个具体动作的状态、幂等键、expected_version、before_hash |
| `forget_tombstones` | 永久可见性屏蔽、重摄取屏蔽、内容是否已清理、审计读取标记 |
| `forget_stage_runs` | Stage 与 Outbox Job 的关联、执行 token、失败次数 |
| `content_provenance_refs` | 规范化内容来源引用，支持从内容追踪到 Turn/Event/Artifact 等来源 |

关键点：`ForgetTarget` 和 `ForgetDependency` 是持久化清理计划，不是每次重试重新全表扫描。`ForgetBatch` 用复合游标续跑，不使用 OFFSET，避免并发插入/删除导致分页跳过或重复。

## 13.3 Phase A：在一个请求事务里先屏蔽，再异步清理

`execute_phase_a_shield()` 不自己 commit，由调用方事务统一提交。其顺序：

1. 规范化 Selector；
2. 计算确定性 selector hash；
3. `cutoff = now`，只允许命中请求时间点之前的记录；
4. 创建 `ForgetOperation(status=shielded)`；
5. 写 `ForgetSelectorManifest`；
6. 物化 selector-level Shield；
7. 对显式 ID 物化 entity-level Shield；
8. 对有界显式 IDs 写 Tombstone；
9. 对 bounded memory_ids 立即将 MemoryRecord.lifecycle_state 设为 forgotten，并删除对应 MemoryVectorProjection；
10. 删除对应 CoreMemoryBlock；
11. 对 bounded memory_ids 立即 tombstone RetrievalIndexEntry；
12. 如开启 File Projection，入队 `memory_markdown_project`；
13. 同事务写 `forget_cascade` OutboxJob + ForgetStageRun。

这使得“屏蔽生效”和“清理任务可恢复”一起提交。进程在 commit 后立刻崩溃，也不会出现“数据已被用户请求遗忘，但没有任何后台任务记录”的窗口。

### 选择器类型优先级

`normalize_selector()` 只选出一个 `selector_type`，优先级是：

```text
all_user_data
> memory_ids
> turn_ids
> event_ids
> canonical_key
> thread
> time_range
> scope
```

注意：payload 会保留调用方传入的多个字段，但 `selector_type` 只选其中一个主类型；Phase B 根据 selector_type 分支物化 Target。当前代码没有把多个 selector 定义成“并集”语义。调用方应一次只传一种选择器，或在 API 层显式拒绝混合字段，避免调用方误以为多个字段都会独立生效。

### Entity Shield 与 Tombstone 的数量边界

`MAX_INLINE_SHIELD_TARGETS = 100`：

- entity-level Shield：当前 `phase_a_shield.py` 会对显式 memory_ids/turn_ids/event_ids 逐条写 Shield，即使超过 100 也不会因没有匹配字段而失去立即屏蔽能力；
- Tombstone、立即更新 MemoryRecord、冻结初始 Memory Target：仅在相应显式列表不超过 100 时在 Phase A 内联处理；超出时交给 Phase B 批次物化；
- 宽选择器 canonical_key/thread/time_range/scope/all_user_data 不在 Phase A 展开成实体 ID，而是写 selector Shield。

因此，`100` 不是所有 Phase A 动作的统一上限，不能简单描述为“超过 100 个 ID 就没有即时屏蔽”。但 `_materialize_entity_shields()` 对显式 ID 是逐条写入 Shield，超过 100 也仍在 Phase A 请求事务中完成；如果调用端允许非常大的 ID 列表，这会让同步事务、锁持有时间和 Shield 行数随输入线性增长，应设置接口级最大值或改为可证明安全的分段屏蔽策略。

另一个幂等细节：`execute_phase_a_shield()` 的 `operation_key` 若由调用端传入，可以作为跨重试稳定键；若调用端不传，函数会用新生成的 `operation_id` 拼出默认 key。也就是说，**函数自身不会根据相同 selector 自动去重**；端到端重试是否复用同一个 Forget Operation，取决于上游是否稳定传入 operation_key。

此外，`ForgetVerifier` 当前把 selector_type 不属于 memory_ids/turn_ids/event_ids 的操作一律归为 wide；所以 canonical_key、scope 也会被标记为 `verified_with_coarse_purge`，即使 `_scrub_wide()` 没有对应分支。这进一步说明该状态不能直接解释为已完成全量宽表 Scrub。

## 13.4 Shield 与 Tombstone 的区别

| 机制 | 生命周期 | 主要作用 |
|---|---|---|
| Shield | 临时，成功验证后 retired | 在后台清理尚未完成时先隐藏整个选择器或实体范围 |
| Tombstone | 长期保留 | 永久阻断被遗忘内容的正常可见性和重新摄取 |

Tombstone 关键字段：

- `target_type/target_id`：具体被屏蔽对象；
- `block_visibility`：正常读取是否隐藏；
- `block_reingestion`：新提取是否允许重新生成同一事实；
- `content_purged`：内容是否已完成物理 Scrub；
- `allow_audit_read`：是否保留受控审计读取语义；
- `canonical_key/value_fingerprint/fingerprint_key_version`：对内容做 HMAC-SHA256 指纹，防止用户再次说出同一事实后重新写入；
- `source_event_id/source_turn_record_id`：把记忆事实连接回原始事件/Turn。

`memory_only` 会将对应 Tombstone 的 `allow_audit_read` 设为 true；这不等于普通 Recall 可以再次召回，也不等于可以重新摄取。

## 13.5 Phase B：按 200 条一批冻结 Target 和 Dependency

`handlers_forget.py::BATCH_SIZE = 200`。

`handle_forget_cascade()`：

1. 校验 Outbox claim 与 ForgetOperation；
2. 根据 Selector Manifest 读取冻结 payload；
3. 建立/恢复 `ForgetBatch`；
4. 使用复合游标读取下一批；
5. 对每个匹配对象创建 `ForgetTarget`，记录 source version/hash、Scope、canonical_key、value_hash；
6. 对 Target 做唯一约束去重；
7. 从真实 provenance 发现依赖，写 `ForgetDependency`；
8. 为每个 Target 写 Tombstone；
9. 批次完成后创建下一批或推进到 rebuild stage。

依赖发现不是“把数据库里所有含相同文本的记录扫一遍”。它沿明确的关系追踪：

```text
MemoryRecord
 ├─ MemoryEvidence.source_event_id
 ├─ MemoryEvidence.source_turn_record_id / source_turn_id
 ├─ GraphRelation.source_memory_id
 ├─ MemoryVectorProjection.memory_id
 ├─ RetrievalIndexEntry.source_id
 ├─ MemoryProposal.final_memory_id
 ├─ SegmentSummary.source_event_ids / source_turn_ids
 └─ EpochCheckpoint.source_segment_ids / source_hashes
```

Event / TurnRecord 目标也会沿 turn 关联、Segment 归属、Summary source IDs 发现依赖。`_summaries_containing_event()` 在 PostgreSQL 使用 JSONB `@>` 包含查询；SQLite 等方言先 LIKE 预筛，再 Python 精确过滤。

这条依赖链的核心目的是：不仅删除 canonical Memory，还要让所有从它派生的可检索表示失效。

## 13.6 Phase C：Evidence 重算、SegmentSummary 重建、EpochCheckpoint 红acted

### Evidence 重算

`_recompute_evidence_batch()` 读取被遗忘 Memory 的 Evidence，计算剩余证据状态，并在后续更新目标记忆状态。此步骤用于把“该事实的证据是否还存在”与“该记忆是否已经被 Forget 指定”分开。

但是：一旦 MemoryRecord 是本次 Forget 的 target，Forget 的最终隐私决策优先。Phase D 会删除目标 MemoryRecord 的全部 MemoryEvidence，并把内容改成 `[forgotten]`；不会因为还存在另一条 Evidence 就把被用户明确要求遗忘的 canonical record 重新激活。

### SegmentSummary 重建

`_rebuild_summaries_batch()` 找到受影响的 SegmentSummary：

1. 按 source_event_ids / source_turn_ids 发现关联摘要；
2. 对旧摘要写 Tombstone / retrieval index tombstone；
3. 调用 `_try_generate_summary()` 尝试从“剩余 Event”生成新摘要；
4. 如果没有剩余事件或 LLM 失败，回退为 redacted 摘要；
5. 新摘要写新的 summary_version，并刷新对应 Retrieval Index。

`_try_generate_summary()` 当前实现有明确边界：

- 只取该 Segment 最多 50 个 Event；
- 每条 Event 的 payload.content 最多取 500 字符；
- 只把 Event 转为 `{role:event_type, content:...}` 给 P3 summary prompt；
- 不重放完整原始工具结果，不重建原有 `verified_tool_states`、`unresolved_failures`、`artifact_refs`；
- LLM 输出与空 boundary snapshot 合并；
- LLM 异常时返回 None，由调用方使用 redacted fallback。

它不是把原 SegmentSummary 原样做字符串删除，而是“从可见 Event 重新生成一个语义摘要”。

### EpochCheckpoint 重建

当前 Forget 重建路径采用保守 redaction：对受影响 Epoch 创建/更新红acted Checkpoint，清空 source_segment_ids/source_hashes 和相关语义字段，而不是试图把旧 Checkpoint 中每一项事实逐条逆向删除。这样牺牲部分历史连续性，换取更保守的隐私边界。

## 13.7 Phase D：Scrub 与物理清理

### 单批 Memory Scrub

`_scrub_batch(memory_ids)` 在删除 MemoryEvidence 前先计算专属来源 Event：

- 收集目标 Memory 的 source_event_id；
- 再查这些 Event 是否仍被非目标 MemoryEvidence 引用；
- 只把没有其他存活 Memory 共享的 Event 放入 `exclusive_event_ids`；
- 共享 Event 不在窄选择器路径中直接清空，避免破坏其他仍有效 Memory 的证据。

随后执行：

1. `memory_records.content = '[forgotten]'`；
2. `structured_value = NULL`、`graph_facts = NULL`；
3. 删除以这些 Memory 为 source 的 GraphRelation；
4. 如果 GraphEntity 不再被任何关系引用，则删除孤儿实体；
5. `memory_proposals.raw_payload/normalized_payload = NULL`；
6. 删除 `memory_evidence`；
7. 清空 `memory_maintenance_inputs.snapshot_json`；
8. 清空对应 RetrievalIndexEntry 的 search_text/snippet，title 设 `[forgotten]`，`is_searchable=false`；
9. 对 exclusive source Event 清空 payload。

MemoryRecord 本身保留为 forgotten sentinel，不是简单 `DELETE FROM memory_records`。这样保留生命周期审计与 ID 稳定性，同时删除正文和结构化事实。

### 宽范围 Scrub

`_scrub_wide()` 当前显式读取的选择器字段只有：

- `thread_id`；
- `time_from/time_to`；
- `all_user_data`。

对应会更新/清空的内容承载表包括 Events、TurnRecords、WorkingStates、ContextSnapshots、LLMCalls、Tasks、Artifacts、CompactionInputs、EpochCompactionInputs，以及部分 SegmentSummary/EpochCheckpoint 字段。

但当前代码没有针对 `scope` 和 `canonical_key` 编写与 thread/time/all_user_data 等价的全量宽表 Scrub 分支。它们仍会通过 Memory Target / Dependency 做目标级清理，但不能据此声称“Scope 下所有非 Memory 内容都被全量物理清理”。

## 13.8 Phase E：Verify 实际检查了什么

`handle_forget_verify()` 当前实际检查：

1. 本次 Forget 的 MemoryRecord 是否都已 `lifecycle_state='forgotten'`；
2. 对应 RetrievalIndexEntry 是否仍 `is_searchable=true`；
3. 每个 memory_record target 是否有对应 Tombstone；
4. event / turn_record target 是否按正确 target_type 有 Tombstone。

结果：

- 无失败且窄选择器：`verified`；
- 无失败且宽选择器：`verified_with_coarse_purge`；
- 从旧 forget_requests 迁移且 provenance 不完整：`legacy_unverifiable`；
- 有失败：`failed_retryable`。

成功后将本次 active Shield 改为 retired，永久拦截由 Tombstone 承担。失败时 Shield 保持 active，继续 fail-closed。

**重要边界：Verify 并没有逐条重新读取所有内容承载表，也没有对新生成 Summary 的语义内容做隐私扫描。** 它主要验证 Memory 状态、Retrieval 可搜索性和 Tombstone 覆盖。`verified_with_coarse_purge` 不等于“所有内容列均已被逐项证明不存在”。

## 13.9 Forget 自动恢复

`ForgetReconcileService` 按以下顺序扫描 Stage：

```text
cascade → rebuild_dependencies → purge → verify
```

它检查每个 ForgetStageRun 对应的 OutboxJob：

- Job completed：把 StageRun 收敛为 done；
- Job pending/running：认为阶段仍在途，不重复创建；
- Job failed/deadletter：按 backoff 重新激活原 Job；
- 缺少 StageRun：创建确定性 operation_id 的 Job 和 StageRun；
- verify Job completed 但 Operation 尚未进入终态：可重新激活 Verify，避免阶段状态不一致。

恢复退避：`delay_seconds = min(3600, 2 ** min(failure_count, 12))`。最大等待 3600 秒。重激活时清空旧 claim token、locked_by、lease_expires_at、terminal_reason，复用原 Job，而不是创建一份并行清理任务。

## 13.10 Forget 代码审计发现的高风险点

以下均为静态代码发现，建议作为独立测试/修复任务，不代表已在生产环境复现。

### F-01：Summary 重建过滤条件可能让被遗忘 Event 重新进入新摘要 <badge color="success">已修复，见 13.11</badge>

`_try_generate_summary()` 构造 `blocked_ids` 时，只查询当前 operation 的 `ForgetTombstone.target_type == 'event'`。但它读取 Segment 下的 Event 时，没有同时过滤：

- `turn_record` Tombstone 对应 Turn 的全部 Event；
- 本次 memory_record target 的 MemoryEvidence.source_event_id；
- 通过依赖关系已经确认属于被遗忘内容的 Event ID。

因此，若用户遗忘的是一个 Turn，或者遗忘 MemoryRecord 但其来源 Event 没有单独成为 event target，重建 Summary 时这些 Event 可能仍被当作“剩余事件”送入 LLM。新摘要会生成新的 source_event_ids / summary_version，可能重新携带已要求遗忘的语义内容。

风险链：

```text
Forget turn / memory
   → 旧 Summary 被标记
   → _try_generate_summary 只过滤 event Tombstone
   → 目标 Turn 的 Event 仍进入 prompt
   → 生成新 SegmentSummary
   → 新 Summary ID 不一定有本次 Tombstone
   → Verify 不扫描新 Summary 的正文
```

建议修复：重建前先构造本次 operation 的完整 `blocked_event_ids`，并把所有 `turn_record` targets 展开为其 Event IDs；同时从目标 MemoryEvidence 收集 source_event_ids。LLM 只允许看到明确剩余的 Event。增加两个集成测试：`turn_ids` 遗忘后重建 Summary 不含原文；`memory_ids + memory_only/everywhere` 后重建 Summary 不含被遗忘事实。另一个风险是 Verify 当前只检查原有 ForgetTarget 的 Tombstone 覆盖；若重建过程产生新的 Summary ID，而该新 ID 没有被登记为本次 target/dependency，Verify 不会自动发现它的语义内容是否重新包含了原事实。

### F-02：`_scrub_wide()` 对 JSON 数组列使用标量 `IN` 比较，可能根本匹配不到 <badge color="danger">高风险</badge>

当前 SQL 类似：

```sql
UPDATE segment_summaries
SET ...
WHERE source_event_ids IN (SELECT id FROM events WHERE thread_id=:tid)
```

但 `SegmentSummary.source_event_ids` 在 ORM 中是 `JSON` 数组字段；`EpochCheckpoint.source_segment_ids` 同样是 JSON 数组。将整列 JSON 数组与子查询返回的单个字符串做 `IN` 相等比较，不等于“JSON 数组包含该 ID”。因此 thread 范围的 Summary/Checkpoint 清理条件很可能不会命中预期行。

正确做法应按方言使用 JSONB containment / JSON1 `json_each`，或直接通过 SegmentSummary/Checkpoint 与 Segment/Event 的关联关系执行 join 更新；必须分别在 PostgreSQL 与 SQLite 集成测试验证。

### F-03：time_range 的宽表 Scrub 覆盖范围比注释承诺的范围窄

注释声称 time_range 会覆盖 Events、TurnRecords、WorkingStates、ContextSnapshots、LLMCalls、Tasks、Artifacts、CompactionInputs、EpochCompactionInputs、SegmentSummaries、EpochCheckpoints；但当前 time_range 分支实际 SQL 只更新 Events、LLMCalls、ContextSnapshots、Tasks、CompactionInputs、EpochCompactionInputs。没有对 TurnRecords、WorkingStates、Artifacts、SegmentSummaries、EpochCheckpoints 做同等时间范围清理。

如果 time_range 的产品语义是“清理该时间段所有用户数据”，这属于实现覆盖不完整；如果产品语义只针对部分数据，则需要收紧注释、接口文档和用户提示，避免过度承诺。

### F-04：scope / canonical_key 宽选择器没有 `_scrub_wide()` 对应分支

Selector Normalizer 支持 `scope` 与 `canonical_key`，但 `_scrub_wide()` 只按 thread/time/all_user_data 分支。目标级 Memory Scrub 仍会运行，但不能据此认为同一 Scope/Key 相关的全部 Event、Task、Artifact、WorkingState、Summary 等都已被宽范围清理。建议明确每个 Selector 的数据面定义，并增加 scope/canonical_key 的全链路隐私测试。

### F-05：mode 与 selector 组合没有形成严格的服务端契约 <badge color="success">已修复，见 13.12</badge>

`mode` 有 `memory_only/history_only/everywhere`，Selector 又有 memory_ids/turn_ids/event_ids/thread/time/scope 等。当前 `normalize_selector()` 主要选择一个 selector_type，没有看到统一的 mode-selector 兼容矩阵校验。`_scrub_wide()` 只看 selector payload 中是否有 thread/time/all_user_data，不看 mode；因此混合传参时，可能出现 mode 文案与实际 Scrub 行为不一致。

例如 `memory_only + thread_id` 仍会触发 thread 宽范围 Scrub；而 `history_only + memory_ids` 仍会把 memory target 置 forgotten。建议在 API/Tool 层强制只允许一类 Selector，并按 mode 拒绝不兼容的参数组合。

### F-06：memory_only 不一定保留所有原始历史 <badge color="success">已修复，见 13.11</badge>

`_scrub_event_turn_targets()` 在 `memory_only` 时直接返回，但 `_scrub_batch()` 会计算目标 Memory 的 exclusive source_event_ids，并在任何 mode 下清空这些 Event payload。也就是说，Memory 专属来源 Event 可能被清空，而共享给其他未遗忘 Memory 的 Event 会保留。

因此源码行为更准确的描述是：memory_only 不执行显式 Event/Turn target 的全量清理，但仍会删除目标 Memory 的专属来源 Event。它不是“所有聊天原文无条件完整保留”。需要产品定义决定这是预期的 provenance 清理，还是与 allow_audit_read 文案冲突。

### F-07：Verify 对宽范围内容完整性的证明有限 <badge color="warning">部分补齐，见 13.11</badge>

Verify 不检查 SegmentSummary/Checkpoint 正文、WorkingState、ContextSnapshot、LLMCall preview、Task/Artifact 内容是否仍含被遗忘数据；宽选择器只要 Memory/Tombstone/Index 检查通过就进入 `verified_with_coarse_purge`。建议：

- 增加每个内容承载表的检查计数；
- 对 Summary/Checkpoint 检查 source ID 是否含本次 blocked source；
- 对 RetrievalIndex 检查所有受影响 source type；
- 对未能精确验证的范围保持 Shield active 或明确显示 coarse/unverifiable，而不是让“verified”被理解为严格内容证明。

---


### F-08：`all_user_data` 宽清理是全表更新，不是按用户范围更新 <badge color="danger">多用户部署条件下的高风险</badge>

`_scrub_wide()` 的 `all_user_data` 分支对 `events`、`turn_records`、`working_states`、`context_snapshots`、`llm_calls`、`tasks`、`artifacts`、`compaction_inputs`、`epoch_compaction_inputs`、`segment_summaries`、`epoch_checkpoints` 执行不带 `WHERE user_id / owner_id` 的 UPDATE。`ForgetOperation` 有 `requested_by` 字段，但该分支没有用它限定更新范围。

如果 AIive 的部署契约是单用户，这可能符合单用户“全部数据”语义；如果部署为多用户/多租户，一次 `all_user_data` 可能影响数据库中其他用户的数据。修复前必须定义 owner/tenant 主键如何贯穿 Event、Turn、Task、Summary、Artifact 等内容表，再把 owner 写入冻结 Manifest，并在所有宽清理 SQL 中使用同一授权范围。

### F-09：Forget 当前未能证明覆盖 AgentTask / TaskEvidence / TaskArtifact 对象存储 <badge color="danger">高风险，需核实</badge>

Task Runtime 使用 `AgentTask`、`AgentAction`、`AgentTaskEvent`、`AgentTaskCheckpoint`、`TaskEvidence`、`TaskArtifact` 等表；大于 4 KiB 的 Evidence 与 Artifact 正文会存入 `task-evidence` / `task-artifacts` content-addressed Object Store。当前已审计的 Forget `TARGET_TYPES` 与 `_scrub_wide()` SQL 清单没有明确列出这些 Task Runtime 表和对应对象存储 bucket；`_scrub_wide()` 中的 `tasks` / `artifacts` 不能仅凭名称就认定等同于 `agent_tasks` / `task_artifacts`。

因此，现有代码不能证明 `all_user_data` 或 thread/time selector 会清除 Task Worker 的目标描述、Action 参数、Evidence 正文和 Artifact 文件。应追踪 `ContentProvenanceRef` 的写入端是否覆盖这些对象；若没有，应新增 Task target/dependency 类型、对象存储删除动作和 Verify 检查。

### F-10：Knowledge Document / Chunk / Vector 是否属于 Forget 的数据面尚未定义

Forget 的 `TARGET_TYPES` 不包含 `document`、`chunk` 或 `knowledge_chunk_vector`，`_scrub_wide()` 也没有针对知识库原文对象、Chunk 正文和 Chunk Vector 的清理分支。若产品定义“遗忘个人记忆/对话”不包括用户主动导入的知识文档，应在 UI/API 文案中明确；若 `all_user_data` 包含知识库，则当前 Forget Saga 尚未证明覆盖这些对象和 `knowledge-documents` 对象存储。

### F-11：超大显式 ID 列表会把大量 Shield 写入压在 Phase A 同步事务中 <badge color="warning">性能风险</badge>

如 15.3 所述，实体 Shield 对显式 ID 列表逐条写入，当前 `MAX_INLINE_SHIELD_TARGETS=100` 并没有限制这一段循环。若上游没有更严格的请求大小限制，大列表会增加 Phase A 延迟、事务日志和锁竞争。建议接口层明确最大列表规模，并对超大集合采用可证明不会出现“尚未屏蔽窗口”的分段机制。

### F-12：Forget 的 operation_key 只有在调用端稳定复用时才具备请求级幂等

`execute_phase_a_shield()` 在未提供 operation_key 时使用新 UUID 生成默认 key。相同 selector 的重复请求不会因此自动合并为同一个 Operation。需要检查公开 Tool/API 的调用端是否传入稳定 operation_key；若没有，应把 request idempotency key 纳入请求契约，并增加“同一请求重试不创建第二个 Saga”的集成测试。

---

## 13.11 实现补记：来源 Event 隔离与摘要重建隐私闭环

本节记录针对 F-01 / F-06 / F-07 与 P0-1 的定向修复（不重写整个 Forget Saga）。目标：被遗忘的 Memory 不能通过原始历史、记忆重新提取或 SegmentSummary 重建重新进入正常上下文，同时严格保留 `memory_only` / `history_only` / `everywhere` 的既定语义。

**持久化 Memory 到 Event 来源关系**：复用现有 `ContentProvenanceRef`，不新增模型。`MemoryWriteService._write_evidence_batch()` 在创建 `MemoryEvidence` 的同一事务写入 `(owner_type=memory_record, owner_id=MemoryRecord.id, source_type=event, source_id=Event.id)`，按唯一约束去重、幂等。Evidence 在 Purge 阶段会被物理删除，但 ProvenanceRef 长期存活，供读取侧屏蔽、防重抽与 Summary 重建反查。Alembic 数据迁移 `f1a2c3d4e5b6` 从历史 `memory_evidence.source_event_id` 回填，幂等、兼容 PostgreSQL/SQLite、无 DDL、不删 Evidence。

**可见性与防重抽**：`ForgetVisibilityService.blocked_target_ids()` 对 Event 追加两类屏蔽：所属 TurnRecord 命中 Shield 或 `block_visibility` Tombstone 的全部子 Event；作为已被遗忘 MemoryRecord 来源的 Event（沿 ProvenanceRef 反查 Memory 的实体/选择器 Shield 与 Tombstone）。`is_tombstone_blocked_reingestion()` 同样沿 ProvenanceRef 拦截，防止后台提取从已遗忘来源重建事实；递归调用只在非 Event 目标上发生，不会无限循环，且不阻断无关新 Event。`can_audit_read()` 改为对同一对象的所有 Tombstone 取 `all(...)`，任何 `content_purged=True` 或 `allow_audit_read=False` 都 fail-closed 拒绝。

**屏蔽时序与长期依据**：对有界显式 `memory_ids`（`MAX_INLINE_SHIELD_TARGETS=100` 以内），Phase A 事务为来源 Event 立即写实体 Shield；宽选择器与超限 ID 不在 Phase A 无界枚举，靠选择器 Shield + 读取侧 Provenance 校验即时生效。Cascade 阶段为来源 Event 持久化 `ForgetTombstone`（含被遗忘 Turn 的全部子 Event），使 Shield 退休后仍有长期屏蔽依据，`allow_audit_read` 随 Forget Mode 落库。

**SegmentSummary 重建**：`_try_generate_summary()` 改用统一 `ForgetVisibilityService.blocked_target_ids()` 过滤所有有效 Event Tombstone、TurnRecord Tombstone 子 Event、以及来源 MemoryRecord 已被遗忘的 Event；过滤发生在最终 50 条上限之前，LLM 只接收未被屏蔽的 Event，新 Summary 的 `source_event_ids` / `source_turn_ids` / `source_hash` 仅由允许来源生成，不是删字符串。`_rebuild_summaries_batch()` 到 `_try_generate_summary()` 的真实调用链有集成测试覆盖（当前 Operation 与后续另一 Operation 均不重新引入已遗忘内容，且保留其他未遗忘 Event）。

**mode 内容语义**：`_scrub_batch()` / `_scrub_wide()` 对 `memory_only` 保留来源 Event 原始 payload（含独占来源 Event），同时保留正常可见性屏蔽与防重抽；`history_only` / `everywhere` 维持不可逆正文清理。

**Verify 完整性**：`_verify_source_event_integrity()` 校验 Memory 来源 Event 的屏蔽覆盖、新 Summary 不引用被屏蔽 Event、`memory_only` 未误清原文、`history_only` / `everywhere` 已完成应有清理；任一不满足或无法证明完整性即不落严格 `verified`。

**测试**：`tests/integration/backend/test_phase6a_forget.py` 覆盖 ProvenanceRef 同事务写入、迁移回填与幂等、memory_only 来源 Event 普通历史隐藏与原文保留、防重抽、everywhere 正文不可恢复、Turn 遗忘先于清理屏蔽全部子 Event、当前与后续 Operation 的 Summary 重建、多 Tombstone 审计 fail-closed、宽选择器 Phase A 不枚举，以及真实 `_try_generate_summary()` 调用链。

**仍未覆盖（既有审计结论）**：F-02（JSON 数组列标量 IN 比较）、F-03/F-04（宽 Scrub 覆盖与 scope 分支）、F-05（mode 与 selector 契约）、F-08~F-10（多用户边界、Task Runtime / Knowledge 对象与对象存储）、F-11/F-12（超大 ID 列表与 operation_key 幂等）、以及 F-07 中 Checkpoint/WorkingState/LLMCall preview 等其余正文承载面的严格内容证明。

---

## 13.12 实现补记：防重抽闭环、Mode/Selector 契约与 Turn ID 一致性

本节记录 2026-11-05 的定向修复，不重写 Saga、不新增模型、不引入新 LLM。

**防重抽**：`ForgetVisibilityService.is_tombstone_blocked_reingestion()` 在 `Tombstone.block_reingestion` 之外增加 active Shield 检查：来源 Event 实体 Shield、关联 MemoryRecord 实体 Shield（沿 `ContentProvenanceRef`）、TurnRecord Shield（`id` / `turn_id` 双语义）、thread / time_range / scope / canonical_key / all_user_data 选择器 Shield，并应用 cutoff。`blocked_target_ids(..., include_tombstones=False)` 让 Shield 判定排除可见性 Tombstone，避免把 `block_visibility` 与 `block_reingestion` 混为一谈。Phase A 成功后、Cascade 运行前，旧来源 Event 已不可被重新抽取；cutoff 之后的新 Event 不受影响。

**混合 Evidence**：`MemoryWriteService.write()` 与 `_write_single_in_transaction()` 改为 fail-closed：Proposal 含任一被屏蔽来源即拒绝整体，不再要求“全部来源被屏蔽”。被拒绝的 Evidence 不写 `MemoryEvidence` / `ContentProvenanceRef`；Reinforce / Merge / Supersede / 用户强制决策都在检查之后，不能绕过。

**Mode/Selector 契约**：`selector_normalizer.validate_selector_combination()` 成为 API、Tool、Phase A 的唯一校验入口。必须且只能提供一种选择器；`memory_only` 仅允许 memory_ids / canonical_key / scope；`history_only` 仅允许 thread / turn_ids / event_ids / time_range；`everywhere` 允许全部。非法组合在创建 `ForgetOperation` 之前拒绝，杜绝“多选择器静默忽略”。

**Turn ID 一致性**：`handlers_forget._normalize_turn_tokens()` 将 `TurnRecord.id` 与 `turn_id` 任一输入展开为 token 集合；`_discover_for_target()`、`_write_tombstones_for_targets()` 的子 Event 派生、`_scrub_event_turn_targets()` 的 Turn 行与子 Event 清理统一使用该入口。`content_purged` 改为以列查询确认“确实已无剩余正文”为依据，不再在 ID 不匹配、清理未命中时误置 True。

**Verify**：`_verify_source_event_integrity()` 新增持久化保护检查：来源 Event 必须由 event / turn_record / memory_record 三类 Tombstone 之一覆盖，且同时 `block_visibility=True` 与 `block_reingestion=True`；`handle_forget_verify()` 还校验 event / turn 目标的双标志，以及 Turn 目标下全部子 Event 的长期 Event Tombstone。只有活动 Shield、没有持久化保护时保持 `failed_retryable`，不得落严格 `verified`。

**测试**：新增覆盖宽选择器 Phase A 后旧 Event 重抽阻断、>100 memory_ids、canonical_key / scope / thread / time_range / all_user_data、cutoff 后新 Event、全量与部分 Evidence 屏蔽、被屏蔽 Evidence 不落库、Mode / Selector 合法与非法组合、多选择器拒绝、TurnRecord.id / turn_id 双输入清理、content_purged 真实性、Shield-only Verify 失败、Shield 退休后可见性与重抽仍被阻断。

---

# 第十四部分：Persistent Task Worker——任务状态、证据、资源锁与未知执行对账

## 14.1 数据对象不是一张 Task 表

Task Runtime 至少涉及以下持久对象：

| 对象 | 作用 |
|---|---|
| `AgentTask` | 用户委托的任务主记录、状态、brief、task_state、budget、usage、report |
| `AgentRun` | 一次模型推理/执行批次，有自己的 lease、token usage、model、summary |
| `AgentAction` | 一次具体动作提案及其参数、descriptor_hash、idempotency_key、执行状态 |
| `AgentTaskEvent` | Task 时间线事件与状态审计 |
| `AgentTaskCheckpoint` | 恢复任务所需的不可变状态快照 |
| `TaskEvidence` | Action 结果证据，可能 inline 或对象存储 |
| `TaskArtifact` | 工具声明的文件/产物引用 |
| `TaskResourceLock` | Action 对文件/桌面/其他资源的 lease 锁 |
| `AgentTaskWatch` | time/file/node 外部状态观察器 |
| `DesktopActionReceipt` | Desktop Node 对 Action 的 ACK/执行回执 |

因此，“Task 状态持久化”不等于把完整 LLM 对话存到 AgentTask。运行历史由 Action/Event/Evidence/Checkpoint 分层保存。

## 14.2 Run 循环：每次模型只提出一个下一步决策

`PersistentTaskRuntime.run()` 大体执行：

1. 锁定/加载 AgentTask，检查是否终态或阻塞态；
2. 创建或恢复 AgentRun；
3. 检查 TaskBudget（Run 数、模型调用、Action 数、Token、失败次数、时间、成本、deadline）；
4. 如果存在待恢复 Action，先处理 Action，不让模型重复发明一个新的；
5. 如果没有待执行 Action，调用 TaskContextAssembler；
6. 调用 LLM 生成一个 `ActionProposal`；
7. 对 proposal 做结构校验、能力存在性、Scope、descriptor_hash、参数、preconditions、policy 检查；
8. 形成 AgentAction，必要时进入 approval；
9. ready Action 交 CapabilityBroker dispatch；
10. 把结果转成 Evidence/Artifact，更新 Action 状态和 Checkpoint；
11. 达到本 Run 的 Action 上限时结束当前 Run、checkpoint，再入队新 Run 继续。

Decision 类型包括 action、finish、ask_user、watch、search_capabilities、describe_capability。后两者是只读发现决策，不创建 AgentAction，也不调用工具 handler。

## 14.3 Task Context 的实际字段与截断

`TaskContextAssembler.snapshot()` 不加载 Conversation Raw History，也不加载整个 Tool Event 历史。`messages()` 最终生成两条消息：system 角色为 `task_runtime.worker_system`，后面追加 Active Skill 指令和 Goal 模式约束；user 角色为 `task_runtime.task_context`，把 snapshot JSON 序列化后注入。它读取：

- Task：id/type/goal/status/brief/state/budgets/usage；
- 最新 `AgentTaskCheckpoint`（state、plan、reason、version）；
- 最近 20 个 AgentAction；
- 最近 20 个 TaskEvidence；
- 最近 20 个 TaskArtifact；
- 最近 10 个指定重要事件（user_input_received、watch_triggered、approval_denied、task_recovered、action_invalidated）；
- 当前工具视图；
- 当前 Skill；
- 如果是 Goal Task，额外提供当前步骤和步骤状态清单。

截断策略：

- `_bounded_value(value, max_chars)`：小对象保留原结构；大对象变成 `{truncated, original_chars, sha256, preview}`；
- brief 最多 20,000 字符；
- task_state 中 inputs 只保留最后 10 项，每项最多 1,500 字符，旧输入只记数量；
- task_state.plan / last_decision 最多 6,000 字符；
- Checkpoint.state 最多 16,000 字符，Checkpoint.plan 最多 8,000 字符；
- Action.arguments 最多 3,000 字符，result_summary 最多 2,000 字符，error 最多 2,000 字符；
- Evidence/Artifact summary 在 Context 中最多 1,600 字符（EvidenceStore 写入时先经过 1,200 字符 Tool Output Guard）；
- signals.payload 最多 4,000 字符；tools view 最多 24,000 字符；
- 整个 compact task_state 最多 32,000 字符。

这意味着 Worker 恢复不依赖重新拼接整条聊天记录，而依赖 Task 自身的 state/checkpoint/action/evidence。代价是超过窗口的早期 Action/Evidence 不会每次自动全部注入，需要通过 Evidence/Artifact 读取工具按需回查。

## 14.4 工具注入：常驻能力 + 发现状态，而不是全量 Schema

`task_runtime/tool_injection.py::build_tool_view()` 将工具分为：

- resident：每轮常驻能力；
- available：当前 Scope 可见但尚未加载完整定义的能力；
- discovered：模型已通过 search_capabilities 找到的能力 ID；
- loaded：模型已 describe 并加载 descriptor_hash 的能力定义。

ContextAssembler 每轮基于当前 ToolRegistry 重建工具视图。Task State 只保存发现 ID 和定义 hash，不把旧工具 Schema 永久缓存进去。因此工具安装、卸载、版本变化后，下一轮注入会重新解析。

目录工具数量较少时可以直接注入全部 Schema；超过阈值则转入 search/describe 发现流程。发现并不扩大执行权限：真正执行仍经过 CapabilityBroker 的 Scope、TaskPolicy、审批、资源锁和 descriptor_hash 校验。

## 14.5 Action 状态机与执行前检查

Action 状态的主要转换：

```text
LLM ActionProposal（尚未持久化）
      │
      ▼
AgentAction: planned → validated
      │
      ├─ 需要审批 → awaiting_approval → ready
      │
      └─ 不需审批 → ready
                    │
                    ▼
                 dispatched
                    │
                    ▼
                  running
          ┌─────────┼──────────┐
          ▼         ▼          ▼
       succeeded  failed    unknown
                             │
                    Desktop journal 对账
                             │
                  succeeded / failed / ready
```

执行前 `CapabilityBroker.validate_preconditions()` 再检查一次 action 的前置条件。失败则 Action 标为 invalidated，不调用 handler。

Action 被 dispatch 前写入 `execution_run_id` 和 `execution_token`，再 commit 状态，随后才调用外部能力。这意味着进程在 commit 后、外部调用前崩溃时，数据库会看到一个已经 dispatched 但执行结果未知的 Action；系统不能简单假定“还没执行”。

## 14.6 Unknown Action：为什么不能直接重试

`_dispatch_ready()` 对以下错误类型进入 unknown：

- `execution_unknown`；
- `desktop_operation_timeout`；
- `desktop_node_disconnected`。

它会：

1. 把 Action 标记 UNKNOWN；
2. Task 标记 RECONCILING；
3. 结束当前 Run；
4. Checkpoint 中记录 pending_action_refs；
5. 不把 Action 直接改回 READY。

原因是：工具可能已经在外部机器执行成功，只是结果 ACK 丢失。直接重放可能重复写文件、重复发消息或重复执行副作用。

## 14.7 Desktop Node Journal 对账

`DesktopReconciliationService` 是一条具体的 unknown 对账路径：

- `outstanding_action_ids(node_id)` 查询该 Node 上 DISPATCHED/RUNNING/UNKNOWN Action；
- `record_ack()` 记录 received/started；started 时可把 Action 从 DISPATCHED 推进 RUNNING；
- `apply_entry()` 校验 action_id、node_id、idempotency_key、arguments_hash；如果 Node 给出 result_hash，还会重新计算结果 hash 校验；
- journal status=committed 时写 Evidence 并将 Action 标为 SUCCEEDED；
- journal status=failed 时将 Action 标为 FAILED；
- 结算后释放 ResourceLock；当 Task 没有剩余 unknown Action 且处于 RECONCILING/BLOCKED_NODE 时，重新排队 Task；
- `apply_missing()` 只有在 Node 明确报告 journal 中没有该 Action、且服务端 receipt 从未收到 received/started/committed/failed 时，才允许把 Action 改回 READY 重新派发。

如果服务端曾经收到 ACK，但 Node journal 又报告 missing，系统会记录 `desktop_journal_inconsistent`，不会安全重派。

**语义边界：** 这证明 Desktop Node 有 journal-based reconciliation，不等于所有 MCP/HTTP/本地 Shell/任意第三方能力都拥有 exactly-once 执行保证。对没有可查询执行日志的外部工具，unknown 结果仍可能需要人工介入或能力专属对账协议。

还有一个恢复闭环风险：`recover_stale_tasks()` 在 Run lease 过期后，如果发现 Action 处于 DISPATCHED/RUNNING/UNKNOWN，会把 Action 置为 UNKNOWN、Task 置为 RECONCILING，而不是重新入队。已审计的 `DesktopReconciliationService` 只能根据 Desktop Node 的 journal/receipt 对账；源码中没有找到覆盖普通本地 Shell、MCP stdio 或通用 HTTP Action 的统一 reconciler。若这类 Action 在 Worker 崩溃时处于未知状态，Task 可能停留在 RECONCILING，既不能安全重放，也没有已证明的自动结算路径。应给每类可产生外部副作用的 Capability 定义 `query_status(idempotency_key)` / receipt / safe_retry 策略，或者把任务明确转为需要人工处理。

## 14.8 Evidence 与 Artifact 如何保存

`EvidenceStore.capture_result()`：

- 将工具结果 JSON 规范化并计算 SHA256；
- 数据不超过 4,096 bytes 时 inline 存到 TaskEvidence.inline_payload；
- 超过 4,096 bytes 时写 content-addressed Object Store（bucket=`task-evidence`），DB 只保留 bucket/key/hash/size/mime；
- Evidence.summary 统一经过 `guard_tool_output()`，最大约 1,200 字符；大结果不会直接把整个 JSON 放回下轮 Prompt；
- action.evidence_refs 追加 evidence_id/kind/content_hash；
- action.result_summary 只保留 ok、summary、evidence_id、content_size。

如果工具结果中声明 `artifacts`，最多读取前 32 个 Artifact；支持 content_base64 或 content 字符串，写入 `task-artifacts` 对象存储，并生成 TaskArtifact 记录。

读取 Evidence/Artifact 使用 offset/max_bytes range 读取，不必一次把完整对象载入上下文。

## 14.9 Resource Lock

`ResourceLockService.acquire()`：

1. 对 resource_keys 去重并排序；
2. 按固定顺序逐个 `SELECT ... FOR UPDATE`；
3. 如果锁仍在 lease 内且属于另一个 Task/Action，抛 `ResourceBusyError`；
4. 否则创建/更新锁，生成 token，默认 lease 180 秒；
5. release 只删除 token 匹配的锁，防止旧执行者释放新执行者的锁。

此外有 `release_action()` 供 reconciliation 在确认 Action 终态后清理遗留锁；`expire()` 清理 lease 已过期的锁。当前 `ResourceLockService` 本身暴露 acquire/release/release_action/expire，没有看到独立 renew 方法；默认 lease 为 180 秒。若某个 Action 的外部调用可能超过 lease，必须确认调用链是否会显式传入更长 lease 或有其他续期机制，否则锁到期后其他 Action 可能重新获得同一 resource_key。

资源锁只提供数据库层面的协作互斥。真正的文件系统/远程资源是否遵守该锁，还取决于执行工具是否通过 Broker 使用同一 resource_key 约定。

## 14.10 Watcher

当前 `SUPPORTED_WATCHES = {time,file,node}`：

- time：配置 `at`，到点触发；
- file：配置 path 必须落在 TaskScope.allowed_roots，比较 exists/mtime_ns/size；
- node：读取 DesktopNode status + lease_expires_at，过期即视为 offline。

`poll_due()` 每次最多扫描 100 个到期 Watch；未触发时按 `interval_seconds` 重新安排 next_check_at，最小 5 秒、默认 30 秒。触发后追加 watch_triggered 事件；Goal Watch 会交 GoalService 结算；普通阻塞任务可以被重新排队。

NodeOnline 也有事件驱动的快速唤醒路径，不必只依赖轮询。

---

# 第十五部分：MCP / Capability——发现、安装、运行和授权边界

## 15.1 Discovery 不是通用在线 Registry 搜索

`mcp/discovery.py` 当前有一个进程内 `_BUILTIN_CATALOG`，包含四个官方 Reference MCP：

- `@modelcontextprotocol/server-filesystem`；
- `@modelcontextprotocol/server-memory`；
- `@modelcontextprotocol/server-sequential-thinking`；
- `@modelcontextprotocol/server-everything`。

`search_mcp_candidates(goal)` 不发 HTTP 请求，而是本地关键词匹配：

- 名称命中 +3；
- 描述命中 +2；
- 工具名命中 +1；
- alias 命中 +2；
- 按 match_score 降序排序，分数相同保留目录顺序。

候选的 `definition_trust_level` 根据 catalog source 设为 semi_trusted/untrusted。候选类里虽然定义了 official_registry、community、sse、streamable_http、pip 等字段，但这不证明这些来源/传输/包管理已实现。

## 15.2 CapabilityPlanner 风险分数是启发式，不是静态代码审计

`CapabilityPlanner.plan_from_goal()`：

1. LLM 分析用户目标，输出 goal_summary、missing_capability_type、search_keywords、risk_tolerance；
2. 用 LLM keywords 搜索内置候选；没有结果时退回全文搜索；
3. 对候选计算风险；
4. 选择第一个 recommended 候选，否则选择最低风险候选并标记人工审查。

风险公式：

```text
source_trust:
  trusted=0.9, semi_trusted=0.7, untrusted=0.2
version_stability = 0.7（固定常量）
tool_count_risk = min(1, declared_tool_count / 20)
permission_risk = 0.3
  若 risk_notes 含 delete/write/network/exec/shell，则升为 0.7

overall = 1 - (
    source_trust * 0.4
  + version_stability * 0.2
  + (1 - permission_risk) * 0.2
  + (1 - tool_count_risk) * 0.2
)
```

阈值：overall <= 0.35 为 low；<=0.5 为 medium；<=0.65 为 high；更高为 critical。只有 low 会 recommendation=recommended，其他都 needs_review。

这套风险评估不读取 npm 包源码、不扫描依赖 CVE、不执行行为沙箱测试。它是“目录元数据 + 字符串风险提示”的规则评分，不应描述成供应链安全审计。

## 15.3 Installer：包名白名单 + 精确版本 + npm 安装

`install_to_dir()` 的硬约束：

- transport 只能为 stdio；
- version 必须符合 exact semver；
- package_ref 只能是 npm:；
- package_name 必须来自 `allowed_npm_packages()` 内置白名单；
- npm install 到 `.data/mcp_sandbox/{sanitized_name}` 或升级时指定的版本目录；
- 从已安装 package.json 的 bin/main 字段解析 JS 入口；
- 解析出的 entry_js 必须 resolve 到 sandbox_dir 之内；
- Capability.definition.launch 保存 runner=node、entry_js、args、env_keys、sandbox_path、package；
- descriptor_hash 包含 package_ref/version/transport/declared_tools 等稳定定义字段；tool_list_hash 对工具名排序后哈希。

真实安装命令为 `npm install package@version --prefix ... --no-audit --no-fund --loglevel error`，并设置 300 秒超时。

## 15.4 “Sandbox”不是操作系统隔离

需要特别纠正命名带来的误解：`.data/mcp_sandbox` 是 npm 包安装目录，Installer 对 bin 入口做路径 containment 校验；但 Runtime 通过 Python MCP SDK `stdio_client` 启动宿主机 Node 子进程：

```text
node <entry_js> <launch_args...>
```

源码中没有看到该子进程被放进 Docker、nsjail、seccomp、独立低权限 OS 用户或文件系统只读 mount。`cwd`/安装目录不等于 OS 安全边界。

另外，`npm install` 调用没有传 `--ignore-scripts`。npm 生命周期脚本可能在宿主环境执行。当前白名单缩小了包范围，但并不消除供应链脚本风险。

因此文档中应称“受白名单约束的安装目录 + stdio 子进程”，不要称为“已经具备强 OS 沙箱的 MCP 运行时”。

## 15.5 Runtime Session 与环境变量

`MCPRuntimeClient`：

- 每个 capability 建立 stdio 子进程；
- 初始化 MCP ClientSession；
- 维护内部 asyncio Queue；
- 支持 list_tools 和 call_tool；
- Session 空闲超过 idle_timeout 自动关闭；
- 连接层错误会关闭 Session，后续请求可重新启动。

`_build_env()` 先取 MCP SDK default environment，再按 launch spec 的 env_keys/optional_env_keys 从宿主进程环境复制变量。由此需要注意：即使工具 Safety 元数据标记 `can_access_secret=False`，显式配置在 env_keys 的环境变量仍会传给 MCP 进程。应把 env_keys 当成对该 MCP server 的秘密授权，不应把它视为普通运行配置。

## 15.6 Lifecycle 与 Bootstrap

Capability 生命周期服务负责 validate、enable、disable、upgrade、rollback、uninstall 等状态动作。MCP Bootstrap 在启动时读取 active Capability，启动 runtime client，调用 tools/list 获取真实工具定义，再将工具注册到统一 ToolRegistry；disable/uninstall 时调用 unregister，避免工具仍留在 Registry 中。

工具描述符/Schema Hash 在执行前复核，升级或定义变化不能沿用旧 Action 的审批快照。MCP tool call 的结果仍作为不可信工具输出，后续进入 ToolResultGuard/Evidence 等通道。

## 15.7 CapabilityBroker 是最终执行授权点

Worker 发现能力不等于能执行能力。Broker 在 dispatch 前验证：

- capability 是否存在、active 且当前可用；
- capability_id 是否在 TaskScope.allowed_capabilities；
- descriptor_hash 是否仍匹配；
- 参数 Schema 是否合法；
- path 是否位于 allowed_roots；
- host/network 是否符合 allowed_hosts/allow_network；
- secret 使用是否符合 allow_secrets 和 capability policy；
- Action preconditions 是否仍成立；
- 是否需要审批；
- 是否需要获取资源锁。

权限必须在执行时重新校验，不能只相信模型在前一轮看到的工具 Schema。

## 15.8 MCP 当前边界

| 能力 | 源码结论 |
|---|---|
| 本地 curated catalog | 已实现 |
| 实时查询通用官方 Registry | discovery.py 未发现实现 |
| npm package 白名单 | 已实现 |
| pip 包安装 | 未发现实现；Installer 仅接受 npm: |
| stdio transport | 已实现 |
| SSE / Streamable HTTP | 候选类型有字段，但 Installer 明确拒绝非 stdio |
| 工具列表真实 tools/list | 已实现 |
| descriptor hash fencing | 已实现 |
| OS 级强隔离 | 未发现实现 |
| npm install scripts 禁用 | 当前安装命令未禁用 |
| 宿主 env key 透传 | 已实现，属于高权限授权面 |

---

# 第十六部分：SelfDev——从读码到槽位切换的实际闭环

## 16.1 ProjectReader：只读、有界、拒绝敏感路径

`ProjectReader` 的默认上限：

| 项 | 上限 |
|---|---:|
| 单文件读取 | 40,000 bytes |
| 代码上下文总字符 | 120,000 chars |
| 仓库扫描文件 | 20,000 |
| 扫描时单文件最大字节 | 1,000,000 |
| 搜索命中 | 200 |
| Prompt 中命中行 | 30 |
| 初始代码文件数 | 8 |
| 后续关联测试 | 最多补到 16 个文件 |

允许读取的文本扩展名包括 py/ts/tsx/js/jsx/json/toml/yaml/md/txt/css/html/sql/ini/cfg/sh。

拒绝目录包括 `.git`、虚拟环境、node_modules、build/dist、runtime、slots、`.data`、postgres、qdrant、object_store、logs 等；拒绝 `.env`（仅 `.env.example` 例外）、私钥、数据库文件、日志和密钥容器。

路径检查拒绝绝对路径、`..` 和符号链接逃逸。ProjectReader 没有写能力；任何改动必须经 PatchExecutor。

`collect_context(goal)`：

1. 从目标提取文件路径和标识符关键词；纯中文时退化为中文短语；
2. 在可读仓库文件中做大小写不敏感子串搜索；
3. 按命中行数排序选择最多 8 个源文件；
4. 读取文件内容，单文件不超过 40KB；
5. 对 Python backend 文件按命名规则最多补两个相关测试文件；
6. 最终 ProjectContext 包含文件正文、命中行和关键词。

它不是 AST 全仓语义索引，也不是 RAG 向量检索；它是有界文本扫描 + 关键词命中。

## 16.2 Planner 与 Trusted Core

SelfDev Planner 将 ProjectContext、目标和约束交给 LLM，要求输出结构化修改计划。模型建议的操作不是直接执行：

- 操作类型受限于 add_file / modify_file / delete_file；
- target_file 必须是相对路径；
- 目标路径必须留在 candidate app 根目录内；
- `trusted_core.validate_operations()` 在 PatchExecutor 之前拒绝核心安全文件/关键路径修改；
- add/modify 必须有非空 content；
- modify 目标必须存在。

Trusted Core 的作用是给自进化系统设置“不可自主修改的核心边界”，而不是判断 LLM 代码逻辑是否正确。

## 16.3 PatchExecutor：每次从 Active 基线重建 Inactive

`PatchExecutor.apply_to_inactive()`：

1. 执行 Trusted Core 检查；
2. 获取当前 active slot 与 inactive slot；
3. 复制 active `version_manifest.json`；
4. 使用 `sync_app_tree(active/app, inactive/app)` 重新同步整棵 app tree；
5. 逐个验证目标路径不越过 inactive/app；
6. add/modify 写入文件；
7. delete_file 必须调用统一 `safe_delete()`，Scope 仅注册 inactive slot 的 app 根目录，使用 `mode='trash'`；
8. 返回 applied/failed 清单。

因此，删除操作不是直接对生产 active slot 执行，也不是 PatchExecutor 任意调用 `os.remove`。它对 inactive candidate 使用 SafeDelete 的 trash 模式。

每次 apply 都重新从 active slot 同步基线，避免上一次候选补丁残留叠加到下一次；代价是 CandidateSlotGuard 必须在 apply 期间保护候选状态与基线指纹。

## 16.4 RepairGuard：并发锁、基线指纹、恢复日志

RepairGuard 包含三个互补机制：

- `RepairLock`：同一时间只允许一个 repair 流程修改候选槽位；使用 owner token，避免旧进程释放新进程锁；
- `CandidateSlotGuard`：记录 active/inactive slot、基线 fingerprint、候选 fingerprint、状态和是否允许晋升；
- `RepairJournal`：把阶段、操作、失败和恢复进度写成持久日志，服务重启后可以判断上次停在哪个阶段。

当候选代码与预期指纹不一致时，流程不会简单覆盖并继续晋升。恢复逻辑会判断 active baseline 是否仍相同；如果 active 已经变化，则旧 candidate 不能再被视为基于当前生产版本构建的候选。

RepairLoop 在启动时调用 `recover_interrupted()`，恢复/回滚之前中断的候选状态；修复失败会把 candidate 标记为不可晋升，ReleaseManager 在 promote 前再次检查该标记和指纹。

## 16.5 TargetedTestRunner：局部验证，不是全量测试

`run_for_changed_files()` 根据静态 `TEST_MAPPING` 将 changed_files 映射到测试文件：

- 每个测试文件独立执行 `python -m pytest <file> -q --tb=no`；
- 单个测试文件超时 30 秒；
- 测试文件不存在会判失败；
- 所有测试都 skipped、没有实际执行用例时，结果为 degraded，不算成功；
- frontend 变更额外执行 `npm run build`，超时 180 秒；
- changed_files 没有任何测试映射且不是 frontend 时，返回 `ok=false, degraded=true`，不会把“没测”当“通过”。

局限：这是按文件前缀映射的局部测试，不等于全量 unit + integration + migration + PostgreSQL/pgvector 真实服务测试。映射表的完整性决定了它能覆盖多少回归风险。

## 16.6 RepairLoop 的修复轮次

RepairLoop 将一次自修复拆成有边界的阶段：

1. 获取 RepairLock；
2. 恢复上次中断状态；
3. 读取项目上下文；
4. 调用 Planner 生成 patch plan；
5. Trusted Core 验证操作；
6. PatchExecutor 应用到 inactive slot；
7. 计算候选 fingerprint；
8. 运行 TargetedTestRunner；
9. 失败时把诊断反馈给有限轮次的修复流程；
10. 通过后记录候选可验证状态；
11. 只有用户/受信任调用方明确请求晋升，才进入 ReleaseManager。

“修复成功”只代表当前候选通过 RepairLoop 的本地验证条件，不代表它已经切换为生产版本。

## 16.7 Promotion Safety Gate

`evaluate_promotion_gate()` 统一调用 `SafetyGate.evaluate()`：

1. Kernel Contract Checks：检查关键数据库/运行时契约；
2. Replay Eval：始终在隔离环境运行，避免污染生产 DB；
3. Candidate Eval：如果传入候选槽位且 `app/` 存在，则以 subprocess 执行候选自己的 eval，而不是只运行当前进程代码；
4. 只有 critical contract 失败会阻断，warning contract 只记录；
5. replay 必须通过；
6. candidate eval 存在时必须通过；
7. 任何异常都转成 `ok=false`，fail-closed。

注意：Candidate Eval 在没有 candidate_app_dir 时会被跳过；因此调用方必须保证真实晋升入口传入正确 inactive slot。ReleaseManager 的生产 promote 路径会先定位 inactive slot，再调用 `evaluate_promotion_gate()`。

## 16.8 ReleaseManager：健康检查、切换、监控和回滚

`ReleaseManager.promote()`：

1. 获取 release lock；
2. 确认 active slot 为 A/B，另一个为 candidate；
3. `_candidate_block()` 检查候选不可晋升标记、基线/候选 fingerprint；
4. 切换前检查 inactive slot health；
5. 默认执行 Safety Gate，失败直接拒绝；
6. 如果 Supervisor Client 可用，提交 promote 请求，返回 `switch_requested`，真正切换由 Supervisor daemon 完成；
7. 如果没有 Supervisor Client，直接更新 active slot，再做切换后健康检查；
8. 切换失败则回滚到 previous slot；
9. 成功后进入 monitoring 窗口，`monitor_once()` 继续检查健康，失败时自动 rollback。

所以“promote API 返回 ok”在 Supervisor 托管路径中可能只代表请求已受理，不代表新进程已经成功启动并完成监控窗口。

## 16.9 SelfDev 当前边界

| 能力 | 实际实现 |
|---|---|
| 有界仓库读取 | 已实现，关键词扫描，不是 AST/RAG 全仓理解 |
| 结构化补丁计划 | 已实现，LLM 提议 + 服务端验证 |
| Trusted Core | 已实现，保护关键路径 |
| Inactive Slot 隔离 | 已实现，补丁不直接写 Active |
| 删除操作 | 走 SafeDelete，inactive app Scope，trash 模式 |
| 局部测试 | 已实现，依赖静态 TEST_MAPPING |
| Replay Eval / Contract | 已实现，fail-closed |
| Candidate Eval | 候选目录存在时执行；缺目录时跳过 |
| Slot Promote/Rollback | 已实现，Supervisor 可接管真实进程切换 |
| 全量端到端测试覆盖 | 不能由 TargetedTestRunner 证明 |
| OS 级候选代码沙箱 | 未发现完整 OS 级隔离证明 |

---

# 第十七部分：跨模块可靠性语义——不要把 Outbox 等同 exactly-once

## 17.1 各模块的幂等键与 fencing

| 模块 | 关键幂等/版本字段 | 防止的问题 |
|---|---|---|
| Memory Extraction | Outbox claim_token + MemoryIngestionRun.execution_token | 旧 Worker 写入、同 Turn 重复提取 |
| Memory Write | Advisory Lock + record_version + content/state hash + idempotency_key | 同 Key 并发覆盖、基于旧状态写入 |
| Memory Vector | record_version + content_hash + embedding_model | 旧 embedding 覆盖新记忆 |
| Knowledge Vector | chunk content_hash + embedding_model | 复用旧模型/旧内容向量 |
| Retrieval Index | source_version/source_hash + active/building generation + tombstone | 旧刷新任务覆盖新索引、重建期间漏更新 |
| Graph Projection | relation_key + source_record_version + canonical graph_facts | 关系重复、来源记忆失效后仍 active |
| Compaction | Turn/Event Manifest + content_hash + WorkingState snapshot hash + source_hash | 压缩输入在冻结后被篡改或过期摘要提交 |
| Forget | operation_key + selector_hash + target/dependency unique key + action idempotency_key + stage execution_token | 多阶段重复执行、游标跳过、旧 Worker 提交 |
| Task Action | action idempotency_key + arguments_hash + descriptor_hash + execution_token | 重复外部副作用、工具定义变化后继续旧审批 |
| Desktop Action | Node journal + receipt + idempotency_key + arguments_hash + result_hash | 断线后不确定执行结果 |
| SelfDev | RepairLock owner token + candidate fingerprint + repair journal | 并发修改候选、基线漂移后误晋升 |

## 17.2 Outbox 提供的到底是什么

事务 Outbox 的核心保证是：业务状态变化和待执行 Job 在同一个数据库事务提交。它避免“业务已提交但内存队列消息丢失”。

但 Outbox 通常是 at-least-once：Job 可能因 lease 过期、Worker 崩溃或重试被再次执行。因此需要 Handler 自身幂等键、版本 fencing、外部操作 idempotency key 或 reconciliation。

只有当副作用系统本身支持可查询幂等键/执行回执时，才能把“重复投递”收敛成“业务效果只发生一次”。

## 17.3 当前能证明的执行语义矩阵

| 操作 | 可靠性语义 |
|---|---|
| Memory/Graph/Vector/Index 投影 | Outbox at-least-once + source version/hash fencing，目标是幂等最终一致 |
| Segment/Checkpoint Compaction | 冻结输入 + source_hash + claim token，避免过期输入提交 |
| Forget | 多阶段持久 Saga + 目标/动作幂等 + Stage Reconcile；验证失败保留 Shield |
| Task DB 状态 | 数据库事务 + Checkpoint + Outbox 唤醒 |
| Desktop 外部动作 | Node journal + server receipt + result hash 对账；unknown 不盲重放 |
| 普通 MCP/外部 HTTP | 取决于具体 capability 是否实现幂等/回执；不能仅凭 Outbox 宣称 exactly-once |
| Shell 命令 | 进程执行可能产生外部副作用；进程中断时不能仅靠 Outbox 推断命令是否已完成 |
| SelfDev promote | Candidate fingerprint + safety gate + Supervisor health/monitor/rollback；不是数据库事务能原子化整个进程切换 |

## 17.4 多个“真相源”与“派生投影”必须分开

| 真相源 | 派生数据 |
|---|---|
| MemoryRecord + Evidence + Lineage | CoreMemoryBlock、GraphRelation、MemoryVectorProjection、RetrievalIndexEntry、Markdown projection |
| Document 原文对象 + Document 元数据 + Chunk | KnowledgeChunkVector |
| TurnRecord + Event + CompactionInput | SegmentSummary、EpochCheckpoint、RetrievalIndexEntry |
| AgentTask + Action + Event + Evidence/Artifact | TaskContext snapshot（每次重新装配） |
| Capability definition/version | MCP Runtime Session + ToolRegistry tool view |
| Active slot app tree + version manifest | Inactive candidate、Candidate fingerprint、Release monitor state |

排查数据不一致时，应先找到 canonical source，再检查派生投影。直接修复派生表可能被下一次 Outbox refresh/rebuild 覆盖。

---

# 第十八部分：优先级清单与未覆盖范围

以下事项按优先级列出，代表当前源码审查中尚未证明或需要补齐的能力与正确性边界；它们不是已完成的全仓审计结论。

## P0：隐私与执行正确性

1. **Forget Summary 重建过滤完整性**（已完成，见 13.11）：Turn Tombstone、Memory Evidence 来源 Event 进入统一 `blocked_ids`；重建摘要在 50 条上限前过滤，source 字段仅取允许来源。
2. **Forget JSON 数组清理**：修复 `_scrub_wide()` 对 source_event_ids/source_segment_ids 的标量 `IN` 比较，使用真正 JSON containment 或 join。
3. **Forget Verify 证明范围**：把“目标状态验证”与“所有内容承载表清理验证”区分；不能仅靠 Memory/Tombstone 检查就宣称所有内容都已证明清除。
4. **Forget all_user_data 用户边界**：若支持多用户，所有宽范围更新必须按 owner/tenant 限定，不能无条件更新整张表。
5. **Forget Task Runtime / Knowledge 对象覆盖**：确认 AgentTask、AgentAction、TaskEvidence、TaskArtifact、对象存储 bucket、Document/Chunk 是否属于遗忘数据面；补依赖、Scrub 和 Verify。
6. **非 Desktop Unknown Action**：为 MCP/Shell/HTTP 等可能产生副作用的 Action 定义对账或人工恢复流程，避免 RECONCILING 无法收敛。
7. **MCP 子进程权限边界**：明确 npm lifecycle scripts、env_keys 秘密授权和宿主机执行风险；若支持不可信 MCP，增加 OS 级隔离。
8. 在真实 PostgreSQL 与 SQLite 环境分别验证 Forget 的 JSON 更新语句、游标续跑、失败重试和 Shield 保留/退休语义。
9. 按「Forget Saga」一节 F-01 至 F-12 补齐 Forget 集成测试，优先覆盖 Summary 重建过滤、JSON 数组清理、`all_user_data` 用户边界、Task Evidence/Artifact 对象存储清理和 Verify 完整性。

## P1：权限、召回与任务语义

10. 建立 Exact、Lexical、Vector、Graph、Unified Retrieval、Knowledge Search 的统一 Scope/ACL 测试矩阵，特别是多用户/多租户边界。
11. 明确 Forget `mode × selector` 合法组合矩阵；禁止多个 Selector 混用或明确 union/intersection 语义；对混合选择器、超大显式 ID 列表和 operation_key 重试语义补充接口契约。
12. `memory_only` 是否允许清除独占 source Event；修正文案或调整行为。
13. Forget 的 scope/canonical_key/time_range 对所有内容承载表的覆盖清单。
14. Knowledge RAG 加入 workspace/user ACL（如果系统目标包含多用户/多租户）。
15. 完整审计 Goal Trigger 的周期计算、cooldown、max-per-day/week、事件派发、前台 Turn 冲突处理。
16. 完整审计 Desktop Node 注册、心跳、在线租约、journal 持久化、断线重连和节点授权。
17. 对 `MemoryMutationExecutor` 的每一种 mutation（create/reinforce/revise/supersede/merge/promote/sleep/archive/wake/forget）补字段级状态转换表和并发测试。
18. Task Resource Lock 的 lease 续期、长耗时 Action 和跨进程争抢测试。
19. MCP descriptor/version 更新后，检查现存 AgentAction、approval snapshot、ToolRegistry 是否全部被失效/刷新。
20. 对 Forget 的 operation_key 端到端重试语义补集成测试；显式 ID 列表设置最大请求规模。
21. 对所有关键 Prompt 模板补充字段来源、信任级别、注入位置和长度上限，而不只记录调用函数。

## P2：运维和工程质量

22. 为每类 Outbox Handler 形成完整可靠性清单：operation_id、幂等键、lease、fencing、外部副作用、重试安全性、unknown 对账。
23. Knowledge 增加 user/workspace ACL、完整 Chunk 回读接口（而不是仅 200 字符 preview，同时保留原文路径和行号来源）、content_hash mismatch 维护扫描；如有需要再引入标题路径、overlap、父子 Chunk。
24. Knowledge Chunker 增加 Markdown 标题路径、语义边界、overlap 或父子 Chunk（如果产品确实需要）。
25. Knowledge vector maintenance 增加 content_hash mismatch 检测。
26. SelfDev 扩大静态 TEST_MAPPING，关键模块改动触发 PostgreSQL/pgvector/Embedding 集成测试；并评估候选代码的 OS 级隔离。
27. SelfDev TargetedTestRunner 扩大静态测试映射，关键改动触发更高层集成测试；必要时对 candidate 执行 migration/真实依赖测试。
28. ProjectReader 增加结构化符号索引或测试依赖图，降低纯关键词命中导致读错代码的概率。
29. MCP 支持其他 transport 前先定义网络策略、TLS、认证、超时、断线恢复和远端 descriptor pinning。
30. 对资源锁的 lease 续期、长耗时 Action 和跨进程争抢补充压力测试。
31. 重新执行全量测试与真实 PG/TEI 集成测试，并归档命令、环境与结果。

---

# 附录 A：核心源码导航

| 主题 | 文件 |
|---|---|
| Turn 生命周期 | `backend/aiive/runtime/turn_execution.py` |
| AgentGraph | `backend/aiive/runtime/agent_graph.py` |
| 上下文装配 | `backend/aiive/runtime/context_assembler.py` |
| Token 预算 | `backend/aiive/runtime/context_budget.py`、`token_counter.py`、`token_models.py` |
| 历史读取 | `backend/aiive/runtime/thread_state.py` |
| WorkingState | `backend/aiive/runtime/working_state.py` |
| Segment/Epoch | `backend/aiive/runtime/epoch_manager.py`、`compaction.py` |
| 记忆提取/准入 | `backend/aiive/memory/memory_extractor.py`、`memory_gate.py` |
| 记忆写入 | `backend/aiive/memory/memory_write_service.py`、`memory_mutation.py` |
| 记忆冲突 | `backend/aiive/memory/conflict_candidates.py`、`conflict_adjudication.py` |
| 自动召回 | `backend/aiive/memory/automatic_recall.py`、`recall_fusion.py` |
| Memory Vector | `backend/aiive/memory/vector_projection.py`、`vector_bootstrap.py`、`embedding_provider.py` |
| Unified Retrieval | `backend/aiive/retrieval/unified_retriever.py`、`retrieval_index.py`、`indexing_service.py`、`index_rebuild.py` |
| Knowledge RAG | `backend/aiive/knowledge/ingestor.py`、`chunker.py`、`chunk_vectors.py`、`maintenance.py` |
| Graph | `backend/aiive/memory/graph_projection.py`、`graph_expansion.py`、`graph_rebuild.py` |
| Forget | `backend/aiive/worker/handlers_forget.py`、`backend/aiive/forget/` |
| Task Runtime | `backend/aiive/task_runtime/runtime.py`、`context_assembler.py`、`tool_injection.py` |
| Capability Broker | `backend/aiive/control/capability_broker.py`、`task_policy.py`、`scope.py` |
| Goal | `backend/aiive/task_runtime/goal_plan.py`、`goal_service.py`、`goal_trigger_service.py`、`goal_decision.py`、`goal_questions.py` |
| Outbox | `backend/aiive/worker/outbox_worker.py`、`outbox_handlers.py`、`handler_registry.py` |
| MCP | `backend/aiive/mcp/`、`backend/aiive/capability/` |
| SelfDev | `backend/aiive/selfdev/`、`backend/aiive/eval/`、`backend/aiive/supervisor/` |

## 核心函数定位

| 功能 | 核心文件/函数 |
|---|---|
| 记忆提取三阶段 | `worker/outbox_handlers.py::handle_memory_extraction` |
| 记忆候选发现 | `memory/conflict_candidates.py::ConflictCandidateFinder.discover` |
| 冲突语义裁决 | `memory/conflict_adjudication.py::SemanticConflictAdjudicator`、`adjudication_to_plan` |
| Entity Binding 校验 | `memory/conflict_adjudication.py::validate_entity_bindings`、`apply_entity_bindings` |
| 用户确认闭环 | `memory/conflict_confirmation.py::MemoryConflictService` |
| Memory 写入事务 | `memory/memory_write_service.py::write_batch`、`memory/memory_mutation.py::MemoryMutationExecutor` |
| Forget Phase A | `forget/phase_a_shield.py::execute_phase_a_shield` |
| Forget Selector | `forget/selector_normalizer.py::normalize_selector` |
| Forget Cascade | `worker/handlers_forget.py::handle_forget_cascade`、`_materialize_targets`、`_discover_dependencies` |
| Forget Summary 重建 | `worker/handlers_forget.py::_rebuild_summaries_batch`、`_try_generate_summary` |
| Forget Scrub | `worker/handlers_forget.py::handle_forget_purge`、`_scrub_batch`、`_scrub_wide` |
| Forget Verify/Reconcile | `worker/handlers_forget.py::handle_forget_verify`、`forget/reconcile_service.py::ForgetReconcileService` |
| Task Context | `task_runtime/context_assembler.py::TaskContextAssembler.snapshot` |
| Task Action 执行 | `task_runtime/runtime.py::PersistentTaskRuntime._dispatch_ready` |
| Task 恢复 | `task_runtime/repository.py::recover_stale_tasks`、`reconcile_queued_tasks` |
| Desktop 对账 | `desktop/reconciliation.py::DesktopReconciliationService` |
| Evidence | `task_runtime/evidence.py::EvidenceStore.capture_result` |
| Resource Lock | `task_runtime/resource_locks.py::ResourceLockService` |
| Watcher | `task_runtime/watchers.py::WatcherService` |
| MCP Discovery | `mcp/discovery.py::search_mcp_candidates` |
| MCP Risk | `mcp/capability_planner.py::CapabilityPlanner._compute_risk` |
| MCP Installer | `mcp/installer.py::install_to_dir`、`install_sandbox` |
| MCP Runtime | `mcp/runtime_client.py::MCPRuntimeClient._build_env`、`_run` |
| MCP Bootstrap | `mcp/bootstrap.py` |
| Capability Lifecycle | `capability/lifecycle.py::CapabilityLifecycleService` |
| Project Reader | `selfdev/project_reader.py::ProjectReader.collect_context` |
| Patch Executor | `selfdev/patch_executor.py::PatchExecutor.apply_to_inactive` |
| Targeted Tests | `selfdev/targeted_test_runner.py::TargetedTestRunner.run_for_changed_files` |
| Repair Guard | `selfdev/repair_guard.py::RepairLock`、`CandidateSlotGuard`、`RepairJournal` |
| Repair Loop | `selfdev/repair_loop.py::SelfDevRepairLoop.run` |
| Promotion Gate | `eval/gate.py::evaluate_promotion_gate`、`SafetyGate.evaluate` |
| Release | `supervisor/release_manager.py::ReleaseManager.promote`、`monitor_once` |
| Knowledge RAG | `knowledge/ingestor.py::search_chunks`、`knowledge/blocks.py::parse_document_blocks`、`knowledge/chunker.py::chunk_blocks`、`knowledge/chunk_vectors.py::ChunkVectorService` |

---

# 附录 B：主要源码链接

- [ContextAssembler](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/runtime/context_assembler.py)
- [ContextBudget](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/runtime/context_budget.py)
- [EpochManager](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/runtime/epoch_manager.py)
- [Compaction](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/runtime/compaction.py)
- [Automatic Recall](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/memory/automatic_recall.py)
- [Recall Fusion](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/memory/recall_fusion.py)
- [Memory Vector Projection](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/memory/vector_projection.py)
- [Unified Retriever](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/retrieval/unified_retriever.py)
- [Retrieval Indexing](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/retrieval/indexing_service.py)
- [Knowledge Ingestor](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/knowledge/ingestor.py)
- [Knowledge Chunker](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/knowledge/chunker.py)
- [Knowledge Chunk Vectors](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/knowledge/chunk_vectors.py)
- [Graph Projection](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/memory/graph_projection.py)
- [Graph Rebuild](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/memory/graph_rebuild.py)
- [Goal Questions](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/task_runtime/goal_questions.py)
- [Memory Extraction Handler](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/worker/outbox_handlers.py)
- [Conflict Candidate Finder](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/memory/conflict_candidates.py)
- [Conflict Adjudication](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/memory/conflict_adjudication.py)
- [Conflict Confirmation](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/memory/conflict_confirmation.py)
- [Forget Phase A](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/forget/phase_a_shield.py)
- [Forget Models](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/db/forget_models.py)
- [Forget Handlers](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/worker/handlers_forget.py)
- [Forget Reconcile](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/forget/reconcile_service.py)
- [Task Runtime](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/task_runtime/runtime.py)
- [Task Repository Recovery](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/task_runtime/repository.py)
- [Desktop Reconciliation](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/desktop/reconciliation.py)
- [MCP Discovery](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/mcp/discovery.py)
- [MCP Installer](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/mcp/installer.py)
- [MCP Runtime Client](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/mcp/runtime_client.py)
- [Project Reader](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/selfdev/project_reader.py)
- [Patch Executor](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/selfdev/patch_executor.py)
- [Targeted Test Runner](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/selfdev/targeted_test_runner.py)
- [Eval Gate](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/eval/gate.py)
- [Release Manager](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/supervisor/release_manager.py)
- [Knowledge API](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/api/routes_knowledge.py)
- [BM25 Search Service](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/memory/bm25_search.py)
- [Recall Ranking](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/memory/recall_ranking.py)
- [Recall Config](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/memory/recall_config.py)
- [Knowledge Block Parser](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/knowledge/blocks.py)
- [Token Estimator](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/knowledge/tokenization.py)
- [Embedding Input Builder](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/knowledge/embedding_input.py)
- [Knowledge Maintenance](https://github.com/juzixiaoren/AIive/blob/main/backend/aiive/knowledge/maintenance.py)
- [Knowledge Fallback Regression Tests](https://github.com/juzixiaoren/AIive/blob/main/tests/unit/backend/test_chunker_fallback.py)