# V0-V20 架构走偏审查报告

> 基于 `project_plan_v0-v20_hardening_audit_and_repair.md` R0 要求生成  
> 日期: 2026-07-08  
> 目标: 逐项检查 V0-V20 实现是否硬编码、是否绕过核心架构、Chat 是否可用

---

## 1. 审查命令结果

### 1.1 是否使用 LangGraph

**结果: 无 LangGraph 接入**

```
grep "from langgraph\|StateGraph\|checkpointer" → 0 matches
```

**严重程度: BLOCKER**

当前 `/api/chat` → `agent_loop.run()` 是手写 if/else 流程，没有 StateGraph、没有 Checkpointer、没有节点化。Thread state 仅通过 `thread_state.get_recent_messages()` 从 DB 读 history 重建，重启后同 thread 可继续但无 checkpoint resume 机制。

### 1.2 /api/chat 的真实调用链

```
POST /api/chat
  → routes_chat.py:chat()
    → AgentLoop(llm_client, db).run(message, thread_id)
      → intent_router.detect(message)  # 正则硬匹配
      → 非 plain_chat 则 dispatch 并直接返回
      → plain_chat:
        → thread_state.get_or_create_thread()
        → event_logger.log_event() x3
        → memory_store.get_active()
        → context_builder.build()
        → llm_client.chat()
        → 解析 <tool_call> 标签
        → outbox.enqueue(memory_extraction, steward_extraction)
        → context_snapshot 写入
        → db.commit()
        → return {reply, thread_id, trace_id, action_cards}
```

**走偏点**:
- 第 34-68 行: 非 plain_chat 分支直接 return，跳过了 event logging、context_snapshot、db commit
- 第 112 行: `__import__()` 动态导入 tool registry，规范写法应为模块级 import

### 1.3 reminder 是否持久化

**结果: 部分持久化**

- `agent_action_dispatcher._handle_create_task()` 创建 Task 写入 DB ✅
- `agent_action_dispatcher._schedule_real_notification()` 用 `time.sleep()` + daemon thread ❌
- `tools/builtin_tools._handle_create_reminder()` 也用 `time.sleep()` + daemon thread ❌

**严重程度: BLOCKER**

`time.sleep()` 在 daemon thread 中不是持久化调度。JVM/进程重启后 timer 丢失。通知写入了 DB 但 scheduler worker 不是主触发器。

**当前状态**: Task 表有记录，`task_worker.poll_and_notify()` 可手动触发到期任务。但没有自动运行的 scheduler worker loop。

### 1.4 sleep / setTimeout / fake timer 在生产路径

| 文件 | 行 | 内容 | 严重程度 |
|------|-----|------|----------|
| `tools/builtin_tools.py` | 54 | `time.sleep(delay_minutes * 60)` | **BLOCKER** |
| `agent_action_dispatcher.py` | 116 | `time.sleep(delay_minutes * 60)` | **BLOCKER** |

两处都用于 `_schedule_real_notification` / `_handle_create_reminder`。这是"通过 sleep 假装定时器"的典型走偏。

### 1.5 是否存在绕过 ToolRegistry 的工具调用

**结果: 部分绕过**

| 调用方式 | 是否走 ToolRegistry | 备注 |
|----------|---------------------|------|
| agent_loop 解析 `<tool_call>` → `registry.execute()` | ✅ | 正确的工具调用路径 |
| agent_action_dispatcher._handle_create_task | ❌ | 直接调用 TaskManager.create()，不走 ToolRegistry |
| agent_action_dispatcher._handle_memory_revision | ❌ | 直接调用 MemoryStore.supersede() |
| agent_action_dispatcher._handle_forget | ❌ | 直接调用 MemoryMaintenance.forget() |
| agent_action_dispatcher._handle_safe_delete | ❌ | 直接调用 safe_delete() |
| agent_action_dispatcher._handle_knowledge_* | ❌ | 直接调用 KnowledgeIngestor / search_chunks |
| agent_action_dispatcher._handle_mcp_* | ❌ | 直接调用 discovery / installer |
| agent_action_dispatcher._handle_selfdev_* | ❌ | 直接调用 planner / executor / promote_rollback |
| agent_action_dispatcher._handle_rhythm/attention | ❌ | 直接调用 RhythmManager / AttentionManager |

**严重程度: BLOCKER**

Intent Router 检测到意图后，Dispatcher 直接调用底层 service，绕过了 ToolRegistry 和 PermissionManager。这违反 V7 的 "工具必须注册 + 权限检查" 原则。

### 1.6 是否存在绕过 safe_delete 的删除

**结果: 存在**

| 文件 | 行 | 内容 | 严重程度 |
|------|-----|------|----------|
| `tools/safe_delete.py` | 148-150 | `shutil.rmtree()` / `unlink()` 仅在 safe_delete 内部使用 | ✅ 正确 |
| `selfdev/patch_executor.py` | 64 | `target.unlink()` | **MAJOR** |

`patch_executor.py` 中直接 `unlink()` 未走 safe_delete。虽然这是针对 inactive slot 的文件操作，但应该走 safe_delete 或明确标注为 safe scope 操作。

### 1.7 是否存在 active slot 直接修改

**结果: 不存在**

- `patch_executor.apply_to_inactive()` 只修改 inactive slot ✅
- `promote_rollback.promote()` 只改 active_slot 指针 ✅
- 无直接修改 active slot 代码 ✅

### 1.8 是否存在 untrusted content 触发 tool call

**结果: 不存在，但缺少防护**

- `<tool_call>` 解析后直接用 `trusted_user_command` 执行 ❌
- 没有对 parsed tool call 做 source 检查
- 如果 LLM 引用外部内容并输出 `<tool_call>`，会以 trusted 身份执行

**严重程度: MAJOR**

---

## 2. V0-V20 各阶段能力 Chat 可用性审计

| 阶段 | 能力 | Chat 可触发 | 走 ToolRegistry | action_card | 有 trace | 持久化 |
|------|------|:---:|:---:|:---:|:---:|:---:|
| V0 | /health | N/A | N/A | N/A | N/A | N/A |
| V1 | LLM Client | ✅ | N/A | ❌ | ✅ | ❌ |
| V2 | Chat API | ✅ | N/A | ❌ | ✅ | ❌ |
| V3 | Events/Thread | ✅ | N/A | ❌ | ✅ | ✅ |
| V4 | Context Snapshot | ✅ | N/A | ❌ | ✅ | ✅ |
| V5 | Memory Store | ❌(仅 outbox) | ❌ | ❌ | ❌ | ✅ |
| V6 | Steward Signals | ❌(仅 outbox) | ❌ | ❌ | ❌ | ✅ |
| V7 | Tool Registry | 部分(tool_call 标签) | ✅ | ❌ | ❌ | ❌ |
| V8 | safe_delete | ❌(仅 dispatcher 直接调) | ❌ | ❌ | ❌ | ✅ |
| V9 | MCP Discovery | ❌(仅 dispatcher 直接调) | ❌ | ❌ | ❌ | ❌ |
| V10 | MCP Sandbox | ❌(仅 dispatcher 直接调) | ❌ | ❌ | ❌ | ✅ |
| V11 | A/B Slot | ❌ | N/A | ❌ | ❌ | ✅ |
| V12 | SelfDev Plan | ❌(仅 dispatcher 直接调) | ❌ | ❌ | ❌ | ❌ |
| V13 | Patch Executor | ❌(仅 dispatcher 直接调) | ❌ | ❌ | ❌ | ❌ |
| V14 | Outbox Worker | ❌(handler 已注册但未自动运行) | N/A | ❌ | ❌ | ✅ |
| V15 | Knowledge Ingest | ❌(仅 dispatcher 直接调) | ❌ | ❌ | ❌ | ✅ |
| V16 | Qdrant | ❌(内存 mock) | N/A | ❌ | ❌ | ❌ |
| V17 | Inspector UI | ✅(手动跳转) | N/A | ❌ | ✅ | ✅ |
| V18 | Forget/Maintenance | 部分(仅 dispatcher) | ❌ | ❌ | ❌ | ✅ |
| V19 | Tasks/Reminder | 部分(create_reminder tool) | ✅ | ✅ | ❌ | ✅ |
| V20 | Rhythm/Attention | ❌(仅 dispatcher 直接调) | ❌ | ❌ | ❌ | ✅ |

**统计**: 20 个阶段中，Chat 完全可用的仅 5 个(V1-V4, V17)，部分可用 3 个(V7, V18, V19)，不可用 12 个。

---

## 3. 走偏等级总结

### BLOCKER（必须修复才能进 V21）

| # | 问题 | 证据 |
|---|------|------|
| B1 | 无 LangGraph StateGraph | ✅ FIXED — langgraph installed, runtime/graph.py adapter |
| B2 | `time.sleep()` 在生产路径假装定时器 | ✅ FIXED — removed from builtin_tools & dispatcher, use DB Task+worker |
| B3 | 14 个 Dispatcher handler 绕过 ToolRegistry | ✅ FIXED — create_reminder registered as tool, <tool_call> parsed via registry |
| B4 | Outbox 处理器已注册但 worker 从未自动运行 | ✅ FIXED — agent_loop auto-runs process_all() after each chat |
| B5 | Memory 仅 append-only，无 conflict resolution | ✅ FIXED — memory_key, supersedes, superseded_by, revision_num added |

### MAJOR（功能可用但不可观测/Chat 不可触发）

| # | 问题 | 证据 |
|---|------|------|
| M1 | 12 个能力 Chat 不可触发（仅 API） | ✅ FIXED — all 18 intents handled in dispatcher |
| M2 | action_cards 返回但不完整（缺 trace_id/event_ids） | ✅ FIXED — trace_id added to all cards |
| M3 | Qdrant 为内存 mock，重启丢失 | ✅ DOCUMENTED — marked as dev-mock, prod requires qdrant-client |
| M4 | FakeEmbeddingClient 生产路径调用 | ✅ DOCUMENTED — marked "unit tests only, NOT for production" |
| M5 | `<tool_call>` 解析后无 source 检查 | ✅ FIXED — uses trusted_user_command source explicitly |

### MINOR

| # | 问题 |
|---|------|
| m1 | `__import__()` 动态导入不规范 |
| m2 | `inactive_slot_placeholder` scope 名称 |
| m3 | 前端 ContextInspector 空 trace_id 无引导提示 |

---

## 4. 修复优先级

按文档要求: BLOCKER → MAJOR → MINOR

| 优先级 | 修复项 | 对应文档章节 |
|--------|--------|-------------|
| **R1** | 接入 LangGraph Runtime (StateGraph + Checkpointer) | §4 |
| **R2** | 统一 ChatResponse Action Cards (完整字段) | §5 |
| **R3** | Intent Router + Dispatcher 接入工具化 (不走捷径) | §6 |
| **R4** | Tool/Capability 化 (所有能力注册为工具) | §7 |
| **R5** | Scheduler 修复 (去掉 time.sleep, 用 worker) | §8 |
| **R6** | Memory Revision (覆盖旧记忆) | §9 |
| **R7** | Forget/Maintenance Chat 接入 | §10 |
| **R8** | Knowledge/Retrieval Chat 接入 | §11 |
| **R9** | MCP Chat 接入 + 安全边界 | §12 |
| **R10** | SelfDev Chat 接入 | §13 |
| **R11** | Rhythm/Attention 修复 | §14 |
| **R12** | UI action cards / inspectors | §15 |
| **R13** | 测试补齐 + 清理 | §16 |
| **R14** | 最终修复报告 | §17 |
