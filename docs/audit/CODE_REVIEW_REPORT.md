# AIive 代码审查报告

> 审查范围：后端 `backend/aiive`（core / runtime / tools / worker / memory / api）及前端 `frontend/src` 关键文件。
> 审查方式：逐文件阅读 + 跨文件引用/死代码核实。所有结论均给出文件与行号。
> 时间：2026-07-09
>
> **历史快照提示**：§1~§8 的文件名与行号对应当日代码（含已删除的 `runtime/agent_loop.py`、
> `core/intent_classifier.py`、`runtime/context_builder.py`），仅作溯源用；当前落点见
> `docs/CODE_MAP.md` 与 `docs/audit/final_convergence_report.md`，修复结论见 §9。

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

---

## 7. 第一轮架构重构（2026-10-21）状态核销

> 复核范围：本报告第 2、3 节中「死代码 / 重复代码」与「不规范」类待修复项，结合当前代码（仓库 `dev` 分支，提交 `a049594` 之后的工作区状态）逐条核对全仓引用后结论如下。

| 原项 | 结论 | 证据 |
|------|------|------|
| 2.1 `intent_classifier.py` 整文件死代码 | **已解决（已删除）** | 全仓（含 `backend/aiive`、`tests`、`scripts`）仅 `tests/unit/backend/test_agent_graph.py:643` 以字符串形式断言 `AgentGraph` 源码不含 `intent_classifier`，无任何运行时 import；文件已不存在 |
| 2.2 重复意图常量（`intent_classifier.py` vs `memory_gate.py`） | **已解决** | 随 2.1 删除 `intent_classifier.py` 而消除，仅保留 `memory_gate.py` 一份 |
| 2.3 `MemoryGate.decide_str` 废弃方法 | **已解决（已移除）** | `backend/aiive/memory/memory_gate.py` 已无 `decide_str` 定义，全仓零引用 |
| 2.4 `runtime/graph.py` 单节点空壳 + 建议移除 langgraph 依赖 | **部分解决** | `runtime/graph.py` 已删除（全仓零引用）；但 `agent_graph.py` 现基于 LangGraph `StateGraph` 实现真正的图编排，故「移除 langgraph 依赖」不再适用 |
| 3.1 `open(file_path).read()` 无上下文管理器/编码 | **已解决** | `tools/builtin_files.py::_handle_read_text_file` 已使用 `with open(real_path, "r") as f:` |
| 3.4 重复「去 markdown 围栏」逻辑 | **已解决** | 已抽取为 `aiive.core.text_utils.strip_code_fence`（L17），被 `core/action_planner.py`、`memory/memory_extractor.py`、`mcp/capability_planner.py` 复用 |

> 注：第 1 节功能缺陷（intent_type / 工具失败误报 / 流式未清理文本等）与第 3.3、3.5、4.x 项不在本轮架构重构范围内，仍属待修复项，未重复处理。
> 本轮重构（第一阶段）仅做安全的死代码/重复代码清理与模块边界整理，未修改任何业务行为或接口契约。


---

## 8. 第一轮架构重构 · 最终验收总结（2026-10-21）

> 验收基线：仓库 dev 分支、HEAD 提交 a049594 之后的工作区。
> 全程未提交、未推送，用户既有未提交改动（tests/conftest.py 编码、docs/README.md、根 conftest.py、analyze_tests.py）已保留。

### 8.1 具体修改文件与模块迁移

**重构（Phase 2 / 3）**
- backend/aiive/tools/builtin_tools.py：由原约 1360 行单体，缩减为「注册与组合入口 + 共享机制」(_build_safety / _db_handler 装饰器 / _require_ctx / register_builtin_tools 统一注册 36 个工具)，并 re-export 10 个域模块的 handler。
- 新建 10 个域模块（仅实现 handler）：builtin_basic.py、builtin_reminders.py、builtin_memory_write.py、builtin_memory_recall.py、builtin_files.py、builtin_knowledge.py、builtin_web.py、builtin_mcp.py、builtin_selfdev.py、builtin_runtime_state.py。
- 共享机制 _build_safety / _db_handler / _require_ctx 保持定义在 builtin_tools.py（单一实现、域模块复用、不复制装饰器）；域模块通过 from aiive.tools.builtin_tools import _db_handler, _require_ctx 复用。曾尝试下沉到独立 _shared 叶子模块，但测试与 tests/support.py 会 monkeypatch aiive.tools.builtin_tools.SessionLocal 把会话重定向到隔离测试库，为保持该契约已回退（详见 CODE_MAP §10）。
- backend/aiive/runtime/agent_graph.py：提取 4 个共享方法(_prepare_execution / _extract_reply / _classify_memory_signal / _assemble_agent_result)，同步 _execute_graph 与流式 _execute_graph_stream 复用之；对外接口与 LangGraph 语义不变。

**死代码清理（Phase 4）**
- builtin_tools.py / agent_graph.py：移除拆分后遗留的死导入(os / uuid / datetime / timedelta / Path / Session / WorkingStateService / Callable 等)，grep 全文件确认无引用。

**文档（Phase 6）**
- docs/CODE_MAP.md（新增/更新）：§3.4 与 §3.6 同步新结构与 10 个域模块 re-export 表。
- docs/audit/CODE_REVIEW_REPORT.md：§7 核销 2.1–2.4/3.1/3.4；本文 §8 验收。

**测试基础设施（支撑诚实验证，不改业务）**
- 根 conftest.py（原 backend/conftest.py 归并）：统一 SQLite 文件库、AIIVE_MEMORY_VECTOR_ENABLED=false、暴露 db fixture，并对 tests/ 全层级生效。
- pyproject.toml [tool.pytest.ini_options]：全仓唯一测试入口 tests/，统一 addopts(-v --tb=short -ra --strict-markers)，删除 backend/pytest.ini 以消除双入口。
- backend/aiive/task_runtime/worktree.py：Windows UTF-8 容错修复（非 ASCII 路径下 GBK 解码崩溃）。
- tests/unit/backend/test_persistent_task_runtime.py：改用生产 _hash_result/_normalize_remote_path 校验契约（非 ASCII 临时路径）。
- tests/integration/__init__.py：补包 docstring。
- tests/unit/backend/test_event_logger.py：修复 stale mock（移除指向不存在的 get_tool_registry 的 no-op patch），保留真实断言。
- 未删除、未恢复任何测试用例：backend/tests/ → tests/integration/backend/ 的迁移由你完成；本轮逐一核对 28 个被删测试文件在新树下均有 1:1 对应位置（见 8.3）。
- backend/aiive/db/base.py：create_all() 显式导入全部模型模块（models / retention_models / forget_models）。原实现只导入 models，会漏建 forget_tombstones 等表（测试报 no such table），与函数文档「创建所有数据表」不符。
- backend/aiive/selfdev/patch_executor.py：trusted_core 拒绝分支的 operations_failed 由「回填原始 operations」改为带 reason 的诊断项；原实现让调用方拿到没有 reason 的入参副本，失败原因只存在于 issues。
- conftest.py（根）：新增 session 级 autouse fixture，为进程级 SQLite 测试库建表一次（单元/流程层中刻意不请求 db_session 的用例需要）。
- tests/conftest.py：db_session 建表前导入全部模型模块；并把按用例隔离的会话注册到 SessionLocal 分发器（tests/support.py 文档约定但此前未接线）。
- tests/integration/conftest.py：db 夹具由「每用例 drop_all+create_all」改为「会话级建表一次 + 每用例按子表→父表清空数据」，隔离语义不变（每用例仍是干净数据），集成层耗时由约 20 分钟降到约 30 秒。
- 经你授权（选项 A）修改的 4 个测试文件，均为过期或平台相关断言：
  - tests/unit/backend/test_identity_memory.py：Runtime Identity 只渲染 agent 侧字段与 relationship_style；user_display_name 由 Core Memory 投影提供，不重复渲染（依据 context_inspector.py:95-99 的既有设计注释）。
  - tests/unit/backend/test_builtin_mcp.py：路径断言由 POSIX 后缀匹配改为 Path 分段匹配（Windows 下 str(Path) 为反斜杠）。
  - tests/unit/backend/test_patch_executor.py：拒绝原因断言改为 trusted_core 的两类路径越界 reason 集合。
  - tests/unit/backend/test_v2_recall.py 与 test_tool_calling_runtime.py：未改用例内容，失败由测试基础设施修复消除（见 8.4）。

### 8.2 合并 / 删除的代码
- 删除：builtin_tools.py 内联的 36 个 handler 实现 → 迁移至 10 个域模块；agent_graph.py 内两处重复的 reply 抽取/结果组装逻辑 → 合并为共享方法。
- 无新模块充当「无意义转发层」：builtin_tools.py 的 re-export 是组合入口，register_builtin_tools 负责装配；共享机制(_db_handler/_require_ctx)下沉一处供域模块复用。
- 其他删除：agent_graph.py 拆分后遗留的死局部变量（turn_id / turn_record_id / execution_id，同步与流式各 3 个）、_assemble_agent_result 的未使用参数 message_source；仓库根一次性分析脚本 analyze_tests.py。

### 8.3 测试命令与结果
- 环境校正（关键）：此前 .venv 为 uv 复制的非可编辑安装，import aiive 解析到 .venv/Lib/site-packages/aiive（陈旧副本），导致早期测试并未执行重构代码。已执行 uv pip install -e . 建立可编辑安装，import aiive 现解析到 backend/aiive，python -m pytest 直接测试重构代码。
- 全量命令：source .venv/Scripts/activate && export AIIVE_MEMORY_VECTOR_ENABLED=false && python -m pytest（仓库根统一入口；测试统一组织于 tests/integration/backend/，可收集 857 个测试）。
- 最终实测（全部修复后，连续三次复跑结果一致）：857 collected → 857 passed / 0 failed / 0 errors。耗时 315.16s / 330.43s / 356.89s（5~6 分钟，同机波动）。分层实测：unit 532 passed（单独运行 362s）、integration 315 passed（28.61s）、flow 10 passed。
- 与基线对比：同环境修复前为 8 failed / 849 passed / 676.07s（task po7K9R）。本轮既把失败清零，也把全量耗时从 676s 降到 315s（集成层夹具由每用例重建表改为清数据），快于迁移说明中的 867 例旧统计口径。
- 修复前的 8 个失败（execution_context×3、builtin_mcp×1、identity_memory×2、patch_executor×2）与 3 个顺序依赖用例（v2_recall 1 + tool_calling_runtime 2）均已处置，处置方式见 8.4 第 1 条。
- 零回归证明（git stash 仅回退两个源文件至 HEAD、不改测试）：修复前同一失败集在 HEAD 下为 8 failed / 11 passed，失败 ID/错误签名与重构态完全一致 → 这些失败在重构前即存在，不是本轮引入；本轮随后在授权范围内全部处置。
- 回归发现与修复 2（自查）：拆分后 _prepare_execution 内部再次 Trace(trace_id=...) if trace_id else Trace.new()，而同步/流式调用方各自也创建了一个 Trace；当 exec_ctx.trace_id 为空时会生成两个不同的 uuid，导致图内事件（trace 表）与 AgentGraphResult.trace_id 指向不同链路。已改为由调用方创建并传入 trace （_prepare_execution 增加 trace 参数、删除内部重复创建），Trace.new() 调用点由 4 处回归为 2 处、与 HEAD 一致；临时脚本实测 trace_id 为空时 graph/run_ctx/result 三者 id 相同。
- 回归发现与修复 1：首次以重构代码实跑（eOvZzp）出现 6 errors + 1 failed，根因为移除 builtin_tools.SessionLocal 破坏测试与 tests/support.py 的 monkeypatch 契约。已回退 _shared 抽取、将 _build_safety/_db_handler/_require_ctx 归位 builtin_tools，复跑 test_tool_operation_execution + test_v2_recall → 16 passed，全量归零 errors。
- 工具注册契约：import + register_builtin_tools 成功注册 36 个工具，与重构前一致。
- 分层用例数（实测 collect）：unit 532 + integration 315 + flow 10 = 857。迁移说明中的 867 = 857 + 10，多出的 10 个是 tests/unit/backend 下两个待删旧副本（test_turn_execution.py / test_reminder_end_to_end.py）的重复计数；这两个文件现已删除、权威副本在 tests/flow/，故唯一用例数为 857。
- 迁移说明第 7 节列出的 12 项 SessionLocal AttributeError（test_v2_recall、test_builtin_mcp、test_identity_memory×2、test_patch_executor×2 + 6 个 setup error）已在生产侧修复：SessionLocal 与 _db_handler 保留在同一 builtin_tools 命名空间、域模块从 builtin_tools 取共享机制，全量运行 0 errors，test_v2_recall 的写入用例通过（tests/support.py 分发器无需改动）。
- 迁移说明第 7 节记录的 test_tool_calling_runtime 2 例「走真实 AgentGraph 卡死」未再复现：全量运行与单文件运行均无挂死（本轮全量无超时中断）。
- 测试树一致性：backend/tests 已按你的重组迁至 tests/integration/backend/（25 个 test_*.py + _util.py + fixtures/，1:1 迁移，无重复）；git status 中 backend/tests/* 显示 deleted 是该迁移的预期结果，未再恢复或删除任何测试文件。

### 8.4 尚存风险
1. 原有 11 例失败已全部处置（本轮，经你授权选项 A），处置后全量为 0 failed / 0 errors：
   - test_execution_context_source_propagation×3：生产侧把 assembled_ctx.tool_registry 的读取改为 getattr(..., None)。AssembledContext.tool_registry 的默认值即 None，「属性缺失」与「显式 None」语义一致；真实调用方（context_assembler 总会传值）行为不变。
   - test_builtin_mcp×1：测试断言改为 Path 分段匹配，不再依赖 POSIX 分隔符（Windows 下 build_launch_spec 传的是 str(Path) 反斜杠形式，源码保持原样）。
   - test_identity_memory×2：测试对齐现状。Runtime Identity 有意只渲染 agent_runtime_id / agent_display_name / relationship_style，user_display_name 由 Core Memory 投影提供（context_inspector.py:95-99 记录为避免同源重复而移除）。
   - test_patch_executor×2：生产侧 operations_failed 统一返回带 reason 的诊断项（原实现回填的是没有 reason 的入参副本）；测试断言改为 trusted_core 的两类路径越界 reason 集合，仍要求必须给出明确拒绝原因。
   - 用例隔离×3（test_v2_recall 1 例 + test_tool_calling_runtime 2 例）：测试基础设施修复——db_session 会话注册到 SessionLocal 分发器、根 conftest 为进程库建表、create_all 导入全部模型模块（forget_tombstones 缺失的真因）；这三处用例内容未改动。
   处置原则：生产契约缺失（patch_executor 诊断字段、create_all 漏建表）在生产侧修；测试断言过期或平台假设（identity / builtin_mcp）经你授权对齐测试；不放宽断言制造通过（patch_executor 仍断言明确 reason，仅把写死的单一字符串改为受限集合）。
2. 静态检查已实际执行（本轮安装并运行，未关闭或放宽任何规则）：
   - ruff 0.17.1：全量 59 issues。已修真实缺陷 2 个 F821（builtin_runtime_state.py 缺 WorkingStateService 导入，_handle_update_working_state 运行期会 NameError；agent_graph.py 缺 Callable 导入，L69 引号注解运行期求值会 NameError）、15 个 I001、1 个 UP035、6 个 F841（拆分后遗留的死局部变量 turn_id / turn_record_id / execution_id，同步与流式各 3 个）。剩余为既有风格项：UP037 引号注解、UP017 datetime.UTC、B023 闭包捕获循环变量、BLE001 宽泛 except、B010 setattr 常量、C408 dict() 字面量、agent_graph.py 第三方导入块 I001；HEAD 基线同两文件为 29 issues，同类规则均已存在，本轮未新增规则类问题（UP037 由 9 → 21 是因为新增的 4 个共享方法沿用了该文件既有的引号注解风格）。
   - basedpyright 1.40.2（CI 实际使用的类型检查器）：全仓 5 errors / 67 warnings。其中 backend/aiive/task_runtime/worktree.py 的 CompletedProcess 缺类型参数由本轮改动引入，已修为 CompletedProcess[str]（该文件现 0 errors）。剩余 4 个 error 全部位于本轮未触碰的文件：forget/visibility_service.py:189/214/257、worker/handlers_forget.py:1502。重构涉及的 backend/aiive/tools 与 runtime/agent_graph.py 单独检查为 0 errors；pyproject 中既有 [tool.basedpyright] 配置未改动。
   - 67 warnings 主体是跨模块 re-export 私有 handler 触发的 reportPrivateUsage / reportUnusedFunction（域模块拆分的直接结果）。二者不影响 CI 判定（CI 只看 error 退出码），且改名会破坏 tests 对 _handle_* 的既有引用，故本轮保留并记录。
3. 测试基础设施改动（本轮新增，已由全量回归验证）：
   - db_session 现在把隔离会话注册给 SessionLocal 分发器，被测代码内部自开的 SessionLocal() 也落到该用例临时库（此前回落到进程级库）；
   - 根 conftest 为进程级库建表一次，供刻意不请求 db_session 的用例使用；
   - 集成层 db 夹具改为「会话级建表 + 用例级清空数据」，隔离语义不变，集成层 315 例耗时由约 20 分钟降到 28.6s。
   - 顺序依赖问题随之消失：单独运行 test_v2_recall.py + test_tool_calling_runtime.py 由 3 failed / 18 passed 变为 21 passed。
4. PG 特有语义未覆盖：测试用 SQLite 关闭向量、开启 FK；memory_write_service.py 的 PG 事务/锁语义未在本环境回归（见 8.5）。
### 8.5 建议的下一轮目标
- 原有 11 例失败已在本轮清零（见 8.4 第 1 条），下一轮无需重复处理；建议补一条 CI 断言：单元层与集成层分别单文件运行也必须全绿，防止顺序依赖回归。
- 把本轮已可运行的 ruff 0.17.1 与 basedpyright 1.40.2 纳入 CI（本轮未改 CI 检查项以免扩大改动范围），并清零剩余 lint 项与 4 个既有类型错误（forget/visibility_service.py、worker/handlers_forget.py）。
- 集成层已改为「建表一次 + 清数据」，若后续出现需要重建 schema 的用例（如迁移校验），应在该用例内显式 drop/create，避免回退到每用例 DDL。
- 审慎评估 memory_write_service.py（本轮判定为内聚事务服务，零自开会话、13 回归测试通过，但 SQLite 未覆盖 PG 事务语义；若有可独立测试的边界再重写）。
- 循环依赖复核：静态 import 图扫描（CODE_MAP §10）确认本轮未引入加载期环；域模块→builtin_tools 复用共享机制为惰性环（共享符号先于 re-export 定义，import 期安全）；残留 3 个 SCC 均为既有惰性环（runtime.agent_graph↔context_assembler、worker.handlers_forget↔outbox_handlers、control/worker/registry 集群），加载与运行正常，修复需侵入式改动，列为下一轮。可补充 import 顺序的自动化校验。


---

## 9. 第二轮（2026-10-22）：CI 门禁、顺序依赖防线、类型告警清零

> 承接 §8.5「建议的下一轮目标」第 1、2 条。范围仍是 dev 分支工作区，未提交、未推送。

### 9.1 本轮完成

1. **CI 接入 ruff**：`.github/workflows/ci.yml` 新增 `ruff` job，执行 `ruff check backend/aiive --select F,E9`（只把真实缺陷规则设为门禁，风格项不阻断）。
2. **CI 增加顺序依赖防线**：新增 `scripts/check_single_file_tests.sh`，对每个 `test_*.py` 单独起进程运行（根 conftest 用 PID 命名 SQLite 库，进程互不干扰），CI 以 `JOBS=2` 调用；任一文件单独运行失败即视为顺序依赖回归。
3. **basedpyright 告警清零（66 → 0）**：
   - 10 个领域模块的 handler 由私有名 `_handle_*` 改为公开名 `handle_*`，`builtin_tools` 以私有别名 `_handle_* as ...` re-export，tests 与 selfdev 的既有引用不需要改动；
   - 共享机制 `_db_handler` / `_require_ctx` 同步改为 `db_handler` / `require_ctx`（SessionLocal 仍与 `db_handler` 同处 `builtin_tools` 命名空间，monkeypatch 契约不变）；
   - 删除 8 条已失效的 `# pyright: ignore`（mcp 导入 3 条、task_runtime 事件监听 4 条中实际 4 条 + builtin_server 1 条）；
   - `retrieval/retrieval_index.py::next_index_version` 的 `func.max` 空表判断按 `int | None` 收窄，消除「int 与 None 无交集」的恒真比较告警（运行期语义不变：空表返回 1）。
4. **文档同步**：`README.md` 内置工具段（同时修正「共享机制下沉 tools/_shared.py」的错误描述——该方案在第一轮已回退）、`docs/CODE_MAP.md` §8.2/§10.5/§10.6、`runtime/thread_bootstrap.py` 与 `tools/tool_validation.py` 的 docstring。

### 9.2 实测（本机，2026-10-22）

```
ruff check backend/aiive --select F,E9        → All checks passed（0 error）
basedpyright（全仓）                            → 0 errors, 0 warnings, 0 notes
python -m pytest -q --strict-markers          → 857 passed, 0 failed, 408.41s
bash scripts/check_single_file_tests.sh       → 91/91 个测试文件单独运行通过
```

- 改名后先跑工具相关 5 个文件（registry / builtin_mcp / tool_calling_runtime / notification_api / v2_recall）→ 52 passed，再跑全量确认零回归。
- 单文件运行覆盖全部 91 个文件（unit 63 + integration 26 + flow 2），此前只有 `test_v2_recall` 与 `test_tool_calling_runtime` 两个抽样点。

### 9.3 未做与风险

- **CI 已在真实 GitHub Actions 上跑通**（run 39725637725，8/8 job 绿）。推送后依次修掉：basedpyright job 缺项目依赖、
  docker-smoke 的国内镜像跨境超时、启动迁移与 CI 迁移并发撞车、非 loopback 下默认开发令牌被 Settings 拒绝。
  细节见 `docs/reports/2026-10-22_第二轮架构重构阶段总结.md` §3.5。
- **PG 语义回归仍未覆盖**：`memory_write_service.py` 的事务/锁/幂等需要真实 PostgreSQL；本机无 PG 环境，且测试文件归用户维护，本轮未新增 PG 用例。
- **3 个惰性循环依赖未拆**（CODE_MAP §10.3）：修复需侵入 control/worker/registry 契约，仍列为后续项。
- 单文件 job 会把 CI 时长增加约 6 分钟（JOBS=2）；若后续变慢，可提高到 `JOBS=4` 或改为按层抽样。
