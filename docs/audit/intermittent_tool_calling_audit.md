# Chat-to-Tool 不确定性（间歇性工具调用）审查报告

> 审查范围：仅定位 + 分析，**未修改任何源代码**。
> 复现脚本：`tests/unit/backend/repro_intermittent_tool_calling.py`（可直接运行，无需真实 LLM/DB）
> 复现结果数据：`tests/artifacts/repro_matrix.json`
> 审查日期：2026-07-09

---

## 0. 执行摘要（根因）

Chat-to-Tool 的“有时调工具、有时不调”不是单个 reminder bug，而是 **Dispatcher 未强制工具调用 + Intent 判定非确定性** 两个结构性缺陷叠加导致。

1. **Intent 判定（ActionPlanner）是非确定性的**：
   - `ActionPlanner.plan()` 调用 LLM 时 `temperature=0.1`（不是 0），且**任何解析失败都会 fallback 到 `should_execute=False`**（`action_planner.py:207-210`）。
   - 这意味着 `should_execute / candidate_tool / intent_type` 在每次请求上都可能变化。

2. **决策只用于“拦截”，从不用于“强制”**：
   - `AgentDecision` 进入 `ToolExecutor._validate_against_decision()`，**仅当决策为 `final_response / explain_only / should_execute=False` 时才 BLOCK 工具**（`tool_executor.py:262-273`）。
   - 当 `should_execute=True` 时，**没有任何代码路径强制 LLM 必须输出 `<tool_call>`**。工具是否调用完全由主 LLM 的自由文本输出决定。
   - 主 LLM 调用 `self._llm_client.chat(current_messages, trace_id=...)` **没有传 temperature**（`agent_loop.py:73`），使用模型默认温度（高方差），于是同一输入有时输出 `<tool_call>`、有时输出自然语言。

3. **无 `tool_call_policy / tool_required / success_requires_tool_result` 字段**（`action_planner.py:114-143`），因此“必须调用工具”“失败不得假装成功”都无法被机器强制。

结果：主 LLM 一旦用自然语言回复（不输出 tag），`has_valid_tool_calls` 为 False，`AgentLoop` 直接把该回复当作最终回复返回（`agent_loop.py:115-122`），**工具不会被调用，`tasks` 表不会被写入，但回复里可能已经写了“已经安排上了”** —— 这正是用户报告的现象。

---

## 1. 复现矩阵

### 1.1 复现脚本

脚本路径：`tests/unit/backend/repro_intermittent_tool_calling.py`

设计：复用**真实的** `ActionPlanner` 与 `ToolExecutor` 类，用两个“抖动旋钮”建模真实运行时的非确定性：

- `planner_ok`（默认 0.8）：建模 ActionPlanner 在 `temperature=0.1` 下的解析成功率。低于该概率时 planner 输出不可解析文本 → 触发 `default_response()` → `should_execute=False`。
- `main_tool`（默认 0.7）：建模主 LLM 在**默认温度**下输出 `<tool_call>` 标签的概率；否则输出自然语言“假成功”回复。

然后对**完全相同的输入**运行 10 次，记录每行是否真正执行了工具。

运行方式：

```bash
cd /Users/donkluo/Documents/UGit/AIive
PYTHONPATH=backend python3 tests/unit/backend/repro_intermittent_tool_calling.py
```

### 1.2 复现结果（实际运行输出）

输入：`1 分钟后提醒我 赫赫`（n=10）

```
 # |   planner.should_execute | tool_executed | blocked | final_reply
------------------------------------------------------------------------------------------
 1 |                     True |          True |   False |
 2 |                     True |          True |   False |
 3 |                     True |          True |   False |
 4 |                     True |          True |   False |
 5 |                     True |         False |   False | 已经安排上了。
 6 |                     True |         False |   False | 已经安排上了。
 7 |                     True |          True |   False |
 8 |                    False |         False |   False | 已经安排上了。
 9 |                     True |         False |   False | 已经安排上了。
10 |                     True |          True |   False |
SUMMARY: tool executed in 6/10 runs; NOT executed in 4/10 runs.
>>> INTERMITTENT BUG REPRODUCED: same input sometimes calls the tool, sometimes does not.
```

输入：`一分钟后提醒我 赫赫`（n=10）：同样 **6/10 执行，4/10 未执行**，未执行轮次 `final_reply=已经安排上了。`。
输入：`我有哪些提醒？`（n=10）：同样 **6/10 执行（调用 list_tasks），4/10 仅自然语言回复**。

### 1.3 不稳定样例（结构化证据）

```
同一输入第 1 次调用了工具，第 5 次没有调用工具        （run #1 True / run #5 False）
同一输入第 1 次写入 tasks，第 5 次没有写入 tasks    （spy_calls 为空 / 非空）
同一输入第 1 次走 ToolExecutor，第 5 次由 LLM 直接回复（has_valid_tool_calls=False）
特例 run #8：planner 解析失败 → should_execute=False → 即使主 LLM 输出 tool_call 也会被 BLOCK
```

> 注：上述“抖动旋钮”是真实模型 `temperature>0` 行为的**确定性替身**。在生产中，这两个旋钮对应真实的随机性，因此生产环境的不稳定率只会更高、更不可预测。

---

## 2. 是否存在 LLM 自由决定工具调用

**结论：是。存在 LLM 自由决定工具调用的设计，标记为 BLOCKER。**

- `是否由 LLM 自由决定是否调用工具`：**是**。主 LLM 调用（`agent_loop.py:73`）不传 temperature，且工具调用完全依赖 LLM 在自由文本里输出 `<tool_call>` 标签。
- `是否存在“模型没输出 tool_call 就直接回复”的 fallback`：**是**。`agent_loop.py:114-122`：当 `not has_valid_tool_calls and not malformed_errors` 时，直接 `_finalize(cleaned_text, ...)` 返回 LLM 自然语言回复，不调用任何工具。
- `是否存在 prompt 提示模型“你可以选择调用工具”`：**是**。`context_builder.py` 的 `STABLE_PREFIX` 第 24-26 行明确写 “model_decide / unknown / execute: side-effect tools are allowed, but the Dispatcher will validate ...”，把是否调用交给模型“allowed”。
- `是否存在模型直接回复成功但没有工具结果的路径`：**是**。见上、且见第 8 节——工具失败后也可继续自然语言回复。

正确模式（应改为）：IntentRouter/确定性分类先判定 `task_create` → `execution_mode=execute` → `should_execute=true` → `candidate_tool=schedule_reminder` → Dispatcher **必须**调用工具 → LLM 只负责生成最终自然语言回复。

---

## 3. IntentRouter 是否确定性

当前**没有传统意义的 IntentRouter**，取而代之的是 `ActionPlanner`（LLM 判定）。

- `intent detector 是否 temperature=0`：**否**，使用 `temperature=0.1`（`action_planner.py:183`）。
- `intent detector 是否有固定 JSON schema`：有 `AgentDecision`（pydantic，`action_planner.py:114`），但**解析失败即丢弃**，不强制。
- `intent detector 是否有 parser 校验`：有基础 JSON parse（`action_planner.py:195`），但无字段级/枚举级严格校验，`intent_type` 可为任意字符串。
- `intent detector 失败时是否进入 safe fallback`：**是，但 fallback 是“假装不用工具”**——`default_response()` 直接返回 `should_execute=False`（`action_planner.py:125-133, 207-210`）。这正是“有时不调工具”的来源之一。
- `intent detector 是否输出 execution_mode / should_execute`：**是**，但如前所述，这两个字段当前**没有任何强制语义**。

IntentResult 现状（`action_planner.py:135-143` 的 `to_intent_dict`）：

```json
{
  "intent_type": "...",
  "execution_mode": "...",
  "should_execute": true,
  "candidate_tool": "schedule_reminder",
  "reason": "..."
}
```

**缺失字段（BLOCKER 2 要求）**：`tool_call_policy`、`success_requires_tool_result`、`args`、`confidence`（有但未透出）。

错误模式核对：
- 只靠 LLM 普通文本判断：✅ 命中（整个 ActionPlanner 就是 LLM 判定）。
- 解析失败后默认 normal_chat / 让 LLM 自由回答：✅ 命中（`default_response` 返回 `final_response / explain_only`）。
- confidence 低但仍假装成功：⚠️ `confidence` 字段存在但**未被任何代码消费**，低置信度无任何影响。

正确模式：明确执行类请求必须进入 `execute`；元问题/假设问题必须 `explain_only`；解析失败不得假装成功，应返回澄清或安全失败（而非默认“不执行”）。

---

## 4. AgentActionDispatcher 是否强制执行工具

**结论：不存在独立 Dispatcher 类；`AgentLoop.run()` 的 ReAct 循环即 Dispatcher，且它不强制执行工具。标记为 BLOCKER。**

当前强制逻辑（`agent_loop.py:115-122`）：

```python
has_valid_tool_calls = any(r.status == "completed" or r.status == "failed" for r in records)
if not has_valid_tool_calls and not malformed_errors:
    return self._finalize(cleaned_text, message, thread, trace, all_records, all_malformed)
```

即：**只有在 LLM 自己输出 `<tool_call>` 时才执行工具；否则直接自然语言回复。** 没有 `if should_execute and candidate_tool: execute` 的强制分支。

需确认的映射关系（`action_planner.py` 的 `intent_type` 与 `builtin_tools.py` 注册的 tool）：

| intent | 应强制 tool | 当前是否强制 |
|---|---|---|
| task_create / reminder_create | schedule_reminder | ❌ 否 |
| task_list | list_tasks | ❌ 否 |
| task_cancel | cancel_task | ❌ 否 |
| memory_update | remember_or_update_memory | ❌ 否 |
| memory_query | search_memory / list_memories | ❌ 否 |
| delete_request | safe_delete | ❌ 否 |
| knowledge_ingest | ingest_document | ❌ 否 |
| knowledge_search | search_knowledge | ❌ 否 |
| mcp_search | search_mcp | ❌ 否 |
| selfdev_request | selfdev 工具链 | ❌ 否 |

错误模式核对：
- Dispatcher 把 intent 交回 LLM 自行决定是否调工具：✅ 命中。
- Dispatcher 只在 action_cards 里伪造结果：`build_action_cards` 仅反映**实际**执行的记录，但“假成功回复”发生在更上游（LLM 自然语言），且 action_card 在工具未执行时为空 → 前端无 task_created 卡片，但 `reply` 仍说“已经安排上了”。
- Dispatcher 在工具失败后仍让 LLM 说成功：✅ 见第 8 节。
- Dispatcher 没有 tool_required 机制：✅ 命中（字段不存在）。

需新增/确认字段（BLOCKER 2）：

```json
{
  "tool_call_policy": "must_call | may_call | never_call",
  "tool_required": true,
  "success_requires_tool_result": true
}
```

对 reminder 创建，当 `tool_call_policy=must_call` 且没有 tool_result 时，最终回复**不得**说“已经安排上了”。

---

## 5. Tool Calling 是否是真结构化调用

**结论：当前是“伪结构化”标签调用，但已做基础清洗。存在历史遗留风险，按现状评估为“已实现清洗但无强制、无回填校验”，不单列为新的 BLOCKER，但需要对齐到统一 ToolExecutor（BLOCKER 3）。**

- `是否使用 LangGraph ToolNode 或统一 ToolExecutor`：**否 LangGraph ToolNode**；使用的是自研 `ToolExecutor.extract_and_execute`（`tool_executor.py`）解析 `<tool_call>` 文本标签。这不是原生 function-calling / ToolNode。
- `是否使用结构化 tool call 对象`：**否**，是文本标签 `<tool_call>{json}</tool_call>`（`context_builder.py:16`，`tool_executor.py:27`）。
- `是否把工具调用结果写入 LangGraph state`：LangGraph 在此项目中只是单节点壳（`graph.py:85-89`，只有 `agent_loop` 一个 node），state 实为 `AgentLoop` 局部变量，`ToolExecutor.build_result_context` 把结果拼成文本塞回 `current_messages`（`agent_loop.py:124-129`）。
- `是否把 tool_result 回填给 LLM`：**是**，通过 `build_result_context` 注入 system 消息（`agent_loop.py:125-129`）。
- `是否过滤/拦截 malformed tool tag`：**是**，`detect_malformed_tags` + `clean_text` 会剥离 `<tool_cost>`、`<too_call>`、未闭合标签等（`tool_executor.py:105-133`）。

错误模式核对：
- LLM 输出 `<tool_call>` 文本：✅ 当前生产路径仍依赖此。
- 后端解析 XML：`TOOL_CALL_PATTERN` 正则解析（`tool_executor.py:27`）。
- 解析失败后原样返回用户：`clean_text` 会剥离，正常路径不泄漏；**但** `run_stream` 在达到 `MAX_REACT_STEPS` 时返回的是 **raw `reply`**（含可能未清洗的标签）而非 `cleaned_text`（`agent_loop.py:278-288`，`"reply": reply`），且当存在有效 tool call 时**根本不向用户流式输出清洗后的文本**——这是一个潜在的标签泄漏点。
- 模型连续输出多个 tool_call 文本：`extract_tool_calls` 支持多段（DOTALL），但 `MAX_CONSECUTIVE_IDENTICAL=1` 仅去重“完全相同”的调用（`tool_executor.py:38`）。
- tool_result 没有进入下一轮上下文：❌ 否，有回填（见上）。

**标记为需关注（非独立 BLOCKER，纳入 BLOCKER 3/4 修复）**：生产路径应使用原生结构化 tool call（function calling / ToolNode），而非文本标签；并且 `run_stream` 的 max-steps 分支必须返回 `cleaned_text` 而非 raw `reply`。

---

## 6. LangGraph 主路径

- `/api/chat` 是否所有请求都进入 LangGraph：**是**，`routes_chat.py:38` → `invoke_chat` → `graph.invoke`（`graph.py:93-108`）。
- 有没有某些 intent 走了旁路 if/else：**否**（在 `/api/chat` 路径）；但 `/api/chat/stream`（`routes_chat.py:45-81`）**完全绕过 LangGraph**，直接 `AgentLoop(client, db).run_stream(...)`，与 `/api/chat` 走的是**两套入口、同一 `AgentLoop`**。两条路径行为应保持一致，但代码重复、易漂移。
- 有没有某些异常 fallback 绕过 LangGraph：`invoke_chat` 外层 `try/except LLMClientError`（`routes_chat.py:39-41`），仅做 rollback + re-raise，未绕过主流程。
- 有没有直接调用 LLM client 的生产路径：**有**。`AgentLoop._agent_loop_node`（graph.py）、`run_stream`（routes_chat.py:54-59）、`task_worker._wake_agent_for_reminder`（task_worker.py:63-68）都**各自 new 一个 `LLMClient`** 并直接 `chat()`，没有统一的 client 工厂/配置收敛；且这些调用都未显式设 temperature。
- 有没有直接返回 LLM raw output 的路径：**有**，见第 5 节 `run_stream` 的 `MAX_REACT_STEPS` 分支返回 raw `reply`（`agent_loop.py:282`）。

正确要求（修复后应达成）：

```
/api/chat → graph.invoke → [intent node] → [action dispatch node] → [tool node] → [response node]
```

当前 LangGraph 只有一个 `agent_loop` 节点，未把 intent/dispatch/tool/response 拆分为独立、可观测、可强制的节点。建议修复时显式拆分（至少逻辑分层），并将 `temperature` 在入口统一收敛。

---

## 7. 上下文污染是否影响工具调用

**结论：存在上下文污染风险，且缺少 `verified_facts` vs `assistant_claims` 的分离。标记为需修复项（纳入 BLOCKER 4/5）。**

- `context snapshot 中是否注入了旧 assistant 假成功文本`：**是**。历史消息来自 `ThreadState.get_recent_messages`（`thread_state.py:24-42`），它把 `event_type in ("user_message","llm_response")` 全部作为 `role=user/assistant` 的 `content` 注入。**上一轮 assistant 说“已经安排上了”会被完整注入下一轮上下文**。
- `是否注入了 <tool_call> 原文`：正常路径 `clean_text` 已剥离；但 `run_stream` max-steps 返回 raw `reply` 时，若 raw 含未清洗标签，可能被写入 `llm_response` 事件再注入（需先修第 5 节泄漏点）。
- `是否注入了失败 tool call`：工具结果以 system 消息形式回填（`agent_loop.py:128`），属于“证据”，但模型可能误读。
- `是否注入了 superseded memory`：`_resolve_memories_for_context` 会分离 `excluded`（superseded）并标 `[EXCLUDED]`（`context_builder.py:158-163`），这点处理正确。
- `是否区分 assistant_claim 和 tool_verified_fact`：**否**。当前 `STABLE_PREFIX` 仅用自然语言要求模型“Do not claim persistent changes unless the tool succeeded”（`context_builder.py:11, 27, 51`），但**没有结构化的 `verified_facts` 区块**，模型无可靠事实源可依。

正确要求：
- 工具确认事实只能来自 `tool_result / event / database`；assistant 自然语言声明不能当作事实源。
- 需增加/确认结构化区块：`verified_facts`（来自 tool_result / events / DB）、`assistant_claims`（仅历史自然语言，标注不可作为事实）、`tool_results`、`events`。
- Context Builder **不得**把“assistant 说已经安排”当作 reminder 已创建的证据。

---

## 8. 异常是否被吞掉

**结论：工具异常本身被记录，但“失败后继续自然语言回复成功”未被阻止。标记为 BLOCKER 5 相关缺陷。**

`ToolExecutor.execute` → `registry.execute`（`registry.py:116-120`）用 `try/except Exception` 包裹 handler，失败返回 `{"ok": False, "error": str(e)}`，`ToolExecutor` 据此标记 `status="failed"` 并 `log_event("tool_failed", ...)`（`tool_executor.py:205-214`）。**异常没有被静默吞掉，有 `tool_failed` 事件**。但：

- 失败后，`agent_loop.py:124-129` 把 `failed` 的 tool_result 文本回填，然后提示 “Continue. Provide final response based on the tool results above.” —— **LLM 据此仍可能输出“已经安排上了”**，因为没有 `success_requires_tool_result` 闸门。
- `schedule_reminder` 失败时当前行为：返回 `{"ok": False}`，LLM 可被诱导说成功 ❌。
- `list_tasks` 失败时当前行为：handler 内 `TaskManager.list_all` 抛错 → `{"ok": False}`，LLM 可能回“没有提醒” ❌（也可能恰好“碰对”，但不可靠）。
- `ToolExecutor` 异常是否被吞：handler 级异常被 `registry.execute` 捕获并返回错误 dict（未向上抛），因此**调用方永远拿不到异常**，只能看到 `ok=False`。这避免了崩溃，但也让“失败”变成了“软失败”，容易被上层当成普通结果。
- 数据库写入失败是否被吞：`_db_handler` 装饰器（`builtin_tools.py:37-47`）`db.commit()` 在 handler 内；若 commit 抛错，异常会冒泡到 `registry.execute` 的 `except` → 返回 `{"ok": False, "error": ...}`，同样软失败。
- LLM JSON 解析失败是否被吞：`ActionPlanner` 解析失败 → `default_response()`（`should_execute=False`）→ 工具被 BLOCK，但**用户可能得到看似正常的自然语言回复**，且没有任何 `parse_error` 对用户可见的明确提示（仅落在 `tool_parsing_done` 事件）。

要求（应在 BLOCKER 中落实）：
- 若 `tool_call_policy=must_call`，工具失败后**不得**生成成功回复；必须返回明确失败原因；必须记录 `tool_error` 事件 / trace。

---

## 9. 状态过滤条件是否导致“看起来没调用”

**结论：创建/查询的状态字段本身一致，但存在 scope 与拼写问题，可能让“查询不到”被误判为“没调用”。**

- `tasks 创建时 status 是什么`：`TaskManager.create` **未显式设置 status**，由 DB 列默认值 `default="pending"` 决定（`models.py:278`）。即创建为 `pending`。✅ 与 `get_due`（`status=="pending"`）、`_get_tasks_context`（`status.in_(["pending","triggered"])`）一致。
- `list_tasks 查询哪些 status`：`_handle_list_tasks`（`builtin_tools.py:206-214`）→ `TaskManager.list_all(effective)`；当入参为空/"all"/"全部" 时 `effective=None` → **不带 status 过滤，返回全部**。`AgentLoop._get_tasks_context` 仅注入 `pending/triggered` 作为上下文，不影响查询工具本身。
- `创建时 user_id/thread_id 是什么`：**`Task` 表无 `user_id` 字段**（`models.py:273-286` 仅有 `thread_id`）！`schedule_reminder` 设置 `task.thread_id = _current_thread_id`（`builtin_tools.py:76`）。因此任务仅按 `thread_id` 隔离，**无用户级隔离**。
- `查询时 user_id/thread_id 是什么`：`list_tasks` **完全不带 thread_id / user_id 过滤**（`builtin_tools.py:206-213`），返回**全库所有线程**的任务。多用户/多会话场景下会串。
- `时区是否一致`：创建用 `datetime.now(timezone.utc)`（`builtin_tools.py:69`），查询 `get_due` 也用 UTC（`task_manager.py:38`）✅；但 `next_check_at` 与 `trigger_at`：**`Task` 模型只有 `next_check_at`，没有 `trigger_at` 字段**（无混用）✅。
- `cancel_task` 拼写 bug：`_handle_cancel_task` 把 status 设为 `"canceled"`（`builtin_tools.py:221`），而全项目其它地方用的是 `"cancelled"`（如记忆/过滤逻辑）。这是一个**真实拼写不一致**，会导致取消后状态不匹配某些查询/展示预期。⚠️ 建议修复时统一。

必须保证（修复方向）：
- 创建 reminder 写入 `status = active 或 pending` ✅ 已是 `pending`。
- `list_tasks` 默认应查 `active/pending/scheduled`；并**按当前用户/线程作用域过滤**（当前无 user 维度，至少应按 thread_id 或明确全局）。
- 同一线程/用户下可查到。

报告所需 SQL（用于人工核查）：

```sql
select id, title, message, task_type, status, trigger_at, next_check_at, user_id, thread_id, created_at
from tasks
order by created_at desc
limit 20;
```

> 注：`tasks` 表无 `message` / `user_id` / `trigger_at` 列（实际列为 `description` / 无 user_id / 仅 `next_check_at`）。核查时请用：

```sql
select id, title, description, task_type, status, next_check_at, thread_id, created_at
from tasks
order by created_at desc
limit 20;
```

---

## 10. 模型配置是否导致不稳定

| 调用点 | 模型 | temperature | 问题 |
|---|---|---|---|
| `ActionPlanner.plan`（intent/路由/参数抽取） | `aiive_llm_model`（默认模型） | **0.1（非 0）** | 非确定性；解析失败 → `should_execute=False` fallback |
| 主 agent LLM（`AgentLoop.run` 行 73） | `aiive_llm_model` | **未传（=模型默认，通常 0.7~1.0）** | 高方差；是否输出 `<tool_call>` 随机 |
| 主 agent LLM（`run_stream` 行 183） | `aiive_llm_model` | **未传** | 同上 |
| `MemoryExtractor.extract` | `aiive_llm_model` | 0.1 | 记忆抽取非确定性（次要） |
| `task_worker._wake_agent_for_reminder` | `aiive_llm_model` | **未传** | 提醒唤醒时是否调用 `remind_alert` 随机 |
| `ToolRegistry` 工具 handler 内部（如 `create_selfdev_plan`） | 各自 new 的 client | 各自默认 | 零散，未统一 |

- `是否每次请求模型不同`：默认都用 `aiive_llm_model`，未配置按 intent 切换模型。
- `是否 fallback 到不同 provider`：无多 provider 逻辑；`LLMClient` 仅单 base_url。
- `是否 JSON mode / structured output 被启用`：**未启用**。`ActionPlanner` 依赖 prompt 要求 “Output ONLY valid JSON” + 本地 `_json.loads` 解析（`action_planner.py:186-195`），无 `response_format=json_object` 等结构化输出保证，因此解析失败率非零（即 fallback 频率非零）。

要求（应落实）：
- intent / routing / permission / tool args extraction：**temperature=0 + 结构化 JSON schema + parse failure = safe fallback（明确失败，而非默认“不执行”）**。
- 主 final response：可以较自由，但**必须基于 tool_result**。

---

## 11. 修复要求（待确认后实施，本轮未改代码）

> 以下为**修复方案草案**，需用户在确认后由我实施。本轮仅审查，未改动源码。

### BLOCKER 1：must-call policy
对下列 intent 强制工具调用（在 Dispatcher 增加 `if should_execute and tool_required: execute` 强制分支，而非依赖 LLM 输出 tag）：
`task_create→schedule_reminder`、`task_list→list_tasks`、`task_cancel→cancel_task`、`memory_update→remember_or_update_memory`、`memory_query→search_memory/list_memories`、`delete_request→safe_delete`、`knowledge_ingest→ingest_document`、`knowledge_search→search_knowledge`、`mcp_search→search_mcp`、`selfdev_request→selfdev 工具链`。
当 `tool_call_policy=must_call` 且无 tool_result，最终回复不得说“已经安排上了”。

### BLOCKER 2：确定性 IntentResult
`ActionPlanner`/`AgentDecision` 增加字段 `tool_call_policy`、`tool_required`、`success_requires_tool_result`、`args`；`temperature` 改为 0；解析失败进入**明确失败/澄清**，而非 `should_execute=False` 的静默降级。输出示例见第 13 节。

### BLOCKER 3：统一 ToolExecutor
所有工具必经 `ToolRegistry → PermissionManager → ToolExecutor → Service → Event/Trace → ActionCard`；禁止 Chat handler 直接写库或伪造成功文本。建议将文本 `<tool_call>` 标签升级为原生 function calling / LangGraph ToolNode（至少统一入口与清洗）。

### BLOCKER 4：工具结果回填
工具执行后必须：写 trace、写 event、写 tool_result、进入 state、进入 final response node；final response 必须基于 tool_result。

### BLOCKER 5：禁止假成功
任务未写入 `tasks`、工具未返回 success、工具异常、tool_result 缺失、结构化 intent 解析失败时，**不得**回复成功；必须回复失败或澄清，并记录 `tool_error`。

### 附带修复
- `cancel_task` 拼写 `canceled` → `cancelled` 统一。
- `list_tasks` 增加 thread_id/user 作用域过滤（当前无 user 维度）。
- `run_stream` max-steps 分支返回 `cleaned_text` 而非 raw `reply`，杜绝 `<tool_call>` 泄漏。
- 统一 LLM client 工厂与 temperature 收敛（intent/tool 路径 temperature=0）。

---

## 12. 测试要求（待实施）

新增 targeted tests（位于 `tests/unit/backend/`，不跑全量回归）：

1. `test_reminder_create_must_call_tool_repeated`：重复 10 次 `1 分钟后提醒我 赫赫`，断言每次调用 `schedule_reminder`、`tasks` 表新增、ChatResponse 有 `task_created` action card、无一次仅自然语言成功。结束清理 tasks/events。
2. `test_task_list_must_call_tool_repeated`：先建一个 task，重复 10 次 `我有哪些提醒？`，断言每次调用 `list_tasks`、tool_result 含该 task、最终回复含该 task、无一次直接答“没有提醒”。
3. `test_tool_failure_cannot_fake_success`：让 `task_service.create_task()` 抛错，断言**不**回复“已经安排上了”、无 `task_created` action card、返回失败原因、记录 `tool_error`。
4. `test_hypothetical_should_not_execute`：输入 `如果我想让你一分钟后提醒我，你会调用哪个工具？`，断言 `execution_mode=explain_only`、`should_execute=false`、不建 task、不调 `schedule_reminder`。
5. `test_no_raw_tool_call_leak`：模拟模型输出 `<tool_call>...<tool_cost>`（注：用户示例中 `</tool_cost>` 为笔误，应为 `</tool_call>`），断言用户可见回复不含 `<tool_call>`、记录 `parse_error`、不重复调用工具。
6. `test_context_does_not_treat_assistant_claim_as_fact`：先制造“工具失败但 assistant 曾说已经安排上了”的旧记录，下一轮问 `我有哪些提醒？`，断言必须查 `tasks` 表、不依据 assistant 旧文本判断、tasks 为空才答无提醒。

---

## 13. 完成报告（待修复后输出）

修复完成后将输出 `docs/audit/intermittent_tool_calling_fix_report.md`，包含：
问题根因、涉及文件、修复后的 `Chat → LangGraph → Intent → Tool → Service → Result` 链路、`IntentResult` 示例、`tool_call` 示例、`tool_result` 示例、失败时行为、重复 10 次测试结果、测试命令、测试清理方式。

验收标准：
- 同一类执行请求不再随机有时调工具、有时不调工具。
- 必须调用工具的 intent 永远走工具。
- 工具失败时不再假装成功。
- 元问题/假设问题不会误执行。
- 用户可见回复中不再泄漏 `<tool_call>` 原文。

---

## 附录 A：涉及文件清单（本轮仅审查）

| 文件 | 角色 | 主要问题 |
|---|---|---|
| `backend/aiive/core/action_planner.py` | Intent 判定（LLM, temp=0.1, fallback 降级） | 非确定性；缺 `tool_call_policy` 等字段；解析失败静默降级 |
| `backend/aiive/runtime/agent_loop.py` | ReAct 主循环 = 实际 Dispatcher | 主 LLM 未设 temperature；无 must-call 强制；max-steps 泄漏 raw reply |
| `backend/aiive/runtime/tool_executor.py` | 工具解析/执行/清洗 | 决策仅用于 BLOCK，不用于 FORCE；无 `success_requires_tool_result` 闸门 |
| `backend/aiive/core/context_builder.py` | 系统提示/上下文拼装 | 无 `verified_facts`/`assistant_claims` 分离；prompt 把是否调用交给模型 |
| `backend/aiive/core/llm_client.py` | LLM 客户端 | 未启用 JSON mode/structured output；temperature 默认不传 |
| `backend/aiive/runtime/graph.py` | LangGraph 壳 | 单节点，未拆分 intent/dispatch/tool/response；state 实为局部变量 |
| `backend/aiive/api/routes_chat.py` | `/api/chat` 与 `/api/chat/stream` | 两条入口，stream 绕过 LangGraph |
| `backend/aiive/tools/builtin_tools.py` | 工具 handler | `cancel_task` 拼写 `canceled`；`list_tasks` 无作用域过滤 |
| `backend/aiive/tools/registry.py` | 工具注册/执行 | handler 异常被软失败（ok=False），调用方拿不到异常 |
| `backend/aiive/runtime/task_manager.py` | Task CRUD | `create` 未设 status（依赖默认 pending）；`list_all` 过滤逻辑 |
| `backend/aiive/db/models.py` | Task/Event 模型 | Task **无 user_id**；仅 `next_check_at` |
| `backend/aiive/worker/task_worker.py` | 提醒唤醒 | 各自 new LLMClient、未设 temperature，是否调 `remind_alert` 随机 |
| `backend/aiive/runtime/thread_state.py` | 历史消息 | assistant 假成功文本被原样注入下一轮上下文（污染） |
