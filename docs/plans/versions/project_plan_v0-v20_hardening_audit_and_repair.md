# AIive project_plan_v0-v20_hardening_audit_and_repair.md：V0-V20 架构走偏审查与硬化修复总计划

> 上位文档：`final_project_plan_v7.md`  
> 编码约束：`编码规范.md`  
> 当前适用状态：编码 AI 已做到旧 V20，发现 reminder 等能力存在“底层功能/硬编码模拟/未接入工具化能力”的问题。  
> 本文件定位：**只生成这一份 V0-V20 修复文档**，让编码 AI 在当前代码基础上逐项审查并修复，不回滚、不重做、不继续旧 V21。  
> 执行完成后，才允许进入新的 `project_plan_v21.md`。

---

## 0. 为什么必须做这次修复

目前已经暴露出两类系统性问题：

```text
1. 底层 service / API / table / worker 已实现，但没有接入 Chat / Agent Loop / Tool / Capability。
2. 编码 AI 为了通过演示，把功能写成硬编码、sleep、setTimeout、固定句式、假数据或前端模拟。
```

典型例子：

```text
用户：一分钟后给我发“hi”
系统：好的，千早爱音会在1分钟后提醒您。
等待 60 秒后输出：hi
```

这个表现看起来像 reminder，但如果没有以下事实，就不是 AIive 的真实能力：

```text
tasks 表没有持久化记录；
没有 schedule_reminder capability；
没有 task_service.create_task()；
没有 worker 从数据库扫描 due tasks；
没有 notification event；
刷新/重启后任务丢失；
测试靠真实等待或前端 setTimeout。
```

本修复文件的目标不是“让演示看起来能用”，而是让 V0-V20 已实现能力回到最终架构：

```text
Chat / LangGraph Runtime / Agent Loop
↓
Intent Router
↓
Agent Action Dispatcher
↓
Tool / Capability / Service
↓
PostgreSQL Event + Outbox + State
↓
Worker / UI / Trace / Action Cards
```

---

## 1. 本阶段最高优先级规则

### 1.1 禁止硬编码能力

以下实现一律视为走偏，必须修复：

```text
1. 在 chat handler 里直接 if 用户文本 contains “提醒我” 然后返回固定答案。
2. 在 chat handler 里 await asyncio.sleep / time.sleep / setTimeout 等待触发时间。
3. 在前端用 setTimeout 当作真实 reminder 调度。
4. 用固定 demo 数据冒充 memory / task / MCP / tool / knowledge / selfdev 结果。
5. 生产代码中出现 FakeLLM / MockLLM / DummyTool / DemoReminder 等被真实 API 路径调用。
6. 绕过 service 直接写数据库。
7. 绕过 ToolRegistry / PermissionManager 直接执行工具。
8. 绕过 safe_delete 直接 os.remove / shutil.rmtree / rm -rf。
9. 直接修改 active slot 代码，而不是 inactive slot / A-B slot。
10. 将 untrusted 网页/PDF/MCP/tool output 当成用户指令。
```

确定性规则不是完全禁止，但必须放在受控位置：

```text
允许：intent_detector 中用规则识别 “提醒我 / 忘掉 / 删除 / 给自己加能力”。
禁止：chat endpoint 中用规则直接执行结果。
```

### 1.2 禁止“只有 API、Chat 不可用”

每个 V0-V20 已实现能力都必须回答以下问题：

```text
用户能否通过 Chat 自然语言触发？
是否经过 LangGraph / Agent Loop？
是否经过 Intent Router / Agent Action Dispatcher？
是否走 Tool / Capability / Service？
是否有 event / trace / context snapshot？
是否有 action card？
是否可在 UI 看见？
测试是否覆盖 Chat -> Ability -> State -> Event -> UI payload？
测试副作用是否自动清理？
```

如果答案有任何一个是否定，本阶段必须补齐。

### 1.3 禁止偏离技术选型

AIive 当前必须使用以下技术基线。编码 AI 必须先审查当前项目是否符合：

```text
后端：Python + FastAPI + Pydantic v2 + SQLAlchemy 2.x + Alembic + PostgreSQL。
前端：React + Vite + TypeScript。
Agent 编排：LangGraph StateGraph，生产路径不得只用手写 if/else agent_loop。
LLM：OpenAI-compatible LLM Client，真实 LLM smoke 单独运行，单元测试默认 mock。
异步派生任务：PostgreSQL events + outbox_jobs + worker，不使用纯内存队列作为唯一真相源。
定时任务：tasks 表为 truth source，scheduler worker 扫描 due tasks；可以用 APScheduler 辅助唤醒，但不能让 APScheduler 内存任务成为唯一真相源。
工具：ToolRegistry + PermissionManager + ToolExecutor，不允许工具散落在 chat handler。
删除：safe_delete + Scope Registry。
自进化：manifest-based A/B slot，active slot 只读，inactive slot 修改。
测试：pytest，禁止全量回归，使用 fixture/transaction/tmp_path/fake clock 自动清理。
```

如果当前代码没有使用 LangGraph，必须在本修复阶段补一个最小 LangGraph Runtime Adapter。不能继续扩大功能后再补。

---

## 2. 执行顺序总览

编码 AI 必须严格按以下顺序执行：

```text
R0：生成架构走偏审查报告
R1：确认 / 修复 LangGraph Runtime 主路径
R2：统一 ChatResponse Action Cards
R3：统一 Intent Router + Agent Action Dispatcher
R4：修复 Tool / Capability 化边界
R5：修复 Scheduler Capability / Reminder / Routine / Notification
R6：修复 Memory Revision / Context 注入有效版本
R7：修复 safe_delete / Forget / Maintenance 的 Chat 接入
R8：修复 Knowledge / Retrieval 的 Chat 接入
R9：修复 MCP Discovery / Sandbox 的 Chat 接入与安全边界
R10：修复 SelfDev / A-B slot 的 Chat 接入与 active slot 保护
R11：修复 Rhythm / Attention 不能硬编码的问题
R12：补齐 UI action cards / inspectors / notification inbox
R13：补齐测试、清理、可观测验证
R14：提交最终修复报告
```

不得跳过 R0。不得先继续新功能。

---

## 3. R0：架构走偏审查报告

### 3.1 产物

新增文件：

```text
docs/audit/v0_v20_architecture_drift_report.md
```

该报告不是可选项，必须由编码 AI 生成，并且必须包含：

```text
1. 当前是否使用 LangGraph。
2. /api/chat 的真实调用链。
3. reminder 是否持久化。
4. 是否存在 sleep / setTimeout / fake timer / fake data 在生产路径。
5. 是否存在绕过 ToolRegistry 的工具调用。
6. 是否存在绕过 safe_delete 的删除。
7. 是否存在 direct DB write bypass service。
8. 是否存在 active slot 直接修改。
9. 是否存在 untrusted content 触发 tool call。
10. V0-V20 每个阶段能力是否通过 Chat 可用。
```

### 3.2 必须运行的审查命令

以下命令可以按项目实际目录调整，但报告中必须贴出结果摘要：

```bash
grep -R "asyncio.sleep\|time.sleep\|setTimeout\|setInterval" -n backend frontend || true
grep -R "Fake\|Mock\|Dummy\|demo\|hardcoded\|TODO" -n backend frontend || true
grep -R "os.remove\|shutil.rmtree\|rm -rf\|unlink\|rmdir" -n backend || true
grep -R "from langgraph\|StateGraph\|checkpointer\|PostgresSaver\|MemorySaver" -n backend || true
grep -R "INSERT INTO\|session.add\|execute(" -n backend/aiive || true
grep -R "提醒我\|一分钟后\|明早\|以后叫我\|忘掉\|搜索 MCP\|给自己加" -n backend frontend || true
grep -R "ToolRegistry\|PermissionManager\|safe_delete\|task_service\|outbox" -n backend || true
```

这些 grep 不是测试替代品，只用于找走偏点。

### 3.3 走偏等级

每个问题必须标注：

```text
BLOCKER：导致功能是假的、不可持久、绕过安全边界、绕过工具体系。
MAJOR：功能可用但不可观测、Chat 不可触发、缺 trace/action card。
MINOR：命名、目录、类型不规范，但不破坏主链路。
```

BLOCKER 必须在本文件中修复完毕，不允许带入 V21。

---

## 4. R1：LangGraph Runtime 主路径审查与修复

### 4.1 为什么必须检查 LangGraph

AIive 一开始讨论的终局就是长期运行 agent，不是一个普通 FastAPI chat endpoint。LangGraph 的价值是：

```text
线程级状态；
持久化 checkpoint；
长任务可恢复；
human-in-the-loop / approval；
工具调用节点化；
调试和 time travel；
未来 self-evolution / watcher 可以接入图节点。
```

如果当前代码没有使用 LangGraph，后续会越来越像一堆 if/else API，偏离 AIive 目标。

### 4.2 必须满足的生产调用链

`POST /api/chat` 必须进入 LangGraph graph，不允许直接散装执行所有逻辑。

推荐结构：

```text
backend/aiive/runtime/graph.py
backend/aiive/runtime/state.py
backend/aiive/runtime/nodes/context_node.py
backend/aiive/runtime/nodes/intent_node.py
backend/aiive/runtime/nodes/action_node.py
backend/aiive/runtime/nodes/llm_node.py
backend/aiive/runtime/nodes/persist_node.py
```

最小 StateGraph：

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

`dispatch_action` 根据 intent 类型决定是否调用 tool/service。`maybe_call_llm` 不得绕过已执行的 action。

### 4.3 Checkpointer 要求

生产环境必须持久化 thread state：

```text
优先：langgraph checkpoint postgres / PostgresSaver。
临时：自研 DBThreadCheckpointer，但必须写入 PostgreSQL threads/checkpoints 表。
禁止：生产路径只用 MemorySaver 或纯内存 dict。
```

测试环境可以使用 InMemory checkpointer。

### 4.4 如果当前没有 LangGraph 怎么修

不得重写全部业务。加 adapter：

```text
1. 保留现有 services。
2. 新增 graph.py，把现有 context_builder、intent_router、dispatcher、llm_client、event_logger 包成 nodes。
3. /api/chat 改为调用 graph.invoke / graph.astream。
4. 每次调用必须传 thread_id。
5. graph state 中保存 user_message、context、intent_result、action_result、llm_result、events、action_cards。
```

### 4.5 验收

```text
/api/chat 真实调用 LangGraph。
每次 Chat 都有 thread_id 和 checkpoint。
重启后同 thread 能继续上下文。
测试里可注入 fake checkpointer。
R0 报告中证明生产路径不再只靠手写 agent_loop。
```

测试命令建议：

```bash
pytest tests/runtime/test_langgraph_chat_flow.py -q
```

---

## 5. R2：统一 ChatResponse Action Cards

### 5.1 问题

V0-V20 很多能力可能有 API 或 DB 结果，但 Chat 返回只有一段文本，导致用户无法验证到底发生了什么。

### 5.2 必须实现

保留旧字段，新增兼容字段：

```python
class ChatResponse(BaseModel):
    reply: str
    thread_id: str
    trace_id: str
    action_cards: list[ActionCard] = []
    pending_operations: list[PendingOperation] = []
    intent_debug: IntentDebug | None = None
```

ActionCard：

```python
class ActionCard(BaseModel):
    card_type: Literal[
        "memory_created",
        "memory_revised",
        "memory_superseded",
        "task_created",
        "task_updated",
        "task_cancelled",
        "notification",
        "tool_result",
        "delete_decision",
        "knowledge_ingest",
        "knowledge_result",
        "forget_result",
        "maintenance_report",
        "mcp_candidates",
        "mcp_sandbox_result",
        "selfdev_plan",
        "selfdev_run",
        "approval_required",
        "rhythm_summary",
        "attention_state"
    ]
    title: str
    summary: str
    trace_id: str
    event_ids: list[str] = []
    resource_refs: dict[str, str] = {}
    status: Literal["pending", "completed", "failed", "needs_review"]
    payload_preview: dict[str, Any] = {}
```

### 5.3 禁止

```text
禁止只在 reply 文本里说“已创建”。
禁止前端自己猜测 action 类型。
禁止 action card 没有 trace_id。
```

### 5.4 验收

每个 Chat 触发的非纯聊天能力至少返回一个 action card。

---

## 6. R3：Intent Router + Agent Action Dispatcher

### 6.1 目标

所有 Chat 中触发的非纯聊天能力必须经过统一调度，不允许散落在 endpoint。

推荐路径：

```text
POST /api/chat
  -> LangGraph detect_intent node
  -> LangGraph dispatch_action node
  -> AgentActionDispatcher.dispatch()
  -> service/tool/capability
```

### 6.2 IntentResult schema

```python
class IntentResult(BaseModel):
    intent_type: Literal[
        "plain_chat",
        "memory_write",
        "memory_revision",
        "create_task",
        "list_tasks",
        "cancel_task",
        "show_notifications",
        "tool_call",
        "safe_delete",
        "knowledge_ingest",
        "knowledge_search",
        "forget_memory",
        "maintenance_scan",
        "mcp_search",
        "mcp_sandbox_install",
        "selfdev_plan",
        "selfdev_apply_inactive",
        "selfdev_promote",
        "selfdev_rollback",
        "rhythm_query",
        "attention_query"
    ]
    confidence: float
    instruction_source: Literal[
        "trusted_user_command",
        "trusted_approval",
        "untrusted_content",
        "tool_result"
    ]
    extracted_args: dict[str, Any]
    requires_confirmation: bool = False
    reason: str
```

### 6.3 检测策略

使用“规则优先 + strict JSON LLM fallback”：

```text
高确定性：提醒我、以后叫我、忘掉、删除、搜索 MCP、给自己加能力。
低确定性：调用 LLM 输出 strict JSON。
LLM 输出只是 proposal，不能执行。
执行必须由 Dispatcher + Service + Permission 决定。
```

### 6.4 验收

```text
所有 Chat 能力触发都能在 trace 中看到 intent_result。
untrusted_content 的 intent_result 不能触发工具执行。
```

---

## 7. R4：Tool / Capability 化修复

### 7.1 核心原则

任何“能做事”的能力都必须是 Capability 或内部 Service。Chat 不得直接做事。

### 7.2 Capability schema

```python
class CapabilityDefinition(BaseModel):
    capability_id: str
    version: str
    description: str
    risk_level: Literal["low", "medium", "high", "critical"]
    requires_confirmation: bool
    writes_external_world: bool
    can_access_secret: bool
    can_delete: bool
    allowed_instruction_sources: list[str]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    definition_source: Literal[
        "local_builtin",
        "agent_generated",
        "mcp_registry",
        "remote_mcp",
        "user_installed"
    ]
    definition_trust_level: Literal["trusted", "semi_trusted", "untrusted"]
    descriptor_hash: str | None = None
```

### 7.3 最低必须注册的内置能力

```text
schedule_reminder
list_tasks
cancel_task
safe_delete
search_knowledge
ingest_document
search_mcp
install_mcp_sandbox
create_selfdev_plan
apply_patch_to_inactive_slot
run_targeted_tests
promote_slot
rollback_slot
forget_memory
run_memory_maintenance
query_rhythm
query_attention_state
```

### 7.4 Tool Guardrails

每次 tool 执行前后必须检查：

```text
tool input guard：source/risk/confirmation/scope。
tool output guard：标记 trust_level=tool_result，不得成为 instruction。
```

### 7.5 验收

```text
Chat 触发 reminder / delete / MCP / selfdev / knowledge 时，trace 中能看到 capability_id。
PermissionManager 决策有 event。
高风险 capability 不确认不执行。
```

---

## 8. R5：Scheduler Capability / Reminder / Routine / Notification 修复

### 8.1 问题

旧 V19/V20 可能把 reminder 做成硬编码、sleep 或前端 setTimeout。这必须修复。

### 8.2 正确架构

```text
Chat 输入
↓
LangGraph detect_intent
↓
AgentActionDispatcher
↓
schedule_reminder capability
↓
task_service.create_task()
↓
tasks 表
↓
Chat 立即返回 task_created card
↓
scheduler worker 扫描 due tasks
↓
notification_service.create_notification_event()
↓
Chat / Notification Inbox 显示
```

### 8.3 数据库要求

`tasks` 至少包含：

```text
task_id
task_type = reminder | routine | condition_watch
title
message
status = active | paused | triggered | completed | expired | failed
trigger_at
next_check_at
recurrence_rule
condition_expr
delivery_channel = local_chat | notification_inbox | external_message
requires_confirmation
created_from_event_id
created_by = user | agent
last_checked_at
completed_at
```

`events` 中必须出现：

```text
task.created
task.triggered
notification.created
```

### 8.4 Worker 要求

```text
worker 从数据库读 due tasks。
worker 不依赖内存中保存的任务。
服务重启后 due task 仍可触发。
routine 触发后计算下一次 next_check_at，不直接 completed。
reminder 触发后 completed。
condition_watch 未满足则保持 active 并更新 next_check_at。
```

可以使用 APScheduler 或 asyncio loop 做唤醒，但它只能调用 `task_worker.tick()`；不能作为 truth source。

### 8.5 Chat 语义要求

必须支持：

```text
一分钟后提醒我 hi
一分钟后给我发 hi
明早提醒我带伞
每天早上提醒我喝水
取消刚才那个提醒
列出我的提醒
```

本地提醒不需要确认；外部发消息需要确认。

### 8.6 禁止

```text
禁止在 /api/chat 内 await sleep。
禁止前端 setTimeout 作为真实调度。
禁止写死 “hi”。
禁止只返回文本不写 tasks。
禁止测试真实等待 60 秒。
```

### 8.7 测试

使用 fake clock / injectable clock：

```bash
pytest tests/tasks/test_chat_schedule_reminder.py -q
pytest tests/tasks/test_task_worker_due_notifications.py -q
```

测试必须验证：

```text
Chat 创建 task。
ChatResponse 有 task_created card。
推进 fake clock 61 秒。
worker tick 产生 notification event。
task 状态 completed。
测试数据自动清理。
重启 worker 后仍能触发。
```

---

## 9. R6：Memory Revision / Context 有效版本注入修复

### 9.1 问题

旧 memory append-only 会导致“叫我 A / 叫我 B”同时注入。

### 9.2 必须实现

`memory_records` 增加或确认：

```text
memory_key
revision
revision_of
supersedes
superseded_by
valid_from
valid_to
update_reason
last_source_event_id
```

MemoryStore 必须有：

```python
create()
update_content()
supersede()
find_by_memory_key()
resolve_for_context()
```

### 9.3 规则

```text
同 memory_key + single_active，只允许一个 active。
用户显式最新说法 > 用户旧说法。
untrusted content 不得覆盖 user_profile / preference / policy。
旧记录变 superseded，不 hard delete。
Context Builder 只注入 resolve_for_context() 返回的有效版本。
Context Snapshot 记录 excluded_memory_ids 和 reason=superseded。
```

### 9.4 验收

```text
用户：以后叫我 A。
用户：以后叫我 B。
用户：我叫什么？
回复 B。
数据库 A=superseded，B=active。
context snapshot 注入 B，排除 A。
```

---

## 10. R7：safe_delete / Forget / Maintenance Chat 接入修复

### 10.1 必须审查

```text
是否存在绕过 safe_delete 的删除。
Chat 中“删掉/清理/忘掉”是否走 service。
forget 是否产生 tombstone。
maintenance 是否产生 report card。
```

### 10.2 必须实现

```text
Chat 删除文件 -> safe_delete capability。
Chat 忘掉记忆 -> forget_memory service。
Chat 整理旧记忆 -> maintenance_plan + dry run/action card。
```

### 10.3 禁止

```text
禁止 LLM 直接生成 rm -rf 并执行。
禁止 chat handler 直接 unlink。
禁止 hard delete 用户重要记忆。
```

### 10.4 验收

```text
删除测试文件：safe_delete allow，文件进入 trash/quarantine 或删除记录。
删除 home/root/project root：deny。
忘掉称呼：memory forgotten/superseded 后 context 不再注入。
```

---

## 11. R8：Knowledge / Retrieval Chat 接入修复

### 11.1 必须审查

```text
知识摄入是否只是 API。
Chat 是否能说“摄入这个文件/检索这个文档”。
Context Snapshot 是否记录 injected_chunk_ids。
检索结果是否标记 source/trust。
```

### 11.2 必须实现

```text
Chat-to-Knowledge Ingest。
Chat-to-Knowledge Search。
source cards。
Context Evidence Pack。
```

### 11.3 禁止

```text
禁止把知识库内容当系统指令。
禁止用固定 demo 文档冒充检索。
禁止检索结果无 source_ref。
```

---

## 12. R9：MCP Discovery / Sandbox Chat 接入与安全修复

### 12.1 必须审查

```text
MCP discovery 是否查真实 registry / 配置源，还是硬编码候选。
MCP 安装是否 sandbox。
tool descriptor 是否 hash/version。
MCP tool output 是否 untrusted。
```

### 12.2 必须实现

```text
Chat：帮我找一个能查 GitHub issue 的 MCP。
↓
search_mcp capability
↓
候选卡片 mcp_candidates
↓
用户选择或低风险 sandbox smoke
↓
mcp_sandbox_result card
```

### 12.3 安全边界

```text
远程 MCP 默认 semi_trusted/untrusted。
工具描述只是 metadata，不是 instruction。
descriptor_hash 必须记录。
高风险 MCP 不自动 active。
```

---

## 13. R10：SelfDev / A-B Slot Chat 接入修复

### 13.1 必须审查

```text
用户说“给自己加能力”是否触发 selfdev_plan。
补丁是否只作用 inactive slot。
测试是否 targeted。
promote 是否经过 supervisor。
是否存在 active slot 直接写文件。
```

### 13.2 必须实现

Chat 支持：

```text
给自己加一个 X 能力。
只生成修改计划。
在 inactive slot 测试。
如果通过，切换到新版本。
回滚。
```

必须返回：

```text
selfdev_plan card
selfdev_run card
promote/rollback card
```

### 13.3 禁止

```text
禁止直接改 active slot。
禁止不测试 promote。
禁止运行全量测试。
```

---

## 14. R11：Rhythm / Attention 不能硬编码

### 14.1 问题

Personal Rhythm 很容易被写成固定文案：

```text
“你最近很忙，要注意休息。”
```

这不是 AIive 的个人管家能力。

### 14.2 必须实现

Rhythm 必须来自真实数据：

```text
tasks
reminders
recent chat events
attention_state
memory_records 中的作息/偏好/项目 deadline
notification history
```

必须能回答：

```text
我今天有哪些提醒？
我最近在忙什么？
我是不是很久没继续某个任务？
刚才说论文，现在切回代码，之前代码进度是什么？
```

### 14.3 验收

```text
创建两个 reminder 后，rhythm summary 能列出它们。
切换话题后，attention_state 保留短时间 suspended topic。
没有数据时，不得编造生活节奏。
```

---

## 15. R12：UI 修复

### 15.1 必须实现

Chat Page 必须展示 action cards：

```text
task_created
notification
memory_revised
knowledge_result
mcp_candidates
mcp_sandbox_result
selfdev_plan
approval_required
```

新增或修复：

```text
Notification Inbox
Tasks mini panel
Trace link
Tool run card
MCP candidate card
SelfDev status card
```

### 15.2 禁止

```text
禁止前端自己伪造任务状态。
禁止前端 setTimeout 代替 worker。
禁止 UI 展示和后端状态不一致。
```

---

## 16. R13：测试与清理要求

### 16.1 禁止全量回归

本修复阶段不得运行：

```bash
pytest
npm test
pnpm test
```

必须只运行相关测试：

```bash
pytest tests/runtime/test_langgraph_chat_flow.py -q
pytest tests/tasks/test_chat_schedule_reminder.py -q
pytest tests/memory/test_memory_revision.py -q
pytest tests/tools/test_chat_tool_dispatch.py -q
pytest tests/delete/test_chat_safe_delete.py -q
pytest tests/mcp/test_chat_mcp_bridge.py -q
pytest tests/selfdev/test_chat_selfdev_bridge.py -q
```

### 16.2 清理必须由 fixture 完成

```text
测试写入数据库：fixture 事务回滚或按 test_run_id 删除。
测试写文件：tmp_path，测试结束自动删除。
测试删除功能：只删除测试文件/测试目录。
测试 Qdrant：test collection，结束删除。
测试 MCP：sandbox temp dir，结束清理。
测试 reminder：fake clock，不真实等待。
```

### 16.3 外部 LLM

```text
单元测试默认 mock LLM。
真实 LLM 只允许 smoke 脚本，例如 scripts/smoke/smoke_real_llm_chat.py。
真实 LLM smoke 不作为普通 pytest 的一部分。
```

---

## 17. V0-V20 逐阶段审查清单

编码 AI 必须逐项检查并在 `docs/audit/v0_v20_architecture_drift_report.md` 打勾。

### V0：项目地基

```text
[ ] 是否使用约定技术栈 FastAPI / SQLAlchemy / Alembic / PostgreSQL / React Vite / LangGraph。
[ ] 是否存在临时 demo server 替代正式后端。
[ ] 是否有 pyproject / package.json / env example。
[ ] 健康检查是否查真实依赖，而不是返回固定 OK。
```

### V1：真实 LLM

```text
[ ] 生产路径是否调用 OpenAI-compatible LLM Client。
[ ] FakeLLM 是否只在 tests 中。
[ ] 真实 LLM smoke 是否单独脚本。
```

### V2：Chat API / Chat Page

```text
[ ] /api/chat 是否进入 LangGraph。
[ ] Chat Page 是否展示真实后端返回。
[ ] 是否存在固定示例回复。
```

### V3：PostgreSQL / Events / Thread

```text
[ ] Chat 是否写 event。
[ ] trace_id 是否贯穿。
[ ] thread state 是否持久化。
```

### V4：Context

```text
[ ] Context 是否由 ContextAssembler 构建。
[ ] Snapshot 是否保存。
[ ] trust_level 是否存在。
```

### V5：Memory

```text
[ ] Memory 是否不再 append-only。
[ ] resolve_for_context 是否排除 superseded。
[ ] Memory action card 是否可见。
```

### V6：Personal Signals

```text
[ ] personal signals 是否来自 memory/events/tasks。
[ ] 是否存在固定 persona 文案冒充个性化。
```

### V7：Tool Registry

```text
[ ] 每个可执行能力是否注册为 Capability。
[ ] PermissionManager 是否机械执行。
```

### V8：safe_delete

```text
[ ] 删除是否全部走 safe_delete。
[ ] Scope Registry 是否有效。
```

### V9：MCP Discovery

```text
[ ] 是否支持真实 registry/config source。
[ ] 候选是否带 source/version/hash。
[ ] 是否没有把候选写死。
```

### V10：MCP Sandbox

```text
[ ] sandbox install 是否隔离。
[ ] smoke 是否可清理。
[ ] active 前是否 risk gate。
```

### V11：Supervisor / A-B Slot

```text
[ ] active slot 是否只读。
[ ] supervisor 是否管理 active pointer。
[ ] manifest 是否排除 data/postgres/logs。
```

### V12：SelfDev Plan

```text
[ ] selfdev plan 是否只是 proposal。
[ ] 是否有 action card。
```

### V13：Inactive Patch / Promote

```text
[ ] patch 是否只进 inactive slot。
[ ] promote 是否必须 health check。
[ ] rollback 是否可用。
```

### V14：Outbox

```text
[ ] 是否使用 DB outbox。
[ ] 是否不存在纯内存任务作为唯一 truth。
```

### V15：Knowledge Ingest

```text
[ ] 是否真实 parse/chunk。
[ ] 是否保存 source_ref。
[ ] Chat 是否能触发 ingest。
```

### V16：Qdrant / Hybrid Retrieval

```text
[ ] 是否真实写入 Qdrant 或测试 collection。
[ ] Retrieval result 是否可追踪。
```

### V17：Inspectors

```text
[ ] UI 是否读真实 API。
[ ] Trace/Context/Retrieval 是否能跳转。
```

### V18：Forget / Maintenance

```text
[ ] Forget 是否从 context 排除。
[ ] Maintenance 是否有 report。
[ ] Chat 是否能触发。
```

### V19：Proactive Task

```text
[ ] Reminder 是否是 schedule_reminder capability。
[ ] Worker 是否读 DB due tasks。
[ ] 是否无 sleep/setTimeout 伪提醒。
```

### V20：Rhythm / Attention

```text
[ ] Rhythm 是否来自真实 tasks/events/memory。
[ ] Attention 是否软切换。
[ ] Chat 是否能查询。
```

---

## 18. 最终验收脚本

完成本文件后，必须提供一个 smoke 脚本：

```text
scripts/smoke/smoke_v0_v20_hardening.py
```

该脚本按顺序验证：

```text
1. /api/chat 进入 LangGraph 并产生 trace。
2. “以后叫我 A” 创建 memory。
3. “以后叫我 B” supersede A。
4. “我叫什么” 只注入 B。
5. “一分钟后提醒我 hi” 创建 task，不等待。
6. fake clock 推进后 worker 产生 notification。
7. “列出我的提醒” 可见。
8. “搜索一个文件管理 MCP” 返回 candidates，不自动高风险安装。
9. “删除测试文件” 走 safe_delete。
10. “给自己加一个低风险能力” 只生成 selfdev plan，不直接改 active。
```

Smoke 产生的数据必须使用 `test_run_id`，最后由 cleanup helper 清理。

---

## 19. 完成标准

本修复阶段完成的标准：

```text
docs/audit/v0_v20_architecture_drift_report.md 已生成。
所有 BLOCKER 已修复。
/api/chat 进入 LangGraph。
Chat 可以触发 reminder，并通过持久化 task + worker 完成。
Memory 支持 supersede。
所有 V0-V20 核心能力都有 Chat/action card/trace 路径。
生产路径无 Fake/Demo/Hardcoded 能力。
删除全部走 safe_delete。
测试全部 targeted，副作用自动清理。
```

完成后，才允许进入新的 `project_plan_v21.md`。
