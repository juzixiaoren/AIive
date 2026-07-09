# V20 ActionPlanner 重构审计报告

> 生成时间: 2026-07-09
> 审计范围: agent_loop / intent_classifier / memory_gate / context_builder / builtin_tools / registry / dispatcher

---

## 一、`_infer_intent()` 调用点

| 调用位置 | 文件 | 行号 | 触发频率 |
|----------|------|------|----------|
| `run()` 首次 | agent_loop.py | 60 | 每轮对话 1 次 |
| `run()` ReAct 循环 | agent_loop.py | 92 | 每个 ReAct step 1 次 |
| `run_stream()` 首次 | agent_loop.py | 111 | 流式对话 1 次 |
| `run_stream()` 循环 | agent_loop.py | 158 | 每个流式 step 1 次 |

**关键问题**: `_infer_intent()` 创建的 `IntentClassifier()` **不传入 `llm_client`**（第 275 行），导致 LLM 分类路径永远不会执行。规则路径返回 `None` 后直接进入 `model_decide` 回退。因此当前架构实际上是**规则优先 + 无声降级**，并非语义理解。

---

## 二、关键词分类表位置

### 2.1 `intent_classifier.py` `_classify_rules()`（生产路径）

| 类别 | 行号 | 内容 |
|------|------|------|
| 假设问题 | 82-98 | `"如果" + "怎么做"` `"调用哪个工具"` `"如果我想"` 等 |
| 解释询问 | 100-107 | `"能不能告诉/介绍/解释/说明"` |
| 清空/遗忘 | 114-130 | `"清空记忆" "忘掉" "忘记" "forget"` 等 17 项 |
| Agent 改名 | 132-143 | `"以后你叫" "你改名叫"` 等 |
| 用户称呼 | 145-155 | `"以后叫我" "称呼我"` 等 |
| 真实姓名 | 157-167 | `"我的真实姓名" "我真名叫"` |
| 提醒 | 169-178 | `"提醒" + 时间词` `"一分钟后"` |
| 例行 | 180-190 | `"每天" + "提醒"` |
| 偏好 | 192-207 | `"我喜欢" + "你" + "回答/说话"` |
| 记忆命令 | 208-218 | `"记住" "别忘了" "记下"` |
| 礼貌请求 | 220-230 | `"能不能" + 动作词 → execute` |
| 用户自称 | 232-241 | `"我叫" → user_identity_update` |
| 执行触发 | 243-251 | `"现在" "立即" "马上" "帮我" "删除"` |
| 实现咨询 | 256-263 | `"怎么实现" "如何实现"` |
| 定义咨询 | 265-272 | `"是什么意思" "什么意思"` |

### 2.2 `agent_loop.py`（已废弃，死代码）

| 列表 | 行号 | 状态 |
|------|------|------|
| `_EXPLAIN_HYPOTHETICAL_PATTERNS` | 293-299 | **死代码**：不被 `_infer_intent` 调用 |
| `_EXPLAIN_KEYWORDS` | 303-306 | **死代码** |
| `_POLITE_REQUEST_ACTION_VERBS` | 310-313 | **死代码** |
| `_CLEAR_FORGET_PATTERNS` | 349-355 | **死代码** |
| `_EXECUTE_COMMAND_MAP` | 358-373 | **死代码** |

### 2.3 `memory_gate.py`

| 列表 | 行号 | 内容 |
|------|------|------|
| `_EXPLICIT_MEMORY_COMMANDS` | 28-35 | 23 项关键词 |
| `_QUESTION_PATTERNS` | 53-55 | 7 项 |
| `_TASK_CONTEXT_PATTERNS` | 59 | 6 项 |
| `_ROUTINE_INDICATORS` | 64-66 | 5 项 |
| `_DANGER_PATTERNS` | 71-74 | 2 项 |

**关键**: `decide(content, user_message)` 直接读取 `user_message.lower()` 做关键词匹配，**不理解语义**。

---

## 三、`forget_memory` 调用链

1. `builtin_tools.py` 第 123-129 行：`_handle_forget_memory(db, memory_id, reason)`
2. 只接受 `memory_id` 参数，**不支持 `scope=all`**
3. 直接调用 `MemoryMaintenance(db).forget(memory_id, reason)`

---

## 四、`remember_or_update` 调用链

1. `builtin_tools.py` 第 104-120 行：`_handle_remember_or_update(db, content, memory_type, memory_key)`
2. 若 `memory_key` 存在且匹配已有 active → `supersede`
3. 否则 → `create`

---

## 五、Dispatcher 现状

1. `agent_action_dispatcher.py` 第 5-33 行：硬编码 `INTENT_TOOL_MAP` 将意图映射到工具名
2. **没有** AgentDecision 模型
3. **没有** 机械执行规则（execution_mode 检查、confirmation 检查）
4. `ToolRegistry.execute()` 有 `requires_confirmation` 检查（第 108-114 行），但未集成到统一的分发流程

---

## 六、PermissionManager 现状

- 位于 `tools/permission_manager.py`
- 提供 `check(tool, params, decision)` 接口
- 需要 `AgentDecision` 作为输入

---

## 七、LLM 调用现状

| 组件 | 是否使用 LLM | 说明 |
|------|-------------|------|
| `_infer_intent()` | **否** | `IntentClassifier(llm_client=None)` |
| `AgentLoop.run()` | 是 | `self._llm_client.chat(messages)` |
| `MemoryExtractor` | 是 | `self._llm_client.chat(extraction_prompt)` |
| `StewardSignalExtractor` | 是 | `self._llm_client.chat(steward_prompt)` |

---

## 八、ContextBuilder STABLE_PREFIX 现状

- 第 20-36 行：已包含 `model_decide` 说明
- 第 31-33 行：`model_decide / unknown / execute allow side-effect tools`
- 第 26-30 行：`explain_only / dry_run forbid side-effect tools`
- **但** Prompt 仍写的是参考 Intent Result block，而非 AgentDecision

---

## 九、本次需删除/废弃/替换的代码

### 需删除

| 位置 | 内容 | 原因 |
|------|------|------|
| `agent_loop.py` L293-411 | 6 个关键词列表 + `_is_explain_question` + `_detect_execute_command` | 死代码，已被 IntentClassifier 替代 |
| `memory_gate.py` L28-74 | 5 个关键词列表 | 应由 AdmissionPolicy 替代 |
| `memory_gate.py` `decide()` | 旧方法 | 应由 `decide_v2` 替代 |

### 需废弃（加 DEPRECATED 标记）

| 位置 | 内容 | 替代方案 |
|------|------|----------|
| `agent_loop.py` L272-285 | `_infer_intent()` | ActionPlanner.plan() |
| `intent_classifier.py` L68-275 | `_classify_rules()` 全部 | ActionPlanner LLM structured output |

### 需新增

| 位置 | 内容 |
|------|------|
| `core/action_planner.py` | `ActionPlanner` + `AgentDecision` |
| `memory/memory_gate.py` | `MemoryAdmissionInput` + `MemoryAdmissionDecision` + `decide_v2()` |
| `memory/memory_write_service.py` | `MemoryWriteService` |
| `memory/memory_extractor.py` | `ExtractedMemory` pydantic model |
| `tools/builtin_tools.py` | `forget_memory` 支持 `scope=all` |

---

## 十、虚假通过的测试

| 测试 | 问题 |
|------|------|
| `test_intent_inference.py` 全部 22 个 | 测试的是 `IntentClassifier` 的规则路径，未涉及 LLM 路径 |
| `test_memory_gate.py` 全部 18 个 | 测试的是旧 `decide()` API，该 API 已不再被 AgentLoop 调用 |
| `test_reminder_end_to_end.py` 2 个 | Worker 轮询未产生通知事件 |
| `test_notification_api.py` 1 个 | 同上 |

---

## 十一、剩余风险（预判）

| 风险 | 等级 | 说明 |
|------|------|------|
| LLM structured output 不一致 | BLOCKER | 需要 robust 的 JSON 解析和重试 |
| MemoryGate 旧测试全部失效 | MAJOR | 需重写 18 个测试 |
| forget_memory scope 变更影响存量 | MINOR | 旧调用只有 memory_id，向后兼容即可 |
| ActionPlanner 延迟增加 | MINOR | 多一次 LLM 调用，约 0.5-2s |
