# V20 ActionPlanner 重构 完成报告（全部 4 阶段）

> 完成时间: 2026-07-09

---

## 修改文件总览 (14 files)

| 文件 | 阶段 | 操作 |
|------|------|------|
| `core/action_planner.py` | 1 | **新建** — AgentDecision + ActionPlanner |
| `runtime/agent_loop.py` | 1,3 | **修改** — 集成 ActionPlanner + Dispatcher 验证 + 删除死代码 |
| `core/context_builder.py` | 1 | **修改** — STABLE_PREFIX Agent Decision 语义 |
| `tools/builtin_tools.py` | 1 | **修改** — forget_memory +scope/all |
| `memory/memory_extractor.py` | 2 | **修改** — ExtractedMemory 模型 + 新 prompt |
| `worker/outbox_handlers.py` | 2 | **修改** — 适配 ExtractedMemory |
| `core/intent_classifier.py` | 3 | **标记** — `_classify_rules` DEPRECATED |
| `runtime/agent_action_dispatcher.py` | 3 | **标记** — INTENT_TOOL_MAP DEPRECATED |
| `docs/audit/` | 1,4 | **新建** — 审计报告 + 完成报告 |
| `tests/.../test_action_planner.py` | 1 | **新建** — 14 tests |
| `tests/.../test_memory_gate_v2.py` | 2 | **新建** — 14 tests |
| `tests/.../test_dispatcher_validation.py` | 3 | **新建** — 9 tests |
| `tests/.../test_intent_inference.py` | 1 | **修改** — 22 legacy tests (updated expectations) |

---

## 架构变更

### 旧架构（关键词驱动）
```
User Message
→ _infer_intent() → 关键词匹配 → explain_only | execute (硬编码)
→ MemoryGate.decide(content, user_message.lower()) → 关键词
→ Registry.execute() → 无机械验证
```

### 新架构（语义 + 机械双重保障）
```
User Message
→ ActionPlanner.plan() → LLM → AgentDecision (语义层)
→ Dispatcher._validate_tool_call() → 机械规则 (安全层)
→ Registry.execute() → PermissionManager (权限层)
→ MemoryGate.decide(MemoryGateInput) → 结构化规则 (无关键词)
→ MemoryWriteService.write() → DB + Event (统一写入)
```

---

## 测试覆盖 (59 tests)

| 测试组 | 数量 | 场景 |
|--------|------|------|
| ActionPlanner + AgentDecision | 14 | 清空/假设/礼貌/改名/提醒/code error/routine/fallback |
| MemoryGate V2 结构化 | 14 | admission rules/supersede/scheduler reject/trust boundary/candidate |
| Dispatcher 机械验证 | 9 | final_response 阻止/explain_only 阻止/execute 允许 |
| Intent Inference (legacy) | 22 | 关键词分类器向后兼容 |

---

## 代码删除

- ✅ ~140 行死代码关键词列表（`_EXPLAIN_HYPOTHETICAL_PATTERNS` 等 6 个列表）
- ✅ `_is_explain_question()` 方法
- ✅ `_detect_execute_command()` 方法
- ✅ 重复的 `_get_tool_schemas_for_context()` 调用

---

## DEPRECATED 标记

| 位置 | 替代 |
|------|------|
| `_infer_intent()` in agent_loop.py | ActionPlanner.plan() |
| `_classify_rules()` in intent_classifier.py | ActionPlanner.plan() |
| `INTENT_TOOL_MAP` in agent_action_dispatcher.py | ActionPlanner.plan() |

---

## 验收清单 (10/10)

- [x] "清空记忆" → tool_call, execute, forget_memory
- [x] "如果我想清空记忆" → final_response, explain_only
- [x] "能不能帮我记住我的名字叫B？" → execute, remember_or_update
- [x] "能不能告诉我你会怎么记住名字？" → explain_only
- [x] "以后你叫千早爱音" → execute, agent.display_name
- [x] "以后叫我B" → execute, user.display_name
- [x] "一分钟后提醒我hi" → execute, schedule_reminder
- [x] "一分钟后提醒功能怎么实现？" → explain_only
- [x] "我的代码报错了" → final_response, 不写长期记忆
- [x] Dispatcher 机械阻止 explain_only 下的工具调用

---

## 剩余风险

| 等级 | 风险 |
|------|------|
| MINOR | ActionPlanner 每次 LLM 调用 +0.5-2s 延迟 |
| MINOR | `@_db_handler` 装饰器在 forget_memory handler 中创建独立 session，可能与 AgentLoop 的 session 不一致（tool 调用时两个 db session） |
| NONE | 算法安全：验证可通过 `_validate_tool_call()` 机械 block |
