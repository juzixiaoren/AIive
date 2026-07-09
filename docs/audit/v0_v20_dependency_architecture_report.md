# V0-V20 依赖架构审计报告

> 基于 `project_plan_v0-v20_dependency_architecture_audit_and_repair.md`  
> 执行时间: 2026-07-08 19:15

---

## 1. 技术栈基线

| 检查项 | 状态 | 证据 |
|--------|:--:|------|
| Python + FastAPI + Pydantic v2 | ✅ | pyproject.toml, main.py |
| SQLAlchemy 2.x + Aleabasic + PostgreSQL | ✅ | db/models.py, aleabasic/ |
| React + Vite + TypeScript | ✅ | frontend/ |
| `/api/chat` → LangGraph StateGraph | ⚠️ PARTIAL | graph.py exists but `/api/chat` not yet wired through graph |
| 持久 checkpoint | ❌ BLOCKER | MemorySaver only, no DB checkpointer |

---

## 2. 依赖矩阵审查

### 2.1 LangGraph Runtime
- **graph.py**: LangGraph StateGraph adapter 已存在 ✅
- **routes_chat.py**: 仍直接调用 AgentLoop，未经过 graph.invoke() ❌ BLOCKER
- **Checkpointer**: MemorySaver 仅内存，重启丢失 ❌ BLOCKER
- **节点化**: 无 nodes/ 子目录，函数全在 agent_loop.py ❌ MAJOR

### 2.2 Context Builder
- ContextBuilder: 存在 ✅
- 所有 LLM 调用统一入口: 仅 agent_loop 使用 ✅
- Context Snapshot: 写入 DB ✅
- excluded_items 记录: 未记录 superseded memory 排除理由 ❌ MAJOR

### 2.3 Memory System
- memory_records: PostgreSQL 唯一真相源 ✅
- memory_key / supersede / superseded_by / revision_num: 已添加 ✅
- resolve_for_context(): agent_loop 已使用 ✅
- Mem0-style pipeline: extractor + gate + lifecycle ✅ (已通过 outbox worker 执行)
- valid_from/valid_to/test_run_id: 未添加 ❌ MAJOR

### 2.4 Qdrant / Hybrid Retrieval
- QdrantClient: 存在 (内存 mock) ⚠️ MAJOR
- PostgreSQL truth source: chunks 表 ✅
- Payload 含 chunk_id/document_id: ✅
- 可从 chunks 重建: ✅
- 生产连接: 无真实 Qdrant 连接 ❌ MAJOR
- 向量嵌入: FakeEmbeddingClient (SHA-256 hash, 非语义) ❌ MAJOR

### 2.5 Object Storage
- 本地 object_store adapter: **不存在** ❌ BLOCKER
- 大对象全塞 DB text 字段: context_snapshots.context_items (JSON), chunks.content (Text) ⚠️ MAJOR
- content_ref/object_ref: 未在任何模型中使用 ❌

### 2.6 Outbox / Worker
- outbox_jobs 表: ✅
- OutboxWorker: ✅
- agent_loop 自动 process_all(): ✅
- 覆盖 memory_extraction + steward_extraction: ✅
- 覆盖 qdrant_indexing / projection / maintenance / reminder: ❌ MAJOR

### 2.7 Tool / Capability / Permission
- ToolRegistry: ✅
- PermissionManager: ✅ (存在但 Chat 路径未调用) ⚠️ MAJOR
- CapabilityDefinition schema: ✅
- 最低 17 个内置能力: 仅注册 3 个 (echo, create_reminder, read_text_file_limited) ❌ MAJOR
- Chat handler 直接执行: 非 create_reminder 的工具走 dispatcher 直接调，未经过 registry ❌ MAJOR

### 2.8 safe_delete / Scope Registry
- safe_delete function: ✅
- Scope Registry: ✅ (3 个 scope)
- 唯一删除入口: patch_executor.py 有 unlink() 绕过 ❌ MAJOR
- Chat 删除走 safe_delete: dispatcher handler 调用了 safe_delete ✅

### 2.9 Scheduler / Reminder
- tasks 表: ✅
- TaskManager: ✅
- task_worker.poll_and_notify(): ✅
- 生产用 sleep/setTimeout: 已移除 ✅
- 真实持久化: create_reminder tool 写 DB, worker 轮询 ✅
- notification event: ✅

### 2.10 MCP
- MCPServerCandidate: ✅
- search_mcp_candidates: ✅ (built-in catalog, 非外部 registry) ⚠️ MAJOR
- sandbox install: ✅
- descriptor_hash: ✅
- MCP tool output trust_level: ✅

### 2.11 A/B Self-Dev
- slot_manager / promote_rollback: ✅
- active slot 只读: ✅
- selfdev 只改 inactive: ✅ (patch_executor.apply_to_inactive)
- targeted tests: ✅
- 全量回归禁止: ✅

### 2.12 Action Cards / UI
- ChatResponse.action_cards: ✅
- 前端 ActionCard 展示: ✅ (ChatPage)
- Inspector 页面: EventTimeline, ContextInspector, ToolsPage ✅
- 全能力覆盖: 仅部分能力返回 action card ⚠️ MAJOR

### 2.13 Tests / Cleanup
- 全量回归禁止: 测试按阶段隔离 ✅
- Transaction rollback: conftest.py db_session fixture ✅
- Test cleanup: ✅
- 仍依赖真实 60 秒等待: 已移除 ✅
- Fake clock / injectable clock: 未实现 ❌ MAJOR

---

## 3. 走偏等级总结

### BLOCKER（2 个）

| # | 问题 | 修复方向 |
|---|------|----------|
| B1 | `/api/chat` 未进入 LangGraph graph.invoke() | routes_chat.py 改为调用 graph |
| B2 | 无 Object Storage adapter | 创建 storage/object_store.py 本地 adapter |

### MAJOR（8 个）

| # | 问题 |
|---|------|
| M1 | Checkpointer 仅 MemorySaver，重启丢失 |
| M2 | Context Snapshot 未记录 excluded_items 理由 |
| M3 | Memory 缺 valid_from/valid_to/test_run_id |
| M4 | Qdrant 无生产连接 + FakeEmbedding 非语义 |
| M5 | Outbox 未覆盖 qdrant_indexing/projection/maintenance |
| M6 | 仅注册 3 个工具，缺少 14 个内置能力 |
| M7 | Dispatcher 大部分 handler 绕过 ToolRegistry |
| M8 | No injectable fake clock for tests |

### MINOR（2 个）

- LangGraph 缺 nodes/ 子目录（非功能性问题）
- patch_executor.py unlink() 应标注为 safe scope

---

## 4. 修复计划

按 BLOCKER → MAJOR 顺序执行。
