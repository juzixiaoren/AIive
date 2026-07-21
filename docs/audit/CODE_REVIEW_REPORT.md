# AIive 代码审查报告

> 审查范围：后端 `backend/aiive`（core / runtime / tools / worker / memory / api）及前端 `frontend/src` 关键文件。
> 审查方式：逐文件阅读 + 跨文件引用/死代码核实。所有结论均给出文件与行号。
> 时间：2026-07-09

---

## 0. 总览

| 类别 | 数量 | 代表问题 |
|------|------|----------|
| 功能缺陷 / Bug | 5 | `intent_type` 恒为 `plain_chat`、工具失败被当作成功、流式 max-steps 返回未清理文本 |
| 死代码 / 重复代码 | 5 | 整个 `intent_classifier.py` 未被引用、LangGraph 单节点空壳、重复常量 |
| 硬编码 / 不规范 | 5 | `open().read()` 无上下文管理器、`pr._manager` 私有访问、无确认清空全部记忆 |
| 性能 / 体验可优化 | 5 | 每步 ReAct 都重跑 Planner、逐字符流式、通知页不刷新 |

---

## 1. 功能缺陷（应优先修复）

### 1.1 `intent_type` 返回值恒为 `"plain_chat"`（Bug）
`backend/aiive/runtime/agent_loop.py:526`
```python
"intent_type": meta.get("intent_type", "plain_chat"),
```
`meta` 来自 `self._last_ctx_meta`（由 `ContextBuilder.build` 返回的 meta 字典），该字典**不含** `intent_type` 字段（见 `context_builder.py:395-413`）。真正的意图类型保存在 `self._last_intent` 中。因此无论用户输入什么，API 与前端拿到的 `intent_type` 永远是 `"plain_chat"`。
**建议**：改为 `self._last_intent.get("intent_type", "normal_chat")`。

### 1.2 工具执行失败被误报为成功（Bug）
`backend/aiive/tools/registry.py:116-118`
```python
result = reg.handler(**params)
return {"ok": True, "result": result}
```
`ToolExecutor.execute` 据此判定状态：`status="completed" if result.get("ok") else "failed"`（`tool_executor.py:208`）。但 `result` 是 `execute` 的返回 `{"ok": True, "result": <handler 返回值>}`，其顶层 `ok` **永远为 True**（除非 handler 抛异常）。于是 handler 内部返回 `{"ok": False, "error": ...}`（如 `memory_gate rejected`、`Reminder not found`、`deleted_count` 等）的情况，都会被标记为 `completed`，并进入 `tool_results` 展示为成功。
**影响**：记忆被拒、提醒不存在等真实失败在前端显示为成功；`build_action_cards` 的 `blocked/error` 之外的失败态无法体现。
**建议**：`execute` 应透传 handler 的 `ok`，例如 `return {"ok": bool(result.get("ok", True)), "result": result}`；或在 `ToolExecutor.execute` 中判断嵌套 `result["result"].get("ok")`。

### 1.3 流式 max-steps 返回未清理文本（Bug）
`backend/aiive/runtime/agent_loop.py:278`
```python
result = self._finalize(reply, message, thread, trace, all_records, [])
```
非流式 `run()` 在收尾时使用 `cleaned_text`，而 `run_stream()` 在达到 `MAX_REACT_STEPS` 时直接使用原始 `reply`。若最后一次 LLM 输出仍含 `<tool_call>...</tool_call>`，这些标签会原样泄露给用户。
**建议**：统一使用 `cleaned_text`（或复用 `tool_executor.clean_text`）。

### 1.4 `done` 事件中 `tool_results` 在 max-steps 时恒为空（Bug）
`backend/aiive/runtime/agent_loop.py:285-287`
```python
"tool_results": [],
```
流式达到步数上限时，`done` 的 `tool_results` 被硬编码为 `[]`，但 `all_records` 中其实已有执行记录，与正常退出路径（使用 `all_records`）不一致。

### 1.5 `run()` 收尾用 `dir()` 判断变量是否存在（脆弱写法）
`backend/aiive/runtime/agent_loop.py:150`
```python
final_text = cleaned_text if 'cleaned_text' in dir() else reply
```
`cleaned_text` 由循环体内 `extract_and_execute` 赋值，依赖 `dir()` 探测局部变量，可读性差且脆弱。
**建议**：在循环前显式初始化 `cleaned_text = reply`，或在循环体外用 `nonlocal`/容器持有。

---

## 2. 死代码 / 重复代码

### 2.1 整个 `intent_classifier.py` 是死代码
- `class IntentClassifier`（`intent_classifier.py:292`）在后端**未被任何运行时代码实例化**（仅在该文件自身与文档中出现；`memory_gate.py:5,95` 只是注释提及）。
- 实际运行时意图判定走 `ActionPlanner`（`agent_loop.py:394`），`IntentClassifier`/`_classify_rules`/`_classify_llm` 全部未被调用。
- 文件头部注释自相矛盾：第 68-71 行声称 `_classify_rules` 已废弃、生产用 ActionPlanner，但整套关键词函数仍完整保留（约 200 行）。
**建议**：删除 `intent_classifier.py`，或若想保留规则快路径，应明确接入 `AgentLoop` 并在测试中引用。

### 2.2 重复的意图常量
`intent_classifier.py:46-61` 的 `MEMORY_ELIGIBLE_INTENT_TYPES` / `SCHEDULER_INTENT_TYPES` 与 `memory_gate.py:26-41` 的 `MEMORY_ELIGIBLE` / `SCHEDULER_INTENTS` 是同一组意图集合的两份拷贝（命名还不完全一致）。由于 2.1，前者的常量纯属死代码。
**建议**：保留 `memory_gate.py` 中一份，删除 `intent_classifier.py` 中的副本。

### 2.3 `MemoryGate.decide_str` 废弃方法
`backend/aiive/memory/memory_gate.py:236-244`
标注 `DEPRECATED` 但仍保留，且调用方已不存在（若仍有，应改用 `decide`）。属于死接口。
**建议**：确认无调用后删除。

### 2.4 LangGraph 包装是单节点空壳（过度设计）
`backend/aiive/runtime/graph.py`
- `get_graph` 构建的图只有 `START → agent_loop → END` 一个节点，等价于直接调用 `AgentLoop.run`。
- `DBCheckpointer.put` 是 `return {}` 的空实现（`graph.py:46-48`），`get_tuple` 读出的消息也未被 `AgentLoop` 复用（后者自行 `get_recent_messages`）。默认 `MemorySaver` 同样无实际作用。
- 引入 `langgraph` 依赖却未获得图编排/检查点价值。
**建议**：非流式入口 `routes_chat.py:chat` 直接调用 `AgentLoop.run`（与流式一致），移除 `graph.py` 与 `langgraph` 依赖；若确实需要检查点，再实现真正可用的 checkpointer。

### 2.5 `streaming` 的 `action_card` SSE 事件从未发出
`frontend/src/api/chat.ts:132-134` 在 switch 中处理了 `action_card` 事件，但 `agent_loop.run_stream` 只 yield 了 `tool_call / tool_result / token / done`，从不 yield `action_card`。该事件类型形同死代码。
**建议**：要么在流式过程中对 action_card 单独 yield（提升即时反馈），要么从前端移除该分支。

---

## 3. 硬编码 / 不规范

### 3.1 `open(file_path).read()` 无编码、无上下文管理器
`backend/aiive/tools/builtin_tools.py:432`
```python
content = open(file_path).read()
```
文件句柄未关闭（资源泄漏），且未指定 `encoding`，中文文档易抛 `UnicodeDecodeError`。
**建议**：`with open(file_path, encoding="utf-8") as f: content = f.read()`。

### 3.2 访问私有属性 `pr._manager`
`backend/aiive/tools/builtin_tools.py:485`
```python
active = pr._manager.get_active_slot()
```
直接依赖 `PromoteRollback` 的内部实现，重构时极易断裂。
**建议**：在 `PromoteRollback` 暴露公开方法（如 `get_active_slot()`），不要跨模块访问 `_manager`。

### 3.3 清空全部记忆无需确认（安全隐患）
`backend/aiive/tools/builtin_tools.py:296-326`
- `scope="all"` 直接删除所有 active 记忆，没有任何二次确认。
- 第 312-315 行的 `if reason.lower() not in (...): pass` 是**空操作死代码**，本意可能是做校验，实际什么都没做。
- `forget_memory` 在 `register_builtin_tools` 中 `risk_level="medium"` 且 `requires_confirmation=False`（`builtin_tools.py:522`），高危操作未要求确认。
**建议**：`scope="all"` 应要求 `requires_confirmation=True`；删除无意义的 `pass` 分支；对危险操作增加显式确认。

### 3.4 重复的「去 markdown 围栏」逻辑（且写法脆弱）
`backend/aiive/core/action_planner.py:188-193` 与 `backend/aiive/memory/memory_extractor.py:98-102` 存在**完全相同的**围栏剥离代码：
```python
for fence in ("```json", "```"):
    if raw.startswith(fence):
        raw = raw[len(fence):].strip()
    if raw.endswith("```"):
        raw = raw[:-3].strip()
```
逻辑能跑通但绕且重复；若模型返回首尾都有 ``` 的嵌套情况容易出错。
**建议**：抽取为共享工具函数（如 `strip_code_fence(text)`），用正则一次性处理 `^```[a-z]*\n?` 与尾随 ```` ``` ````。

### 3.5 默认数据库凭据硬编码
`backend/aiive/config.py:18`
```python
database_url: str = "postgresql+psycopg://aiive:aiive_dev@localhost:5432/aiive"
```
作为 pydantic 默认值可接受，但凭据明文写死在源码中。`.env` 的 `DATABASE_URL` 会覆盖它，建议保持默认值但避免提交真实口令；当前 `.env` 含明文真实 API Key（`sk-...`），虽已被 `.gitignore` 忽略，仍建议改用密钥管理/环境变量注入，避免误提交。

---

## 4. 性能与体验可优化

### 4.1 每个 ReAct 步骤都重跑 ActionPlanner（额外 LLM 调用）
`backend/aiive/runtime/agent_loop.py:394`（`_build_messages` 内调用 `self._action_planner.plan`），而 `_build_messages` 在每次循环迭代、以及每次重建上下文时都会执行（`agent_loop.py:127,133,261,266`）。一个多步任务可能产生数十次 Planner LLM 往返。
**建议**：一次用户轮次只调用一次 Planner，将 `decision` 缓存；后续步骤复用同一决策（ReAct 的工具执行本就基于同一意图）。

### 4.2 逐字符流式输出（体验差）
`backend/aiive/runtime/agent_loop.py:243-244`
```python
for char in cleaned_text:
    yield {"event": "token", "data": {"text": char}}
```
按字符推送 token，网络帧过多、前端渲染抖动。
**建议**：按词/子串切片（如按标点或固定长度分块），或直接复用 `LLMClient.chat_stream` 的流式 token 进行二次渲染。

### 4.3 通知页只挂载时拉取一次，无刷新
`frontend/src/pages/NotificationsPage.tsx:28-30`
仅在 `useEffect` 挂载时 `fetch` 一次，新提醒到达后页面不会更新，用户需手动刷新。
**建议**：轮询（如每 5–10s）或订阅 SSE/WebSocket；同时与 worker 的 10s 轮询节奏对齐。

### 4.4 确认/延时提醒走「自然语言 → LLM 自主决策调工具」（已修复）
设计正确，不应增加专用端点：前端点击「确认/延时」时自动向 Chat 发送自然语言（如 `确认提醒 <id>`、`延时提醒 <id> N 分钟`），由 LLM 通过 `ActionPlanner` 判定为 `tool_call` 并调用 `confirm_reminder` / `snooze_reminder` 工具。后端工具、schema 注入、ToolExecutor 均支持该路径。原审查中「建议增加专用端点」结论已推翻。

当前实现：
- **前端接线**：`ChatPage` 的提醒卡片渲染确认和延时按钮，由 `handleReminderAction` 统一处理。
- **调用路径**：按钮把隐藏系统指令发送到 `/api/chat/system`，仍由 LLM 决定调用 `confirm_reminder` / `snooze_reminder`，不绕过 ToolRegistry。
- **清理结果**：旧的 `confirmReminder` / `snoozeReminder` 前端包装函数已删除，避免普通聊天流与系统指令流两套约定并存。
- **可靠性**：`PLANNER_PROMPT` 包含 `确认提醒/延时提醒 → confirm_reminder/snooze_reminder` 示例，确保工具调用通过策略校验。

### 4.5 每次 outbox 任务都新建 LLMClient
`backend/aiive/worker/outbox_handlers.py:11-17`
`_get_llm_client()` 在每个 job 内 `LLMClient(...)` 新建实例（无连接池/复用）。
**建议**：提升到模块级单例或依赖注入，避免重复构造。

### 4.6 `scheduler_daemon` 静默吞异常 + 单进程风险
`backend/aiive/worker/scheduler_daemon.py:18-20`
```python
except Exception:
    pass
time.sleep(10)
```
- 异常被完全吞掉，调度失败无日志、无告警，难以排查。
- 守护线程方案依赖单进程常驻；多 worker 部署会重复轮询（虽幂等但浪费），进程重启期间错过的时间窗靠后续轮询自愈。
**建议**：至少记录 `logger.exception`；考虑用独立进程/celery 等托管定时任务，保证高可用与可观测。

---

## 5. 代码质量小项（可选）

- `agent_loop._resolve_memories_for_context` 与 `_get_runtime_identity` 各自重复读取 `get_active()`（`agent_loop.py:294-335`），可合并一次查询。
- `builtin_tools._build_safety` 先计算 `descriptor_hash` 再放入 `base`，但随后用 `k in fields` 过滤时 `descriptor_hash` 多半被丢弃，计算浪费（`builtin_tools.py:28-30`）。
- `llm_client.chat` 对 `data["choices"][0]` 直接索引，空 `choices` 时会抛 `IndexError` 被外层泛型 `except` 包成 `LLMClientError`，可给出更明确的错误原因（`llm_client.py:86`）。
- `routes_chat.chat` 捕获 `LLMClientError` 后 `raise` 但未转换为 HTTP 错误响应（FastAPI 会返回 500），建议显式返回 4xx/5xx 结构。

---

## 6. 修复优先级建议

| 优先级 | 项 | 风险 |
|--------|----|------|
| P0 | 1.2 工具失败误报成功 | 用户看到「成功」但实际未执行 |
| P0 | 1.1 intent_type 恒为 plain_chat | 前端/下游逻辑失真 |
| P1 | 3.3 清空全部记忆无确认 + 死代码 pass | 数据安全 |
| P1 | 1.3 / 1.4 流式未清理文本与空 tool_results | 标签泄露/信息缺失 |
| P1 | 4.4 确认/延时走自然语言 | 不可靠、脱钩会话 |
| P2 | 2.1/2.2/2.3/2.4 死代码清理 | 可维护性 |
| P2 | 4.1/4.2/4.3 体验与性能 | 使用体感 |
| P3 | 3.1/3.2/3.4/3.5/4.5/4.6 规范与健壮性 | 长期质量 |

> 说明：本报告仅针对静态代码审查。运行期行为（如通知是否真实触发、ReAct 实际收敛）需结合集成测试与日志进一步验证；其中 scheduler/reminder 已采用「DB Task + worker 轮询」的真实调度，未发现 `time.sleep`/前端 `setTimeout` 冒充定时器的违规实现。
