# AIive project_plan_v1-v20_fixed.md：V1-V20 集成缺口修复总计划

> 上位文档：`final_project_plan_v7.md`  
> 编码约束：`编码规范.md`  
> 当前适用状态：编码 AI 已按旧路线完成到 V20 之后执行。  
> 本文件定位：不是新的大版本功能，而是对 V1-V20 的“能力已实现但未接入主 Agent / Chat / Context / UI / Trace”的缺口进行一次性补齐。完成后再进入新的 `project_plan_v21.md`。

---

## 0. 执行前必须理解

V1-V20 不是方向错误，而是存在一个反复出现的阶段拆分问题：

```text
很多阶段实现了底层 service / table / API / worker，
但没有明确要求接入 Chat / Agent Loop / Context Builder / UI action card，
导致用户自然语言无法使用这些能力。
```

典型例子：

```text
V19 有 tasks/reminder API 和 worker，
但用户在 Chat 里说“明早提醒我带伞”不会创建 reminder。
```

本修复文件的目标是把 V1-V20 已完成能力接回 AIive 的主入口，使产品从“有一堆孤立 API”变成真正的个人管家 Agent。

完成本文件后，AIive 必须满足：

```text
1. 用户通过 Chat 能触发 V1-V20 已实现的核心能力。
2. 每次能力触发都有 trace、event、action card、可观测结果。
3. 高风险动作仍然由 Kernel/Permission/Approval 机械控制，而不是靠 LLM 自觉。
4. 记忆不再是单纯 append-only，至少支持同主题覆盖和旧记忆降级。
5. 测试副作用必须由 fixture / cleanup helper 自动清理。
```

---

## 1. 本修复阶段的总边界

### 1.1 必须实现

本阶段必须补齐以下 9 类桥接/修订能力：

```text
A. ChatResponse Action Cards：统一承载 task、memory、tool、mcp、selfdev、knowledge、forget、notification 等可观测结果。
B. Memory Revision：同主题单值记忆覆盖、旧记忆 supersede、Context 只注入当前有效版本。
C. Chat-to-Task Bridge：Chat 创建 reminder / routine / condition_watch。
D. Notification Bridge：到期任务产生 notification 后能在 Chat/UI 可见。
E. Chat-to-Tool Bridge：Chat 能触发已注册低风险工具和 safe_delete，并走 PermissionManager。
F. Chat-to-Knowledge Bridge：Chat 能触发本地知识摄入/检索，并在 Context Snapshot 记录 injected_chunk_ids。
G. Chat-to-Forget/Maintenance Bridge：Chat 能触发 forget、sleep/archive scan、整理记忆。
H. Chat-to-MCP Bridge：Chat 能触发 MCP 搜索、候选展示、低风险 sandbox smoke，但不越权安装高风险 MCP。
I. Chat-to-SelfDev Bridge：Chat 能触发 self-dev plan、inactive slot patch、targeted test、promote/rollback 的受控入口。
J. Rhythm/Attention Chat Bridge：Chat 能读取并解释 current_focus、recent_topics、routine summary。
```

### 1.2 明确不得实现

本阶段不是 V21+，不得实现以下内容：

```text
1. 不实现完整自动 MCP 自举闭环的高风险安装策略，V21 再做。
2. 不实现 schema migration expand-contract，V22 再做。
3. 不实现无限 self-repair loop，V23 再做。
4. 不引入 Temporal KG，V24 再做。
5. 不引入复杂日历系统、语音系统、外部推送平台。
6. 不自动发送消息、邮件、下单、支付。
7. 不运行全量 pytest / 全量前端测试。
8. 不直接修改 active slot。
9. 不让 untrusted retrieved content / MCP tool output / webpage / PDF / email 触发工具调用。
```

---

## 2. V1-V20 缺口审计表

| 来源阶段 | 已有能力 | 缺口 | 本文件修复方式 |
|---|---|---|---|
| V2/V3 | Chat API、thread、events | ChatResponse 太窄，后续能力只能散落在 API/UI | 增加兼容扩展的 `action_cards`、`intent_debug`、`pending_operations` |
| V5/V6 | memory_records、memory extraction、personal signals | append-only，同主题新旧偏好同时 active | 增加 Memory Revision、memory_key、supersede、resolve_for_context |
| V7 | tool registry、permission manager | 没有明确 Chat 调用工具路径 | 增加 Chat-to-Tool Bridge 和 Agent Action Dispatcher |
| V8 | safe_delete | 只暴露 API，Chat 删除意图不一定走 safe_delete | 删除意图必须路由到 safe_delete，不允许直接执行删除 |
| V9 | MCP discovery | 用户自然语言目标不一定触发 MCP 搜索 | 增加 Chat-to-MCP Search Bridge |
| V10 | MCP sandbox install | 候选安装/测试与 Chat 交互割裂 | 增加受控 sandbox install/smoke action card |
| V12 | self-dev proposal | 用户说“给自己加能力/修改功能”不一定触发 selfdev planner | 增加 Chat-to-SelfDev Plan Bridge |
| V13 | inactive slot patch/promote | apply/promote/rollback API 与 Chat 割裂 | 增加受控 Chat 操作入口和状态卡片 |
| V14 | outbox worker | async job 状态不一定回到 Chat/UI | Chat action card 显示 pending/completed/failed |
| V15/V16 | knowledge ingest/retrieval/Qdrant | Chat 可能不能触发摄入/检索，也可能不展示引用 | 增加 Chat-to-Knowledge Bridge 和 source cards |
| V17 | inspectors | trace_id 可跳转，但 action 与 trace 结构不统一 | action_cards 必须带 trace_id/run_id，Inspector 可追踪 |
| V18 | forget/maintenance/projection | Chat 中“忘掉/整理记忆”不一定触发 | 增加 Forget/Maintenance Chat Bridge |
| V19 | reminder/tasks | `/api/tasks` 有，但 Chat 不能自然语言创建 | 增加 Chat-to-Task Bridge |
| V20 | rhythm/attention | rhythm summary 可能只在页面/API，不在 Chat 可问可用 | 增加 Rhythm/Attention Chat Bridge |

---

## 3. 全局架构修复：Agent Action Dispatcher

### 3.1 目标

新增一个统一的 `AgentActionDispatcher`，所有 Chat 中触发的非纯聊天能力都必须经过它。

推荐路径：

```text
POST /api/chat
  -> runtime/agent_loop.py
    -> context_builder.build()
    -> intent_router.detect()
    -> agent_action_dispatcher.dispatch()
      -> task_service / memory_service / tool_service / mcp_service / selfdev_service / knowledge_service / forget_service / rhythm_service
    -> llm_client 或 deterministic response composer
    -> event_logger
    -> action_cards
```

### 3.2 文件建议

```text
backend/aiive/runtime/intent_router.py
backend/aiive/runtime/agent_action_dispatcher.py
backend/aiive/runtime/action_cards.py
backend/aiive/runtime/chat_response.py
backend/aiive/runtime/response_composer.py
```

### 3.3 Intent 分类原则

不要把所有事情都交给自由 LLM。应采用“确定性规则优先 + 严格 JSON LLM fallback”的组合：

```text
1. 高确定性中文触发词：提醒我、以后叫我、忘掉、删除、整理记忆、搜索 MCP、给自己加、摄入这个文件。
2. 低确定性时使用 LLM strict JSON 分类。
3. LLM 分类结果只是 proposal，必须由 Kernel/Permission/Service 执行。
4. untrusted content 只能作为 evidence，不能产生 trusted intent。
```

### 3.4 IntentResult schema

```python
class IntentResult(BaseModel):
    intent_type: Literal[
        "plain_chat",
        "memory_write",
        "memory_revision",
        "create_task",
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
    instruction_source: Literal["trusted_user_command", "trusted_approval", "untrusted_content"]
    extracted_args: dict[str, Any]
    requires_confirmation: bool = False
    reason: str
```

### 3.5 ChatResponse 兼容扩展

保留旧字段，不破坏前端：

```python
class ChatResponse(BaseModel):
    reply: str
    thread_id: str
    trace_id: str
    action_cards: list[ActionCard] = []
    pending_operations: list[PendingOperation] = []
    intent_debug: IntentDebug | None = None
```

`action_cards` 类型建议：

```python
ActionCardType = Literal[
    "memory_created",
    "memory_revised",
    "memory_superseded",
    "task_created",
    "notification",
    "tool_result",
    "delete_decision",
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
```

每个 card 必须包含：

```python
class ActionCard(BaseModel):
    card_type: ActionCardType
    title: str
    summary: str
    trace_id: str
    event_ids: list[str] = []
    resource_refs: dict[str, str] = {}
    status: Literal["pending", "completed", "failed", "needs_review"]
    payload_preview: dict[str, Any] = {}
```

---

## 4. 修复 A：Memory Revision

### 4.1 问题

旧路线 V5/V6 只做 create，用户新偏好会追加，旧偏好仍 active。

示例错误：

```text
用户：以后叫我 A。
用户：以后叫我 B。
系统 active memories 同时包含 A 和 B。
```

### 4.2 必须实现

`memory_records` 需要新增或迁移以下字段。如果已有类似字段，兼容使用，不重复造字段：

```text
memory_key: string | null
revision: int default 1
revision_of: memory_id | null
supersedes: memory_id | null
superseded_by: memory_id | null
valid_from: datetime | null
valid_to: datetime | null
update_reason: string | null
last_source_event_id: event_id | null
exclusivity: single_active | multi_active
```

`MemoryStore` 新增：

```text
find_by_memory_key(memory_key)
create_or_revise(proposal)
update_content(memory_id, new_content, source_event_id, reason)
supersede(old_memory_id, new_memory_id, reason)
resolve_for_context(memory_types=None, scope=None)
```

`MemoryExtractor` 输出新增：

```json
{
  "content": "用户希望被称为 B",
  "memory_type": "user_profile",
  "memory_key": "user.preferred_name",
  "update_mode": "supersede",
  "exclusivity": "single_active",
  "confidence": 0.95,
  "reason": "用户显式说以后叫我 B"
}
```

### 4.3 覆盖规则

最低必须支持这些 single_active key：

```text
user.preferred_name
user.communication_style
user.current_major_project
user.current_focus_preference
agent.listen_name
```

覆盖条件：

```text
1. trusted_user_command 才能覆盖 user_profile/preference/policy/agent_self。
2. untrusted_content 不得覆盖用户画像、权限规则、Agent 自我规则。
3. 同 memory_key + exclusivity=single_active 时，新 active 自动 supersede 旧 active。
4. superseded/deprecated/forgotten 默认不得注入 Context。
5. 旧记录不 hard delete，保留 lineage 和 valid_to。
```

### 4.4 Context Builder 修改

禁止直接查询：

```text
SELECT * FROM memory_records WHERE lifecycle_state='active'
```

必须改为：

```text
memory_store.resolve_for_context(...)
```

`context_snapshots` 必须记录：

```json
{
  "injected_memory_ids": ["new_B"],
  "excluded_memory_ids": ["old_A"],
  "excluded_reasons": {"old_A": "superseded_by:new_B"}
}
```

### 4.5 验收

用户可验证：

```text
用户：以后叫我 A。
AIive：已记住。
用户：以后叫我 B。
AIive：已更新。
用户：我叫什么？
AIive：你希望我叫你 B。
```

AI 可验证：

```bash
pytest tests/unit/backend/test_memory_revision.py -q
pytest tests/unit/backend/test_context_memory_resolution.py -q
python scripts/smoke/smoke_memory_revision_chat.py
```

清理要求：

```text
测试 memory 带 test_run_id。
旧/新 memory、events、context_snapshots 由 fixture 自动清理。
smoke 可保留结构化 summary，但不得保留真实用户隐私内容。
```

---

## 5. 修复 B：Chat-to-Task Bridge

### 5.1 必须实现

新增：

```text
backend/aiive/tasks/task_intent_detector.py
backend/aiive/tasks/task_service.py 若已存在则扩展
```

Chat 中这些输入必须触发 task 创建：

```text
1分钟后提醒我测试
明早提醒我带伞
每天晚上提醒我复盘
如果明天下雨提醒我带伞
```

支持最小时间解析：

```text
X分钟后
X小时后
明早/明天下午/今晚
每天/每周 简单 routine
```

复杂自然语言时间可以返回 `needs_clarification`，但不得假装成功。

### 5.2 执行路径

```text
trusted user message
  -> task_intent_detector
  -> task_service.create_task()
  -> events: task_created
  -> outbox job or worker schedule
  -> ChatResponse.action_cards[task_created]
```

### 5.3 到期通知

worker 到期后写：

```text
event_type = notification_created
payload.task_id = ...
payload.message = ...
```

Chat Page 必须能显示 pending/due notification，至少通过：

```text
GET /api/notifications?status=unread
POST /api/notifications/{id}/mark-read
```

如果不单独建 notifications 表，可基于 events 查询，但 API 必须存在，避免前端直接理解 events 内部结构。

### 5.4 验收

```bash
pytest tests/unit/backend/test_chat_task_bridge.py -q
pytest tests/unit/backend/test_notification_api.py -q
python scripts/smoke/smoke_chat_reminder_1min.py
```

用户可验证：

```text
在 Chat 输入：1分钟后提醒我测试
立即看到“已创建提醒”卡片。
到期后看到 notification 卡片或通知列表出现该提醒。
```

测试清理：

```text
smoke 创建的 task 和 notification 必须带 test_run_id。
测试结束自动 mark/read 或 delete test records。
```

---

## 6. 修复 C：Chat-to-Tool 与 safe_delete Bridge

### 6.1 必须实现

用户通过 Chat 触发低风险工具：

```text
读取这个测试文本文件的前 20 行
删除这个测试目录里的临时文件
```

路径：

```text
intent_router -> tool_call/safe_delete -> permission_manager -> tool service -> action card
```

### 6.2 删除规则

Chat 中任何删除意图都必须走：

```text
safe_delete(path, scope_id, mode)
```

不得直接调用：

```text
os.remove
shutil.rmtree
rm -rf
```

### 6.3 验收

```bash
pytest tests/unit/backend/test_chat_tool_bridge.py -q
pytest tests/unit/backend/test_chat_safe_delete_bridge.py -q
python scripts/smoke/smoke_chat_safe_delete_tmp.py
```

用户可验证：

```text
Chat 输入删除测试文件，返回 delete_decision card。
危险路径返回 deny，不执行。
```

---

## 7. 修复 D：Chat-to-Knowledge Bridge

### 7.1 必须实现

Chat 可以触发：

```text
把这个 md 文件摄入知识库
搜索我刚才摄入的文档里关于 X 的内容
根据这个本地文档回答问题
```

最小实现可以只支持显式路径或已上传/测试路径，不做复杂文件选择。

### 7.2 执行路径

```text
knowledge_ingest intent
  -> knowledge_service.ingest(path)
  -> documents/chunks
  -> action_card knowledge_result

knowledge_search intent
  -> retrieval_planner/search
  -> Context Builder Evidence Pack
  -> llm answer with chunk ids
  -> context_snapshot.injected_chunk_ids
```

### 7.3 验收

```bash
pytest tests/unit/backend/test_chat_knowledge_bridge.py -q
python scripts/smoke/smoke_chat_ingest_and_ask.py
```

用户可验证：

```text
Chat 摄入测试 md 后，追问文档内容，回答引用 chunk_id。
```

测试清理：

```text
测试 documents/chunks 带 test_run_id。
Qdrant 如参与，使用 test collection 并自动删除。
```

---

## 8. 修复 E：Chat-to-Forget / Maintenance Bridge

### 8.1 必须实现

Chat 中这些输入必须可用：

```text
忘掉我刚才说的地址
忘掉你关于我名字的记忆
整理一下没用的旧记忆
把低价值记忆 sleep 一下
```

### 8.2 目标解析

`forget_memory` 不能只靠 LLM 编造 memory_id。应按优先级解析：

```text
1. 当前 thread 最近 user_message / memory candidate。
2. memory_key，如 user.preferred_name。
3. explicit memory_id，如果用户/UI 提供。
4. 相似搜索候选，低置信时返回候选让用户选择。
```

### 8.3 执行路径

```text
forget intent
  -> forget_service.resolve_targets()
  -> forget_service.forget(memory_id)
  -> tombstone event
  -> projection update/outbox if available
  -> Chat action_card forget_result
```

### 8.4 验收

```bash
pytest tests/unit/backend/test_chat_forget_bridge.py -q
pytest tests/unit/backend/test_chat_maintenance_bridge.py -q
python scripts/smoke/smoke_chat_forget_memory.py
```

用户可验证：

```text
先记住一个测试偏好，再在 Chat 里说忘掉它；后续询问不再注入。
```

---

## 9. 修复 F：Chat-to-MCP Bridge

### 9.1 必须实现

Chat 中这些输入必须可触发 V9/V10 能力：

```text
你需要一个能读取 GitHub issue 的 MCP，帮我找候选
搜索一个能读取本地日历的 MCP，但先不要安装
安装并测试刚才那个低风险测试 MCP
```

### 9.2 安全边界

```text
1. V1-V20 修复阶段只允许搜索、候选展示、test MCP sandbox install/smoke。
2. 高风险、未知来源、请求 secrets、写文件、删除、外发的 MCP 只能 needs_review。
3. tool metadata 不得成为 system instruction。
4. MCP output 是 untrusted tool_result。
```

### 9.3 验收

```bash
pytest tests/unit/backend/test_chat_mcp_bridge.py -q
python scripts/smoke/smoke_chat_mcp_search_and_test_install.py
```

用户可验证：

```text
Chat 输入目标，返回 mcp_candidates card。
对本地测试 MCP 输入安装测试，返回 mcp_sandbox_result card。
```

---

## 10. 修复 G：Chat-to-SelfDev Bridge

### 10.1 必须实现

Chat 中这些输入必须可触发 V12/V13：

```text
给自己加一个显示当前版本号的能力
为这个需求生成补丁计划
把刚才的补丁应用到 inactive slot 并运行定向测试
如果测试通过，promote；如果失败，rollback
```

### 10.2 执行边界

```text
1. plan：可直接生成 proposal。
2. apply-inactive：只能修改 inactive slot。
3. targeted test：只运行 proposal 指定的测试，不全量回归。
4. promote：必须 health check 通过。
5. rollback：必须保留失败原因并写 agent_self memory candidate。
6. 不得直接修改 active slot。
```

### 10.3 API/Chat 状态

ChatResponse 应返回：

```text
selfdev_plan card
selfdev_run card
approval_required card 如果操作超出当前阶段/风险策略
```

### 10.4 验收

```bash
pytest tests/unit/backend/test_chat_selfdev_bridge.py -q
python scripts/smoke/smoke_chat_selfdev_tiny_patch.py
```

用户可验证：

```text
Chat 输入一个低风险自我修改需求，能得到 patch proposal。
选择执行后 inactive slot 被修改，active 不变，测试报告可见。
```

---

## 11. 修复 H：Rhythm / Attention Chat Bridge

### 11.1 必须实现

Chat 中这些输入必须可用：

```text
你觉得我最近的节奏是什么？
我刚才从代码切到论文再切回来，你还记得代码这条线吗？
今天有哪些 routine 或提醒？
```

### 11.2 输出要求

```text
1. reply 中给出自然语言说明。
2. action_cards 包含 rhythm_summary 或 attention_state。
3. context_snapshot 记录 attention decision。
```

### 11.3 验收

```bash
pytest tests/unit/backend/test_chat_rhythm_attention_bridge.py -q
python scripts/smoke/smoke_chat_rhythm_attention.py
```

---

## 12. 修复 I：Approval Request 最小闭环

### 12.1 为什么现在补

V7 已经有 `approval_required` 概念，但如果没有持久 approval request，后面接 email/message/order 或高风险工具时会继续出现“需要确认但用户无处确认”的割裂。

### 12.2 必须实现

如果尚未实现，新增：

```text
approval_requests 表
approval_service.py
GET /api/approvals
POST /api/approvals/{id}/approve
POST /api/approvals/{id}/deny
Approval card in Chat Page
```

### 12.3 本阶段只做框架，不做真实外发

```text
允许产生 approval_request。
允许 approve/deny 改状态。
不实现真实 send_email / send_message / purchase。
```

### 12.4 验收

```bash
pytest tests/unit/backend/test_approval_service.py -q
pytest tests/unit/backend/test_chat_approval_card.py -q
python scripts/smoke/smoke_approval_request.py
```

---

## 13. 前端统一修复

### 13.1 Chat Page 必须支持 Action Cards

新增或扩展：

```text
frontend/src/components/chat/ActionCard.tsx
frontend/src/components/chat/cards/TaskCreatedCard.tsx
frontend/src/components/chat/cards/NotificationCard.tsx
frontend/src/components/chat/cards/MemoryRevisionCard.tsx
frontend/src/components/chat/cards/ToolResultCard.tsx
frontend/src/components/chat/cards/DeleteDecisionCard.tsx
frontend/src/components/chat/cards/KnowledgeResultCard.tsx
frontend/src/components/chat/cards/McpCandidatesCard.tsx
frontend/src/components/chat/cards/SelfDevCard.tsx
frontend/src/components/chat/cards/ApprovalRequiredCard.tsx
frontend/src/components/chat/cards/RhythmSummaryCard.tsx
```

不要求复杂美观，但必须：

```text
1. 空状态明确。
2. 错误状态明确。
3. 每张卡能显示 trace_id 或跳转 Inspector。
4. 不破坏纯聊天消息展示。
```

### 13.2 API 类型

```text
frontend/src/api/chat.ts 必须兼容旧 ChatResponse，同时支持 action_cards。
```

---

## 14. 统一事件与 Trace 要求

每个 bridge 至少写入：

```text
intent_detected
agent_action_dispatched
action_completed / action_failed
```

并把相关 ID 写入 action card：

```text
task_id
memory_id
superseded_memory_id
tool_call_id
mcp_candidate_id
selfdev_request_id
knowledge_document_id
forget_request_id
approval_request_id
notification_id
```

Context Snapshot 至少扩展：

```text
injected_memory_ids
excluded_memory_ids
excluded_reasons
injected_chunk_ids
attention_decision
intent_type
```

---

## 15. 测试总要求

### 15.1 只允许运行本修复相关测试

允许：

```bash
pytest tests/unit/backend/test_memory_revision.py -q
pytest tests/unit/backend/test_context_memory_resolution.py -q
pytest tests/unit/backend/test_chat_task_bridge.py -q
pytest tests/unit/backend/test_notification_api.py -q
pytest tests/unit/backend/test_chat_tool_bridge.py -q
pytest tests/unit/backend/test_chat_safe_delete_bridge.py -q
pytest tests/unit/backend/test_chat_knowledge_bridge.py -q
pytest tests/unit/backend/test_chat_forget_bridge.py -q
pytest tests/unit/backend/test_chat_maintenance_bridge.py -q
pytest tests/unit/backend/test_chat_mcp_bridge.py -q
pytest tests/unit/backend/test_chat_selfdev_bridge.py -q
pytest tests/unit/backend/test_chat_rhythm_attention_bridge.py -q
pytest tests/unit/backend/test_approval_service.py -q
```

Smoke：

```bash
python scripts/smoke/smoke_memory_revision_chat.py
python scripts/smoke/smoke_chat_reminder_1min.py
python scripts/smoke/smoke_chat_safe_delete_tmp.py
python scripts/smoke/smoke_chat_ingest_and_ask.py
python scripts/smoke/smoke_chat_forget_memory.py
python scripts/smoke/smoke_chat_mcp_search_and_test_install.py
python scripts/smoke/smoke_chat_selfdev_tiny_patch.py
python scripts/smoke/smoke_chat_rhythm_attention.py
python scripts/smoke/smoke_approval_request.py
```

禁止默认运行：

```bash
pytest
pytest tests
npm test
pnpm test --all
```

### 15.2 测试清理

所有测试必须使用：

```text
test_run_id
created_by_test = true
fixture teardown
cleanup_report.json
```

清理失败时，测试必须失败。

不可逆操作必须使用副本：

```text
删除：tmp_path 副本。
数据库：test schema / transaction rollback / test_run_id 清理。
Qdrant：test collection。
object store：test prefix。
selfdev：test slot 或 inactive slot 的测试副本。
```

---

## 16. 一次性综合验收脚本

新增：

```text
scripts/smoke/smoke_v1_v20_fixed_end_to_end.py
```

它必须按顺序验证：

```text
1. Chat 正常回复。
2. “以后叫我 A” -> active memory。
3. “以后叫我 B” -> A superseded，B active。
4. “1分钟后提醒我测试” -> task_created card。
5. 到期 -> notification event/card。
6. “摄入测试 md” -> document/chunks。
7. “问测试 md 内容” -> injected_chunk_ids。
8. “忘掉刚才关于名字的记忆” -> forget_result。
9. “搜索一个测试 MCP” -> mcp_candidates。
10. “生成一个低风险 selfdev proposal” -> selfdev_plan。
11. rhythm/attention 查询返回 card。
12. 所有测试数据自动清理。
```

输出：

```json
{
  "ok": true,
  "phase": "v1-v20-fixed",
  "trace_ids": [],
  "created_resources": {},
  "cleanup_report": "tests/artifacts/v1_v20_fixed/<run_id>/cleanup_report.json"
}
```

---

## 17. Done Definition

本文件完成的标准：

```text
1. ChatResponse 支持 action_cards，旧前端/旧 API 不破坏。
2. 用户通过 Chat 能创建 reminder，并看到到期 notification。
3. 用户通过 Chat 能更新同主题单值记忆，旧记忆 superseded，不再注入 Context。
4. 用户通过 Chat 能触发 low-risk tool 和 safe_delete，权限由 PermissionManager 机械控制。
5. 用户通过 Chat 能触发 knowledge ingest/search，Context Snapshot 记录 injected_chunk_ids。
6. 用户通过 Chat 能触发 forget/maintenance，忘记后不再注入。
7. 用户通过 Chat 能触发 MCP search 和 test MCP sandbox smoke。
8. 用户通过 Chat 能触发 selfdev proposal 和受控 inactive slot 操作入口。
9. 用户通过 Chat 能查询 rhythm/attention 状态。
10. Approval request 有最小持久闭环。
11. Event/Trace/Inspector 能看到每次 action 的路径。
12. 所有新增测试副作用自动清理。
```

---

## 18. 阶段完成后必须更新

完成后更新：

```text
docs/current_phase.md
tests/artifacts/v1_v20_fixed/<run_id>/summary.json
tests/artifacts/v1_v20_fixed/<run_id>/commands.txt
tests/artifacts/v1_v20_fixed/<run_id>/cleanup_report.json
```

`docs/current_phase.md` 记录：

```text
Phase: V1-V20 Fixed Integration Patch
Status: completed
Implemented:
- ChatResponse action cards
- Memory revision / supersede / context resolution
- Chat-to-task bridge
- Notification bridge
- Chat-to-tool/safe_delete bridge
- Chat-to-knowledge bridge
- Chat-to-forget/maintenance bridge
- Chat-to-MCP bridge
- Chat-to-selfdev bridge
- Rhythm/attention chat bridge
- Approval request minimal loop
Observable Result:
- 用户可从 Chat 使用 V1-V20 核心能力
Tests Run:
- 只列实际运行的定向测试和 smoke
Cleanup Result:
- cleanup_report 路径
Known Gaps:
- 不实现 V21+ 高级自举 / schema migration / Temporal KG
Next Phase:
project_plan_v21.md
```
