# V20 Memory Architecture Drift Report

> ⚠️ 本文为历史审计记录。所列 BLOCKER/MAJOR 问题已在终局重构中修复，
> 当前架构与实现以 `docs/memory_architecture_design.md`（v2.0，已与代码核对）为准。
> 本报告的"目标架构"即现在的实际实现。

> 审计时间: 2026-07-09  
> 审计范围: MemoryGate, MemoryExtractor, StewardSignalExtractor, MemoryStore, remember_or_update tool, outbox_handlers, agent_loop, context_builder

---

## 一、总体结论

**当前 MemoryGate 是伪 V20。** 虽然使用了 `dataclass` 封装输入输出，但核心决策逻辑仍然是 5 张硬编码关键词表驱动的。命名上叫 `decide_v2()`，实际上决策方式与 V1 无本质区别——只是把 `if kw in msg_lower: return "active"` 加了几个条件分支。

**不符合 AIive 最终架构。** AIive 需要的是：Intent Classifier → structured extraction → admission policy（纯结构化字段决策）→ MemoryWriteService。当前架构缺失 Intent Classifier、缺失 MemoryWriteService，MemoryGate 越权承担了意图判断、问题类型分类、任务调度等职责。

---

## 二、逐项审查

### ✅ 通过项

| # | 检查项 | 状态 | 备注 |
|---|--------|------|------|
| — | `decide_v2()` 接受结构化 `MemoryGateInput` | ✅ | dataclass 定义正确 |
| — | `MemoryGateDecision` 返回结构化决策 | ✅ | 含 decision/update_mode/supersede_memory_ids/reason |
| — | `ContextBuilder` 接收 `resolved_memories`/`excluded_memories` | ✅ | v2 build() 已支持 |
| — | `ContextSnapshot` meta 记录 `injected_memory_ids`/`excluded_memory_ids` | ✅ | _finalize() 写入 |
| — | `MemoryStore.supersede()` 生成正确 lineage | ✅ | revision_num+1, superseded_by 回写 |
| — | `AgentLoop` 传递 intent 到 outbox payload | ✅ | 最近修改已加入 |

### ❌ 未通过项（BLOCKER）

#### B1. MemoryGate 仍依赖 5 张关键词表做决策

**位置**: `memory_gate.py:45-79`

```
_EXPLICIT_MEMORY_COMMANDS (15 phrases)
_QUESTION_PATTERNS (14 phrases)
_TASK_CONTEXT_PATTERNS (13 phrases)
_STRONG_PREFERENCE (9 phrases)
_ROUTINE_INDICATORS (12 phrases)
```

**当前行为**: `decide_v2()` 中 Rule 2-6 全部使用 `user_message.lower()` + `in` 遍历这些关键词表来决定 active/candidate/reject。

**违反规则**: "禁止关键词表直接决定 active memory"。MemoryGate 不得负责理解自然语言句式。这些短语应作为 Extractor 的弱特征，不应作为 Gate 的决策依据。

**严重度**: BLOCKER

#### B2. `source == "tool_call"` 直接 bypass gate

**位置**: `memory_gate.py:90-106`

```python
if inp.source == "tool_call":
    return MemoryGateDecision(decision="active", ...)
```

**当前行为**: 只要 source 是 `tool_call`，一律返回 active，不检查 intent、不检查 execution_mode、不检查 trust boundary。

**违反规则**: "source == 'tool_call' 不得 bypass gate。工具调用不是绝对可信来源。必须经过 IntentClassifierGate、PermissionManager、MemoryAdmissionPolicy。"

**场景**: 如果 LLM 错误地在 explain_only 场景下调用了 remember_or_update（例如被 untrusted external content 诱导），当前代码会直接写入 active memory。

**严重度**: BLOCKER

#### B3. 旧接口 `decide(content, user_message)` 仍存在且未标记 deprecated

**位置**: `memory_gate.py:209-212`

```python
def decide(self, content: str, user_message: str) -> str:
    """Backward-compatible wrapper..."""
```

**当前行为**: 接口可调用，注释中未标记 `@deprecated`。好在 `outbox_handlers.py` 已经改为调用 `decide_v2()`。

**违反规则**: "可以临时保留用于旧测试，但必须标记 deprecated，并确保生产路径不再调用。"

**严重度**: MAJOR（生产路径未调用，但未标记）

#### B4. `user_message.lower() + in` 判断决定 active memory

**位置**: `memory_gate.py:116-184` (Rules 2-6)

```python
msg_lower = inp.user_message.lower()
for pat in _QUESTION_PATTERNS:
    if pat.lower() in msg_lower: ...
for pat in _TASK_CONTEXT_PATTERNS:
    if pat.lower() in msg_lower: ...
for cmd in _EXPLICIT_MEMORY_COMMANDS:
    if cmd.lower() in msg_lower: ...
for pref in _STRONG_PREFERENCE:
    if pref.lower() in msg_lower: ...
for routine in _ROUTINE_INDICATORS:
    if routine.lower() in msg_lower: ...
```

**当前行为**: 核心决策依赖 `str.lower()` + `in` 关键词匹配。

**违反规则**: "MemoryGate 只做 admission policy，即根据结构化输入决定。自然语言理解必须由 Intent Classifier / Memory Extractor 负责。"

**严重度**: BLOCKER

#### B5. routine/schedule 被 MemoryGate 直接 active

**位置**: `memory_gate.py:176-184`

```python
for routine in _ROUTINE_INDICATORS:
    if routine.lower() in msg_lower:
        return MemoryGateDecision(decision="active", memory_type="schedule", ...)
```

**当前行为**: "每天早上提醒我喝水" → MemoryGate 直接返回 active schedule memory。

**违反规则**: "routine/schedule 不得直接进入 active memory。'每天早上提醒我喝水'应交给 Scheduler/TaskService。Memory 最多记录低优先级 steward signal。"

**严重度**: BLOCKER

#### B6. "叫我"/"call me" 解析为 `user.name` 而非 `user.display_name`

**位置**: `memory_gate.py:228-230`

```python
if "我叫" in msg_lower or "我的名字" in msg_lower or "call me" in msg_lower:
    inp.extracted_memory_key = "user.name"
```

**当前行为**: 所有 "叫我" / "call me" → `memory_key = user.name`

**违反规则**:
- "以后叫我 B" → 应为 `user.display_name`
- "我的真实姓名是李光悦" → 应为 `user.name`
- "以后你叫千早爱音" → 应为 `agent.display_name`（当前完全无法处理）

**严重度**: BLOCKER

#### B7. 未区分 `agent.display_name` 与 `user.display_name`

**位置**: 全局。`_resolve_key_and_type()` 只处理 `user.name`，没有 `agent.display_name`、`agent.persona.*`、`user.preference.*` 的解析。

**违反规则**: "必须区分用户希望被怎么称呼 (user.display_name)、用户真实姓名 (user.name)、Agent 显示名 (agent.display_name)、Agent persona。"

**严重度**: BLOCKER

#### B8. Question pattern 表代替 Intent Classifier

**位置**: `memory_gate.py:53-58, 115-122`

```python
_QUESTION_PATTERNS = ["如果", "怎么做", "怎么实现", ...]
```

**当前行为**: MemoryGate 用自己的 `_QUESTION_PATTERNS` 判断用户是否在提问，而非依赖 Intent Classifier 的 `execution_mode` 输出。

**违反规则**: "禁止用硬编码 question pattern 表判断用户是否在提问。Intent Classifier 输出 execution_mode=explain_only，MemoryGate 看到后 reject。"

**严重度**: BLOCKER

#### B9. 缺失 Intent Classifier

**位置**: 全局。当前 intent 由 `agent_loop.py:_infer_intent()` 用简单关键词匹配实现，不是独立模块。

**当前行为**:
```python
EXPLAIN_PATTERNS = ["如果.*你会怎么做", "能不能", "怎么实现", ...]
EXECUTE_PATTERNS = ["记住", "我叫", "提醒", "定时", "马上", "帮我", ...]
```

**违反规则**: "不能用 MemoryGate 的硬编码 patterns 判断用户是否在提问。必须有独立 Intent Classifier。"

**严重度**: BLOCKER

#### B10. 缺失 MemoryWriteService

**位置**: 全局。所有记忆写入直接走 `MemoryStore.create()` 或 `store.supersede()`。

**当前调用链**:
- `outbox_handlers.py`: `store.create()` → 无 event 日志（仅 log_event 通知）
- `builtin_tools.py`: `store.create()` / `store.supersede()` → 无 gate decision event
- 缺失: `memory.superseded` / `memory.rejected` / `memory_gate_decision` 事件

**违反规则**: "必须新增 MemoryWriteService，所有记忆写入都必须经过它。必须写 events: memory.created / memory.superseded / memory.rejected。任何代码不得直接写 memory_records 绕过 MemoryWriteService。"

**严重度**: MAJOR（DB 写入功能正常，但缺失事件/审计）

#### B11. Extractor 不接收 intent_result

**位置**: `memory_extractor.py:24-26`, `steward_signal_extractor.py:30-32`

```python
def extract(self, user_message: str, reply: str, trace_id: str | None = None)
```

**当前行为**: Extractor 只知道 user_message 和 reply，不知道 intent_type 和 execution_mode。

**违反规则**: "StewardSignalExtractor.extract() 必须接收 intent_result 和 source。execution_mode != execute 时不得生成 active signal。"

**严重度**: MAJOR

#### B12. StewardSignalExtractor 忽略 source trust boundary

**位置**: `steward_signal_extractor.py:30-32`

**当前行为**: extract() 不接收 source 参数，无法区分 trusted_user_message vs untrusted_external_content。assistant reply 可能与外部内容一同被当作事实来源。

**违反规则**: "source != trusted_user_message 时不得抽取 user_profile/preference/routine。assistant reply 只能作为上下文，不能作为事实来源。"

**严重度**: MAJOR

---

## 三、架构对比

### 当前架构（伪 V20）

```
User Message
  → AgentLoop._infer_intent() (简单关键词匹配)
  → AgentLoop._build_messages()
    → ContextBuilder.build() (注入 resolved/excluded memories)
  → LLM chat (可能调用 remember_or_update tool)
    → _handle_remember_or_update() → MemoryStore.create() [绕过 Gate]
  → _finalize()
    → outbox.enqueue(memory_extraction)
      → MemoryExtractor.extract() (产生候选)
      → MemoryGate.decide_v2() (关键词表决策) [仍在使用]
      → MemoryStore.create() [绕过 MemoryWriteService]
```

### 目标架构

```
User Message
  → Intent Classifier → IntentResult
  → Execution Intent Gate
  → ToolRegistry.execute() [如果 should_execute=true]
    → PermissionManager
    → MemoryAdmissionPolicy/MemoryGate
    → MemoryWriteService → MemoryStore (+ events)
  → ContextBuilder.resolve_for_context()
  → ContextSnapshot
```

---

## 四、调用链逐条分析

### 4.1 remember_or_update 工具路径

**文件**: `builtin_tools.py:104-120`

```python
def _handle_remember_or_update(db, content, memory_type="fact", memory_key=""):
    store = MemoryStore(db)
    if memory_key:
        for old in store.get_active():
            if old.memory_key == memory_key:
                new_rec = store.supersede(old.id, content, ...)
                return ...
    rec = store.create(content=content, lifecycle_state="active", ...)
```

**缺失检查**:
- ❌ 未检查 intent.execution_mode（可能在 explain_only 时被调用）
- ❌ 未经过 MemoryGate
- ❌ 未经过 MemoryWriteService
- ❌ 未写 memory_gate_decision event
- ❌ 未写 memory.created / memory.superseded event（仅靠 agent_loop event logger）
- ❌ 未检查 trust boundary（外部内容可能诱导 LLM 调用此工具）
- ✅ supersede 逻辑正确（memory_key 匹配 → store.supersede）

### 4.2 outbox auto-extraction 路径

**文件**: `outbox_handlers.py:21-54`

```python
def handle_memory_extraction(db, payload, trace_id):
    candidates = extractor.extract(user_message, reply, trace_id)
    for c in candidates:
        result = gate.decide_v2(MemoryGateInput(...))
        if result.decision == "reject": continue
        record = store.create(content=content, lifecycle_state=result.decision, ...)
```

**缺失检查**:
- ❌ Extractor 未接收 intent_result
- ❌ MemoryGate 仍使用关键词表决策（Rules 2-6）
- ❌ 未经过 MemoryWriteService
- ✅ 最近已传递 intent 到 payload
- ✅ 已使用 `decide_v2()` 而非旧 `decide()`

### 4.3 context resolve 路径

**文件**: `agent_loop.py:104-135` (`_resolve_memories_for_context`)

```python
def _resolve_memories_for_context(self):
    all_active = list(self._memory_store.resolve_for_context())
    # Group by memory_key; keep latest
    seen_keys = {}
    for mem in all_active:
        key = mem.memory_key
        if key and key in seen_keys:
            excluded.append({..., "exclusion_reason": "superseded"})
            seen_keys[key] = {...}
        elif key:
            seen_keys[key] = {...}
        else:
            resolved.append(...)
    resolved.extend(seen_keys.values())
    return resolved, excluded
```

**当前行为**: ✅ 正确按 memory_key 去重，排除旧记录

**缺失**:
- ❌ `resolve_for_context()` = `get_active()` — 不过滤 confidence、不检查 exclusivity
- ❌ 同 key 多 active 的根因未在写入时阻止（MemoryStore 不做 uniqueness check）

---

## 五、测试覆盖

**文件**: `tests/unit/backend/test_memory_gate.py`

**当前测试**: 18 个单元测试，均调用 `decide_v2()` 并断言 `decision`/`update_mode` 等字段。

**缺失**:
- ❌ 无端到端测试（不检查数据库、event、trace、context snapshot）
- ❌ 无 "explain_only 不写记忆" 的集成测试
- ❌ 无 "supersede 后 context snapshot 正确" 的集成测试
- ❌ 无 "reminder 不写 active memory" 的测试
- ❌ 无 "untrusted content blocked" 的测试
- ❌ 测试只断言 Python 对象，不检查任何持久化副作用

---

## 六、BLOCKER / MAJOR / MINOR 分类

### BLOCKER (8 项)

| ID | 问题 | 位置 |
|----|------|------|
| B1 | 5 张关键词表驱动 active memory 决策 | `memory_gate.py:45-79, 116-184` |
| B2 | `source == "tool_call"` bypass gate | `memory_gate.py:90-106` |
| B4 | `user_message.lower() + in` 核心决策 | `memory_gate.py:116-184` |
| B5 | Routine 直接 active 而非交 Scheduler | `memory_gate.py:176-184` |
| B6 | "叫我" → `user.name` 而非 `user.display_name` | `memory_gate.py:228-230` |
| B7 | 未区分 agent.display_name / user.display_name | `memory_gate.py:218-239` |
| B8 | Question pattern 表代替 Intent Classifier | `memory_gate.py:53-58, 115-122` |
| B9 | 缺失独立 Intent Classifier | `agent_loop.py:_infer_intent()` |

### MAJOR (4 项)

| ID | 问题 | 位置 |
|----|------|------|
| M1 | 缺失 MemoryWriteService | 全局：所有 write 直接走 MemoryStore |
| M2 | Extractor 不接收 intent_result/source | `memory_extractor.py:24-26`, `steward_signal_extractor.py:30-32` |
| M3 | 旧 `decide()` 接口未标记 deprecated | `memory_gate.py:209-212` |
| M4 | StewardSignalExtractor 忽略 trust boundary | `steward_signal_extractor.py:30-32` |

### MINOR (3 项)

| ID | 问题 | 位置 |
|----|------|------|
| N1 | `resolve_for_context()` 等于 `get_active()`，无 confidence 过滤 | `memory_store.py:88-90` |
| N2 | MemoryStore 不做同 key uniqueness check | `memory_store.py:13-43` |
| N3 | 测试无端到端覆盖 | `tests/unit/backend/test_memory_gate.py` |

---

## 七、修复路线

按 BLOCKER → MAJOR → MINOR 顺序，建议分 3 个阶段：

### Phase 1: BLOCKER 修复（核心架构）

1. **创建独立 Intent Classifier** (B9, B8)
   - 新建 `core/intent_classifier.py`
   - 输出 `IntentResult` dataclass
   - 合并现有 `_infer_intent()` 并增强（规则 + LLM structured output）
   - 覆盖: tool_choice_question, hypothetical_question, agent_identity_update, user_identity_update, reminder_create, routine_create, user_preference_update, normal_chat

2. **重写 MemoryGate** (B1, B2, B4, B5, B6, B7)
   - 删除所有 5 张关键词表
   - 删除 `source == "tool_call"` bypass
   - 删除 `user_message.lower() + in` 判断
   - 新增基于结构化字段的纯规则:
     - execution_mode != execute → reject
     - intent_type 不在允许列表 → reject
     - memory_key 为空 → candidate/reject
     - confidence < 0.7 → candidate
     - existing_memory 存在 + 同 key → supersede
   - 实现 stable memory_key resolver (user.display_name / agent.display_name / user.name / agent.persona.* / user.preference.*)

### Phase 2: MAJOR 修复（写入管线）

3. **创建 MemoryWriteService** (M1)
4. **更新 Extractor 接口** (M2, M4)
5. **标记旧接口 deprecated** (M3)

### Phase 3: MINOR + 测试

6. **增强 resolve_for_context** (N1)
7. **MemoryStore uniqueness check** (N2)
8. **端到端 targeted tests** (N3)

---

*报告结束。下一步：进入 Phase 1 BLOCKER 修复。*
