# AIive 项目进展报告

> 生成时间：2026-07-08  
> 已完成阶段：V0 - V11（共 12 个阶段）  
> 测试总数：**149 passed**，零失败  
> 数据库迁移：**5 次 Alembic migration**，全部应用  

---

## 一、整体架构

```
backend/aiive/
├── main.py                    # FastAPI 入口 + create_app() factory
├── config.py                  # pydantic-settings 配置管理
├── api/                       # 8 个路由模块（16 个 API 端点）
│   ├── routes_health.py       # GET /health
│   ├── routes_chat.py         # POST /api/chat
│   ├── routes_debug.py        # GET /api/debug/events, /traces/{id}, /llm_calls
│   ├── routes_memories.py     # GET/POST /api/memories
│   ├── routes_personal.py     # GET /api/personal-signals
│   ├── routes_tools.py        # GET /api/tools, POST /api/tools/safe-delete
│   ├── routes_mcp.py          # POST /api/mcp/search, GET /api/capabilities
│   ├── routes_mcp_install.py  # POST /api/mcp/{name}/install-sandbox, /smoke
│   └── routes_selfdev.py      # GET /api/selfdev/slots, POST /slots/health-check
├── core/
│   ├── llm_client.py          # LLMClient + FakeLLMClient + LLMResponse
│   └── context_builder.py     # ContextBuilder + Stable Prefix + Memory Injection
├── db/
│   ├── base.py                # SQLAlchemy engine + SessionLocal + get_db
│   └── models.py              # 9 张 ORM 表
├── runtime/
│   ├── agent_loop.py          # 核心对话引擎（Memory + Context + Steward）
│   ├── event_logger.py        # 事件日志
│   ├── thread_state.py        # Thread 管理 + 历史消息重建
│   └── trace.py               # Trace ID 生成
├── memory/
│   ├── memory_store.py        # CRUD
│   ├── memory_extractor.py    # LLM 记忆抽取
│   ├── memory_gate.py         # 门控规则（active/candidate）
│   ├── memory_types.py        # 11 种记忆类型枚举
│   └── steward_signal_extractor.py  # routine/preference 信号识别
├── tools/
│   ├── registry.py            # ToolRegistry + CapabilitySafetySchema
│   ├── builtin_tools.py       # echo + read_text_file_limited
│   ├── permission_manager.py  # 权限管理器
│   └── safe_delete.py         # SafeDeleteScopeRegistry + safe_delete()
├── mcp/
│   ├── discovery.py           # MCP 发现 + 内置 catalog (6 个)
│   ├── installer.py           # install_sandbox + run_smoke
│   └── runtime_client.py      # MCPRuntimeClient
└── supervisor/
    ├── slot_manager.py        # A/B Slot 管理
    ├── launcher.py            # 启动器
    └── health_probe.py        # 健康探针

frontend/
└── src/
    ├── App.tsx
    ├── pages/ChatPage.tsx     # 浅色主题 Chat 页面
    ├── api/chat.ts            # sendMessage() API 封装
    └── ...

tests/unit/backend/            # 15 个测试文件，149 tests
scripts/smoke/                 # 4 个 smoke 脚本
```

---

## 二、阶段完成详情

### V0：项目地基、技术栈锁定与健康检查
| 项目 | 内容 |
|------|------|
| **产出** | `pyproject.toml`、`README.md`、`.env.example`、`docker-compose.yml`、FastAPI 骨架 |
| **API** | `GET /health` → `{"ok":true,"service":"AIive","version":"0.1.0"}` |
| **技术栈** | Python 3.12+ / FastAPI / Pydantic v2 / pytest |
| **测试** | `test_health.py` (2 tests) |

### V1：真实 LLM Client 与 Smoke 验证
| 项目 | 内容 |
|------|------|
| **产出** | `core/llm_client.py` — LLMClient (OpenAI-compatible) + FakeLLMClient |
| **配置** | `AIIVE_LLM_API_KEY` / `AIIVE_LLM_BASE_URL` / `AIIVE_LLM_MODEL` / `AIIVE_LLM_TIMEOUT_SECONDS` |
| **模型** | DeepSeek (`api.deepseek.com/v1`，model=`deepseek-chat`) |
| **Smoke** | `scripts/smoke/smoke_llm_ping.py` → ok=true, latency 可见 |
| **测试** | `test_llm_client.py` (12 tests) |

### V2：Chat API 与 React Chat Page 最小闭环
| 项目 | 内容 |
|------|------|
| **产出** | `api/routes_chat.py` + `runtime/agent_loop.py` + React 前端 |
| **API** | `POST /api/chat` → `{"reply","thread_id","trace_id"}` |
| **前端** | React 19 + Vite + TypeScript + Tailwind（浅色主题）|
| **Smoke** | `scripts/smoke/smoke_chat_api.py` |
| **测试** | `test_chat_api.py` (7 tests) |

### V3：PostgreSQL、Event Log、Trace 与 Thread State
| 项目 | 内容 |
|------|------|
| **产出** | `db/models.py` (Thread/Event/LLMCall)、`event_logger.py`、`thread_state.py`、`trace.py` |
| **DB** | PostgreSQL 16 via docker-compose + Alembic 初始化 |
| **事件类型** | `chat_started` / `user_message` / `llm_response` / `context_truncated` |
| **API** | `GET /api/debug/events`、`/api/debug/traces/{id}`、`/api/debug/llm_calls` |
| **多轮验证** | Turn 2 正确回忆 Turn 1 的 "小明" ✅ |
| **测试** | `test_event_logger.py` (4) + `test_thread_state.py` (6) + 更新 chat (9) |

### V4：Context Assembly v0 与 Context Snapshot
| 项目 | 内容 |
|------|------|
| **产出** | `core/context_builder.py` — Stable Prefix + Working Set + ContextItem |
| **核心内容** | Trust Boundary（外部内容≠指令）、LLM 非执行器声明、stable_prefix_hash |
| **API** | `GET /api/debug/traces/{id}` — 返回完整 snapshot + events + llm_call |
| **测试** | `test_context_builder.py` (9) + `test_context_snapshot.py` (5) |

### V5：Memory Records、抽取候选与召回注入
| 项目 | 内容 |
|------|------|
| **产出** | `memory/memory_store.py` + `memory_extractor.py` + `memory_gate.py` |
| **门控规则** | 明确关键词（"记住"/"叫我"）→ active；偏好句式（"我喜欢"）→ active；其他 → candidate |
| **注入** | Context Builder Evidence Pack 自动注入 active memories |
| **API** | `GET /api/memories`、`POST /api/memories` |
| **测试** | `test_memory_store.py` (6) + `test_memory_extractor.py` (5) + `test_memory_gate.py` (7) |

### V6：Personal Steward Signals：偏好、节奏与提醒雏形
| 项目 | 内容 |
|------|------|
| **产出** | `memory_types.py` (11 种类型) + `steward_signal_extractor.py` |
| **信号类型** | routine（每天/每周/工作日）、preference、user_profile |
| **Gate 扩展** | 14 个 routine 检测模式（每天/每周/weekday/daily 等） |
| **API** | `GET /api/personal-signals` |
| **测试** | `test_memory_types.py` (3) + `test_steward_signal_extractor.py` (6) |

### V7：Tool Registry、Capability Safety 与 Permission Manager
| 项目 | 内容 |
|------|------|
| **产出** | `tools/registry.py` + `builtin_tools.py` + `permission_manager.py` |
| **安全 Schema** | 11 字段 CapabilitySafetySchema（descriptor_hash、risk_level、requires_confirmation 等） |
| **内置工具** | `echo`（low risk）、`read_text_file_limited`（medium risk，需确认，仅 ~/Documents） |
| **机械 Guard** | untrusted source 直接拦截；requires_confirmation → approval_required |
| **API** | `GET /api/tools` |
| **测试** | `test_tool_registry.py` (12) + `test_permission_manager.py` (8) |

### V8：safe_delete 与 Scope Registry
| 项目 | 内容 |
|------|------|
| **产出** | `tools/safe_delete.py` — SafeDeleteScopeRegistry + 三种删除模式 |
| **模式** | trash（→ .trash/）、quarantine（→ .quarantine/）、hard_delete_for_test_only |
| **6 类拒绝** | /、home root、repo root、scope 外、symlink、未解析路径 |
| **API** | `POST /api/tools/safe-delete` |
| **测试** | `test_safe_delete.py` (15 tests) |

### V9：MCP Discovery v0：搜索与候选提案，不安装
| 项目 | 内容 |
|------|------|
| **产出** | `mcp/discovery.py` — MCPServerCandidate + 内置 catalog |
| **Catalog** | 6 个 MCP server（filesystem, github, postgres, brave-search, memory, puppeteer） |
| **Trust** | official_registry → semi_trusted，community → untrusted |
| **API** | `POST /api/mcp/search`、`GET /api/capabilities?state=candidate` |
| **约束** | 仅搜索候选，不安装/不启动/不调用 |
| **测试** | `test_mcp_discovery.py` (10 tests) |

### V10：MCP Sandbox Install v0：测试服务器、只读工具与能力激活
| 项目 | 内容 |
|------|------|
| **产出** | `mcp/installer.py` + `mcp/runtime_client.py` + 3 张新 DB 表 |
| **DB 表** | capabilities、capability_versions、mcp_install_records |
| **状态流转** | candidate → sandbox → smoke(ok) → active / smoke(fail) → needs_review |
| **Hash 检测** | descriptor_hash 变化 → 自动降级 needs_review |
| **API** | `POST /api/mcp/{name}/install-sandbox`、`POST /api/mcp/{id}/smoke` |
| **测试** | `test_mcp_installer.py` (10) + `test_mcp_runtime_client.py` (6) |

### V11：Supervisor 与 manifest-based A/B Slot
| 项目 | 内容 |
|------|------|
| **产出** | `supervisor/slot_manager.py` + `launcher.py` + `health_probe.py` |
| **Slot 管理** | /slots/A、/slots/B + runtime/active_slot 指针 + version_manifest.json |
| **Manifest** | 仅含代码/配置/依赖，排除 data/postgres/qdrant/logs/.env |
| **API** | `GET /api/selfdev/slots`、`POST /api/selfdev/slots/health-check` |
| **测试** | `test_slot_manager.py` (10) + `test_supervisor_health.py` (5) |

---

## 三、测试覆盖

| 阶段 | 测试文件 | Tests |
|------|----------|-------|
| V0 | test_health | 2 |
| V1 | test_llm_client | 12 |
| V2 | test_chat_api | 9 |
| V3 | test_event_logger + test_thread_state | 10 |
| V4 | test_context_builder + test_context_snapshot | 14 |
| V5 | test_memory_store + test_memory_extractor + test_memory_gate | 18 |
| V6 | test_memory_types + test_steward_signal_extractor | 9 |
| V7 | test_tool_registry + test_permission_manager | 20 |
| V8 | test_safe_delete | 15 |
| V9 | test_mcp_discovery | 10 |
| V10 | test_mcp_installer + test_mcp_runtime_client | 15 |
| V11 | test_slot_manager + test_supervisor_health | 15 |
| **总计** | **15 个测试文件** | **149** |

所有测试均使用 transaction rollback 或 tmp_path 自动清理，零副作用。

---

## 四、数据库表

| 表名 | 阶段 | 用途 |
|------|------|------|
| threads | V3 | 对话线程 |
| events | V3 | 事件日志（chat_started/user_message/llm_response/truncated/memory_*/steward_signal/delete_request） |
| llm_calls | V3 | LLM 调用记录（model/latency/preview） |
| context_snapshots | V4 | 每次 LLM 调用的上下文快照（items/meta/injected_memory_ids） |
| memory_records | V5 | 长期记忆（11 种类型，candidate/active 生命周期） |
| capabilities | V10 | 能力注册表（candidate/sandbox/active/needs_review） |
| capability_versions | V10 | 能力版本记录（descriptor_hash/tool_list_hash/smoke_result） |
| mcp_install_records | V10 | MCP 安装记录 |

---

## 五、API 端点汇总

| 方法 | 路径 | 阶段 | 说明 |
|------|------|------|------|
| GET | `/health` | V0 | 健康检查 |
| POST | `/api/chat` | V2 | 对话（支持多轮） |
| GET | `/api/debug/events` | V3 | 事件查询 |
| GET | `/api/debug/llm_calls` | V3 | LLM 调用查询 |
| GET | `/api/debug/traces/{id}` | V4 | Trace 详情 + Snapshot |
| GET | `/api/memories` | V5 | 记忆列表 |
| POST | `/api/memories` | V5 | 手动创建记忆 |
| GET | `/api/personal-signals` | V6 | Personal Steward 信号 |
| GET | `/api/tools` | V7 | 工具列表 |
| POST | `/api/tools/safe-delete` | V8 | 安全删除 |
| POST | `/api/mcp/search` | V9 | MCP 候选搜索 |
| GET | `/api/capabilities` | V9 | 候选能力列表 |
| POST | `/api/mcp/{name}/install-sandbox` | V10 | Sandbox 安装 |
| POST | `/api/mcp/{id}/smoke` | V10 | Smoke 测试 |
| GET | `/api/mcp/capabilities` | V10 | 已安装能力 |
| GET | `/api/selfdev/slots` | V11 | A/B Slot 状态 |
| POST | `/api/selfdev/slots/health-check` | V11 | Slot 健康检查 |

---

## 六、功能场景速览（人话版）

### 1. 聊天 + 记性

多轮对话，能记住同一对话里的上下文：

```
你：我叫小明，喜欢喝咖啡
AIive：好的小明，我记住了！

你：我刚才说我叫什么？
AIive：你叫小明，还说你喜欢喝咖啡。
```

### 2. 长期记忆

明确告诉它的事会永久记住，闲聊不会乱记：

```
你：记住，我每天早上 7 点起床跑步
AIive：已记录，每天早上 7 点跑步。

（下次新对话）
你：我的作息是什么？
AIive：根据记录，你每天早上 7 点跑步。
```

### 3. 记忆管理

随时查看它记住的一切：

```
GET /api/memories          → 所有记忆列表
GET /api/personal-signals  → 作息、偏好、个人信息
POST /api/memories         → 手动添加记忆
```

### 4. 工具 + 安全

内置两个安全工具：

- **echo**：低风险，无需确认，你说啥回啥
- **读文件**：只能读 ~/Documents 里的文件，最多 50 行，需要你确认

每个工具都有安全标签：风险等级、是否需确认、能否写文件/删东西/读密钥。

### 5. 安全删除

所有删除必须走 safe_delete，三种模式：
- trash → 移到回收站
- quarantine → 隔离区
- hard_delete → 彻底删除（仅测试）

绝对拒绝删除：根目录、Home、项目根目录、软链接、白名单外路径。

### 6. 搜索外部能力（MCP）

能帮你搜到合适的外部工具，但不乱装：

```
POST /api/mcp/search  {"goal": "github"}
→ github MCP：能创建 issue、查 PR、读代码
→ 风险说明："需要 token，能读写仓库"
```

找到后可以装到 sandbox 试运行，通过 smoke 测试才正式激活。内置 6 个常见 MCP 候选。

### 7. A/B 双版本运行

预留两套插槽（A/B），一套运行一套空闲。未来可以热升级——先在空闲插槽装新版，测试通过再切换。

```
GET /api/selfdev/slots  → 查看 A/B 状态
```

### 8. 对话全追溯

每轮对话都有完整记录，随时可查：

```
GET /api/debug/traces/{trace_id}
→ 这次对话用了什么上下文 + 每个事件的时间线 + 模型耗时
```

---

## 七、当前状态

- ✅ **V11 completed**，149 测试全通过
- ✅ **PostgreSQL 16** 运行中，8 张业务表
- ✅ **DeepSeek** 真实 LLM 接入，多轮对话正常
- ✅ **React 前端** Chat Page 可构建运行（浅色主题）
- ✅ **Memory 系统** 完整（抽取→门控→注入→召回）
- ✅ **Tool 安全体系**（CapabilitySafetySchema + safe_delete）
- ✅ **MCP 发现+安装管线**（6 个内置 catalog + sandbox smoke）
- ✅ **A/B Slot 基础设施**（version_manifest + health probe）
- ✅ **测试全覆盖**，自动清理

### 下一阶段
**V12：Self-Dev Patch Proposal — 只生成补丁计划**（LLM 分析代码生成 patch 建议，不自动修改）
