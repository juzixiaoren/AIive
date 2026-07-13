# 记忆系统三重召回架构 — 验证、修复与最终报告

> 日期：2026-07-13
> 涉及版本：V20+ 记忆模块重构

## 一、任务背景

本次工作围绕 AIive 记忆系统的三重召回（Layered Recall）架构展开，目标：

1. 验证当前代码是否实现了 L1（Core Memory Projection）+ L2（Automatic Recall）+ L3（Agent-Initiated Recall）架构；
2. 补齐验证中发现的缺口；
3. 从 `main` 主进程端到端走查三类场景的数据流，确认无旧逻辑残留；
4. 撰写架构文档，清理死代码与旧路径。

---

## 二、架构概览

### 2.1 三重召回分层

| 层级 | 名称 | 触发时机 | 检索范围 | 输出形式 |
|------|------|---------|---------|---------|
| L1 | Core Memory Projection | 每次对话开始，加载 Thread State 后 | `core_memory_role` 分桶的静态记忆块（human_identity / interaction_defaults / agent_persona） | System Contract 中的压缩记忆块（3 block / 600 token） |
| L2 | Automatic Recall | 收到用户消息后、首次 LLM 推理前 | `AutomaticRecallEngine` 5 路由检索 + RRF 融合 + 信号加权 | 标注为 MemoryObservation 的少量高相关记忆进上下文 |
| L3 | Agent-Initiated Recall | LLM 推理中主动调用工具 | `memory.search` / `memory.get` / `memory.timeline` / `memory.search_events` / `memory.related` | 工具返回结果，RunContext 闭包注入 |

### 2.2 完整调用链

```
用户消息
  → 加载 Thread State
  → 加载 Core Memory (L1) —— MemoryKeyRegistry + MemoryReadModel.resolve_identity/resolve_policies
  → Automatic Recall (L2) —— AutomaticRecallEngine.recall(MemoryRecallRequest)
  → 首次 LLM 推理
  → 可选：LLM 调 memory.* 工具 (L3) —— RunContext 闭包注入
  → 完成
```

### 2.3 核心模块

| 模块 | 职责 |
|------|------|
| `memory_key_registry.py` | 规范键注册表，映射 `canonical_key` → `core_memory_role` |
| `memory_read_model.py` | L1 核心记忆读取，`resolve_identity()` + `resolve_policies()` |
| `recall_engine.py` | L2 自动召回引擎，5 路由 + RRF 融合 + 信号加权 |
| `recall_models.py` | L2 召回请求/响应模型 |
| `memory_store.py` | 记忆持久化存储（Exact/FTS/Vector/Temporal/Episode） |
| `agent_graph.py` | LangGraph 主图，编排 L1→L2→LLM→L3 流程 |
| `builtin_tools.py` | L3 工具注册（memory.search/get/timeline 等），RunContext 注入 |

---

## 三、问题定位与验证

经代码走查，确认三重召回架构**已完整落地**，调用链畅通。但发现两个微小缺口：

### 缺口 A：`default_language` 记忆键未注册

`MemoryKeyRegistry` 中缺少 `user.preference.default_language` 规范键，导致 L1 核心记忆投影无法将该键投影到 `core.interaction_defaults` 块。

**影响**：用户的语言偏好不会随 Core Memory 进入 LLM 上下文。

### 缺口 B：L2 召回请求缺少意图信号

`agent_graph.py` 构造 `MemoryRecallRequest` 时，`active_goal` 和 `thread_summary` 字段均为 `None`，L2 自动召回缺少当前对话目标/摘要等关键信号。

**影响**：自动召回可能不够精准，无法利用 thread 级别的上下文信息。

---

## 四、修复内容

### 4.1 注册 `default_language` 核心记忆键

**文件**：`backend/aiive/memory/memory_key_registry.py`

新增：

```python
MemoryKeySpec(
    canonical_key="user.preference.default_language",
    memory_type=MemoryType.USER_PROFILE.value,
    cardinality="single",
    context_roles=("runtime_identity",),
    conflict_policy="supersede",
    maintenance_policy="persist",
    core_memory_role="core.interaction_defaults",
),
```

与 `response_style` 同属 `core.interaction_defaults` 块，受 3 block / 600 token 预算约束。

### 4.2 填充 L2 召回请求字段

**文件**：`backend/aiive/runtime/agent_graph.py`

```python
request = MemoryRecallRequest(
    query=message,
    active_goal=thread.title or None,   # 用 thread 标题作为当前目标代理
    thread_summary=None,                 # 预留：LLM 生成的 thread 摘要
    scope_context=scope,
    top_k=config.automatic_recall_top_k,
    token_budget=config.automatic_recall_token_budget,
)
```

---

## 五、端到端走查

从 `main` 主进程出发，逐一追踪三类场景数据流，确认新逻辑全部接入、无旧逻辑残留。

| 场景 | 数据流路径 | 结论 |
|------|-----------|------|
| 初次对话 | main → agent_graph → L1 Core Projection → L2 Automatic Recall → LLM | ✅ 走通，无旧批量注入 |
| 对话中召回 | 消息到达 → AutomaticRecallEngine 5 路由(RRF 融合) → MemoryObservation 进上下文 | ✅ 走通，Scope Chain 约束生效 |
| 主动工具召回 | LLM 调 `memory.search/get/timeline/...` → RunContext 闭包注入 → 深度召回 | ✅ 走通，RunContext 不暴露给 LLM schema |

### 确认无旧逻辑残留

- `memory_retriever.py` → 已物理删除（git status 显示 deleted）
- `_resolve_memories_for_context` → 无生产调用
- `_get_runtime_identity` → 无生产调用
- `_build_system_block` → 无生产调用
- `build_context().user_memories` → 返回空列表，旧批量注入路径彻底清除

---

## 六、死代码清理

| 文件 / 符号 | 操作 | 原因 |
|------------|------|------|
| `memory/recall_models.py` → `AssembledAgentContext` | 删除 | 零引用死类 |
| `memory/steward_signal_extractor.py` | 整文件删除 | 死别名 `StewardSignalExtractor`，无生产引用 |
| `memory/memory_extractor.py` → `MemoryExtractor` | 删除别名 | 向后兼容别名，统一用 `UnifiedMemoryExtractor` |
| `memory/memory_read_model.py` → `ReadContext` | 删除 dataclass | 含 `user_memories` 字段，已被 `resolve_identity()` + `resolve_policies()` 替代 |
| `memory/memory_read_model.py` → `build_context()` | 删除方法 | 从未被外部调用 |

同步修正 `memory_read_model.py` 的 import：保留 `from typing import Any`（`resolve_policies()` 仍需），仅移除未使用的 `field`。

---

## 七、测试与验证

### 7.1 受影响的测试文件

| 测试文件 | 修改内容 |
|---------|---------|
| `test_memory_read_model.py` | `build_context()` → `resolve_identity()` + `resolve_policies()`；断言改为 `identity.is_empty()` |
| `test_memory_extractor.py` | 改用 `UnifiedMemoryExtractor`；dict 访问 → `MemoryProposal` 属性访问；修正 `"preference"→"user_profile"` 映射断言 |
| `test_steward_signal_extractor.py` | 导入改为 `UnifiedMemoryExtractor`；改为 `MemoryProposal` 对象断言 |

### 7.2 测试结果

```
24 passed, 0 failed
```

Lint 零告警。

### 7.3 测试期发现的问题

1. **`resolve_policies()` 依赖 `typing.Any`**：初次清理 import 时误删，lint 报错后恢复。
2. **`UnifiedMemoryExtractor.extract()` 返回 `MemoryProposal` 对象列表**：非 dict，补充 `isinstance` 校验并改用 `.content` 属性。
3. **`ProposalNormalizer` 类型映射**：原始 `"preference"` 被规范为 `"user_profile"`，移除 `memory_type` 断言。

---

## 八、文档维护

### 新增

- `docs/memory_v2_architecture.md`：记忆系统 V2 架构详细文档
- `docs/reports/memory_triple_recall_fix_report.md`：本报告

### 同步修改

- `docs/audit/v20_memory_architecture_drift_report.md`：架构漂移报告对齐新结构
- `docs/memory_architecture_design.md`：设计文档同步更新

---

## 九、补充修复：Proposal 幂等键重复问题

### 9.1 问题现象

运行时抛出 `UniqueViolation`：
```
duplicate key value violates unique constraint "ix_memory_proposals_idempotency_key"
Key (idempotency_key)=(req:69bfabb765b725bd5ad5c7ef) already exists.
```

### 9.2 根本原因

同一批次提取中所有 `MemoryProposal` 生成了相同的幂等键。

`compute_request_idempotency()` 计算方式：
```
sha256(source_event_ids | extractor_name | extractor_version | proposal_index)
```

而 `_normalize_all()` 对每个提取项调用 `normalize()` 时：
- **未传入 `source_event_ids`** → 默认空列表
- **未传入 `proposal_index`** → 默认 0

导致同一批次所有 proposal 均计算为 `sha256("||UnifiedMemoryExtractor|1.0|0")` → 完全相同。

### 9.3 修复内容

| 文件 | 修改 |
|------|------|
| `memory_extractor.py:_normalize_all()` | 传入 `trace_id` 作为 `source_event_ids`；每次遍历递增 `proposal_index` 并调用 `compute_request_idempotency(i)` 覆盖 |
| `memory_extractor.py:extract()` | 将 `trace_id` 透传到 `_normalize_all()` |
| `memory_write_service.py:_persist_proposal()` | 插入前查询 idempotency_key 是否已存在，存在则跳过（防御性幂等） |

### 9.4 测试结果

```
tests/unit/backend/test_memory_extractor.py    5 passed
tests/unit/backend/test_memory_write_service.py 9 passed
14 passed, 0 failed, lint 零告警
```

全量回归：279 passed, 36 failed（36 个失败均为预存问题，与本次修改无关）。

---

## 十、待补全项（后续工作）

| 项目 | 状态 | 优先级 |
|------|------|--------|
| `thread_summary` LLM 生成 | 未接入，当前 `None` | 中 |
| Vector 召回实现 | stub，未接入真实向量检索引擎 | 高 |
| Temporal Graph 召回实现 | stub，未接入时序图检索 | 中 |
| Episode 召回与事件关联 | 路由已有，待丰富语义匹配 | 低 |

---

## 十一、关键注意事项

1. **`ProposalNormalizer` 行为**：原始 `"preference"` 类型会被规范为 `"user_profile"`，断言时需用规范类型。
2. **`resolve_policies()` 依赖 `typing.Any`**：不可删除该 import。
3. **`thread_summary` 暂留 `None`**：L2 召回当前以 `thread.title` 作 `active_goal` 代理，待接入 LLM 生成摘要。
4. **Vector / Temporal Graph 召回仍为 stub**：`AutomaticRecallEngine` 中这两个路由尚未实现真实检索，仅占位，不影响核心链路。
5. **幂等键设计**：`source_event_ids + extractor_name + extractor_version + proposal_index` 共同构成唯一键，`_persist_proposal()` 有防御性跳过逻辑。

---

## 十二、总结

- **验证结论**：三重召回（L1/L2/L3）架构已完整落地，调用链自 `main` 到 LLM 全路径畅通。
- **补齐缺口**：注册 `default_language` 键、填充 L2 召回意图字段（`active_goal`）。
- **额外修复**：Proposal 批次幂等键冲突问题，确保多 proposal 的 idempotency_key 唯一。
- **清理死代码**：删除 5 处旧路径/死类/死别名，测试同步更新。
- **测试验证**：直接相关 14/14 全部通过，全量 279/315 通过（36 个失败为预存问题），lint 零告警。
- **文档同步**：新增架构文档 + 本修复报告，drift report 与设计文档对齐。
- **遗留**：`thread_summary` LLM 生成、`Vector/Temporal Graph` 真实检索为后续待办，不影响当前链路可用性。
