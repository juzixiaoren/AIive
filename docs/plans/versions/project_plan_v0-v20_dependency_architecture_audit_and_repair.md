# AIive project_plan_v0-v20_dependency_architecture_audit_and_repair.md

> 适用状态：编码 AI 已做到旧 V20，且已经暴露 reminder 等功能被硬编码/模拟的问题。  
> 本文件定位：在当前 V20 代码基础上做一次 **终局技术依赖与架构一致性审查 + 修复**。  
> 目标：确认 V0-V20 不是“能演示”，而是已经落在 AIive 终局技术栈和主链路上。  
> 执行规则：先审查，生成报告，再修 BLOCKER；不得继续旧 V21；不得跳过审查直接编码。

---

## 0. 这次修复的根因

这次不是单个 reminder bug，而是阶段计划之前写得不够硬，导致编码 AI 可以用以下方式“假完成”：

```text
1. 总纲写了 LangGraph，但阶段里没有强制 /api/chat 必须走 LangGraph。
2. 总纲写了 memory_records / Mem0-style / revision / conflict，但早期阶段允许 append-only 太久。
3. 总纲写了 Context Builder / Router，但阶段里没有强制所有能力都经过 context/action/trace。
4. 总纲写了 Qdrant / Object Storage，但阶段里没有要求到 V20 前审查是否真的接入。
5. 总纲写了 ToolRegistry / Permission / safe_delete，但阶段里没有强制所有 chat action 通过 tool/capability。
6. 总纲写了 tasks/outbox/worker，但 reminder 可以被写成 sleep/setTimeout/硬编码演示。
7. 总纲写了 MCP discovery/sandbox，但没有要求检查是否真的基于 registry metadata、descriptor hash、sandbox smoke。
8. 总纲写了 A/B slot，但没有要求检查 selfdev 是否误改 active slot。
```

所以本文件不是继续加功能，而是把 V0-V20 拉回终局架构。

---

## 1. 最高优先级：先生成架构审计报告

编码 AI 必须先生成：

```text
docs/audit/v0_v20_dependency_architecture_report.md
```

报告必须逐项回答：

```text
1. 当前后端是否是 Python + FastAPI + Pydantic v2 + SQLAlchemy 2.x + Alembic + PostgreSQL？
2. /api/chat 是否真的进入 LangGraph StateGraph？如果没有，是手写 if/else 还是散装 agent_loop？
3. LangGraph 是否有持久 checkpoint？生产路径是否使用 PostgreSQL checkpointer 或自研 DB checkpointer？
4. Context Builder 是否是所有 LLM 调用的唯一上下文装配入口？有没有地方直接拼 prompt？
5. memory_records 是否是记忆生命周期唯一真相源？有没有其他表/文件自行决定 memory active/sleep/forget？
6. 记忆是否支持 revision/supersede/resolve_for_context？是否仍 append-only？
7. Mem0-style extraction 是否存在为 extractor + gate + lifecycle pipeline，而不是简单把每句话都写 memory？
8. Qdrant 是否在 V16 后真正接入？如果未接入，是否仍在用临时 list/string search 冒充 hybrid retrieval？
9. Object Storage / local object store adapter 是否存在？LLM 输出、context snapshot、raw docs、patch/test logs 是否有 content_ref/object_ref，而不是只塞 DB 大字段？
10. PostgreSQL Outbox 是否存在？异步 memory extraction、indexing、maintenance、task notification 是否通过 outbox/worker，而非内存队列？
11. ToolRegistry / PermissionManager / ToolExecutor 是否为所有工具调用主路径？有没有 chat handler 直接执行工具？
12. safe_delete + Scope Registry 是否是唯一删除入口？有没有 os.remove/shutil.rmtree/rm -rf/unlink 直接删除？
13. Scheduler 是否是真实持久化 task worker？有没有 sleep/setTimeout/前端模拟 reminder？
14. MCP discovery/sandbox 是否基于 registry/config metadata、descriptor_hash、risk、sandbox smoke？有没有硬编码候选？
15. A/B slot 是否真实存在？selfdev 是否只改 inactive slot？有没有 active slot 直接 patch？
16. Trace / Context Snapshot / Action Card 是否覆盖所有 chat-triggered ability？
17. Tests 是否 targeted、可清理、不依赖真实 60 秒等待、不污染生产 DB/Qdrant/object store？
```

审计报告必须把问题分级：

```text
BLOCKER：能力是假的、不可持久、绕过核心架构、安全边界失效。
MAJOR：能力可用但不走 chat/action card/trace/context，或不可观测。
MINOR：命名/目录/类型不规范，但不破坏主链路。
```

BLOCKER 必须在本修复阶段完成；MAJOR 原则上应完成；MINOR 可列为 follow-up。

---

## 2. 必须执行的审计命令

编码 AI 可以根据项目目录调整路径，但报告必须贴结果摘要。

```bash
# LangGraph / runtime
rg "from langgraph|StateGraph|checkpointer|PostgresSaver|MemorySaver|graph\.invoke|graph\.astream" backend || true
rg "def chat|/api/chat|ChatResponse|agent_loop|if .*message|contains|startswith" backend || true

# fake/hardcode/demo/sleep
rg "asyncio\.sleep|time\.sleep|setTimeout|setInterval|Fake|Mock|Dummy|Demo|hardcoded|模拟|demo" backend frontend || true
rg "一分钟后|提醒我|hi|明早|以后叫我|忘掉|删除|搜索 MCP|给自己加" backend frontend || true

# DB / ORM / migrations
rg "SQLAlchemy|declarative_base|Mapped\[|alembic|revision|op\.create_table|session\.add|execute\(" backend alembic || true

# memory / context
rg "memory_records|MemoryStore|update_content|supersede|resolve_for_context|memory_key|ContextBuilder|context_snapshot|ContextItem" backend || true

# Qdrant / object store / outbox
rg "qdrant|QdrantClient|hybrid|RRF|object_store|ObjectStorage|content_ref|object_ref|outbox_jobs|OutboxWorker" backend || true

# tools / permissions / deletion
rg "ToolRegistry|PermissionManager|ToolExecutor|CapabilityDefinition|safe_delete|ScopeRegistry|os\.remove|shutil\.rmtree|unlink|rmdir|rm -rf" backend || true

# tasks / scheduler / worker
rg "tasks|TaskService|schedule_reminder|TaskWorker|notification|APScheduler|due|next_check_at" backend frontend || true

# MCP / selfdev / slots
rg "mcp|descriptor_hash|sandbox|registry|capability_versions|active_slot|inactive_slot|slot_manager|manifest" backend || true
```

---

## 3. 终局依赖矩阵：V20 前必须满足什么

> 不是所有终局依赖都必须在 V20 前完整实现，但 V20 前必须有明确的 truth source、adapter 或检查点，不能用硬编码替代。

| 模块 | 终局选型 | V20 前最低要求 | 如果当前没有，如何修 |
|---|---|---|---|
| 后端 API | FastAPI + Pydantic v2 | `/api/chat`、debug APIs、service layer 基于 FastAPI/Pydantic | 不要继续扩 Flask/Django；新增 FastAPI app factory，保留 service 迁移 |
| ORM/DB | SQLAlchemy 2.x + PostgreSQL + Alembic | events、threads、llm_calls、memory_records、tasks 等主表在 PostgreSQL，migration 可重复 | 建 Alembic 基线；禁止 SQLite-only 作为生产设计 |
| Agent runtime | LangGraph StateGraph + checkpointer | `/api/chat` 主路径进入 graph；thread_id 进入 checkpoint | 加 LangGraph Runtime Adapter，不重写 services |
| 短期状态 | LangGraph checkpoint | 当前 thread state 可恢复、可重放基本信息 | 自研 DB checkpointer 可临时，但必须持久化 |
| LLM 接入 | OpenAI-compatible client | 真实 LLM smoke；单测 mock；llm_calls 有 model/prompt/schema version | 若散落调用，统一到 LLMClient |
| Context | Context Builder/Router | 所有 LLM 调用必须先生成 Context Snapshot；不得直接拼 prompt | 建 ContextBuilder facade，旧 prompt 逻辑迁入 |
| Memory truth | PostgreSQL memory_records | lifecycle_state、memory_key、revision、supersede、resolve_for_context | append-only 必须修 |
| Memory extraction | Mem0-style pipeline | extractor -> gate -> candidate/active/revise；不是每句都记 | 加 MemoryExtractor + MemoryGate |
| Temporal KG | Graphiti/Zep-style 最终层 | V20 前可不接 Graphiti，但不得把 KG 当 truth source；可留 projector 接口 | 后续 V27 接 PG graph/Graphiti adapter |
| Knowledge | PostgreSQL documents/chunks + Qdrant | V15 chunks；V16 Qdrant 派生索引；retrieval runs 可观测 | 未接 Qdrant 必须列 BLOCKER/MAJOR，看 V16 是否已执行 |
| Object storage | Local object store adapter -> S3/MinIO | 至少本地 object_store adapter；raw docs/context/patch/log 大对象用 object_ref | 不要把大对象全塞 DB text |
| Event async | PostgreSQL events + outbox_jobs + worker | 异步派生任务走 outbox；内存队列不能是 truth source | 加 outbox table + worker tick |
| Tools | ToolRegistry + Permission + Executor | Chat action 都经 Dispatcher -> Tool/Capability/Service | 禁止 endpoint 直接执行 |
| Deletion | safe_delete + Scope Registry | 所有删除路径经 safe_delete | 直接删除列 BLOCKER |
| Scheduler | tasks table + worker | Reminder/routine/watch 持久化；worker 扫 due tasks | sleep/setTimeout 真实调度列 BLOCKER |
| MCP | registry/config discovery + sandbox | V9/V10 有 discovery/sandbox；descriptor_hash/risk 记录 | 硬编码候选列 MAJOR/BLOCKER |
| Selfdev | manifest-based A/B slot | V11-V13 只改 inactive slot，active 受保护 | active patch 列 BLOCKER |
| UI 可观测 | React + Vite + TS | Chat action_cards + trace links；无假数据 truth source | 前端模拟状态列 BLOCKER/MAJOR |

---

## 4. R1：LangGraph 主路径修复

### 4.1 目标

`POST /api/chat` 必须进入 LangGraph，不允许生产路径只用手写 if/else agent_loop。

### 4.2 推荐结构

```text
backend/aiive/runtime/graph.py
backend/aiive/runtime/state.py
backend/aiive/runtime/nodes/ingest_node.py
backend/aiive/runtime/nodes/context_node.py
backend/aiive/runtime/nodes/intent_node.py
backend/aiive/runtime/nodes/action_node.py
backend/aiive/runtime/nodes/llm_node.py
backend/aiive/runtime/nodes/persist_node.py
```

最小图：

```text
ingest_user_message
↓
build_context
↓
detect_intent
↓
dispatch_action
↓
maybe_call_llm
↓
persist_response
```

### 4.3 禁止

```text
/api/chat 里直接 if 用户说提醒 -> 创建假回复。
/api/chat 里直接拼 prompt 调 LLM。
/api/chat 里直接调工具或写 DB。
生产路径只用 MemorySaver / dict 作为状态。
```

### 4.4 验收

```text
1. R0 报告证明 /api/chat 进入 graph.invoke/astream。
2. 每轮 chat 有 checkpoint 或持久 thread state。
3. 重启后同 thread_id 能继续看到近期状态。
4. tests/runtime/test_langgraph_chat_flow.py 通过。
```

---

## 5. R2：Context Builder / Router 修复

### 5.1 目标

所有 LLM 调用必须经 Context Builder，避免散落 prompt、硬编码 profile、漏注入/乱注入记忆。

### 5.2 必须实现/检查

```text
ContextBuilder.build(trace_id, thread_id, user_message, intent_result) -> ContextRun
ContextRun 包含 stable_prefix、working_set、evidence_pack、excluded_items。
ContextItem 必须有 source、trust_level、kind、content_ref/preview、token_estimate。
所有 LLMClient.call() 必须接受 context_ref 或 context_items。
context_snapshots 表记录本次注入和排除理由。
```

### 5.3 禁止

```text
在某个 service 内自己拼完整 prompt 绕过 ContextBuilder。
把 untrusted web/PDF/MCP/tool output 当 instruction。
把 superseded memory 注入 context。
```

### 5.4 验收

```text
1. 任意 chat trace 能查 Context Snapshot。
2. 记忆 A superseded 后不进入 injected_items，只出现在 excluded_items。
3. Tool output trust_level=tool_result，不是 user/system instruction。
```

---

## 6. R3：Memory 系统修复

### 6.1 目标

把 memory 从 append-only 修为可修订、可覆盖、可解释。

### 6.2 必须字段

`memory_records` 至少包含：

```text
memory_id
memory_type
memory_key
lifecycle_state = candidate | active | sleeping | archived | superseded | deprecated | forgotten
content
confidence
source_event_id
last_source_event_id
revision
revision_of
supersedes
superseded_by
lineage
pinned
valid_from
valid_to
created_at
updated_at
test_run_id
```

### 6.3 必须服务方法

```python
create_candidate(...)
activate(memory_id)
find_by_memory_key(memory_key)
update_content(memory_id, new_content, reason)
supersede(old_memory_id, new_memory_id, reason)
resolve_for_context(memory_types=None, thread_id=None, query=None)
forget(memory_id)
```

### 6.4 Mem0-style pipeline 要求

这里的 “Mem0-style” 不是必须安装 mem0 包，而是必须具备类似 pipeline：

```text
user/tool/event
↓
extract memory proposal
↓
classify memory_type + memory_key
↓
gate confidence / explicitness / trust source
↓
create candidate / active / revise / supersede
↓
Context Builder resolve_for_context
```

### 6.5 必测场景

```text
用户：以后叫我 A。
系统：记住。
用户：以后叫我 B。
系统：已更新。
用户：我叫什么？
系统：B。
```

数据库必须是：

```text
A lifecycle_state = superseded
B lifecycle_state = active
B.supersedes 包含 A
Context Snapshot injected 包含 B，不包含 A
```

---

## 7. R4：Object Storage / 大对象引用修复

### 7.1 目标

终局里 Object Storage 是原始证据层。V20 前不一定要接 MinIO/S3，但必须有可替换的本地 adapter，不允许所有原始文件、context、LLM output、patch/test logs 全塞数据库文本字段。

### 7.2 必须实现最小 adapter

```text
backend/aiive/storage/object_store.py
```

接口：

```python
put_bytes(bucket: str, key: str, data: bytes, metadata: dict) -> ObjectRef
put_text(bucket: str, key: str, text: str, metadata: dict) -> ObjectRef
get(ref: ObjectRef) -> bytes
exists(ref: ObjectRef) -> bool
delete(ref: ObjectRef)  # must call safe_delete for local files
```

本地目录：

```text
.data/object_store/
  raw-documents/
  context-snapshots/
  llm-outputs/
  test-logs/
  patches/
  exports/
```

### 7.3 数据库要求

```text
documents.raw_object_ref
context_snapshots.object_ref 可选
llm_calls.input_context_ref / output_ref 可选
patches.patch_ref 可选
test_results.log_ref 可选
```

### 7.4 验收

```text
1. 摄入文档时原文有 object_ref。
2. 大 context snapshot 可落 object store。
3. 测试产生 object store prefix 由 fixture 清理。
```

---

## 8. R5：Qdrant / Retrieval 修复

### 8.1 目标

如果项目已执行到 V20，V16 的 Qdrant/Hybrid Retrieval 应该已经完成。必须审查当前是否真实接入。

### 8.2 必须检查

```text
1. 是否有 QdrantClient 或清晰的 VectorStore adapter。
2. documents/chunks 是否仍以 PostgreSQL 为真相源。
3. Qdrant payload 是否包含 chunk_id/document_id/source_hash。
4. 是否能从 chunks 重建 Qdrant collection。
5. RetrievalRun / RetrievalCandidate 是否记录候选、分数、来源、gate decision。
6. Context Builder 是否只注入 gate 后 chunks。
```

### 8.3 如果没有 Qdrant

如果 V16 被跳过或假实现：

```text
1. 先实现 QdrantAdapter 接口。
2. 开发环境可用 docker qdrant 或 in-memory fake 仅用于 unit test。
3. smoke 必须对真实 Qdrant 或本地 docker 运行。
4. 不得用 Python list cosine 冒充生产检索。
```

### 8.4 验收

```text
1. ingest 文档 -> chunks 入 PostgreSQL -> embeddings 入 Qdrant。
2. search query -> retrieval_run -> candidates -> context injected。
3. 删除/forget 后派生 Qdrant 向量不可召回。
```

---

## 9. R6：Outbox / Worker 修复

### 9.1 目标

异步派生任务必须用 PostgreSQL outbox，不能只用内存 queue。

### 9.2 必须覆盖

```text
memory extraction
qdrant indexing
markdown/json projection update
maintenance scan
forget sync
reminder notification
mcp sandbox smoke 可选
```

### 9.3 outbox_jobs 字段

```text
job_id
job_type
payload
status = pending | running | completed | failed | deadletter
source_event_id
trace_id
retry_count
next_run_at
last_error
created_at
updated_at
test_run_id
```

### 9.4 验收

```text
1. Chat 不阻塞等待 memory extraction。
2. worker 重启后 pending job 仍可消费。
3. 失败 job 有 retry/deadletter。
4. 测试 job 自动清理。
```

---

## 10. R7：Tool / Capability / Permission 修复

### 10.1 目标

所有能做事的能力必须走：

```text
LangGraph -> IntentRouter -> AgentActionDispatcher -> ToolRegistry/Capability -> PermissionManager -> Service
```

### 10.2 最低内置能力

必须注册：

```text
schedule_reminder
list_tasks
cancel_task
show_notifications
safe_delete
search_knowledge
ingest_document
forget_memory
run_memory_maintenance
search_mcp
install_mcp_sandbox
create_selfdev_plan
apply_patch_to_inactive_slot
run_targeted_tests
promote_slot
rollback_slot
query_rhythm
query_attention_state
```

### 10.3 禁止

```text
Chat endpoint 直接 schedule task。
Chat endpoint 直接删除文件。
Chat endpoint 直接调用 MCP。
Chat endpoint 直接 patch 文件。
```

### 10.4 验收

```text
任意非纯聊天能力的 trace 里都有 capability_id、permission_decision、action_card。
```

---

## 11. R8：Scheduler Capability / Reminder 修复

### 11.1 正确链路

```text
Chat 输入 “一分钟后给我发 hi”
↓
LangGraph detect_intent(create_task)
↓
Dispatcher
↓
schedule_reminder capability
↓
task_service.create_task()
↓
tasks 表
↓
Chat 立即返回 task_created card
↓
task_worker.tick(fake clock / real clock)
↓
notification.created event
↓
Notification Inbox / Chat 可见
```

### 11.2 禁止

```text
await sleep / time.sleep。
前端 setTimeout 作为真实调度。
硬编码 hi。
只返回文本不写 tasks。
真实单测等待 60 秒。
```

### 11.3 测试

```text
使用 injectable clock / freezegun / 自研 FakeClock。
推进 fake clock 61 秒。
调用 worker.tick()。
验证 notification.created event。
清理 task/event/notification。
```

---

## 12. R9：MCP 修复

### 12.1 目标

MCP 是 AIive 自举能力主线，不能硬编码候选或跳过 sandbox。

### 12.2 必须检查/修复

```text
MCPServerCandidate: name/source/version/package_ref/transport/declared_tools/descriptor_hash/risk_notes。
search_mcp_candidates(goal) 不得返回固定列表，必须读 registry/config source。
sandbox install 使用临时目录/subprocess wrapper，shell=False。
MCP tool output trust_level=tool_result/untrusted，不得成为 instruction。
descriptor_hash 变化 -> needs_review。
```

### 12.3 验收

```text
用户：你需要一个能读 GitHub issue 的能力。
系统：通过 Chat 返回 mcp_candidates action card，包含来源、版本、风险。
低风险候选可以 sandbox smoke。
高风险候选不能自动 active。
```

---

## 13. R10：A/B Self-Dev 修复

### 13.1 目标

Self-dev 不得直接修改 active slot。

### 13.2 必须检查

```text
active_slot 是否只读。
inactive_slot 是否通过 manifest 复制代码/config/lockfile，不复制 data/postgres/qdrant/object_store/logs。
patch 是否只应用到 inactive slot。
targeted tests 是否只在 inactive slot 运行。
promote 是否经过 health check。
rollback 是否能恢复 active slot。
```

### 13.3 禁止

```text
直接改当前运行代码目录。
复制 data/postgres/qdrant/object_store/logs 到 slot。
全量回归测试。
```

---

## 14. R11：UI / Action Cards / Inspectors 修复

### 14.1 目标

用户必须能验证功能不是假的。

### 14.2 必须覆盖

```text
Chat Page 显示 action_cards。
Task Dashboard 显示真实 tasks API。
Notification Inbox 显示 notification events。
Memory Dashboard 显示 active/superseded/forgotten。
Retrieval Inspector 显示 retrieval candidates。
Context Inspector 显示 injected/excluded items。
Tool/Capability Dashboard 显示 capability states。
MCP Dashboard 显示 candidate/sandbox/hash/risk。
SelfDev Dashboard 显示 slots/patch/test/promote。
```

### 14.3 禁止

```text
前端假数据。
前端自造状态作为 truth source。
前端 setTimeout 冒充 worker。
```

---

## 15. R12：测试与清理修复

### 15.1 测试原则

```text
禁止全量回归测试。
只运行当前修复相关 targeted tests。
单元测试默认 mock LLM / fake clock / temporary DB transaction。
真实 LLM 只用于 smoke，必须可跳过。
测试产生的 DB/Qdrant/object store/files 必须由 fixture 自动清理。
不可逆操作必须对副本/test scope 执行。
```

### 15.2 必须新增 cleanup helpers

```text
tests/helpers/cleanup.py
cleanup_test_run(test_run_id)
cleanup_db_records(test_run_id)
cleanup_object_store_prefix(test_run_id)
cleanup_qdrant_collection(test_run_id)
cleanup_temp_slots(test_run_id)
```

---

## 16. 最终验收：V0-V20 dependency hardening 完成标准

编码 AI 必须生成：

```text
docs/audit/v0_v20_dependency_architecture_report.md
docs/audit/v0_v20_dependency_hardening_completion_report.md
```

完成报告必须列出：

```text
1. 每个终局依赖是否已接入、计划后续接入或明确不需要。
2. 每个 BLOCKER 的修复 commit/文件列表。
3. 每个 Chat 能力的真实调用链。
4. 每个能力是否有 action card/trace/context/event。
5. 测试命令和结果。
6. 清理验证结果。
7. 仍保留的 MAJOR/MINOR 和原因。
```

最低可手动验证场景：

```text
1. 多轮 chat 经过 LangGraph，trace 可查。
2. “以后叫我 A -> 以后叫我 B -> 我叫什么” 只回答 B。
3. “一分钟后给我发 hi” 写 tasks，worker 到期产生 notification，不是 sleep。
4. “搜索一个 GitHub issue MCP” 返回真实候选/配置源，不是硬编码。
5. “删除这个测试文件” 经过 safe_delete decision。
6. 摄入一个 md 文件后可检索，retrieval inspector 可见候选。
7. Qdrant collection 可从 chunks 重建。
8. context snapshot 显示 injected/excluded。
9. selfdev 只改 inactive slot。
```
