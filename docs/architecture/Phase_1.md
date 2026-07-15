# AIive Phase 1：有界上下文、Epoch 与 Segment

> 版本：v2（修订版）  
> 状态：方案确认，等待实施  
> 前置：Phase 0.5A（TurnRecord、turn_id、ContextBundle）已完成  
> 前置：Phase 0.5B（Outbox claim/lease/fencing、MemoryIngestionRun）已完成

---

## 目录

1. [目标与本阶段范围](#1-目标与本阶段范围)
2. [当前上下文调用链审计](#2-当前上下文调用链审计)
3. [绕过统一 ContextAssembler 的入口](#3-绕过统一-contextassembler-的入口)
4. [最终预算公式](#4-最终预算公式)
5. [完整请求整体计数流程](#5-完整请求整体计数流程)
6. [有界历史读取算法](#6-有界历史读取算法)
7. [TokenCounter 方案（LiteLLM）](#7-tokencounter-方案litellm)
8. [ContextBudget 数据结构](#8-contextbudget-数据结构)
9. [WorkingState Schema 与生产者](#9-workingstate-schema-与生产者)
10. [Epoch / Segment / Turn 数据关系](#10-epoch--segment--turn-数据关系)
11. [数据模型与迁移](#11-数据模型与迁移)
12. [Soft / Hard Threshold 时序](#12-soft--hard-threshold-时序)
13. [大工具结果引用方案](#13-大工具结果引用方案)
14. [ContextSnapshot 保留策略与并发](#14-contextsnapshot-保留策略与并发)
15. [系统事件与运行时事件](#15-系统事件与运行时事件)
16. [无界 Fallback 删除方案](#16-无界-fallback-删除方案)
17. [Phase 1 / Phase 3 边界](#17-phase-1--phase-3-边界)
18. [修改与新增文件清单](#18-修改与新增文件清单)
19. [旧数据兼容](#19-旧数据兼容)
20. [回滚方案](#20-回滚方案)
21. [测试矩阵](#21-测试矩阵)

---

## 1. 目标与本阶段范围

### 1.1 目标

让用户继续使用同一个永久逻辑会话，但每次模型输入、近期消息、WorkingState、工具结果和 ContextSnapshot 都严格有界。

### 1.2 本阶段范围

1. 审计当前流式与非流式上下文构建入口，找出所有绕过统一预算的路径
2. 引入 Epoch、Segment、SegmentSummary、EpochCheckpoint 的关系（仅数据模型）
3. 实现配置化 ContextBudget，覆盖 8 个分区
4. 实现基于 LiteLLM 的 provider-neutral TokenCounter
5. 实现唯一的 ContextAssembler（流式/非流式共享）
6. 建立有界 WorkingState
7. 实现 soft/hard threshold：调用模型前强制裁剪
8. 建立 Segment 状态机（open → sealing → sealed → failed）
9. 建立 Epoch 状态机（active → sealing → sealed → archived）
10. 工具大型输出引用化
11. 限制 ContextSnapshot 数量和容量
12. 兼容已有 Thread、TurnRecord 和 Event
13. 保持用户侧连续会话

### 1.3 本阶段不做

- LLM SegmentSummary 生成
- EpochCheckpoint 自动生成
- segment_sealing Outbox Handler
- Idle Compaction
- Daily Dream
- 自动遗忘
- 全量冷历史迁移
- Qdrant/FTS/KG 完整实现
- 第二个 Supervisor LLM
- 关键词或正则意图路由
- 扩写系统提示词
- 重做 Phase 0.5B Memory Ingestion

---

## 2. 当前上下文调用链审计

### 2.1 Phase 0.5A 主路径（生产中）

```
POST /api/chat
  └─ routes_chat.chat()
       └─ TurnExecutionService.execute_turn()
            ├─ Phase 1: _resolve_and_preempt()              [幂等/抢占]
            ├─ Phase 2: _load_context()
            │    └─ AgentGraph._load_context_only()
            │         ├─ _build_agent_context()              [Stable Contract + Core Memory + Automatic Recall]
            │         │    ├─ RecallConfig()
            │         │    ├─ MemoryReadModel.resolve_identity()
            │         │    ├─ load_core_memory(db, config)
            │         │    ├─ AutomaticRecallEngine.recall()
            │         │    ├─ _persist_recall_run()
            │         │    └─ assemble_system_content()
            │         ├─ thread_state.get_recent_messages(max_turns=20)
            │         └─ _snapshot_context()
            │              返回 _Ctx (匿名 dataclass)
            ├─ Phase 3: AgentGraph._execute_graph(ctx_bundle)
            │    ├─ _build_history_messages(history)
            │    ├─ initial_messages = [SystemMessage] + history + recall + [HumanMessage]
            │    ├─ compiled.invoke({"messages": initial_messages})
            │    ├─ _extract_tool_records()
            │    └─ _build_post_context_items()
            ├─ Phase 4: 记忆分类 (LLM，事务外)
            └─ Phase 5: _finalize_turn()                     [Fencing + Event + Snapshot + Outbox]
```

**关键发现：**
- `_load_context` 返回匿名 `_Ctx` dataclass（第 956 行），而非 `ContextBundle`（第 142 行死代码）
- `get_recent_messages` 默认 `max_turns=20`，仅以消息数量为预算，无 token 计数
- 上下文组装分散在 5 个位置，没有统一的 ContextAssembler

### 2.2 当前上下文总 token 无任何限制

`initial_messages` 直接传给 LangGraph 的 `compiled.invoke()`，没有任何 pre-flight token 检查。仅有的限制是：
- `max_turns=20`（消息数量）
- `top_k=8`（recall 数量）
- `core_memory_token_budget=600`（cores memory 内部预算）

### 2.3 当前 ContextSnapshot 问题

- 每 Turn 创建一个 ContextSnapshot
- `meta.full_contents` 包含完整的 system prompt、历史、recall 内容、工具结果
- 随历史累积线性增长，无数量上限
- 无并发轮换机制

---

## 3. 绕过统一 ContextAssembler 的入口

| 入口 | 文件:行 | 方式 | 影响 |
|------|--------|------|------|
| `AgentGraph.run()` | `agent_graph.py:1062` | 内联构建 initial_messages，调用 `_finalize` | 不经过 TurnExecutionService |
| `AgentGraph.run_stream()` | `agent_graph.py:1333` | 同上，且工具事件跟踪逻辑不同 | 不同步 |
| `AgentGraph.run_system()` | `agent_graph.py:1161` | 覆盖 system_content，内联构建 | 绕过 TurnRecord 幂等 |
| `AgentGraph.run_runtime_event()` | `agent_graph.py:1248` | RuntimeEvent 内联注入 | 绕过 TurnRecord 幂等 |
| `_load_context_only()` | `agent_graph.py:924` | 自己调用全套上下文构建逻辑 | 分散 |
| `_execute_graph()` else 分支 | `agent_graph.py:812-824` | ctx_bundle=None 时内联重建 | 死代码 |
| `_build_history_messages()` | `agent_graph.py:974` | `json.dumps` 完整工具结果放入 ToolMessage | 无界 |
| `POST /api/chat/system` | `routes_chat.py:75-84` | 直接 `AgentGraph.run_system()` | 旁路 |
| `TaskWorker._wake_agent` | `task_worker.py:150-158` | 直接 `AgentGraph.run_runtime_event()` | 旁路 |

**Phase 1 必须关闭所有入口，统一为单一 ContextAssembler。**

---

## 4. 最终预算公式（v3：统一 margin 模型）

### 4.1 唯一安全余量来源

所有估算误差统一由 `TokenCount.safe_tokens` 表达（LiteLLM 路径 `estimated * 1.10 + 500`，fallback 路径 `estimated * 1.30 + 1000`）。

**不存在独立的 `safety_margin` 分区。** 最终 Hard Gate 只使用：

```python
final_token_count.safe_tokens + requested_output_tokens <= context_window
```

### 4.2 核心公式

```python
@property
def hard_input_limit(self) -> int:
    """模型输入 token 硬上限（不含安全余量，余量在 TokenCount 内）。"""
    return self.model_context_window - self.reserved_output.hard_limit_tokens

@property
def soft_input_limit(self) -> int:
    return int(self.hard_input_limit * 0.80)
```

**reserved_output 永远先扣除**，不可被任何输入分区占用。

### 4.3 分区表（deepseek-chat, context_window=128000）

| 分区 | Soft Limit | Hard Limit | 优先级 | 说明 |
|------|-----------|-----------|--------|------|
| reserved_output | - | 4096 | - | 减除项，不可占用 |
| stable_contract | 3500 | 4000 | 1 | Kernel Contract + identity + policies |
| core_memory | 500 | 600 | 2 | Core Memory blocks 投影 |
| working_state | 1500 | 2000 | 3 | 结构化渲染文本 |
| tool_definitions | 4000 | 6000 | 4 | 工具 Schema |
| recent_messages | 85000 | 97000 | 5 | 近期 Turn 消息 |
| retrieved_memory | 1000 | 1200 | 6 | Recall pack |
| tool_results | 5000 | 8000 | 7 | 当前/历史工具结果 |

hard_input_limit = 128000 - 4096 = 123904

分区 hard 之和: 4000+600+2000+6000+97000+1200+8000 = 118800 ≤ 123904 ✓

### 4.4 启动时验证

```python
def validate(self) -> None:
    total = sum(p.hard_limit_tokens for p in self._partitions) + self.reserved_output.hard_limit_tokens
    if total > self.model_context_window:
        raise ValueError(f"Partition hard limits ({total}) exceed context window ({self.model_context_window})")
```

### 4.5 最终 Hard Gate

```python
final_safe = token_counter.count_messages(model, final_messages, final_tools).safe_tokens
if final_safe + requested_output_tokens <= context_window:
    return assembled_context  # ✓
# ✗ → 裁剪 → 重新整体计数 → 循环
```

不再存在第二套独立 margin 公式。

---

## 5. 完整请求整体计数流程

### 5.1 两阶段计数

**Phase I: 分区级计数** — 各分区独立计数，用于报告和裁剪决策。

**Phase II: 最终整体 Hard Gate** — 完成裁剪后，对最终 `messages + tools schema + 实际 provider 请求结构` 整体调用 `TokenCounter.count_messages()`。只有 `final_safe_tokens + requested_output_tokens <= context_window` 才放行。

### 5.2 整体计数与裁剪循环

```python
def assemble(self, ...) -> AssembledContext:
    for attempt in range(MAX_TRIM_ROUNDS):  # MAX_TRIM_ROUNDS = 5
        # ── 分区裁剪 ──
        result = self._build_and_trim_partitions(attempt)
        
        # ── Phase II: 最终整体 Hard Gate ──
        final_messages = result.messages
        final_tools = result.tools_schema
        
        total_count = self._token_counter.count_messages(
            model=self._profile.full_name,
            messages=final_messages,
            tools=final_tools,
        )
        
        final_safe = total_count.safe_tokens
        
        if final_safe + self._requested_output_tokens <= self._profile.context_window:
            return AssembledContext(
                messages=final_messages,
                tools=final_tools,
                partition_report=result.report,
                total_token_count=total_count,
                is_safe=True,
            )
        
        # 未通过 → 更激进裁剪
        if attempt == MAX_TRIM_ROUNDS - 1:
            raise ContextBudgetExceededError(
                safe_tokens=final_safe,
                context_window=self._profile.context_window,
                partition_report=result.report,
            )
        
        self._tighten_budget(attempt)
```

### 5.3 裁剪轮次策略

| 轮次 | 动作 |
|------|------|
| 0 | 初始分区预算，soft limit 裁剪 |
| 1 | 各分区 hard limit 裁剪；大 tool results → refs |
| 2 | recent_messages 从最早 Turn 整 Turn 删除 |
| 3 | tool_results 全部 refs |
| 4 | retrieved_memory 按 relevance_score 截断到 top-3；working_state 缩减 |

### 5.4 ContextBudgetExceededError

API 返回 HTTP 413：

```json
{
    "error": "context_budget_exceeded",
    "safe_tokens": 135000,
    "context_window": 128000,
    "hard_input_limit": 121900
}
```

---

## 6. 有界历史读取算法（v3：keyset pagination）

### 6.1 核心约束

- 使用 **keyset pagination**（`WHERE turn_sequence < $last ORDER BY turn_sequence DESC LIMIT $page_size`），禁止 OFFSET
- 任何二级保护触发后**立即停止整个读取循环**（不处理当前页剩余 Turn）
- **仅当前用户消息是强制保留项**；历史中不存在"无论如何必放"的 Turn
- 超大历史 Turn 先尝试工具结果引用化，仍超预算则整 Turn 排除
- 并发插入期间保证无重复、无漏读

### 6.2 算法伪代码

```python
def load_recent_messages_bounded(
    db, thread_id, token_budget, token_counter, model, normalizer,
    max_pages=20, max_turns=100, max_events=5000, max_raw_bytes=2*1024*1024,
) -> tuple[list[dict], BoundedReadStats]:
    """
    Keyset DESC 分页读取，边读边估算，达到任一上限立即停止。
    最后反转为时间正序。
    """
    all_turns: list[list[dict]] = []
    current_tokens = 0
    pages_read = 0
    total_events = 0
    total_bytes = 0
    stopped_by: str | None = None
    
    last_sequence: int | None = None  # keyset cursor
    page_size = 20
    
    while pages_read < max_pages:
        # ── Keyset pagination ──
        query = (
            db.query(TurnRecord)
            .filter(
                TurnRecord.thread_id == thread_id,
                TurnRecord.status == "completed",
            )
            .order_by(TurnRecord.turn_sequence.desc())
            .limit(page_size)
        )
        if last_sequence is not None:
            query = query.filter(TurnRecord.turn_sequence < last_sequence)
        
        turns_page = query.all()
        pages_read += 1
        
        if not turns_page:
            break
        
        for turn in turns_page:
            last_sequence = turn.turn_sequence  # 更新 keyset cursor
            
            if len(all_turns) >= max_turns:
                stopped_by = "max_turns"
                break
            
            events = (
                db.query(Event)
                .filter(
                    Event.thread_id == thread_id,
                    Event.turn_id == turn.turn_id,
                )
                .order_by(Event.turn_event_index.asc())
                .all()
            )
            total_events += len(events)
            
            if total_events > max_events:
                stopped_by = "max_events"
                break
            
            turn_dicts = _events_to_dicts(events)
            turn_bytes = sum(len(json.dumps(d).encode()) for d in turn_dicts)
            total_bytes += turn_bytes
            
            if total_bytes > max_raw_bytes:
                stopped_by = "max_raw_bytes"
                break
            
            # ── 工具结果引用化（预算前）──
            turn_dicts = normalizer.normalize_turn(turn_dicts)
            
            # ── 增量 token 估算 ──
            turn_msgs = _dicts_to_messages(turn_dicts)
            tc = token_counter.count_messages(model, turn_msgs)
            
            if current_tokens + tc.safe_tokens > token_budget:
                # 再次尝试：工具结果全部引用化
                turn_dicts_sparse = normalizer.normalize_turn_sparse(turn_dicts)
                if turn_dicts_sparse != turn_dicts:
                    turn_msgs = _dicts_to_messages(turn_dicts_sparse)
                    tc = token_counter.count_messages(model, turn_msgs)
                    if current_tokens + tc.safe_tokens > token_budget:
                        stopped_by = "token_budget"
                        break
                    turn_dicts = turn_dicts_sparse
                else:
                    stopped_by = "token_budget"
                    break
            
            current_tokens += tc.safe_tokens
            all_turns.append(turn_dicts)
        
        if stopped_by:
            break  # 任何上限触发 → 立即停止整个循环
    
    # ── 反转时间正序 ──
    all_turns.reverse()
    result: list[dict] = []
    for turn_dicts in all_turns:
        result.extend(turn_dicts)
    
    return result, BoundedReadStats(
        pages_read=pages_read,
        turns_included=len(all_turns),
        total_events=total_events,
        total_bytes=total_bytes,
        estimated_tokens=current_tokens,
        stopped_by=stopped_by,
    )
```

### 6.3 停止规则（修订）

| 触发条件 | 默认值 | 行为 |
|---------|--------|------|
| `pages_read >= max_pages` | 20 | 停止循环（keyset pagination 已失效） |
| `len(all_turns) >= max_turns` | 100 | 停止循环 |
| `total_events > max_events` | 5000 | 停止循环 |
| `total_bytes > max_raw_bytes` | 2MB | 停止循环 |
| `current_tokens + tc.safe_tokens > token_budget`（稀疏化后仍超） | 97000 | 停止循环 |

### 6.4 与 v2 的关键差异

- ❌ 删除"首个历史 Turn 超预算也必放"；
- ❌ 删除 OFFSET 分页；
- ❌ 删除"触发上限后仍处理当前页剩余 Turn"；
- ✅ keyset pagination 保证并发插入不产生重复/漏读；
- ✅ ToolResultNormalizer 参与历史读取，预算内优先引用化而非整 Turn 丢弃。

---

## 7. TokenCounter 方案（LiteLLM）

### 7.1 接口

```python
@runtime_checkable
class TokenCounter(Protocol):
    def count_messages(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> TokenCount: ...
```

### 7.2 默认实现：LiteLLMTokenCounter

```python
class LiteLLMTokenCounter:
    def __init__(self, safety: TokenSafetyConfig, profile: ModelProfile):
        self._safety = safety
        self._profile = profile

    def count_messages(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> TokenCount:
        try:
            from litellm import token_counter as litellm_count
            estimated = litellm_count(model=model, messages=messages, tools=tools)
            margin = max(
                int(estimated * self._safety.litellm_margin_ratio),
                self._safety.litellm_min_margin_tokens,
            )
            return TokenCount(
                estimated_tokens=estimated,
                safety_margin_tokens=margin,
                safe_tokens=estimated + margin,
                source="litellm",
                confidence="high",
                model=model,
            )
        except Exception as e:
            return self._fallback_estimate(model, messages, tools, e)
```

### 7.3 Conservative Fallback

Fallback 不得只读 `message.content`。将最终完整请求规范化序列化后统一估算：

```python
def _fallback_estimate(self, model, messages, tools, error) -> TokenCount:
    """
    覆盖: system/user/assistant/tool messages、
    tool_calls、tool arguments、tool results、tools schema、
    结构化 content、其他实际提交字段。
    """
    total = 0
    import json as _json
    
    # 消息估计
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            # UTF-8 字节数保守估计 (CJK: ~1.5 tokens/char)
            text_bytes = len(content.encode("utf-8"))
            total += max(1, int(text_bytes / 1.6))
        elif isinstance(content, list):
            # 结构化 content (如 vision)
            total += len(_json.dumps(content, ensure_ascii=False).encode("utf-8")) // 2
        total += 4  # role + overhead per message
        
        # tool_calls
        for tc in m.get("tool_calls", []) or []:
            tc_str = _json.dumps(tc, ensure_ascii=False)
            total += len(tc_str.encode("utf-8")) // 2
    
    # tool definitions
    if tools:
        tools_str = _json.dumps(tools, ensure_ascii=False)
        total += len(tools_str.encode("utf-8")) // 2
    
    # 保守 margin (明显大于 LiteLLM 路径)
    margin = max(
        int(total * self._safety.fallback_margin_ratio),
        self._safety.fallback_min_margin_tokens,
    )
    
    return TokenCount(
        estimated_tokens=total,
        safety_margin_tokens=margin,
        safe_tokens=total + margin,
        source="conservative_fallback",
        confidence="low",
        model=model,
    )
```

### 7.4 TokenCount 与 TokenSafetyConfig

```python
@dataclass(frozen=True)
class TokenCount:
    estimated_tokens: int
    safety_margin_tokens: int
    safe_tokens: int          # = estimated + margin
    source: str               # "litellm" | "conservative_fallback"
    confidence: str           # "high" | "low"
    model: str

@dataclass(frozen=True)
class TokenSafetyConfig:
    litellm_margin_ratio: float = 0.10      # +10% buffer
    litellm_min_margin_tokens: int = 500
    fallback_margin_ratio: float = 0.30     # +30% buffer (larger)
    fallback_min_margin_tokens: int = 1000
```

### 7.5 模型名称格式与 ModelProfile

```python
@dataclass(frozen=True)
class ModelProfile:
    provider: str              # "deepseek"
    model_id: str              # "deepseek-chat"
    full_name: str             # "deepseek/deepseek-chat" (LiteLLM format)
    context_window: int        # 128000
    max_output_tokens: int     # 4096
    min_output_tokens: int     # 256
```

模型名称由 `ModelProfile` 统一提供，禁止散落硬编码。

### 7.6 不实现

- 不下载 DeepSeek/OenAI/HuggingFace 官方 tokenizer
- 不按 provider 手工实现 tokenizer adapter
- 不追求与服务端 token 完全一致

### 7.7 调用后 usage 记录（仅观测）

```python
@dataclass
class TokenEstimationRecord:
    model: str
    estimated_tokens: int
    safe_tokens: int
    actual_prompt_tokens: int | None      # API usage.prompt_tokens
    actual_completion_tokens: int | None  # API usage.completion_tokens
    source: str
    confidence: str
    timestamp: datetime
```

用途：日志、监控、误差统计、偏差分析。不参与调用前预算决策。

### 7.8 覆盖范围

`count_messages` 必须传入全部实际提交内容：
- system message（Stable Contract + Core Memory）
- 历史消息（含 tool_calls/tool_result）
- 当前用户消息
- recall/retrieved memory 消息
- WorkingState 渲染文本
- tools schema 定义
- 额外结构化内容序列化后计入

### 7.9 ContextAssembler 不直接依赖 LiteLLM

ContextAssembler 只依赖 `TokenCounter` Protocol：

```python
class ContextAssembler:
    def __init__(self, token_counter: TokenCounter, budget: ContextBudget, ...):
        self._token_counter = token_counter  # 抽象接口
        # 无 from litellm import ...
```

### 7.10 依赖管理

```
litellm>=1.50.0,<2.0.0
```

---

## 8. ContextBudget 数据结构

```python
@dataclass(frozen=True)
class PartitionBudget:
    name: str
    soft_limit_tokens: int
    hard_limit_tokens: int
    priority: int                      # 裁剪优先级 (越小越优先保留)
    description: str

@dataclass(frozen=True)
class ContextBudget:
    model_context_window: int
    reserved_output: PartitionBudget
    stable_contract: PartitionBudget
    core_memory: PartitionBudget
    working_state: PartitionBudget
    tool_definitions: PartitionBudget
    recent_messages: PartitionBudget
    retrieved_memory: PartitionBudget
    tool_results: PartitionBudget
    
    @property
    def hard_input_limit(self) -> int:
        return self.model_context_window - self.reserved_output.hard_limit_tokens
    
    @property
    def soft_input_limit(self) -> int:
        return int(self.hard_input_limit * 0.80)
    
    def validate(self) -> None:
        total = sum([
            self.stable_contract.hard_limit_tokens,
            self.core_memory.hard_limit_tokens,
            self.working_state.hard_limit_tokens,
            self.tool_definitions.hard_limit_tokens,
            self.recent_messages.hard_limit_tokens,
            self.retrieved_memory.hard_limit_tokens,
            self.tool_results.hard_limit_tokens,
            self.reserved_output.hard_limit_tokens,
        ])
        if total > self.model_context_window:
            raise ValueError(
                f"Partition hard limits ({total}) exceed context window "
                f"({self.model_context_window})"
            )
```

默认值由 `ModelProfile` 和模型确定。通过 `aiive.config.settings` 可按环境覆盖。

---

## 9. WorkingState Schema 与生产者

### 9.1 数据模型

```text
id: String(36) PK
thread_id: String(36) FK → threads.id
epoch_id: String(36) FK → epochs.id NULL
current_objective: Text NULL
open_loops: JSON            -- [{id, description, priority, created_at}]
active_constraints: JSON     -- [{id, description, source}]
pending_approvals: JSON      -- [{id, action, requested_at}]
artifact_refs: JSON          -- [{ref, kind, description}]
verified_tool_states: JSON   -- [{tool_name, state_key, state_value, verified_at}]
uncommitted_side_effects: JSON -- [{ref, description, risk}]
running_tool_state: JSON     -- [{tool_call_id, name, started_at}]
token_count: Integer
version: Integer              -- 乐观锁
updated_at: DateTime

UNIQUE (thread_id)
```

### 9.2 生产者分工

| 字段 | 生产者 | Phase 1 实现 |
|------|--------|-------------|
| `pending_approvals` | 运行时代码 | `_finalize_turn` 确定性写入 |
| `artifact_refs` | 运行时代码 | ContextAssembler / `_finalize_turn` 确定性写入 |
| `verified_tool_states` | 运行时代码 | 工具完成后立即写入 |
| `uncommitted_side_effects` | 运行时代码 | 工具完成后立即写入 |
| `running_tool_state` | 运行时代码 | **工具执行生命周期维护**（见 §9.5） |
| `current_objective` | 语义字段 | Phase 1: 显式更新（新工具 `update_working_state`） |
| `open_loops` | 语义字段 | Phase 1: 显式更新 |
| `active_constraints` | 语义字段 | Phase 1: 显式更新 |

### 9.5 running_tool_state 生命周期（v3 新增）

**必须在工具执行生命周期中维护，不得只在 `_finalize_turn` 写入。**

```python
# 工具开始前：add
WorkingStateService.add_running_tool(
    db=db,
    thread_id=thread_id,
    entry={
        "turn_record_id": turn.id,
        "execution_id": execution_id,
        "tool_call_id": tc_id,
        "name": tool_name,
        "started_at": datetime.now(timezone.utc),
    },
)

# 工具完成或失败后：remove + 更新 verified_tool_states
WorkingStateService.remove_running_tool(
    db=db,
    thread_id=thread_id,
    tool_call_id=tc_id,
)
if success:
    WorkingStateService.update_verified_tool_state(
        db=db,
        thread_id=thread_id,
        tool_name=tool_name,
        state={"ok": True, "verified_at": datetime.now(timezone.utc)},
    )
```

**崩溃恢复**：在每次 `_resolve_and_preempt` 成功后（Phase 1），检查 `running_tool_state` 中是否存在 `turn_record_id` 对应的 TurnRecord 已不是 `running`。若是，确定性清理这些条目：

```python
def recover_orphaned_tools(db, thread_id):
    stale = []
    for entry in get_running_tools(thread_id):
        turn = db.get(TurnRecord, entry["turn_record_id"])
        if turn is None or turn.status != "running":
            stale.append(entry["tool_call_id"])
    for tc_id in stale:
        remove_running_tool(db, thread_id, tc_id)
```

这保证崩溃后 `running_tool_state` 不会永久阻止 Segment 密封。

### 9.3 语义字段更新方式（Phase 1）

- 新增内置工具 `update_working_state`，注册到 ToolRegistry
- 主 Agent 自行决定是否调用
- 参数 schema：

```json
{
    "field": "open_loops | current_objective | active_constraints",
    "operation": "add | remove | update",
    "payload": {...},
    "idempotency_key": "string"
}
```

- `WorkingStateService.update_field()`：
  - `source_turn_id` 记录
  - schema validation
  - `version` 乐观锁
  - 字段 token cap（每个列表 ≤ 10 项）
  - `idempotency_key` 去重

### 9.4 渲染格式

```text
## Working State (current operational context, bounded)
### Current Objective
<text>
### Open Loops
- [P0] <description>
### Active Constraints
- <constraint> (source: <source>)
### Pending Approvals
- <action> (requested: <time>)
### Referenced Artifacts
- <ref>: <description>
```

### 9.5 不实现（Phase 3）

- 异步 WorkingStateProposal 后台 LLM
- 语义字段自动提取
- WorkingState OutboxJob handler

---

## 10. Epoch / Segment / Turn 数据关系

### 10.1 本阶段不引入 ConversationStream

Phase 1 使用 `Thread` 作为永久逻辑会话。`Epoch.thread_id` 直接 FK → `threads.id`。

理由：单用户场景 Thread 即 ConversationStream。多 Stream 隔离时，通过新增 `ConversationStream` 包装（向后兼容）。

### 10.2 实体关系

```
Thread (永久会话)
  └─ 1:N ─ Epoch (后端运行阶段)
       └─ 1:N ─ Segment (Epoch 内可密封片段)
            └─ 1:N ─ TurnRecord (用户轮次，不可变归属)

WorkingState (1:1 Thread，当前活跃状态)
EpochCheckpoint (1:1 Epoch，Phase 3 填充)
SegmentSummary (1:1 Segment，Phase 3 填充)
Artifact (独立实体，通过 ref 引用)
```

### 10.3 Epoch 模型

```text
id: String(36) PK
thread_id: String(36) FK → threads.id, NOT NULL
epoch_no: BigInteger, NOT NULL
status: String(32), NOT NULL          -- active | sealing | sealed | archived
start_turn_sequence: BigInteger NULL
end_turn_sequence: BigInteger NULL
checkpoint_id: String(36) FK → epoch_checkpoints.id NULL, DEFERRABLE INITIALLY DEFERRED
created_at: DateTime
sealed_at: DateTime NULL

UNIQUE (thread_id, epoch_no)
```

`checkpoint_id` 使用 DEFERRABLE FK：Epoch INSERT（checkpoint=NULL），EpochCheckpoint INSERT（epoch_id=epoch.id），UPDATE Epoch SET checkpoint_id，同一事务提交。

### 10.4 Segment 模型

```text
id: String(36) PK
epoch_id: String(36) FK → epochs.id, NOT NULL
thread_id: String(36) FK → threads.id, NOT NULL
segment_no: BigInteger, NOT NULL
status: String(32), NOT NULL          -- open | sealing | sealed | failed
start_turn_sequence: BigInteger
end_turn_sequence: BigInteger NULL
source_hash: String(64) NULL           -- open 时 NULL，sealed 时写入
summary_id: String(36) FK → segment_summaries.id NULL, DEFERRABLE INITIALLY DEFERRED
pending_seal_at: DateTime NULL
sealed_by_turn: BigInteger NULL
created_at: DateTime
sealed_at: DateTime NULL

UNIQUE (epoch_id, segment_no)
CHECK (start_turn_sequence IS NOT NULL)
CHECK (end_turn_sequence IS NULL OR end_turn_sequence >= start_turn_sequence)
```

### 10.5 TurnRecord 不可变归属

```text
TurnRecord 新增:
  epoch_id: String(36) FK → epochs.id, NOT NULL
  segment_id: String(36) FK → segments.id, NOT NULL
```

在 `_resolve_and_preempt` 创建 TurnRecord 时，从当前 active Epoch/openia Segment 读取并写入。值一旦写入，永不修改。

### 10.8 并发约束（v3 新增）

**Partial Unique Indexes**：

```sql
-- 每个 thread 最多一个 active Epoch
CREATE UNIQUE INDEX uq_epoch_thread_active
ON epochs (thread_id) WHERE status = 'active';

-- 每个 epoch 最多一个 open Segment
CREATE UNIQUE INDEX uq_segment_epoch_open
ON segments (epoch_id) WHERE status = 'open';
```

**Turn 创建归属（短事务 + 行锁）**：

```python
def _resolve_and_preempt(self, message, thread_id, turn_id):
    db = SessionLocal()
    try:
        # 锁定 Thread 行 → 序列化 epoch/segment 创建
        thread_row = db.query(Thread).with_for_update().filter(Thread.id == committed_tid).one()
        
        # 获取或创建 active Epoch
        epoch = db.query(Epoch).filter(
            Epoch.thread_id == committed_tid, Epoch.status == "active"
        ).first()
        if epoch is None:
            epoch = self._create_epoch(db, committed_tid)
        
        # 获取或创建 open Segment
        segment = db.query(Segment).filter(
            Segment.epoch_id == epoch.id, Segment.status == "open"
        ).first()
        if segment is None:
            segment = self._create_segment(db, epoch)
        
        # 创建 TurnRecord（不可变归属）
        turn = TurnRecord(
            thread_id=committed_tid,
            turn_id=turn_id,
            epoch_id=epoch.id,
            segment_id=segment.id,
            status="not_started",
            ...
        )
        db.add(turn)
        db.commit()
    except IntegrityError:
        db.rollback()
        # 唯一约束冲突 → 重新读取（另一个并发请求已创建）
        db.close()
        return self._resolve_and_preempt_with_retry(...)
    finally:
        db.close()
```

**唯一约束冲突处理**：捕获 `IntegrityError` 后重新执行带 `with_for_update()` 的完整读取路径，不再使用失效的 ORM 对象。

### 10.9 删除 `start_turn_sequence IS NULL` 条件

`Segment.start_turn_sequence` 为 `NOT NULL`（有 `CHECK` 约束），"`start_turn_sequence IS NULL` 禁止密封"是无效条件。不可密封条件更新为：

1. 存在 `status='running'` 的 Turn → 禁止
2. 存在 `status='interrupted_unknown'` 的 Turn → 禁止
3. `uncommitted_side_effects` 非空 → 禁止
4. `pending_approvals` 非空 → 禁止
5. `running_tool_state` 非空 → 禁止（v3 新增）
6. 当前 Segment 内无新的 completed Turn → 禁止

### 10.7 Turn Range 无重叠

服务层断言：新 Segment 的 `start_turn_sequence` > 上一 sealed Segment 的 `end_turn_sequence`。
PostgreSQL 可使用 exclusion constraint；SQLite 使用代码断言。

---

## 11. 数据模型与迁移

### 11.1 新增表

- `epochs`
- `segments`
- `segment_summaries`
- `epoch_checkpoints`
- `working_states`
- `artifacts`

### 11.2 修改表

**ContextSnapshot:**
```text
+ epoch_id: String(36) FK → epochs.id NULL
+ segment_id: String(36) FK → segments.id NULL
+ turn_sequence: BigInteger NULL
+ retention: String(32) DEFAULT 'temporary'  -- current | previous | audit | temporary
+ token_total: Integer DEFAULT 0
```

meta 停止保存 `full_contents`，改为 `artifact_refs`。

新增 Partial Unique Index:

```sql
CREATE UNIQUE INDEX uq_snapshot_thread_current
ON context_snapshots (thread_id, retention)
WHERE retention = 'current';
```

**TurnRecord:**
```text
+ epoch_id: String(36) FK → epochs.id, NOT NULL
+ segment_id: String(36) FK → segments.id, NOT NULL
```

**LLMCall:**
```text
+ estimated_prompt_tokens: Integer NULL
+ safe_prompt_tokens: Integer NULL
+ token_source: String(32) NULL       -- litellm | conservative_fallback
```

### 11.3 迁移策略

- 新增表：`CREATE TABLE IF NOT EXISTS`
- 新增列：`ALTER TABLE ... ADD COLUMN`，全部 NULLABLE + DEFAULT
- 已有 ContextSnapshot：新列 NULL，不影响现有数据
- 原有 Thread：惰性创建默认 Epoch（epoch_no=1, status='active'）+ Segment（segmentano=1, status='open'）
- Downgrade: `DROP TABLE` + `DROP COLUMN`

### 11.4 迁移文件命名

`<rev_id>_phase1_epoch_segment.py`
Depends on: `c8d9e0f1a2b3` (Phase 0.5B outbox ingestion)

---

## 12. Soft / Hard Threshold 时序

```
ContextAssembler.assemble()
│
├─ 1. 加载各分区内容
│
├─ 2. Phase I: 逐分区 token 计数
│   ├─ stable_contract:   3200 tokens  (limit 4000) ✓
│   ├─ core_memory:        400 tokens  (limit 600)  ✓
│   ├─ working_state:     1100 tokens  (limit 2000) ✓
│   ├─ tool_definitions:  3500 tokens  (limit 6000) ✓
│   ├─ recent_messages:  85000 tokens  (limit 97000) ✓
│   ├─ retrieved_memory:   800 tokens  (limit 1200)  ✓
│   ├─ tool_results:      2000 tokens  (limit 8000)  ✓
│   └─ total:             96000 tokens
│
├─ 3. 检查分区 soft threshold
│   └─ recent_messages 85000 > soft(85000)? → pending_seal=True
│      → 标记 Segment 待密封（不阻塞当前 Turn）
│
├─ 4. 分区 hard threshold 裁剪
│   └─ 所有分区 ≤ hard limit。无需裁剪。
│
├─ 5. Phase II: 最终整体 Hard Gate（安全余量统一来自 TokenCount.safe_tokens）
│   ├─ final_messages = 组装后的完整消息列表
│   ├─ final_tools = 工具 schema 列表
│   ├─ total_count = token_counter.count_messages(model, final_messages, final_tools)
│   ├─ final_safe = total_count.safe_tokens = 96000 + 9600(LiteLLM 10% margin) = 105600
│   ├─ final_safe + requested_output(4096) = 109696
│   └─ 109696 ≤ 128000 → ✓ 通过
│
├─ 6. 返回 AssembledContext
│   ├─ messages: list[dict]
│   ├─ tools: list[dict]
│   ├─ partition_report: dict
│   ├─ is_safe: True
│   ├─ pending_seal: True
│   └─ total_token_count: TokenCount
│
└─ 7. 调用模型
    └─ 保证 safe_tokens + output ≤ context_window
```

Hard gate 不通过时：重新裁剪 → 重新整体计数 → 直到通过或明确失败。

---

## 13. 大工具结果引用方案（v3：ToolResultNormalizer）

### 13.1 处理时机

**工具结果必须在进入下一次 LLM 前完成引用化，不得等到 `_finalize_turn()`。**

```
工具执行
  → ToolResultNormalizer.normalize(result)
    → token 检查 (TokenCounter)
    → 超限: 持久化 Artifact + 生成 deterministic preview
    → 构造 ToolMessage（已引用化）
  → 下一次 LLM 调用
```

### 13.2 单结果限制

```python
SINGLE_TOOL_RESULT_INLINE_LIMIT = 2000  # tokens
```

单个工具结果超过此值时，**无论总预算是否充足**，替换为引用化预览。

### 13.3 Artifact 保存时机

Artifact 必须在引用首次进入模型上下文前完成持久化（独立短事务）：

```python
def normalize(self, result_text: str, thread_id: str, trace_id: str) -> ToolResultView:
    estimated = self._token_counter.count_messages(
        self._model, [{"role": "user", "content": result_text}]
    ).estimated_tokens
    
    if estimated <= SINGLE_TOOL_RESULT_INLINE_LIMIT:
        return ToolResultView(inline=result_text, is_reference=False)
    
    # 持久化 Artifact（独立事务，不依赖 Turn 事务）
    db = SessionLocal()
    try:
        artifact = Artifact(
            thread_id=thread_id, trace_id=trace_id,
            kind="tool_result",
            ref=f"artifact://{uuid4().hex[:12]}",
            content=result_text,
            content_hash=hashlib.sha256(result_text.encode()).hexdigest(),
            token_count=estimated,
        )
        db.add(artifact)
        db.commit()
        ref_id = artifact.id
    finally:
        db.close()
    
    return ToolResultView(
        inline=None,
        is_reference=True,
        reference=_build_deterministic_preview(result_text, artifact=ref_id),
    )
```

### 13.4 确定性预览规则（Phase 1 不使用 LLM）

```python
def _build_deterministic_preview(result_text: str, artifact: str) -> dict:
    preview = {
        "artifact_ref": f"artifact://{artifact}",
        "content_hash": hashlib.sha256(result_text.encode()).hexdigest(),
        "byte_count": len(result_text.encode("utf-8")),
    }
    
    # 文本: 头尾各取 200 字符
    if len(result_text) <= 500:
        preview["summary"] = result_text
    else:
        preview["summary"] = result_text[:200] + "\n...[truncated]...\n" + result_text[-200:]
    
    # JSON: 顶层 keys + 数组长度
    try:
        data = json.loads(result_text)
        if isinstance(data, dict):
            preview["top_keys"] = list(data.keys())[:20]
            for k, v in data.items():
                if isinstance(v, list):
                    preview[f"len_{k}"] = len(v)
                elif isinstance(v, dict):
                    preview[f"keys_{k}"] = list(v.keys())[:10]
        elif isinstance(data, list):
            preview["item_count"] = len(data)
            if data and isinstance(data[0], dict):
                preview["columns"] = list(data[0].keys())
    except (json.JSONDecodeError, TypeError):
        pass
    
    # 错误模式检测
    lines = result_text.split("\n")
    errors = [l for l in lines if "error" in l.lower() or "exception" in l.lower() or "traceback" in l.lower()]
    if errors:
        preview["error_count"] = len(errors)
        preview["first_error"] = errors[0][:200]
    
    # 表格检测 (CSV/TSV)
    if "\t" in lines[0] if lines else False or "," in lines[0] if lines else False:
        preview["row_count"] = len(lines)
        preview["header"] = lines[0][:200] if lines else ""
    
    return preview
```

### 13.5 工具结果引用化后的 ToolMessage

```python
# 未超限（正常路径）
ToolMessage(content=full_result, tool_call_id=tc_id)

# 超限（引用化路径）
ToolMessage(
    content=json.dumps({
        "artifact_ref": "artifact://abc123",
        "summary": "共 1,234 行日志，3 个错误...",
        "content_hash": "sha256:def456",
        "byte_count": 98765,
        "row_count": 1234,
        "first_error": "Connection timeout at 12:34:56",
    }),
    tool_call_id=tc_id,
)
```

### 13.6 原子裁剪

- tool_call + tool_result 成对处理，不拆半
- 历史消息裁剪时整 Turn 删除工具交互，不截断 result 文本

---

## 14. ContextSnapshot 保留策略与并发（v3：turn_sequence 排序）

### 14.1 保留策略

| 类型 | 数量 | 说明 |
|------|------|------|
| `current` | ≤1 | 最大 turn_sequence 的已完成 Turn 快照 |
| `previous` | ≤1 | current 之前的最新快照 |
| `audit` | ≤5 | Epoch/Segment 边界快照 |
| `temporary` | 事务后删除 | 超出策略的快照 |

总上限 ≤7 条 per thread。

### 14.2 原子轮换规则（基于 turn_sequence）

在 fenced 最终事务中，按 `turn_sequence`（严格单调递增）而非 `created_at` 决定 current。

```python
def rotate_snapshot(db, thread_id, new_snapshot, new_turn_sequence):
    # 以 Thread 行锁串行化
    db.query(Thread).with_for_update().filter(Thread.id == thread_id).one()
    
    current = (
        db.query(ContextSnapshot)
        .filter(
            ContextSnapshot.thread_id == thread_id,
            ContextSnapshot.retention == "current",
        )
        .first()
    )
    
    if current is None or new_turn_sequence > (current.turn_sequence or 0):
        # ── 正常路径：新 Turn 比 current 更新 ──
        # previous → temporary
        db.query(ContextSnapshot).filter(
            ContextSnapshot.thread_id == thread_id,
            ContextSnapshot.retention == "previous",
        ).update({"retention": "temporary"})
        # current → previous
        if current:
            current.retention = "previous"
        # new → current
        new_snapshot.retention = "current"
    else:
        # ── 晚完成路径：旧 Turn 不应覆盖 current ──
        # 直接归为 temporary（或 audit）
        new_snapshot.retention = "temporary"
    
    db.add(new_snapshot)
    
    # 清理 excess temporary（只保留最新 5 条）
    excess = (
        db.query(ContextSnapshot)
        .filter(
            ContextSnapshot.thread_id == thread_id,
            ContextSnapshot.retention == "temporary",
        )
        .order_by(ContextSnapshot.turn_sequence.desc())
        .offset(5).all()
    )
    for snap in excess:
        db.delete(snap)
    
    # 验证不变量
    current_count = db.query(ContextSnapshot).filter(
        ContextSnapshot.thread_id == thread_id,
        ContextSnapshot.retention == "current",
    ).count()
    assert current_count <= 1
```

### 14.3 约束保护

```sql
-- 保证每个 thread 最多 1 条 current
CREATE UNIQUE INDEX uq_snapshot_thread_current
ON context_snapshots (thread_id, retention)
WHERE retention = 'current';
```

### 14.4 并发测试（v3 新增）

- **正常场景**：两个 Turn 按 turn_sequence 顺序完成 → 更大的 sequence 成为 current
- **乱序完成（旧 Turn 晚到）**：Turn A (seq=5) 先完成成为 current，Turn B (seq=3) 后完成 → B 归为 temporary，A 保持 current
- **并发插入**：两个 Turn 同时完成 → 行锁串行化，第二个看到已有 current 且 sequence ≤ 自己的 sequence

### 14.5 审计快照

Epoch 或 Segment 密封时（Phase 3），当前 `current` 快照标记为 `retention="audit"`。

---

## 15. 系统事件与运行时事件（v3：幂等 ID 修正）

### 15.1 入口统一

`/chat/system` 和 `TaskWorker._wake_agent_for_reminder` 统一走 `TurnExecutionService` + `ContextAssembler`。

### 15.2 幂等 ID 生成（v3 修正）

**禁止使用随机 UUID、Python 内置 `hash()`。** 改为 SHA-256 对规范化业务字段生成：

```python
import hashlib, json

def make_system_operation_id(
    source: str,
    thread_id: str,
    caller_request_id: str,        # 调用方提供的稳定 ID
    canonical_payload: dict,       # 规范化后的请求体
) -> str:
    canonical = json.dumps({
        "source": source,
        "thread_id": thread_id,
        "caller_request_id": caller_request_id,
        "payload": canonical_payload,
    }, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return digest

def make_runtime_turn_id(task_id: str, occurrence_id: str) -> str:
    """
    周期任务的 turn_id = task_id + occurrence_id。
    不能只用 task_id，否则不同触发会被错误去重。
    """
    canonical = f"runtime:{task_id}:{occurrence_id}"
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return "runtime_" + digest[:32]
```

### 15.3 参数定义（v3 修订）

| 参数 | `/chat/system` | `TaskWorker` runtime_event |
|------|---------------|---------------------------|
| `source` | `"system_command"` | `"runtime_event"` |
| `operation_id` | `sha256(source, thread_id, request_id, payload)` | `sha256(task_id, occurrence_id)` |
| `turn_id` | `"system_" + operation_id[:32]` | `"runtime_" + operation_id[:32]` |
| 进入用户可见历史 | 否 | 否 |
| 参与 recent completed turns | 否 | 否 |
| 重试幂等 | 是 (TurnRecord + fingerprint + operation_id) | 是 (TurnRecord + fingerprint + operation_id) |

### 15.4 防止污染

- 系统事件不创建 `event_type="user_message"` Event
- 系统提示追加明确的 `## System Command (backend — NOT user input)` / `## Runtime Event (backend-scheduled — NOT user input)` 标记
- `get_recent_messages` 默认不返回系统 Turn（通过 `turn_id LIKE 'system_%'` 或 `turn_id LIKE 'runtime_%'` 过滤）

---

## 16. 无界 Fallback 删除方案

### 16.1 禁止的 Fallback

**删除**：`ContextAssembler` 失败 → 回退到 Phase 0.5A `AgentGraph._load_context_only` 无界路径。

**保留但收窄**：`ENABLE_EPOCH_CONTEXT` 环境变量仅用于部署切换（整站开关），不用于单请求静默回退。

### 16.2 允许的降级

| 场景 | 降级行为 |
|------|---------|
| `LiteLLMTokenCounter` 失败 | → `ConservativeFallbackCounter`（有界、保守 margin、低置信度） |
| `WorkingStateService` 读取失败 | → WorkingState 分区为空 + audit log entry |
| `load_core_memory` 失败 | → core_memory 分区为空 + audit log entry |
| `AutomaticRecallEngine.recall` 失败 | → retrieved_memory 分区为空 + audit log entry |
| `ContextAssembler` 无法装入预算 | → `ContextBudgetExceededError` (HTTP 413)，不调用模型 |

### 16.3 ContextBudgetExceededError

```python
class ContextBudgetExceededError(Exception):
    def __init__(self, safe_tokens, context_window, partition_report):
        self.safe_tokens = safe_tokens
        self.context_window = context_window
        self.partition_report = partition_report
```

API 返回 HTTP 413，附带诊断信息。

---

## 17. Phase 1 / Phase 3 边界（v3 追加说明）

### Phase 1（立即实现）

| 能力 | 状态 |
|------|------|
| Epoch/Segment/SegmentSummary/EpochCheckpoint 数据模型 | ✅ |
| WorkingState 数据模型 + 确定性字段自动维护 + 显式语义字段 | ✅ |
| Artifact 数据模型 | ✅ |
| Turn → Epoch/Segment 不可变归属（行锁 + partial unique index） | ✅ |
| `pending_seal` 标记 | ✅ |
| 不可密封条件检查 | ✅ |
| 首次 Epoch + Segment 惰性创建 | ✅ |
| ContextBudget + TokenCounter（LiteLLM）+ ContextAssembler（唯一入口） | ✅ |
| Hard/Soft threshold 两阶段强制（安全余量统一在 TokenCount 内） | ✅ |
| Keyset DESC 分页有界历史读取 | ✅ |
| ContextSnapshot 并发安全 + 数量上限（turn_sequence 排序） | ✅ |
| ToolResultNormalizer（工具执行后、下次 LLM 前） | ✅ |
| running_tool_state 生命周期维护 + 崩溃恢复 | ✅ |
| `/chat/system` + `TaskWorker` 统一到 TurnExecutionService + 幂等 ID | ✅ |
| EpochManager 显式 API（不依赖自动触发） | ✅ |

### `_tighten_budget()` 不可变 TrimPlan（v3 追加）

`ContextAssembler._tighten_budget()` 每次 `assemble()` 调用必须创建独立的不可变 `TrimPlan`，不得修改共享的 `ContextBudget` 实例：

```python
def assemble(self, ...):
    trim_plan = TrimPlan.from_budget(self._budget, attempt=0)  # 不可变副本
    for attempt in range(MAX_TRIM_ROUNDS):
        result = self._build_and_trim(trim_plan)
        ...
        if failed:
            trim_plan = trim_plan.next_round()  # 返回新实例
```

### Phase 3（推迟）

| 能力 | 状态 |
|------|------|
| LLM SegmentSummary 生成 | ❌ |
| EpochCheckpoint 自动生成 | ❌ |
| segment_sealing OutboxJob Handler | ❌ |
| Idle Compaction | ❌ |
| WorkingState 语义字段自动提取 | ❌ |
| **Epoch 自动密封（soft threshold 触发）** | ❌ |

**Phase 1 不做自动 Epoch 密封。** `pending_seal` 标记后，密封由 EpochManager 显式 API 触发。相关测试只能验证显式 EpochManager 切换，不得依赖 soft threshold 自动生成 summary/checkpoint。

---

## 18. 修改与新增文件清单

### 新增文件

| 文件 | 职责 |
|------|------|
| `backend/aiive/runtime/context_assembler.py` | 唯一 ContextAssembler |
| `backend/aiive/runtime/token_counter.py` | TokenCounter Protocol + LiteLLMTokenCounter + ConservativeFallbackCounter |
| `backend/aiive/runtime/token_models.py` | TokenCount, TokenSafetyConfig, ModelProfile, TokenEstimationRecord |
| `backend/aiive/runtime/context_budget.py` | ContextBudget, PartitionBudget |
| `backend/aiive/runtime/working_state.py` | WorkingStateService + update_working_state tool |
| `backend/aiive/runtime/epoch_manager.py` | Epoch 惰性创建、Segment 管理、不可密封条件检查 |
| `backend/alembic/versions/<rev>_phase1_epoch_segment.py` | Phase 1 数据迁移 |

### 修改文件

| 文件 | 修改内容 |
|------|---------|
| `db/models.py` | 新增 Epoch/Segment/SegmentSummary/EpochCheckpoint/WorkingState/Artifact；ContextSnapshot 加列；TurnRecord 加 epoch_id/segment_id；LLMCall 加估算字段 |
| `runtime/turn_execution.py` | `_load_context` → 调用 ContextAssembler；`_finalize_turn` 快照裁减 + 并发轮换 + Epoch/Segment 更新；透传 TokenCounter/ContextBudget |
| `runtime/agent_graph.py` | 删除 run()/run_stream()/run_system()/run_runtime_event() 中的内联上下文构建；`_load_context_only` 不再自建上下文；`_execute_graph` 合并旁路分支；`_build_history_messages` 工具结果按预算裁减；`_snapshot_context` 移除 full_contents |
| `runtime/thread_state.py` | get_recent_messages 新增 DESC 分页 + token_budget + 在线估算版本 (load_recent_messages_bounded) |
| `runtime/graph.py` | invoke_chat 透传新参数 |
| `api/routes_chat.py` | `/chat/system` → TurnExecutionService(source="system_command")；`/chat/stream` → execute_turn_stream (真实流式) |
| `worker/task_worker.py` | `_wake_agent_for_reminder` → TurnExecutionService(source="runtime_event") |
| `memory/recall_config.py` | ContextBudget 默认值移出；删除 estimate_tokens |
| `memory/context_assembly.py` | assemble_system_content 移除 recall pack 拼接 |
| `core/llm_client.py` | ModelProfile 辅助；get_model_profile() |
| `config.py` | 新增 ContextBudget 覆盖配置项 |
| `tests/unit/backend/conftest.py` | FakeTokenCounter + TestContextBudget fixture |

---

## 19. 旧数据兼容

### 19.1 现有 Thread → Epoch + Segment

- 惰性创建：首次 Turn 时检测 Thread 无 active Epoch，自动创建：
  - Epoch(thread_id=thread.id, epoch_no=1, status="active", start_turn_sequence=<当前 turn_sequence>)
  - Segment(epoch_id=epoch.id, thread_id=thread.id, segment_no=1, status="open", start_turn_sequence=<当前 turn_sequence>)

### 19.2 turn_id IS NULL 的旧 Event

- 旧 Event 归属到 Segment start_turn_sequence=0 范围（逻辑兼容）
- `get_recent_messages` legacy 合并逻辑不变（0.5A 的 `_query_legacy_groups`）

### 19.3 ContextSnapshot 旧记录

- 旧记录 `epoch_id/segment_id/retention` 均为 NULL → 不影响新逻辑
- 清洗：最新 1 条标记 `retention='current'`，其余 `retention='temporary'`
- `full_contents` 后续不再写入，旧值保留不清除

### 19.4 TurnRecord 旧记录

- `epoch_id/segment_id` 为 NULLABLE（兼容现有数据）
- 新 Turn 必定写入；旧 Turn 保持 NULL
- 历史读取时 JOIN 兼容 NULL

---

## 20. 回滚方案

### 20.1 部署回滚

1. `alembic downgrade -1` → 删除所有 Phase 1 表和列
2. 新文件不影响旧代码（新增文件不导入即可）
3. API 路由还原到 0.5A 版本

### 20.2 运行时回退

`ENABLE_PHASE1_CONTEXT` 环境变量控制是否使用 ContextAssembler：

- `true`（默认）：使用 Phase 1 有界上下文
- `false`：使用 Phase 0.5A 无界上下文（仅用于紧急回退，不得长期运行）

这是**部署级开关**，不是请求级 fallback。

---

## 21. 测试矩阵（v3 修订）

| ID | 测试场景 | 预期行为 | 版本 |
|----|---------|---------|------|
| T01 | Token 上限 | 10/100/1000/10000 Turn 后，每轮 input token ≤ hard_input_limit | - |
| T02 | P99 线性 | P99 context token 不随总历史线性增长 | - |
| T03 | Hard gate 两阶段 | `safe_tokens + output ≤ context_window` 才调用模型 | v3 |
| T04 | 用户消息唯一（强制保留） | 当前用户消息在 messages 中恰好出现一次；历史不强制保留 | v3 |
| T05 | 多工具 call/result 不拆断 | 裁剪时整 Turn 删除，不拆半 pair | - |
| T06 | 大工具结果引用化 | 单个 result > 2000 tokens → artifact_ref + deterministic preview（不论总预算） | v3 |
| T06a | ToolResultNormalizer 时机 | 工具结果进入下一次 LLM 前已完成引用化，不在 _finalize_turn | v3 新增 |
| T06b | Artifact 首次引用前可读 | Artifact 在独立事务中持久化，引用前 DB 可读取 | v3 新增 |
| T07 | WorkingState 保持 | Epoch 切换后 open_loops/constraints/artifacts 不丢 | - |
| T08 | 执行中不可密封 | running Turn · interrupted_unknown · pending approval · uncommitted effect · running_tool_state → 禁止密封 | v3 |
| T08a | running_tool_state 维护 | 工具开始前 add，完成后 remove + update verified，不在 _finalize_turn 批量写 | v3 新增 |
| T08b | running_tool_state 崩溃恢复 | 对应 TurnRecord 非 running → 确定性清理，不永久阻止密封 | v3 新增 |
| T09 | 流式/非流式一致 | 同一消息产生相同 AssembledContext | - |
| T10 | Epoch 切换无感 | 用户连续对话不受影响（显式 EpochManager API，非自动触发） | v3 |
| T11 | ContextSnapshot 并发 | ≤7 条，仅 1 current（按 turn_sequence），旧 Turn 晚完成不覆盖新 current | v3 |
| T11a | ContextSnapshot 乱序完成 | Turn A(seq=5) 先完成→current；Turn B(seq=3) 后完成→temporary，A 保持 current | v3 新增 |
| T12 | PostgreSQL migration | `alembic upgrade head` / `downgrade -1` 成功（含 partial unique indexes） | v3 |
| T13 | Phase 0.5A Turn 幂等 | TurnConflictError/turn_cached/fingerprint 测试全部通过 | - |
| T14 | Phase 0.5B Outbox/Memory | Outbox 消费、MemoryIngestionRun 正常 | - |
| T15 | TokenCounter 接口隔离 | ContextAssembler 只依赖 TokenCounter Protocol，不 import LiteLLM | - |
| T16 | LiteLLM 计数 | deepseek/deepseek-chat 返回 >0 | - |
| T17 | Fallback 不返回 0 | LiteLLM 异常 → conservative fallback safe_tokens > 0 | - |
| T18 | Fallback safe margin 更高 | same input → fallback safe > litellm safe | - |
| T19 | 模型切换无修改 | 替换为 openai/gpt-4o-mini → ContextAssembler 不改 | - |
| T20 | 全部内容计入 | system/WorkingState/recall/history/tools_schema/tool_calls/tool_results 均计入 | - |
| T21 | Hard gate 用 safe_tokens | `safe_tokens + output ≤ context_window`（唯一公式，无独立 margin） | v3 |
| T22 | 实际 usage 记录 | API 后 actual_prompt_tokens 入 TokenEstimationRecord | - |
| T23 | 安全余量防溢出 | LiteLLM 偏小 + TokenCount margin → 仍 ≤ window | v3 |
| T24 | LiteLLM 不可用 | conservative fallback 仍正常回复 | - |
| T25 | DeepSeek API 不变 | LLMClient.chat() 路径未被 LiteLLM 替换 | - |
| T26 | 工具定义预算 | tools_schema > 6000 tokens → priority DESC 截断，无关键词路由 | - |
| T27 | Keyset 分页有界 | 1000/10000 Turn 下 pages_read ≤ max_pages，events_read ≤ max_events；并发插入不重复不漏读 | v3 |
| T28 | Hard gate 重试 | 首轮裁剪不通过 → 进一步裁剪 → 直到通过或 5 轮后异常；TrimPlan 不可变 | v3 |
| T29 | 无界回退禁止 | ContextAssembler 失败不调 0.5A 路径，返回 413 | - |
| T30 | Snapshot 原子轮换 | 行锁 + turn_sequence 排序，uq_snapshot_thread_current 保证唯一 | v3 |
| T31 | 系统事件不污染 | `/chat/system` 和 runtime_event 不产生 user_message event；不参与 recent completed turns | - |
| T31a | 系统事件幂等 ID | turn_id = SHA-256 生成，禁止 UUID/hash()；runtime 用 task_id + occurrence_id | v3 新增 |
| T32 | Turn 不可变归属 | epoch_id/segment_id 写入后不可修改 | - |
| T33 | 启动预算验证 | 分区 hard sum + output > window → 启动失败 | v3 |
| T34 | Fallback 序列化全量 | conservative fallback 覆盖 messages + tools + calls + results + 结构化 content | - |
| T35 | Turn range 无重叠 | 新 Segment start > 上 Segment end | - |
| T36 | WorkingState 确定性生产 | 确定性字段由 TurnExecutionService 自动维护（生命周期内） | v3 |
| T37 | 历史读取线性有界 | 100/1000/10000 Turn 下 pages_read 有常数上界，耗时 O(budget) | - |
| T38 | Epoch/Thread 唯一约束 | 并发创建 Epoch/Segment 时 IntegrityError → 重读，不过期 ORM 对象 | v3 新增 |
| T39 | 无独立 safety_margin 分区 | ContextBudget 不含 safety_margin 分区，TokenCount.safe_tokens = 唯一余量 | v3 新增 |
| T40 | 历史超大 Turn 处理 | 工具结果引用化仍超预算 → 整 Turn 排除（当前用户消息除外） | v3 新增 |

---

> **方案状态**: 等待确认  
> **下一步**: 确认后进入代码实施阶段
