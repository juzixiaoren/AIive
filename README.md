# AIive

AIive — 面向个人用户的本地 Agent OS，具备长期记忆、工具执行和自进化能力。

## 技术栈

| 层 | 技术 |
|---|------|
| 后端 | Python 3.12+ / FastAPI / LangChain + LangGraph |
| LLM | DeepSeek（OpenAI 兼容 API） |
| 数据库 | PostgreSQL 16 / SQLAlchemy 2.0 / Alembic |
| 向量检索 | Qdrant（知识库嵌入） |
| 前端 | React 18 / TypeScript / Vite 5 / TailwindCSS 3 |
| 实时通信 | WebSocket |
| 定时任务 | APScheduler 3.10 |
| 测试 | pytest 8.0 |

## 快速开始

```bash
# 1. 启动 PostgreSQL
docker compose up -d

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 填入 LLM API Key 等信息

# 3. 安装依赖
pip install -e .

# 4. 启动后端
uvicorn aiive.main:app --reload --port 8000

# 5. 启动前端（新终端）
cd frontend && npm install && npm run dev

# 6. 健康检查
curl http://localhost:8000/health
```

## 项目结构

```
AIive/
├── backend/aiive/
│   ├── api/              # REST API 路由（18 个模块）
│   ├── core/             # LLM 客户端、动作规划器
│   ├── runtime/          # Agent 图编排、注意力管理、策略引擎
│   ├── memory/           # 记忆系统（统一写入服务、冲突解析、自动召回）
│   ├── tools/            # 工具注册表、26+ 内置工具、安全删除
│   ├── mcp/              # MCP 工具集成（发现、安装、沙箱）
│   ├── selfdev/          # 自进化（补丁规划、槽位管理）
│   ├── db/               # SQLAlchemy 模型、数据库配置
│   ├── knowledge/        # 文档知识库（摄入、分块、Qdrant 索引）
│   ├── worker/           # 后台 Worker（出站处理、任务调度）
│   ├── supervisor/       # 守护进程（健康探针、槽位管理）
│   ├── context/          # 运行上下文
│   └── storage/          # 对象存储
├── frontend/src/
│   ├── pages/            # ChatPage、ToolsPage、NotificationsPage 等
│   └── api/              # 前端 API 客户端
├── docs/                 # 设计文档与阶段计划
├── tests/unit/backend/   # 单元测试（47 个测试文件）
├── docker-compose.yml    # PostgreSQL 16
└── alembic.ini           # 数据库迁移配置
```

## 核心架构

```
用户输入 → AgentGraph (LangGraph)
              ├── _assistant     (LLM + bind_tools)
              ├── _policy_check  (ToolRegistry 校验)
              └── _tools_node    (ToolNode 执行)
              ↓
         回复 + 事件持久化 + 记忆提取
```

- **AgentGraph**：基于 LangGraph StateGraph，assistant → policy_check → tools 循环
- **ToolRegistry**：统一工具注册与发现，含 schema / risk_level / permission 元数据
- **MemoryWriteService**：统一事务化记忆写入入口，含冲突解析、证据追踪、向量投影
- **AutomaticRecallEngine**：多路记忆召回（精确键 / FTS / 向量 / 时序图 / 最近事件）

## 内置工具（28 个，9 大类）

### 基础
- `echo` — 回显输入消息

### 提醒/任务
- `schedule_reminder` — 创建定时提醒
- `remind_alert` / `confirm_reminder` / `snooze_reminder` — 提醒生命周期
- `list_tasks` / `cancel_task` — 任务管理
- `show_notifications` / `dismiss_notifications` — 通知管理

### 记忆管理（写入）
- `remember_or_update` — 记住或更新长期记忆
- `forget_memory` — 遗忘/删除记忆（4 种范围）
- `run_memory_maintenance` — 扫描记忆库健康状态

### 记忆召回（只读）
- `memory_search` — 语义搜索长期记忆
- `memory_timeline` — 获取记忆详情 + 版本历史 + 证据链
- `memory_event_log` — 关键词搜索原始 episodic 事件

### 文件操作
- `safe_delete` — 安全删除（scope 保护、符号链接拒绝）
- `read_text_file` — 读取 ~/Documents 下文本文件

### 知识库
- `ingest_document` — 导入文档到知识库
- `search_knowledge` — 搜索已导入文档

### MCP 集成
- `search_mcp` — 搜索 MCP 候选服务器
- `install_mcp_sandbox` — 安装到沙箱
- `plan_capability` — 分析目标 → 搜索 → 评估风险 → 生成计划

### 自进化
- `create_selfdev_plan` — 生成自进化补丁计划
- `apply_patch_to_inactive_slot` / `promote_slot` / `rollback_slot` — 槽位管理

### 节奏/注意力
- `query_rhythm` / `query_attention` — 节奏摘要与注意力状态

## API 端点（部分）

| 前缀 | 功能 |
|------|------|
| `/api/chat` | 主聊天交互入口（同步 + 流式） |
| `/api/memories` | 记忆 CRUD |
| `/api/tools` | 工具列表与调用 |
| `/api/capabilities` | 能力管理 |
| `/api/mcp` | MCP 安装与冒烟测试 |
| `/api/notifications` | 通知管理 |
| `/api/attention` | 注意力状态 |
| `/api/selfdev` | 自进化操作 |
| `/api/knowledge` | 知识库操作 |
| `/api/threads` | 线程管理 |
| `/api/ws` | WebSocket 实时通信 |

## 测试

```bash
# 运行所有测试
python -m pytest tests/ -v

# 按模块筛选
python -m pytest tests/ -v -k "memory"
python -m pytest tests/ -v -k "tool"
```
