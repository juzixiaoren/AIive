# AIive 待办事项

> 更新时间：2026-07-22
> 规则：按顺序处理；每完成一项，记录实现、测试和文档结果，再复核下一项。未接线能力优先完整实现，只有已有替代逻辑且确认废弃的代码才删除。

## 1. 文件投影完整接线（已完成，2026-07-22）

**问题**：`MemoryProjection.to_json()` 仅由实时 API 使用，`to_markdown()` 和 `write_projection()` 未进入生产链；`memory_markdown_project` 默认关闭、不在 Outbox allowlist，也没有 handler。当前文件写入非原子，Markdown/JSON 分别查询，可能产生不一致快照。

**已实施方案 A**：
- 新增 `AIIVE_MEMORY_FILE_PROJECTION_ENABLED` 和固定 `AIIVE_MEMORY_FILE_PROJECTION_DIR`，Job payload 不接受路径；
- 记忆 mutation 通过稳定 `memory_file_projection:{memory_id}:{record_version}` 幂等键在同一事务入队；
- `memory_markdown_project` 已加入 allowlist 并注册真实 Handler；
- `MemoryProjection` 一次读取确定排序的 active + valid + 未被 Forget Shield/Tombstone 屏蔽的快照，同时渲染 Markdown 和 JSON；
- 使用同目录临时文件、`fsync` 和 `os.replace` 原子覆盖，JSON 携带 snapshot ID 和 Markdown SHA-256；
- 后台 promote/archive/sleep/merge 与交互式创建、强化、合并、唤醒等路径统一经共享投影入口；
- 遗忘 Phase A 在 Shield 生效的同一事务中触发全量重建，宽选择器也不会继续暴露在文件投影中；
- 配置示例与 `memory_architecture_design.md` 已同步；
- 验证通过：文件投影与生命周期 22 项、Phase 6A 遗忘 41 项、HandlerRegistry/Outbox handler 10 项，共 73 项测试通过；相关文件静态检查 0 告警。

**边界**：投影当前保留既有最近 100 条限制；它是可重建派生视图，不是真相源。Markdown 与 JSON 分别原子替换，机器消费者应以最后写入的 JSON 为 commit manifest，并校验 `markdown_sha256`。

## 2. 记忆维护报告 action card 闭环（已完成，2026-07-22）

**问题**：`run_memory_maintenance` 返回的 `scan` 只进入通用工具结果，前端没有专用消费；已声明的 `maintenance_report` 卡片未生成。`scan` 只是入队时快照，不能冒充最终执行结果。

**已实施方案 A**：
- `run_memory_maintenance` 已标记为 `writes_external_world=True`，经持久化 `ToolOperation` 执行，不再作为只读线程池工具直接写 Outbox；
- 工具使用可信 `RunContext` 生成手工维护幂等窗口，返回独立 `maintenance_operation_id` 与 `pre_enqueue_scan`；
- 初始聊天结果生成 `pending maintenance_report`，明确显示“入队前诊断快照”，不声明维护已完成；
- 维护 Outbox payload 持久化来源 thread、turn、trace；真实 `MemoryMaintenanceRun` 完成或重试耗尽 deadletter 后，Worker 聚合 candidate/applied/skipped 与各 action_type 的真实 applied 数量；
- Worker 在 Outbox 终态事务中追加 `maintenance_report_terminal` Event，事务提交后通过线程 WebSocket 推送 `completed`/`failed` 卡片；
- 前端按维护 operation ID 原位更新同一卡片，专用 UI 分别展示入队前扫描、真实终态统计和失败原因；
- `ThreadState` 历史查询纳入终态事件，并按 operation ID 折叠到原 `llm_response` 卡片，刷新页面后仍恢复真实终态；
- `memory_architecture_design.md` 已同步。

**验证**：HandlerRegistry、维护调度/Worker/历史恢复、生命周期共 40 项测试通过；前端 `npm run build` 通过（仅保留既有 chunk 大小警告）；相关文件静态检查 0 告警。

## 3. Context Inspector 空结果契约（已完成，2026-07-22）

**问题**：`GET /api/context-runs/{trace_id}` 无数据时返回 `200 + {snapshots: []}`，前端却判断 `data.error`，导致“未找到”分支不可达；详情接口又返回 `200 + {error}`，契约不一致。

**已实施方案 A**：
- 主接口无快照统一返回 HTTP 404，错误码为 `context_snapshot_not_found`；
- item 详情接口无快照或无条目统一返回 HTTP 404，分别使用 `context_snapshot_not_found` / `context_item_not_found`；
- 预期 404 不再记录为后端异常日志；
- 前端增加 `ContextRunResponse`、`ContextSnapshot`、`ContextItemDetailResponse` 和显式加载状态机；
- 前端区分 loading、404 未找到和其他加载错误，删除不可达的 `data.error` 分支；
- trace 变化时立即清理旧 data、展开项、弹窗、详情缓存和 loading item，并使用 AbortController 取消旧请求；
- 主接口和详情路径参数均进行 URL 编码；
- `ARCHITECTURE_AND_REVIEW.md` 已同步。

**验证**：Inspector/敏感度/投影相关 7 项后端测试通过；前端 `npm run build` 通过（仅既有 chunk 大小警告）；相关文件静态检查 0 告警。

## 4. UnifiedRetriever 降级边界收口（已完成，2026-07-22）

**问题**：`AutomaticRecallEngine` 不是废弃代码，而是 `UnifiedRetriever` 的 MemoryRecord 路由实现；但 `ContextAssembler` 在统一检索异常后又直接调用它，违反唯一编排入口约定，并可能在 Session 已被 `flush()` 污染时再次失败、丢失 Summary/Checkpoint 和统一 trace。

**已实施方案 A**：
- 保留 `AutomaticRecallEngine` 作为 `UnifiedRetriever` 内部 MemoryRecord 路由，删除 `ContextAssembler` 的直接 import 和 fallback；
- `UnifiedRetriever.retrieve()` 统一收口编排流水线异常，返回 `retrieval_pipeline_degraded + all_routes_degraded` 的 fail-closed 空结果，不向调用方抛出后触发二次检索；
- exact、memory、index 按请求实际启用情况独立降级；全部已尝试路由失败时增加稳定 `all_routes_degraded` note；
- DEEP raw history expansion 独立降级为 `raw_history_route_degraded`，保留已成功取得的 Summary/Checkpoint 父级命中；
- `degraded` 只由故障类 notes 驱动，正常的 `fail_closed_filtered:n` 安全过滤不再误报运行降级；
- ContextAssembler 先完成唯一一次检索和结果转换，再使用 `SessionLocal` 独立短事务写 `RetrievalRun/MemoryRecallRun` 及 candidates；
- 诊断事务失败会独立 rollback、记录日志并返回原检索结果，不污染调用方 Session、不触发二次召回；
- 降级 notes 被追加到 `routes_executed` 诊断字段，Inspector 可观察本轮退化原因；
- `memory_architecture_design.md` 与 `ARCHITECTURE_AND_REVIEW.md` 已同步。

**验证**：Phase 5 UnifiedRetriever、DEEP、ContextAssembler 诊断隔离与上下文装配共 60 项测试通过；相关文件静态检查 0 告警。测试仅保留既有 Python 3.14 依赖兼容和 SQLite datetime 弃用警告。

## 5. 系统消息来源结构化（已完成，2026-07-22）

**问题**：`should_skip_system_message()` 在提醒确认/延时的真实 `/api/chat/system` 路径可以命中，并非死代码；但依赖 `[系统指令]` 等正文前缀，存在用户误命中、新内部消息漏标和格式漂移风险。

**已实施方案 A**：
- 新增强类型 `MessageSource`：`user`、`system_command`、`runtime_event`；`TurnExecutionService` 构造时校验来源，未知内部来源直接拒绝；
- 同步/流式普通 Chat 由服务端固定为 `user`，`/api/chat/system` 固定为 `system_command`，reminder delivery 固定为 `runtime_event`；
- `ChatRequest` 与 `SystemChatRequest` 使用 `extra=forbid`，客户端伪造 `message_source` 会被 422 拒绝；
- 来源写入请求 `user_message` Event payload，并显式传入同步/流式 AgentGraph；
- 工具 `RunContext.source` 使用真实本轮来源，不再硬编码 `user_chat`；
- 记忆信号 `resolve_action` 优先按结构化来源确定性跳过，显式 `user` 即使正文以 `[系统指令]` 开头也不会误跳过；
- `memory_extraction` Outbox payload 持久化 `message_source`，Worker 执行前再次校验；system command 和 runtime event 不进入提取；
- 仅旧 Outbox payload 缺少字段时使用文本前缀兼容，并记录兼容命中；未知非空来源按 fail-closed 跳过；
- `memory_architecture_design.md` 已同步，并明确当前 `/api/chat/system` 的来源标记不等于额外权限，未来赋权需另加认证授权。

**验证**：消息来源策略、客户端伪造防护、Turn/Outbox 持久化、ContextAssembler 与 Worker handler 共 19 项测试通过；后端相关目录静态检查 0 告警。测试仅保留既有 Python 3.14 LangChain 兼容警告。

**补丁（2026-07-23 · 方案 C）**：第 5 项声明"显式传入同步/流式 AgentGraph"，但原实现仅在 `_load_context` 透传来源，两处图入口（`_execute_graph` / `_execute_graph_stream`）仍保留 `message_source=MessageSource.USER` 默认值，导致 `/api/chat/system` 与 reminder 进入的回合在图内（工具 `RunContext.source` 与记忆信号分类）静默退化为 `user`，与"工具 RunContext.source 使用真实本轮来源"声明不符。现引入不可变 `TurnExecutionContext`（`backend/aiive/runtime/execution_context.py`）作为回合元数据单一事实源，由 `TurnExecutionService` 一次性构造并贯穿 `_load_context`、两个图入口、工具 `RunContext` 与记忆策略；图入口不再接受分散的可省略形参，来源漏传在构造处即暴露。回归测试见 `tests/test_execution_context_source_propagation.py`（4 项全通过），既有 turn_execution / context_assembler 测试无回归。

## 6. Developer 页展示 active_epoch_id（已完成，2026-07-22）

**问题**：`active_epoch_id` 已进入前端状态但未渲染；它是关联 Epoch、Segment、Checkpoint 和日志的稳定实体 ID，不应删除。

**已实施方案 A**：现有 Epoch 卡片主值展示 `#active_epoch_no`，次值展示 `active_epoch_id` 前 8 位，卡片 `title` 保留完整 ID；未增加卡片数量或改变四列布局。

**验证**：前端 `npm run build` 通过，静态检查 0 告警；仅保留既有 Vite chunk 大小警告。该修改只接线既有响应字段，不改变接口或功能契约，因此无需额外更新架构文档。

## 7. Capabilities 页展示 created_at（已完成，2026-07-22）

**问题**：MCP 能力的 `created_at` 已返回并存入状态但未展示。

**已实施方案 A**：已安装能力卡片展示浏览器本地格式化的“安装于”时间，原始 ISO 值保留在 `title`；缺失或非法时间显示 `-`，未扩展后端模型字段。

**验证**：前端 `npm run build` 通过，静态检查 0 告警；仅保留既有 Vite chunk 大小警告。该修改只展示已有接口字段，因此无需更新接口或架构文档。

## 8. health_check 命名去混淆（已完成，2026-07-22）

**问题**：`GET /health` 与 `POST /api/selfdev/slots/health-check` 路径和方法不同，不存在 HTTP 冲突；但两个函数同名会降低搜索、堆栈和 OpenAPI SDK 可读性。

**已实施方案**：SelfDev 路由函数重命名为 `check_slot_health`，并显式设置 `operation_id="check_selfdev_slot_health"`；HTTP 方法、路径、参数、返回值及 `Launcher.health_check()` 调用均未改变。

**验证**：Developer/SelfDev 路由安全测试 5 项通过，静态检查 0 告警；仅保留既有 Starlette TestClient 弃用警告。该修改不改变外部行为，因此无需更新接口文档。

## 9. ContextSnapshot.trace_id 显式约束（已完成，2026-07-22）

**问题**：实际不存在 nullable 漂移；`Mapped[str]` 会推导 `nullable=False`，与 baseline 一致。问题仅是表达不够显式。

**已实施方案**：ORM 显式增加 `nullable=False`，不生成迁移；增加 metadata 测试断言该列不可空。

**验证**：新增 metadata 测试通过；相关文件静态检查 0 告警。

## 10. MemoryRecord.sensitivity 迁移一致性保障（已完成，2026-07-22）

**问题**：baseline 中该列可空且无默认值，但后续 `a6d91e7c3f42` 在升级到 head 时回填并设置 NOT NULL、server default 和 CheckConstraint；最终 schema 与 ORM 一致，不应篡改已发布 baseline。

**已实施方案 A**：保留既有迁移链；增加空 SQLite 数据库真实 `upgrade head` 测试，反射并验证 nullable、server default、CheckConstraint 与 ORM 一致；同时修正 pgvector 迁移仅在 PostgreSQL 执行 `CREATE EXTENSION`，使文档承诺的 SQLite upgrade 路径真实可执行。

**验证**：空库迁移契约测试 2 项通过，Alembic 确认为唯一 head `d4f8a1c7e2b9`；相关文件静态检查 0 告警。

## 11. 删除 LegacyForgetRequest 与旧表（已完成，2026-07-22）

**问题**：`LegacyForgetRequest` 无活跃业务调用，新 Forget Saga 已完全使用 `forget_operations` 等新表；经确认，旧 `forget_requests` 仅含不需要保留的开发数据。

**已实施方案**：删除 `LegacyForgetRequest` ORM 映射；新增 `d4f8a1c7e2b9` 增量迁移删除旧表及索引，不篡改 baseline；downgrade 可重建空表结构，但不恢复历史数据；新遗忘流程测试明确验证 ORM metadata 不含旧表且真实 Phase A 只写新 Saga 表；Phase 6A 与记忆架构文档已同步。

**验证**：Phase 6A、schema 迁移和 metadata 共 44 项测试通过，相关文件静态检查 0 告警；仅保留既有 Python 3.14 依赖兼容与 SQLite datetime 弃用警告。

## 12. AttentionManager 主流程接线（已完成，2026-07-23）

**问题**：`AttentionManager.recompute()` 与 `get_current()` 原本仅由 Attention API 和 `query_attention` 工具调用；重算结果不会进入 `ContextAssembler`，对当前或后续回合的模型决策无影响。

**已实施方案 A**：
- 新增 `resolve_for_turn()`，以当前回合之前最近一条用户消息计算真实空闲间隔，保留 4 小时软切换和 12 小时硬切换阈值；
- 首回合建立焦点，连续普通回合只计算不写库；仅在 `continue/suspend/switch` 状态转换或硬切换焦点改变时新增 `AttentionState`；
- 当前主题使用本轮用户消息清洗后的前 50 字，硬切换时更新持久焦点，软切换不覆盖原焦点；
- `ContextAssembler` 将注意力渲染为独立、有界系统消息，明确焦点是用户来源数据而非指令，并以 `untrusted` 记录快照；该消息纳入整体 token hard gate、分区报告和 `ContextSnapshot`，不写入 WorkingState，保持其操作上下文语义；
- 注意力解析使用嵌套事务保存点，失败时 fail-open 跳过注意力上下文，不阻断聊天且不污染外层会话；同步与流式回合共用该路径。

**验证**：注意力状态机 6 项、上下文快照/密封 7 项通过；新增覆盖首回合初始化、连续回合不重复写入、软切换、恢复、硬切换更新焦点、真实上下文注入与失败降级。相关文件静态检查 0 告警。

## 13. MemoryVectorProjectionService.delete 版本安全接线（待实施）

**问题**：`MemoryVectorProjectionService.delete(memory_id, expected_version)` 提供了防旧删除任务误删新投影的版本保护，但生产无调用方；当前 `refresh()` 和 Forget 路径各自直接删除向量投影，删除语义分散。

**确认方案 A**：梳理 supersede、lifecycle 下线与 Forget 路径的版本语义，将可获得正确 `record_version` 的异步删除路径统一接入 `MemoryVectorProjectionService.delete()`；保留 `refresh()` 作为按真相源回源的 upsert/下线重建入口，并补充旧版本删除不得移除新版本投影的并发回归测试。

**影响与前置设计**：需区分真正删除 MemoryRecord 与仅改变生命周期的场景，确保每个调用点传入的 `expected_version` 是产生删除意图时的版本。Forget 批量删除若无法逐记录提供版本，应保持独立的受事务/Shield 约束实现，不能机械替换。

## 14. 死代码清理与任务取消契约收口（已完成，2026-07-22）

**实施内容**：保留暂未启用的工具审批元数据与 `confirm_tools` 扩展点；删除 `AgentDecision`、缓存失效伪投影、旧 `TaskWorker`/`run_once`、内存 Qdrant 检索原型、`AssembledContext.pending_seal` 输出字段和低层 `LLMClient.chat_stream`；保留当前 LangGraph 真 token 流。候选记忆提升统一复用内容/结构化哈希匹配；检索与 SelfDev 缺失资源统一返回 HTTP 404；新增 `POST /api/tasks/{task_id}/cancel`，并将通知删除的取消逻辑收口至 `TaskManager.cancel()`。

**边界**：AttentionManager 主流程接线和 `MemoryVectorProjectionService.delete` 版本安全接线仍保留在第 12、13 项，未在本次改动中实施。

## 15. 注意力旧 recompute 路径下线（已完成，2026-07-23）

**问题**：主流程使用 `resolve_for_turn()`（仅状态转换写库），但 `query_attention` 工具和 `POST /api/attention/recompute` 仍走旧 `recompute()`：每次调用无条件新增一条 `AttentionState`，idle 计算基准不一致（上条 State 时间 vs 上条用户消息时间）；且工具把字面量 `"query"` 写入 `focus_topic`，污染主流程渲染的注意力上下文。

**已实施方案 A（查询只读化，删除 recompute）**：
- 删除 `AttentionManager.recompute()`；新增只读 `inspect()`，返回最新持久状态并附带空闲评估（`idle_hours`/`projected_decision`/`projected_suggestion`），与 `resolve_for_turn` 共用同一空闲基准（最近用户消息、排除当前回合）和阈值决策逻辑（抽取为 `_decision_for_idle()`）；
- `query_attention` 工具改为只读，`thread_id` 缺省取 `RunContext.thread_id`，不再写库、不再污染焦点；
- 删除无调用方的 `POST /api/attention/recompute` 端点（v20 遗留的手动重算入口，前后端均无引用）；`GET /api/attention/current` 保持不变；
- 新增 `scripts/repair_attention_noise.py` 清理历史噪声：删除 `focus_topic="query"` 污染记录，并按线程收敛连续重复记录；已对当前库执行（清理 1 条污染记录，thread_id 为空串的旧工具写入）。

**验证**：新增只读查询与清理脚本测试 5 项；全量 264 项测试通过；路由验证 `POST /attention/recompute` 返回 404、`GET /attention/current` 正常注册；相关文件静态检查 0 告警。
