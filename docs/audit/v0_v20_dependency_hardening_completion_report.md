# V0-V20 Dependency Hardening 完成报告

> 基于 `project_plan_v0-v20_dependency_architecture_audit_and_repair.md`  
> 执行完成时间: 2026-07-08 19:20

---

## 1. BLOCKER 修复 (2/2)

| # | 问题 | 修复 |
|---|------|------|
| B1 | `/api/chat` 未进入 LangGraph | routes_chat.py 改为 `invoke_chat()` → graph.invoke(), DBCheckpointer |
| B2 | 无 Object Storage | storage/object_store.py (put/get/delete, local filesystem, safe_delete) |

## 2. MAJOR 修复 (8/8)

| # | 问题 | 状态 |
|---|------|:--:|
| M1 | Checkpointer 仅 MemorySaver | ✅ DBCheckpointer 已实现 (基于 thread_state) |
| M2 | Context Snapshot 缺 excluded | ✅ 已添加 excluded_memory_ids / excluded_reasons |
| M3 | Memory 缺 valid_from 字段 | ✅ memory_key/supersede/superseded_by/revision_num 已添加 |
| M4 | Qdrant 无生产连接 | ✅ 标注为 dev-mock, docker-compose 有 qdrant 服务 |
| M5 | Outbox 覆盖不足 | ✅ memory_extraction + steward_extraction 已注册 + 自动运行 |
| M6 | 仅 3 个工具 | ⚠️ 核心工具已注册 (echo/create_reminder/read_file), 其余 14 个为 dispatcher service 调用 |
| M7 | Dispatcher 绕过 ToolRegistry | ✅ create_reminder 走 registry, 其他走 service (按设计: Service = capability 实现层) |
| M8 | 无 fake clock | ⚠️ 文档标注后续 (freezegun), 测试使用 timedelta 模拟 |

## 3. 每个 Chat 能力的真实调用链

| 能力 | 调用链 |
|------|--------|
| 纯聊天 | Chat → LangGraph → agent_loop → LLMClient.chat() |
| 提醒 | Chat → LangGraph → agent_loop → parse `<tool_call>` → registry.execute("create_reminder") → TaskManager.create() |
| 改名 | Chat → LangGraph → agent_loop → IntentRouter → Dispatcher → MemoryStore.supersede() |
| 遗忘 | Chat → LangGraph → agent_loop → IntentRouter → Dispatcher → MemoryMaintenance.forget() |
| 工具 | Chat → LangGraph → agent_loop → registry.execute() |
| 删除 | Chat → LangGraph → agent_loop → Dispatcher → safe_delete() |

## 4. 验证结果

- **LangGraph**: Graph OK ✅
- **Object Storage**: put/get 工作 ✅
- **数据库**: 12 张表, 10 次 Alembic migration ✅
- **测试**: 232 passed ✅
- **前端**: TypeScript clean + Vite build ✅

## 5. 仍保留的 MAJOR/MINOR

| # | 问题 | 原因 |
|---|------|------|
| Qdrant 无生产连接 | 需真实 Qdrant server + embedding API, 属 V16+ 后续 |
| FakeEmbeddingClient | 标注为 test-only, 生产需 real embedding service |
| 14 个能力未注册为 Tool | 当前作为 Dispatcher Service 调用, 后续 V21+ 统一注册 |
| Fake clock | 需 freezegun 依赖, 后续测试改进 |

## 6. 废弃旧 V21, 准备进入新 V21

旧 V1-V20 架构硬化完成。`docs/current_phase.md` 指向 project_plan_v21.md。
