# AIive

> 普通聊天机器人每次都像第一次见你。AIive 是一个运行在本地、会持续记住你并与能力一起成长的个人 Agent。

## 它怎么"记得你"：一个完整场景

普通的对话机器人，聊完就忘。AIive 不一样——它会把你说过的目标、偏好、决策**长期保留**，并在合适的时机自动召回；当信息冲突时，它会**用新结论替代旧结论**，而不是把矛盾的两条都留着；你随时可以**查看记忆的来源**，也可以**要求遗忘**，而且遗忘是真正生效的。

下面用一条连续的时间线演示（对话为示意，括号内的工具调用对应系统真实能力）：

**第 1 天 · 提取长期目标**

```
你：我最近在准备 Go 后端开发的面试，重点复习高并发和网络编程。
AIive（remember_or_update）：已记住你的长期目标
  [user_profile] 求职方向 = Go 后端开发
  [user_profile] 近期重点 = 高并发 / 网络编程（面试准备中）
```

**第 7 天 · 自动召回，无需你重提**

```
你：帮我看看今天的安排。
AIive（自动召回，无需你重提）：结合你正在准备 Go 后端面试，
       建议把"刷 LeetCode 并发题"加进今日计划，并复习 epoll 与 goroutine 调度。
```

**第 8 天 · 改变方向，旧记忆被替代而非并存**

```
你：其实我不面 Go 了，转去面前端 React。
AIive（冲突解析 → supersede，而非并存）：
  ✅ 原记忆「求职方向 = Go 后端」已标记为 替代（superseded）
  ✅ 新记忆「求职方向 = 前端 React」生效
  ⚠️ 不会同时保留两条相互冲突的结论。
```

**第 9 天 · 查看来源，并要求遗忘**

```
你：这条"求职方向"记忆是哪来的？把来源给我看。
AIive（memory_timeline）：
  版本历史：
    v1（7/1）「准备 Go 面试」来源：你的原话"我最近在准备 Go 后端开发的面试…"
    v2（7/8）「前端 React」替代 v1
  证据：thread#xxx turn#yyy 原文「我最近在准备 Go 后端开发的面试…」

你：忘了它吧。
AIive（forget_memory）：已遗忘「求职方向」相关记忆。

你：现在还记不记得我的求职方向？
AIive（再次召回，fail-closed 校验）：
  检索结果：无 —— 该记忆已被遗忘，不会经任何检索路径重新出现。
```

这就是 AIive 的核心价值：**持续记忆、智能替代、来源可溯、遗忘可控**——而不是一个每次都从零开始的聊天框。

## 它凭什么不一样（差异化）

- **本地优先**：整套系统跑在你自己的机器上，数据不出本机（PostgreSQL + 可选本地模型），不是把对话转交给第三方托管服务。
- **持续长期记忆**：跨会话保留你的目标、偏好与决策，而不是每次打开都从零开始。
- **智能替代而非冲突并存**：信息更新时旧结论被 `supersede`（标记替代），不会同时保留两条互相矛盾的结论。
- **来源可溯**：每条记忆都有版本历史与原始证据（出自哪一轮、哪条消息），可随时用 `memory_timeline` 查看。
- **遗忘可控且真实生效**：`forget_memory` 经 fail-closed 校验，遗忘后不会经任何检索或召回路径重新泄露。
- **与能力一起成长（自进化）**：通过 `plan_capability` / 自进化槽位管理，Agent 能规划并接入新能力（含 MCP），而非固定工具集。
- **上下文不会爆**：对话历史按 token 预算逐级压缩（摘要 → 检查点 → 按需召回），无限长对话也不撑爆上下文窗口。

## 技术架构

### 技术栈

| 层 | 技术 |
|---|------|
| 后端 | Python 3.12+ / FastAPI / LangChain + LangGraph |
| LLM | DeepSeek（OpenAI 兼容 API） |
| 数据库 | PostgreSQL 16 / SQLAlchemy 2.0 / Alembic |
| 向量检索 | pgvector（PostgreSQL 向量扩展） |
| 前端 | React 19 / TypeScript / Vite 6 / TailwindCSS 3 |
| Android 应用 | React 19 / Capacitor 8 / Android SDK 36（独立 UI） |
| Desktop 应用 | Electron 37 / Electron Forge / 独立 Node Host |
| 实时通信 | WebSocket |
| 定时任务 | APScheduler 3.10 |
| 测试 | pytest 8.0 / basedpyright |

### 核心架构

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
- **MemoryWriteService**：统一事务化记忆写入入口，含冲突解析、证据追踪、核心记忆与统一检索投影
- **AutomaticRecallEngine**：生产可用的多路记忆召回（精确键 / 词汇匹配 / pgvector 语义向量 / 近期情节）；时序图能力尚未接入

## 快速开始

### Linux / macOS（Bash）

```bash
# 1. 配置环境变量
cp .env.example .env
# 编辑 .env 填入 LLM API Key 等信息
# 如需本地向量召回，将 AIIVE_MEMORY_VECTOR_ENABLED 改为 true

# 2. 启动 Docker 服务
# 开启本地向量召回时，会自动拉取 TEI 并将模型缓存到 .data/models/embeddings
./scripts/start.sh

# 3. 查看服务状态
docker compose --profile local-embedding ps
```

本地开发若希望后端和前端直接运行在宿主机：

```bash
docker compose up -d postgres
pip install -e .
uvicorn aiive.main:app --reload --port 8000
cd frontend && npm install && npm run dev
curl http://localhost:8000/health
```

### Android APK

Android 客户端位于 `apps/android/`，使用完全独立的组件与 CSS，不复用 Web
页面样式。它呈现核心流式对话和高危工具审批卡片，不显示普通工具明细、
`trace_id`、记忆、能力、事件等 Web 管理功能。

首次构建前，在 `apps/android/src/config.ts` 中填写部署后的后端根地址；默认值
有意保持为空：

```ts
const DEFAULT_BACKEND_BASE_URL = "http://192.168.1.10:8000";
```

然后通过统一入口构建：

```bash
# 已注册的客户端形态
./scripts/build-app.sh list

# 可直接安装的 debug APK
./scripts/build-app.sh android debug

# 未签名 release APK（发布前需使用自己的密钥签名）
./scripts/build-app.sh android release
```

debug 产物位于 `artifacts/apps/android/AIive-debug.apk`。服务器只部署
`backend/` 和 `frontend/`；APK 本身不随服务器部署，而是通过配置的服务器地址
调用 `/api/chat/stream` 等后端接口。详细说明见
[Android 应用构建指南](docs/guides/ANDROID_APP.md)。

### Electron Desktop

Desktop 客户端既是 AIive 界面，也是只在桌面进程存活时上线的本地执行节点。
Web、Android 或 Electron 界面发出的自然语言指令都由后端 Agent 规划；若目标
线程可寻址到在线桌面节点，Agent 才会看到 `desktop_*` 文件和命令工具。

```bash
# 当前宿主平台的未签名调试包
./scripts/build-app.sh desktop debug

# 当前宿主平台的安装/分发包
./scripts/build-app.sh desktop release
```

默认连接 `http://127.0.0.1:8000`。远程部署时，在启动或打包环境中设置
`AIIVE_DESKTOP_BACKEND_URL=https://你的-aiive-服务`。桌面节点使用出站 WebSocket，
不需要在个人电脑上开放入站端口。

桌面能力覆盖任意绝对路径的读取、目录遍历、文本/二进制写入和修改、复制、
移动、建目录、安全删除与 Shell 命令。删除和 Shell 命令始终进入用户审批；
直接删除默认移入 `~/.aiive/trash`，系统目录、磁盘根、用户主目录及其父目录拒绝
删除。详细协议、节点绑定、权限与构建说明见
[Desktop 应用与本地执行节点指南](docs/guides/DESKTOP_APP.md)。

启用本地 Embedding 时应通过 `./scripts/start.sh` 启动模型服务。模型权重和
Hugging Face 缓存均位于 `.data/models/embeddings/`，该目录不会进入 Git 或
Docker 构建上下文。首次启动需要联网下载镜像与模型，之后复用本地缓存。

### Windows（命令提示符 cmd.exe）
> 推荐使用 **Miniconda** 管理 Python 环境（本仓库实测环境名 `aiive`，Python 3.12）；`curl` 在 Win10+ 自带，否则可用 PowerShell 的 `Invoke-WebRequest`。安装依赖**务必加 `--prefer-binary`**，否则 pip 会选到只有源码包（tar.gz）的 `litellm`，卡在源码构建十几分钟（详见下方注意事项）。

```cmd
REM 1. 启动 PostgreSQL（需先安装 Docker Desktop）
docker compose up -d

REM 2. 配置环境变量
copy .env.example .env
REM    编辑 .env 填入 LLM API Key 等信息（可用 notepad .env 打开）

REM 3. 创建（如尚未创建）并激活 conda 环境，再安装依赖
conda create -n aiive python=3.12 -y
conda activate aiive
pip install --prefer-binary -e ".[dev]"

REM 4. 启动后端
uvicorn aiive.main:app --reload --port 8000

REM 5. 启动前端（新开一个 cmd 窗口）
cd /d frontend
npm install
npm run dev

REM 6. 健康检查
curl http://localhost:8000/health
REM    若系统无 curl，可用： powershell -Command "Invoke-WebRequest http://localhost:8000/health"
```

#### cmd 与 Bash 对照表

| 步骤 | Bash | Windows cmd |
|------|------|-------------|
| 复制文件 | `cp .env.example .env` | `copy .env.example .env` |
| 切换目录（跨盘符） | `cd frontend` | `cd /d frontend` |
| 创建虚拟环境 | `python -m venv venv` | `conda create -n aiive python=3.12 -y` |
| 激活虚拟环境 | `source venv/bin/activate` | `conda activate aiive` |
| 串行执行命令 | `a && b && c` | `a && b && c`（或分行写） |
| 编辑文件 | `nano .env` / `vim .env` | `notepad .env` |
| HTTP 健康检查 | `curl http://...` | `curl http://...`（或 PowerShell 的 `Invoke-WebRequest`） |

> **Windows 安装依赖的坑（litellm 源码构建）**
>
> 在本仓库的 Windows + Miniconda 环境下，直接 `pip install -e .` 时，pip 会解析到**只有源码分发版（`.tar.gz`）**的 `litellm-1.93.0`，卡在 *Preparing metadata / Building wheel from sdist* 长达十几分钟甚至假死。
>
> 解决办法：始终带上 `--prefer-binary` 参数，pip 会改用附带 **wheel（`.whl`）** 的 `litellm-1.91.4` 等版本，几十秒即可装完。
>
> ```cmd
> conda activate aiive
> pip install --prefer-binary -e ".[dev]"
> ```
>
> 同理，后续若单独升级/补装依赖（如 `pip install litellm`），也建议加上 `--prefer-binary`，避免重蹈覆辙。

## 项目结构

```
AIive/
├── apps/android/          # 独立 Android 对话 UI + Capacitor 原生工程
├── apps/desktop/          # Electron UI 外壳 + 隔离的本地执行 Node Host
├── assets/branding/       # Web / Android / Desktop 共用品牌源图与派生资源
├── backend/aiive/
│   ├── api/              # REST / WebSocket API 路由
│   ├── desktop/          # 桌面节点租约、连接调度与逐 Turn 工具叠加
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
├── scripts/build-app.sh  # 可扩展的统一客户端构建入口
└── alembic.ini           # 数据库迁移配置
```

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
- `read_document` — 提取 TXT/Markdown/HTML/JSON/CSV/TSV/DOCX/PDF 纯文本

### 知识库
- `ingest_document` — 导入文档到知识库
- `search_knowledge` — 搜索已导入文档
- `reindex_document` — 从内容寻址原文重建索引

### 联网研究
- `web_search` — DuckDuckGo（免密钥）/ Brave / SearXNG 搜索
- `fetch_web_page` — 带 SSRF、重定向、响应大小限制的网页正文抓取

### 内置 Skills
- `document_processing` — 文档读取、导入、检索与重建
- `web_research` — 多来源网页搜索与正文取证
- `knowledge_base` — 本地持久知识库管理
- `mcp_management` — MCP 搜索、风险规划与沙箱安装

### MCP 集成
- `search_mcp` — 搜索 MCP 候选服务器
- `install_mcp_sandbox` — 安装到沙箱
- `plan_capability` — 分析目标 → 搜索 → 评估风险 → 生成计划
- `aiive-essentials` — 随应用启用的零下载 stdio MCP（文档提取、网页搜索、网页抓取）

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
| `/api/skills` | 内置 Skill catalog 与指令详情 |
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
