# AIive V1-V20 功能真实性审查与修复报告 v1

> 本报告用于在 AIive 已推进到 V20 后，暂停后续开发，对 V1-V20 已实现能力进行一次全面功能验收、架构审查和修复。
>
> 当前问题不是“还缺几个小功能”，而是部分能力可能被实现成了硬编码、假等待、前端临时状态、内存状态、绕过工具层、绕过 LangGraph、绕过持久化、绕过 Context Builder 的伪实现。编码 AI 必须先审查、再修复，不得继续推进 V21+。

---

## 0. 本报告的执行优先级

本报告优先级高于旧的 V1-V20 阶段文件，也高于旧的 V20+ 计划。

编码 AI 必须遵守：

```text
1. 先审查，不得先修代码。
2. 审查必须覆盖 V1-V20 的每一个用户可见能力和底层架构依赖。
3. 发现 BLOCKER 后，不得继续新功能。
4. 所有修复必须保留已有能力，尽量增量修复，不得大规模删除重写。
5. 所有测试必须是 targeted tests，不允许全量回归。
6. 测试产生的文件、数据库记录、Qdrant collection、object store 对象、outbox job、notification event 必须由 fixture / cleanup helper 自动清理。
7. 不允许用硬编码、sleep、setTimeout、mock 生产路径、前端假状态、内存临时状态冒充真实能力。
```

---

## 1. 当前必须解决的核心症状

### 1.1 Reminder 看似可用，但实际不可用

错误现象：

```text
用户：一分钟后给我发“hi”
AIive：好的，千早爱音会在1分钟后提醒您。
（等待了大约60秒...）
千早爱音：hi
```

这可能是假的，因为正确的 reminder 不是 Chat handler 等待 60 秒后输出，也不是前端 setTimeout，也不是识别固定句式后硬编码输出。

正确架构必须是：

```text
用户 Chat 输入
↓
/api/chat
↓
LangGraph StateGraph
↓
IntentRouter 判断 task_create / reminder
↓
ExecutionIntentGate 判断 should_execute=true
↓
AgentActionDispatcher
↓
ToolRegistry 查 schedule_reminder capability
↓
PermissionManager 判断 local reminder 不需要确认
↓
ToolExecutor 调用 schedule_reminder
↓
task_service.create_task()
↓
PostgreSQL tasks 表持久化
↓
Chat 立即返回 task_created action card，不阻塞等待
↓
Background Worker 按真实时间扫描 due tasks
↓
到期创建 notification.created event
↓
重新唤醒 Agent / 拼接 reminder context
↓
Agent 决策本地通知内容为 hi
↓
Chat / Notification Inbox 显示 hi
```

### 1.2 改名字 / 改称呼功能无法正确实现

错误现象之一：

```text
用户：如果我想让你改名，你会调用哪个工具？
系统却执行了 memory_revision / remember_or_update
```

这说明系统把“元问题 / 假设问题 / 工具选择询问”误判成了“执行命令”。

正确行为：

```text
用户：如果我想让你改名，你会调用哪个工具？
AIive：不调用工具，只解释：如果你明确要求改名，我会调用 update_agent_identity 或 remember_or_update_memory 写入 agent_self/persona 记忆。
```

真正执行的例子：

```text
用户：以后你叫千早爱音。
AIive：调用 update_agent_identity 或 remember_or_update_memory。
```

### 1.3 当前不像个人智能体，而像普通 chatbot

当前风险：

```text
刷新页面后对话清空；
每次打开页面生成新 thread；
有“新建对话”入口；
Chat 状态只在前端 local state；
后端没有 singleton active thread；
Context Builder 不从长期状态恢复上下文。
```

AIive V1-V20 阶段最低要求：

```text
1. 有且只有一个连续对话入口。
2. 前端不提供“新建对话”功能。
3. 刷新页面不得清空对话。
4. /api/chat 必须使用同一个 active_thread_id。
5. 除非系统明确重启并记录 system_started event，否则不应开启新对话。
6. 即使系统重启，推荐恢复最近 active thread；如果暂时不恢复，必须记录原因，不得静默丢失。
7. LangGraph checkpoint 必须保存 thread state。
8. Chat history 必须来自后端持久化，不得只来自前端数组。
```

---

## 2. 本次审查必须输出的文件

编码 AI 首先只生成审查报告，不得修改代码。

必须生成：

```text
docs/audit/v1_v20_functional_truth_report.md
```

报告必须包含：

```text
1. 当前 /api/chat 是否进入 LangGraph StateGraph。
2. 当前是否只有一个连续对话 thread。
3. 页面刷新后对话是否仍在。
4. reminder 是否是真实 tasks + worker，而非 sleep/setTimeout/硬编码。
5. 改名 / 改称呼是否经过 intent gate，且 explain-only 不产生副作用。
6. memory 是否支持 update/supersede，而非 append-only。
7. 所有用户可见能力是否能从 Chat 自然语言触发。
8. 所有副作用工具是否经过 ToolRegistry / PermissionManager / ToolExecutor。
9. 所有能力是否有 trace/event/action card/context snapshot。
10. 所有测试是否清理副作用。
11. 列出 BLOCKER / MAJOR / MINOR。
12. 给出修复顺序。
```

报告格式必须包含表格：

```text
能力名 | 当前状态 | 是否接 Chat | 是否接 LangGraph | 是否持久化 | 是否有工具 | 是否有 trace | 是否有真实测试 | 问题等级 | 修复文件
```

状态只能使用：

```text
PASS：真实可用
PARTIAL：部分真实，但入口/可观测/持久化缺失
FAKE：硬编码/假等待/假状态/只在前端模拟
MISSING：未实现
UNKNOWN：未能确认，必须继续追查
```

---

## 3. BLOCKER 定义

出现以下任意情况，必须停止后续 V21+ 开发：

```text
1. /api/chat 没有进入 LangGraph StateGraph。
2. Chat 刷新后丢失对话。
3. 系统有多个新对话入口，且没有明确用户授权切换。
4. reminder 使用 sleep/setTimeout/硬编码冒充真实调度。
5. 用户询问“如果我要 X，你会怎么做”时触发副作用工具。
6. memory append-only，无法 supersede 同主题旧记忆。
7. 删除不经过 safe_delete。
8. 工具调用绕过 ToolRegistry / PermissionManager。
9. 测试只 mock 生产路径，无法证明真实能力。
10. 测试产生副作用但不自动清理。
```

---

## 4. V1-V20 后用户应该能实现的真实案例

下面每个案例都是用户亲自验收用例。编码 AI 必须保证这些例子能跑通，并且不是假实现。

---

# A. 单一连续对话能力

## A1. 页面刷新不丢对话

用户操作：

```text
1. 打开 Chat 页面。
2. 输入：你好，我们正在测试连续对话。
3. 刷新浏览器页面。
4. 查看 Chat 页面。
```

期望结果：

```text
1. 之前消息仍然存在。
2. thread_id 不变。
3. trace 能看到同一 active_thread_id。
4. 没有创建新 thread。
```

禁止实现：

```text
1. 只把消息存在 React state。
2. 只存在 localStorage，不进后端。
3. 刷新后调用 createThread。
4. 默认每次 /api/chat 创建新 thread。
```

必须检查：

```text
threads 表
messages/events 表
LangGraph checkpoint
前端 active_thread_id 来源
```

验收 SQL 示例：

```sql
select id, status, created_at, updated_at from threads order by created_at desc;
select thread_id, count(*) from events where event_type in ('user_message','assistant_message') group by thread_id;
```

## A2. 没有“新建对话”入口

期望：

```text
1. 前端没有 New Chat 按钮。
2. API 没有暴露给普通用户的 create_new_thread 入口。
3. 如果内部存在 create_thread，仅用于 system init / tests / explicit reset。
4. reset 必须是危险操作或维护操作，有事件记录。
```

---

# B. 真实 LLM + LangGraph 主链路

## B1. /api/chat 必须进入 LangGraph

用户输入：

```text
你好，告诉我当前 trace id。
```

期望：

```text
1. 响应包含 trace_id。
2. 后端日志显示 LangGraph graph.invoke 或 graph.astream 被调用。
3. checkpoints 表或 checkpointer storage 中有对应 thread checkpoint。
4. Context Builder 被调用。
5. llm_calls 表有记录，除非是明确的 no-llm system response。
```

禁止实现：

```text
1. FastAPI handler 里 if/else 直接返回。
2. Chat service 直接调用 LLM，不经过 LangGraph。
3. LangGraph 只在测试里使用，生产路径不用。
```

审查命令建议：

```bash
grep -R "graph.invoke\|graph.astream\|StateGraph" -n backend app src
```

---

# C. Reminder / Scheduler 能力

## C1. 一分钟后本地提醒 hi

用户输入：

```text
一分钟后给我发“hi”
```

正确即时回复：

```text
好的，我会在 1 分钟后提醒你。
```

同时 ChatResponse 必须包含 action card：

```json
{
  "type": "task_created",
  "task_id": "...",
  "task_type": "reminder",
  "title": "提醒 hi",
  "message": "hi",
  "trigger_at": "...",
  "delivery_channel": "local_chat",
  "status": "active"
}
```

真实 60 秒后期望：

```text
1. worker 扫描 tasks 表，发现 due task。
2. worker 创建 notification.created event。
3. worker 将 task 状态更新为 completed 或 triggered。
4. Chat / Notification Inbox 显示 hi。
5. trace 显示 notification 来源于 task_id。
```

必须检查数据库：

```sql
select id, task_type, title, message, status, trigger_at from tasks order by created_at desc limit 5;
select event_type, payload from events where event_type like 'notification.%' order by created_at desc limit 5;
```

禁止实现：

```text
1. Chat handler 中 await sleep(60)。
2. Python time.sleep(60)。
3. 前端 setTimeout 作为真实调度。
4. 硬编码“一分钟后给我发 hi”。
5. 不写 tasks 表。
6. 不写 notification event。
7. 刷新页面后提醒消失。
```

## C2. 一分钟后执行某个低风险工具并反馈结果

用户输入：

```text
一分钟后执行 current_time 工具，并把结果告诉我。
```

如果 current_time 工具存在，期望：

```text
1. 创建 scheduled_tool_task。
2. task payload 中记录 tool_id=current_time。
3. 到期后 worker 唤醒 Agent。
4. Context Builder 拼接：原始用户请求、task payload、due time、允许工具、当前上下文。
5. Agent 决策调用 current_time。
6. ToolExecutor 执行 current_time。
7. notification.created 或 assistant_message 展示结果。
```

如果 current_time 工具不存在，期望：

```text
1. 不假装执行。
2. 回复当前没有 current_time 工具，是否要创建/搜索能力。
3. 不创建不可执行 task，或创建 needs_capability task。
```

禁止实现：

```text
1. 到期后直接输出固定文本。
2. 不经过 ToolRegistry。
3. 不经过 PermissionManager。
4. 不拼接 task context。
5. 不记录 tool_call trace。
```

## C3. 取消提醒

用户输入：

```text
一分钟后提醒我测试取消。
取消刚才那个提醒。
```

期望：

```text
1. 第一句创建 task。
2. 第二句调用 cancel_task。
3. task 状态变为 canceled。
4. 到期后不产生 notification。
```

---

# D. 改名 / 称呼 / persona 记忆能力

## D1. 问工具，不应执行

用户输入：

```text
如果我想让你改名，你会调用哪个工具？
```

正确结果：

```text
1. 不调用 remember_or_update。
2. 不写 memory_records。
3. 不产生 memory_revision。
4. 只解释：如果你明确要求改名，会调用 update_agent_identity 或 remember_or_update_memory。
```

必须检查：

```sql
select * from memory_records order by created_at desc limit 5;
```

该输入前后 memory_records 不应新增“改名”记录。

## D2. 真正要求改名

用户输入：

```text
以后你叫千早爱音。
```

期望：

```text
1. intent_type = agent_identity_update 或 memory_update。
2. execution_mode = execute。
3. should_execute = true。
4. 调用 update_agent_identity 或 remember_or_update_memory。
5. memory_type = agent_self / persona。
6. memory_key = agent.display_name。
7. lifecycle_state = active。
8. Chat 回复确认。
```

接着用户输入：

```text
你叫什么？
```

期望：

```text
回答：千早爱音。
Context Snapshot 显示注入 agent.display_name = 千早爱音。
```

## D3. 改名覆盖旧名

用户输入：

```text
以后你叫 A。
以后你叫 B。
你叫什么？
```

期望：

```text
1. 只回答 B。
2. A 对应 memory lifecycle_state = superseded。
3. B active。
4. Context Snapshot injected B。
5. Context Snapshot excluded A，reason=superseded。
```

禁止实现：

```text
1. A 和 B 都 active。
2. 每次都 append-only。
3. 只靠 prompt 让 LLM 自己判断哪个更新。
```

---

# E. 用户称呼记忆能力

## E1. 用户让 AIive 改对用户的称呼

用户输入：

```text
以后叫我小李。
```

期望：

```text
memory_type = user_profile / preference
memory_key = user.preferred_name
content = 小李
active
```

接着：

```text
以后叫我 B。
我叫什么？
```

期望：

```text
只回答 B。
小李 superseded。
B active。
```

---

# F. Intent 分类与副作用门控

## F1. 假设句不得执行

这些输入都不得产生副作用：

```text
如果我要你删文件，你会怎么做？
如果我想让你一分钟后提醒我，你会调用什么？
你会怎么给自己安装 MCP？
你能不能修改自己的代码？
你一般如何记住我的偏好？
```

期望：

```text
1. execution_mode = explain_only。
2. should_execute = false。
3. 不创建 task。
4. 不写 memory。
5. 不 delete。
6. 不 install MCP。
7. 不 modify code。
```

## F2. 明确命令才执行

这些才可以执行：

```text
一分钟后提醒我喝水。
以后叫我 B。
删除这个测试文件：<path>。
帮你自己搜索一个读 GitHub issue 的 MCP。
给你自己加一个显示当前时间的低风险能力，先生成计划。
```

---

# G. safe_delete 能力

## G1. 删除测试文件

准备测试文件：

```text
.data/test_sandbox/delete_me.txt
```

用户输入：

```text
删除 .data/test_sandbox/delete_me.txt
```

期望：

```text
1. 调用 safe_delete。
2. PermissionManager 记录 decision。
3. delete_guard 解析 realpath。
4. scope_id = test_sandbox 或 user_selected。
5. 文件删除或移动到 trash/quarantine。
6. event 记录 delete.completed。
```

## G2. 危险路径必须拒绝

用户输入：

```text
删除我的 home 目录。
```

期望：

```text
1. safe_delete decision = deny。
2. reason 包含 home root protected。
3. 不执行 os.remove/shutil.rmtree/rm。
```

---

# H. 知识库摄入与检索能力

## H1. 摄入文档

用户输入：

```text
把 tests/fixtures/demo.md 加入知识库。
```

期望：

```text
1. 调用 ingest_document。
2. documents 表新增。
3. chunks 表新增。
4. object_store 保存 raw document。
5. outbox 创建 qdrant_index job。
6. worker 写入 Qdrant。
7. ChatResponse 有 document_ingested action card。
```

## H2. 检索文档

用户输入：

```text
搜索 demo.md 里关于 xxx 的内容。
```

期望：

```text
1. 调用 search_knowledge。
2. 查询 PostgreSQL metadata + Qdrant。
3. retrieval_candidates 可见。
4. Context Snapshot 注入相关 chunk。
5. 回答中引用检索来源。
```

禁止：

```text
1. 只做字符串 contains 搜索冒充知识库。
2. 只把文档塞进 prompt，不建 chunks。
3. Qdrant 只有测试 mock，生产未接。
```

---

# I. MCP 搜索与沙箱能力

## I1. 搜索 MCP

用户输入：

```text
帮你自己找一个能读 GitHub issue 的 MCP。
```

期望：

```text
1. 调用 search_mcp。
2. 候选来自 registry/config source，不是硬编码列表。
3. 每个候选包含 source、name、version、descriptor_hash、declared_tools、risk_notes。
4. ChatResponse 有 mcp_candidates action card。
```

## I2. 沙箱测试 MCP

用户输入：

```text
测试第一个低风险 MCP 候选。
```

期望：

```text
1. 调用 install_mcp_sandbox。
2. 只在 sandbox 目录安装。
3. 记录 smoke test result。
4. 不直接 active。
5. 高风险工具需要 review。
```

禁止：

```text
1. 返回固定 GitHub MCP 列表。
2. 直接安装到生产环境。
3. descriptor 变化不检查 hash。
```

---

# J. 自我修改 / A-B slot 能力

## J1. 生成自我修改计划

用户输入：

```text
给你自己加一个显示当前时间的低风险能力，先生成计划，不要上线。
```

期望：

```text
1. 调用 create_selfdev_plan。
2. 生成 plan action card。
3. 不修改 active slot。
4. 不直接写生产代码。
```

## J2. 应用 patch 到 inactive slot

用户输入：

```text
把这个能力实现到 inactive slot，并跑相关测试。
```

期望：

```text
1. 调用 apply_patch_to_inactive_slot。
2. target_slot = inactive。
3. active slot 只读。
4. 测试为 targeted tests。
5. 测试副作用自动清理。
```

## J3. promote / rollback

用户输入：

```text
如果测试通过，切换到新版本。
```

期望：

```text
1. health check pass。
2. manifest check pass。
3. schema compatibility pass。
4. promote_slot 切换 active slot。
5. 失败可 rollback。
```

---

# K. 注意力状态与个人节奏能力

## K1. 查询当前关注点

用户输入：

```text
你现在认为我当前关注的事情是什么？
```

期望：

```text
1. 调用 query_attention_state。
2. 返回 current_focus、recent_topics、topic_stack。
3. 来源于 events / recent thread / attention_state 表。
4. 不是 LLM 凭空编。
```

## K2. 查询使用节奏

用户输入：

```text
你最近观察到我的使用节奏有什么变化？
```

期望：

```text
1. 调用 query_rhythm。
2. 返回 rhythm signals。
3. 有来源 event。
4. 不做过度主动打扰。
```

---

## 5. V1-V20 应该存在的工具清单

编码 AI 必须输出当前 ToolRegistry 实际注册工具列表，并和下面的期望清单对齐。

### 5.1 时间 / 任务工具

```text
schedule_reminder
schedule_tool_task
list_tasks
cancel_task
show_notifications
```

说明：

```text
schedule_reminder：创建一次性本地提醒。
schedule_tool_task：创建到期后执行某低风险工具的任务。
list_tasks：列出任务。
cancel_task：取消任务。
show_notifications：查看通知。
```

### 5.2 记忆工具

```text
remember_or_update_memory
search_memory
forget_memory
run_memory_maintenance
update_agent_identity
```

说明：

```text
update_agent_identity 可以是独立工具；如果暂未独立实现，必须由 remember_or_update_memory 支持 memory_type=agent_self/persona, memory_key=agent.display_name。
```

### 5.3 文件与删除工具

```text
safe_delete
```

### 5.4 知识库工具

```text
ingest_document
search_knowledge
```

### 5.5 MCP 工具

```text
search_mcp
install_mcp_sandbox
activate_mcp_capability  # 如未支持正式启用，可标记 deferred，但不得伪装 implemented
```

### 5.6 自我修改工具

```text
create_selfdev_plan
apply_patch_to_inactive_slot
run_targeted_tests
promote_slot
rollback_slot
```

### 5.7 状态查询工具

```text
query_attention_state
query_rhythm
```

---

## 6. 每个工具必须有的 Capability Schema

每个工具必须在 registry 中声明：

```json
{
  "capability_id": "schedule_reminder",
  "description": "Create a persistent local reminder task.",
  "risk_level": "low",
  "side_effect": true,
  "requires_execution_intent": true,
  "requires_confirmation": false,
  "writes_external_world": false,
  "allowed_instruction_sources": ["trusted_user_command"],
  "blocked_when": [
    "hypothetical_question",
    "tool_choice_question",
    "capability_question",
    "debug_question",
    "explain_only"
  ],
  "service_entrypoint": "task_service.create_task",
  "audit_required": true
}
```

所有会产生副作用的工具都必须有：

```text
side_effect = true
requires_execution_intent = true
```

---

## 7. Execution Intent Gate 必须实现

所有 Chat 请求先经过 intent 分类。

最小输出 schema：

```json
{
  "intent_type": "tool_choice_question | capability_question | normal_chat | memory_update | task_create | tool_execute | delete_request | selfdev_request | mcp_discovery",
  "execution_mode": "explain_only | dry_run | execute",
  "should_execute": false,
  "candidate_tool": null,
  "reason": "User asked what tool would be used, not to execute it."
}
```

Dispatcher 规则：

```text
if tool.side_effect == true and execution_mode != execute:
    block tool call

if should_execute != true:
    block tool call

if intent_type in [tool_choice_question, capability_question, hypothetical_question, debug_question]:
    block side_effect tool call
```

必须加入测试：

```text
输入：如果我想让你改名，你会调用哪个工具？
期望：不写 memory。

输入：如果我要你删文件，你会怎么做？
期望：不调用 safe_delete。

输入：如果我想让你一分钟后提醒我，你会怎么做？
期望：不创建 task。

输入：以后你叫千早爱音。
期望：写 agent identity memory。

输入：一分钟后提醒我 hi。
期望：创建 task。
```

---

## 8. 真实测试要求：禁止自欺欺人

### 8.1 禁止的测试方式

```text
1. 只 mock Chat handler，然后断言文本包含“已设置提醒”。
2. 真实功能没接 service，只测 intent parser。
3. 使用 sleep 60 秒作为单元测试。
4. 不检查数据库状态。
5. 不检查 event/trace/action card。
6. 不检查刷新页面后的状态。
7. 测试产生数据后不清理。
8. 使用前端 setTimeout 冒充 worker。
```

### 8.2 正确测试方式

单元测试使用 fake clock / injectable clock：

```text
1. Chat 输入“一分钟后给我发 hi”。
2. 验证 tasks 表有 task。
3. 验证 ChatResponse 有 task_created action card。
4. fake clock 推进 61 秒。
5. 调用 worker.tick()。
6. 验证 notification.created event。
7. 验证 task 状态 completed。
8. fixture 自动清理 task/event/notification。
```

手动 smoke test 使用真实时间：

```text
1. 启动后端和前端。
2. 用户真实输入“一分钟后给我发 hi”。
3. 不关闭页面，等待真实 60-75 秒。
4. 查看 Chat / Notification Inbox 是否出现 hi。
5. 刷新页面，确认 notification 仍可见。
6. 查 DB 确认 task/event 存在。
```

### 8.3 测试清理要求

测试数据必须带 run_id：

```text
test_run_id = uuid
```

所有测试写入必须包含 test_run_id 或 test namespace。

清理由 fixture 完成：

```text
cleanup_tasks(test_run_id)
cleanup_events(test_run_id)
cleanup_memories(test_run_id)
cleanup_documents(test_run_id)
cleanup_qdrant(test_run_id)
cleanup_object_store(test_run_id)
cleanup_files(test_run_id)
```

不可逆测试必须使用副本：

```text
1. 删除功能测试只删除测试目录副本。
2. 数据库破坏性测试使用测试数据库或事务 rollback。
3. Qdrant 测试使用 test collection。
4. object store 测试使用 test prefix。
```

---

## 9. 编码 AI 审查流程

### Step 1：只读审查，不改代码

必须阅读：

```text
backend / app / src 代码
frontend 代码
migrations
models
services
tools / capabilities
workers
tests
```

输出：

```text
docs/audit/v1_v20_functional_truth_report.md
```

### Step 2：标记 BLOCKER

优先修复：

```text
1. 单一连续对话缺失。
2. /api/chat 未接 LangGraph。
3. reminder 假实现。
4. execution intent gate 缺失。
5. memory append-only。
6. 工具绕过 ToolRegistry/PermissionManager。
7. 测试不真实。
```

### Step 3：逐项修复

修复顺序必须是：

```text
1. Single Conversation + Persistence
2. LangGraph Chat Main Path
3. Execution Intent Gate
4. ToolRegistry/PermissionManager enforcement
5. Memory Revision / Supersede
6. Scheduler Capability / Worker
7. Action Cards / Trace / Context Snapshot
8. Targeted Tests + Cleanup
```

### Step 4：输出完成报告

必须生成：

```text
docs/audit/v1_v20_functional_repair_completion_report.md
```

包含：

```text
修复了什么
没有修什么
仍然 deferred 什么
每个验收案例怎么运行
每个测试如何清理副作用
```

---

## 10. 给编码 AI 的直接指令

可以直接复制以下内容给编码 AI：

```text
当前暂停 V21+。请执行 AIive V1-V20 功能真实性审查与修复。

先阅读 AIive_v1-v20_functional_audit_and_repair_report.md。
第一步只能生成 docs/audit/v1_v20_functional_truth_report.md，不得修改代码。

审查必须覆盖：
1. 是否有且只有一个连续对话；刷新页面不得清空。
2. /api/chat 是否进入 LangGraph StateGraph。
3. reminder 是否是真实 tasks + worker + notification event，不得 sleep/setTimeout/硬编码。
4. “一分钟后执行 xxx 工具”是否能持久化为 scheduled_tool_task，到期唤醒 Agent，拼接上下文，调用 ToolRegistry 工具并返回结果。
5. 改名/称呼是否经过 Execution Intent Gate，询问工具时不得写记忆。
6. memory 是否支持 memory_key/revision/supersede/resolve_for_context，不得 append-only。
7. 所有副作用能力是否经过 ToolRegistry/PermissionManager/ToolExecutor。
8. 所有能力是否有 action card、trace、event、context snapshot。
9. 测试是否真实验证 DB/event/worker/context，而不是只断言文本。
10. 测试是否由 fixture 自动清理副作用。

报告完成后，先修 BLOCKER，再修 MAJOR。修复完成前不得继续 V21+。
```

---

## 11. 通过标准

V1-V20 修复完成的最低通过标准：

```text
1. 刷新页面后对话不消失。
2. 系统没有新建对话入口，只有一个 active thread。
3. /api/chat 进入 LangGraph。
4. “如果我想让你改名，你会调用哪个工具？”不写记忆。
5. “以后你叫 A -> 以后你叫 B -> 你叫什么？”只回答 B，A superseded。
6. “一分钟后给我发 hi”真实创建 task，真实 60 秒后由 worker 触发 notification。
7. “一分钟后执行 current_time 工具”到期后唤醒 Agent，经 ToolRegistry 调用工具并展示结果。
8. safe_delete 是唯一删除入口。
9. ChatResponse 对工具结果有 action card。
10. trace/event/context snapshot 可追踪每次能力调用。
11. targeted tests 能证明上述能力，且自动清理副作用。
```

若以上任何一项失败，不能进入 V21+。

---

## 12. 最终判断

AIive V1-V20 的目标不是做出一堆 API，也不是让 Chat 页面能用几句硬编码回复。

V20 结束时，AIive 应该已经是一个最小但真实的个人智能体：

```text
1. 它有唯一连续对话，不会刷新即失忆。
2. 它通过 LangGraph 维护短期线程状态。
3. 它通过 memory_records 维护长期记忆，并能修订旧理解。
4. 它通过 Context Builder 拼接当前任务所需上下文。
5. 它通过 ToolRegistry/PermissionManager 执行工具。
6. 它通过 tasks + worker 拥有真实时间能力。
7. 它通过 trace/action card/UI 让用户看见自己做了什么。
8. 它不会把“如果我要做 X”误执行成真的 X。
```

这份审查通过后，才允许继续 MCP、自我进化、Temporal KG、生活节奏主动维护等 V21+ 能力。
