# 流式请求性能优化 TODO（待办的进阶方案）

本文件记录已识别但**尚未实施**的进阶优化方案。已实施的第一阶段优化（见文末"已完成"）
采用了成本低、风险小的方案；本文件中的方案属于架构级改动，风险更高，需专门评估后再动手。

背景诊断：前端请求到达后端慢，根因是三重叠加——
1. 流式路径在事件循环上跑同步重活；
2. 每次请求重复做大量 token 计数；
3. DB 连接池默认偏小；
4. 每次请求重建重量对象。

---

## TODO-1：流式路径 DB 访问改为异步（卡点 1 方案 B）

### 现状
第一阶段已用 `asyncio.to_thread` 把 `execute_turn_stream` 的同步准备段
（`_resolve_and_preempt` / `recover_orphaned_tools` / `_load_context`）丢到线程池，
避免阻塞事件循环。这是"止血"方案，本质是把阻塞从事件循环挪到线程池，
并未消除同步 I/O，且受默认线程池大小（`min(32, cpu+4)`）限制，极高并发下线程池
仍可能成为新瓶颈。

### 方案
把 DB 访问改成真正的异步：
- 使用 `create_async_engine` + `AsyncSession`（SQLAlchemy async）。
- 重写 `_resolve_and_preempt` / `_load_context` / `_finalize_turn` 等的 DB 交互为 `async`。
- 处理 `with_for_update` 行锁在 async 下的语义与事务边界。

### 好处
- 真正的异步 I/O，无线程池天花板，架构最干净。
- 事件循环在等待 DB 时可服务其他请求。

### 坏处 / 风险
- 改动巨大：涉及全项目 session 用法、事务边界、行锁语义。
- 回归风险高，需要完整的并发/一致性测试覆盖。
- 与现有同步路径（`execute_turn` 等）并存会带来双份维护成本。

### 触发条件
- 当 `to_thread` 方案下线程池被证实成为瓶颈（高并发压测显示线程池排队）时再启动。

---

## TODO-2：全共享无状态服务对象（卡点 4 方案 A）

### 现状
第一阶段已按"方案 B"共享确定无状态的对象：`ContextBudget` / `ModelProfile` /
`LiteLLMTokenCounter` / `TokenSafetyConfig` / `ToolResultNormalizer`（模块级 `lru_cache` 工厂，
按 model 名缓存）。而 `WorkingStateService` 与 `EpochManager` 仍每请求 `__init__` 新建。

### 方案
把 `WorkingStateService` 与 `EpochManager` 也提升为共享单例（模块级或依赖注入缓存）。

### 依据（已核实）
- `WorkingStateService`：无 `__init__`、无实例字段，所有方法都以 `db` 为参数传入 —— 无状态。
- `EpochManager`：无 `__init__`、无实例字段赋值 —— 无状态。
两者当前实测均为无状态，理论上可直接共享。

### 好处
- 进一步省掉每请求的对象重建开销。

### 坏处 / 风险
- 目前收益很小（这两个对象构造本身极轻量），单独做性价比低。
- 若未来给这两个类添加了 per-request 状态字段，共享会导致跨请求串数据 —— 需在共享处
  加注释约束"必须保持无状态"，并在改动这两个类时复查。

### 触发条件
- 与其它服务对象统一治理时一并处理；或这两个类确认长期无状态后顺手做。

---

## TODO-3：诊断报告降级 / 移除（卡点 2 更激进方案）

### 现状（已核实）
`ContextAssembler._build_reports` 产出的 `partition_reports` 会对各分区反复做 token 计数，
其中 `recent_messages` 每回合都变化、无法跨请求缓存，仍是热路径上的残留 CPU 成本。

**关键事实**：`partition_reports` 在全代码库中**没有任何下游消费方**：
- `turn_execution.py` 只消费 `AssembledContext.total_token_count`（写 `LLMCall` 可观测记录、
  写 `ContextSnapshot` 的 token 总数），从不读 `partition_reports`。
- `ContextBudgetExceededError` 回传前端时，API 层仅序列化 `safe_tokens` / `context_window`，
  未使用 `partition_reports`。
- 长上下文压缩（`compaction.py`）使用其**独立**的 `counter.count_messages`，不读该报告。

第一阶段已通过给 token 计数加内容 hash 缓存（A2）消除同请求内 / 跨请求对稳定分区的
重复编码；但报告本身仍在热路径上生成。

### 方案（择一）
1. 把 `_build_reports` 改为**按需/采样/debug 开关**生成，默认热路径只产出 `total` 行
   （`total` 本就复用已算的 `total_count`，零额外编码）。
2. 若确认永久无消费方，直接移除 `partition_reports` 字段与相关生成逻辑。

### 好处
- 彻底消除报告在热路径上的全量重编码开销（尤其 `recent_messages` / `tool_results`）。

### 坏处 / 风险
- 牺牲这部分可观测性；若未来要接入分区级 token 监控/告警需重新实现或打开开关。

### 触发条件
- 确认（或团队达成一致）`partition_reports` 无需保留为默认产出后实施。

---

## 已完成（第一阶段，低风险止血）
- 卡点 1 方案 A：`execute_turn_stream` 同步准备段用 `asyncio.to_thread` 丢线程池，
  恢复事件循环并发（`runtime/turn_execution.py`）。
- 卡点 2 方案 A1+A2：`LiteLLMTokenCounter.count_messages` 加内容 hash 有界缓存；
  `count_text` 复用进程级共享 counter（`runtime/token_counter.py`）。
- 卡点 3 方案 A：`create_engine` 显式配置连接池并配置化（`db/base.py`、`config.py`）。
- 卡点 4 方案 B：共享确定无状态的 counter/budget/profile/safety/normalizer
  （`runtime/turn_execution.py`）。
