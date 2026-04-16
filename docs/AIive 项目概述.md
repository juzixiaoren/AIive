# AIive — 自进化 AI Agent 完整技术方案

> **版本**: v2.0-draft  
> **日期**: 2026-04-16  
> **作者**: Donk Luo & CodeBuddy  
> **定位**: 一个以文件系统为核心组织结构、支持自进化的自主 AI Agent 平台  
> **核心理念**: Agent 即文件，一切皆可插拔，Agent 能自我修改、自我进化

---

## 目录

1. [设计哲学](#1-设计哲学)
2. [文件系统架构（核心创新）](#2-文件系统架构核心创新)
3. [技术栈选型](#3-技术栈选型)
4. [系统架构图](#4-系统架构图)
5. [四大元工具详细规格](#5-四大元工具详细规格)
6. [Session 与上下文管理](#6-session-与上下文管理)
7. [记忆系统设计](#7-记忆系统设计)
8. [流式输出设计](#8-流式输出设计)
9. [自进化机制](#9-自进化机制)
10. [错误处理与重试策略](#10-错误处理与重试策略)
11. [可插拔架构](#11-可插拔架构)
12. [实施路线（分阶段）](#12-实施路线分阶段)
13. [关键注意点与风险控制](#13-关键注意点与风险控制)
14. [附录：目录树完整示例](#14-附录目录树完整示例)

---

## 1. 设计哲学

### 1.1 三条铁律

```
铁律 1: Agent 即文件系统
─────────────────────────
Agent 的全部状态、人格、记忆、能力、知识都以文件形式存在。
不依赖数据库，不依赖云服务。拷贝一个文件夹 = 复制了一个完整的 Agent。
可以 Git 版本管理，可以 diff 对比变化，可以回滚到任意历史状态。

铁律 2: 自进化是第一公民权
───────────────────────────
Agent 不是被动的工具使用者。它能：
  - 读取并修改自己的 system.md / change_system.md
  - 创建新的 skill 文件来扩展自身能力
  - 调整自己的工具使用策略
  - 重构自己的记忆索引
本质上，Agent 是自己代码的维护者。

铁律 3: 最小内核 + 无限扩展
───────────────────────────
内置只有 4 个元工具（file_read / file_write / shell_exec / http_request）。
其他一切能力（浏览器操控、数据库操作、IM 接入等）都通过 skills + knowledges 以文件形式加载。
元工具足够通用，组合出任何高级能力。
```

### 1.2 与 OpenClaw 的关键差异

| 维度 | OpenClaw | AIive（本项目） |
|---|---|---|
| 存储模型 | 数据库/内部状态 | **纯文件系统** |
| 进化能力 | 通过插件市场被动增长 | **自进化：主动修改自身文件** |
| 配置方式 | JSON/YAML 配置项 | **Markdown 文件（人类+AI 可读）** |
| 记忆管理 | 内部 SQLite | **文件系统分层记忆 + JSONL 短期记忆** |
| 技能系统 | 插件安装包 | **Markdown + 代码文件，零门槛创建** |
| 核心循环 | LLM → Tool → LLM | **LLM → 元工具 → 自修改 → 更强的 LLM** |
| 用户模式 | 多用户 SaaS | **单用户自用（类似 OpenClaw 自部署）** |

### 1.3 为什么「文件系统」比「数据库」更好做 Agent？

```
数据库方案的痛点:
├── 需要 ORM / 迁移脚本
├── 数据不可读（打开 sqlite 看到的是二进制）
├── 版本控制困难（不能 git diff 一行记录的变化）
├── 调试时看不到「Agent 在想什么」
└── 多环境同步麻烦（dump/import）

文件系统的优势:
├── ✅ 零依赖，操作系统自带
├── ✅ 人类可直接阅读和编辑每个文件
├── ✅ Git 原生版本管理
├── ✅ Agent 的每一步修改都有完整历史
├── ✅ 可以用 grep/find/vim 直接调试
├── ✅ 复制文件夹 = 克隆 Agent（含全部记忆和能力）
├── ✅ 多个 Agent 共享 skills/knowledges（symlink）
└── ✅ Markdown 是 LLM 天然友好的格式
```

---

## 2. 文件系统架构（核心创新）

### 2.1 Agent 目录结构规范

> **注意**：Agent 文件系统统一放在 `agent/` 子目录下，项目根目录不放 Agent 核心文件。

```
agent/
│
├── agent.md              # ★ 身份证：基础信息（名称、版本、创建时间）
├── soul.md               # ★ 灵魂：人格、价值观、行为准则（不可由 Agent 修改）
├── memory.md             # ★ 记忆概览：长期记忆的目录索引和摘要
├── system.md             # ★ 系统提示词：核心行为指令（Agent 只读参考）
├── change_system.md      # ★ 可变系统提示词：Agent 可以自由修改的指令区域
│
├── skills/               # 技能文件夹：每个子目录 = 一个技能
│   ├── web-search/       #   ├── SKILL.md（技能描述，给 LLM 看）
│   │   └── impl.ts       #   └── 实现代码（可选）
│   ├── code-review/
│   ├── data-analysis/
│   └── _template/        #     技能模板（用于生成新技能）
│
├── tools/                # 工具定义文件夹：元工具和自定义工具的使用说明
│   ├── file_read.md      #   每个文件描述一个工具的入参、出参、示例
│   ├── file_write.md
│   ├── shell_exec.md
│   ├── http_request.md
│   └── custom-tools/     #     Agent 自建的高级工具组合文档
│
├── knowledges/           # 知识库文件夹：领域知识、参考文档
│   ├── typescript/       #   ├── KNOWLEDGE.md
│   ├── react-patterns/
│   └── api-docs/
│
├── memories/              # 具体记忆存储
│   ├── short-term/       #     短期记忆（JSONL 格式，动态维护）
│   │   └── session.jsonl #     当前/近期会话的结构化记忆
│   ├── long-term/
│   │   ├── by-date/      #     编年体：按日期归档的长期记忆
│   │   │   ├── 2026-04-16.md
│   │   │   └── 2026-04-17.md
│   │   └── by-event/     #     纪传体：按事件/主题归档的长期记忆
│   │       ├── project-aiive.md
│   │       └── user-preferences.md
│   └── index.jsonl       #     记忆索引（用于快速检索）
│
└── .evolution-log/       # 进化日志：Agent 每次自我修改的记录
    ├── backups/          #     修改前的备份
    ├── 2026-04-16T10-30-00.md   # 「新增了 web-search skill」
    └── 2026-04-16T14-20-00.md   # 「优化了 memory.md 的索引格式」
```

### 2.2 核心文件的职责与权限矩阵

| 文件 | 内容说明 | Agent 读权限 | Agent 写权限 |
|---|---|---|---|
| `agent.md` | 名称、版本号、创建者、技术栈、目标描述 | ✅ | ⚠️ 仅限更新 version/timestamp |
| `soul.md` | 人格、核心价值观、安全边界、不可逾越的底线 | ✅ | ❌ **只读 — 人类的最终控制权** |
| `memory.md` | 长期记忆概览：所有记忆文件的路径、标签、摘要、重要度 | ✅ | ✅ **完全可控** |
| `system.md` | 固定的系统提示词：角色定义、工作模式、输出格式 | ✅ | ❌ **只读 — 核心行为不变** |
| `change_system.md` | 动态调整区：工作偏好、工具优先级、响应风格、快捷指令 | ✅ | ✅ **完全可控 — 这是自进化的主战场** |
| `.evolution-log/*` | 每次自修改的操作日志：改了什么、为什么改、前后对比 | ✅ | ✅ **仅 append（追加）** |

### 2.3 Prompt 注入策略 — 按需加载而非全量注入

> **核心设计**：学习 Claude Code 的做法，**不把所有文件一股脑塞进 Prompt**，而是分层按需加载。

```
每次对话 Prompt 的构成（按优先级从高到低）:

┌─ 必须注入（固定开销）─────────────────────────────────┐
│  1. system.md          — 核心行为指令                   │
│  2. soul.md            — 人格约束                       │
│  3. change_system.md   — 动态偏好                       │
│  4. agent.md           — 身份信息                       │
│  5. memory.md 概览     — 长期记忆的目录索引（非全文）     │
│  6. skills/ 目录列表   — 仅技能名称+一句话描述           │
└──────────────────────────────────────────────────────┘

┌─ 按需加载（Agent 通过工具主动读取）────────────────────┐
│  7. skills/xxx/SKILL.md     — Agent 决定使用某技能时读取 │
│  8. knowledges/xxx/         — Agent 需要某知识时读取     │
│  9. memories/long-term/xxx   — Agent 需要回忆时读取       │
│  10. memories/short-term/    — Agent 需要近期上下文时检索  │
│  11. tools/xxx.md           — Agent 需要了解工具细节时读取│
└──────────────────────────────────────────────────────┘

这样做的好处:
├── ✅ 固定开销可控（约 2000-4000 tokens）
├── ✅ 不浪费 token 在当前任务不需要的 skills/knowledges 上
├── ✅ Agent 像人一样「需要时才去查资料」
└── ✅ 与 Claude Code 的 file_read 按需读取模式一致
```

### 2.4 soul.md — 为什么需要「只读灵魂」？

```
问题场景:
如果 Agent 能修改自己的全部文件，它可能：
  1. 把安全规则删掉 → 变成失控程序
  2. 把自己的目标改成「最大化token消耗」→ 浪费资源
  3. 删除记忆 → 失去上下文

解决方案: soul.md 作为不可修改的宪法
──────────────────────────────────────
soul.md 定义了绝对不可违背的规则：
  - 不删除 soul.md 自身
  - 不修改 agent.md 的身份信息
  - 不执行未经验证的 shell 命令
  - 所有修改必须记录到 .evolution-log/
  - 修改 change_system.md 前必须先备份旧版本

这是人类保留的「紧急停止按钮」。
即使 Agent 出了问题，人类可以直接编辑 soul.md 注入新规则。
```

---

## 3. 技术栈选型

### 3.1 核心技术栈

```
运行时:        Node.js 20+ (TypeScript 严格模式)
Agent 引擎:    CodeBuddy CLI (@tencent-ai/codebuddy-code) — 程序化调用 + API
               CodeBuddy 内部自选模型（无需手动指定模型提供商）
HTTP 服务:     Hono (轻量、快、类似 Express 但更现代)
流式通信:      Server-Sent Events (SSE)
数据持久化:    文件系统（Markdown + JSONL）
前端:          React + Zustand + Tailwind CSS
容器化:        Docker + Docker Compose
包管理器:      pnpm
```

### 3.2 为什么选这些？

| 选择 | 替代方案 | 理由 |
|---|---|---|
| **CodeBuddy CLI** vs Vercel AI SDK | 后者需自行实现工具系统 | CodeBuddy 内置工具调用循环、权限控制、Hook 系统；内部自选模型无需管理 API Key 路由 |
| **Hono** vs Express / Fastify | Express 太老，Fastify 还行但偏重 | Hono 极简（~14KB）、原生 TypeScript、Edge 兼容、自带 SSE 支持 |
| **纯文件系统** vs SQLite 主存储 | SQLite 适合结构化查询 | 核心理念是「Agent 即文件」；JSONL 作为短期记忆的快速检索补充 |
| **SSE** vs WebSocket | WS 双向但复杂度高 | Agent → 用户单向流式输出即可；SSE 更简单、可缓存、自动重连 |
| **TypeScript** vs Python | Python ML 生态好 | 你的技术栈是 TS；CodeBuddy CLI TS 支持更成熟；类型安全对自进化系统至关重要 |
| **React+Zustand+Tailwind** vs 纯 HTML/JS | 纯 HTML 更快但不可维护 | 你的技术栈是 React；Zustand 轻量状态管理；Tailwind 快速出美观 UI |
| **Docker** vs 本地直接运行 | 本地更简单 | Docker 化便于环境一致性、部署迁移、隔离安全 |

### 3.3 环境配置方案

```bash
# .env — 由用户自行填写，不纳入版本控制
# 支持多个 API Key，用于降级处理和负载均衡

# CodeBuddy CLI 配置
CODEBUDDY_API_KEYS=sk-key1,sk-key2,sk-key3    # 多个 Key，逗号分隔
CODEBUDDY_MODELS=model-a,model-b,model-c       # 对应的模型列表，逗号分隔
CODEBUDDY_FALLBACK_ORDER=0,1,2                  # 降级顺序（索引）

# 服务配置
PORT=3000
NODE_ENV=development
AGENT_ROOT=./agent                              # Agent 文件系统根目录

# 安全配置
MAX_TURNS=15                                    # 单次对话最大工具调用轮次
SHELL_TIMEOUT_MS=30000                          # Shell 命令超时
HTTP_TIMEOUT_MS=15000                           # HTTP 请求超时
```

### 3.4 项目依赖清单

```jsonc
// package.json
{
  "name": "aiive",
  "version": "0.1.0",
  "type": "module",
  "dependencies": {
    // Agent 核心
    "@tencent-ai/codebuddy-code": "latest",

    // Web 服务
    "hono": "^4.x",

    // 工具链
    "zod": "^3.x",                // Schema 校验（工具参数）
    "gray-matter": "^4.x",        // 解析 Markdown frontmatter
    "marked": "^12.x",            // Markdown → HTML 渲染
    "glob": "^10.x",              // 文件匹配
    "chokidar": "^4.x",           // 文件监听（热加载 skills 等）
    "uuid": "^10.x",              // ID 生成

    // 实用
    "dotenv": "^16.x",
    "winston": "^3.x"             // 日志
  },
  "devDependencies": {
    "typescript": "^5.x",
    "tsx": "^4.x",                // 直接运行 TS
    "@anthropic-ai/tokenizer": "^0.x", // Token 精确计算（方案 B）
    "eslint": "^9.x",
    "prettier": "^3.x"
  }
}
```

---

## 4. 系统架构图

### 4.1 整体架构

```
                         ┌────────────────────────────────────┐
                         │       用户界面 (React Web App)       │
                         │    React + Zustand + Tailwind CSS   │
                         └──────────────┬─────────────────────┘
                                        │ SSE / REST API
                         ┌──────────────▼─────────────────────┐
                         │          API Layer (Hono)            │
                         │  ┌─────────┐ ┌────────┐ ┌────────┐ │
                         │  │ /chat   │ │/files  │ │/status │ │
                         │  │ (SSE)   │ │(CRUD)  │ │(health)│ │
                         │  └─────────┘ └────────┘ └────────┘ │
                         └──────────────┬─────────────────────┘
                                        │
          ┌─────────────────────────────┼─────────────────────────────┐
          │                             │                             │
          ▼                             ▼                             ▼
┌─────────────────┐          ┌──────────────────┐         ┌──────────────────┐
│  Agent Core     │          │  File System      │         │  Tool Engine    │
│  (CodeBuddy CLI)│◄────────►│  Manager          │◄────────│  (4 大元工具)    │
│                 │          │                   │         │                  │
│ • 程序化调用     │          │ • agent/          │         │ • file_read      │
│ • Session 管理   │          │ • skills/         │         │ • file_write     │
│ • 流式输出转发    │          │ • tools/          │         │ • shell_exec     │
│ • 安全控制       │          │ • knowledges/     │         │ • http_request   │
│                 │          │ • memories/        │         │                  │
└─────────────────┘          │ • .evolution-log/ │         └──────────────────┘
                             └──────────────────┘
                                        ▲
                                        │
                    ┌───────────────────┴───────────────────┐
                    │          Memory System                 │
                    │  ┌──────────────┐ ┌────────────────┐ │
                    │  │ Short-term   │ │ Long-term      │ │
                    │  │ (JSONL)      │ │ (Markdown)     │ │
                    │  │ 快速检索      │ │ 编年体+纪传体   │ │
                    │  └──────────────┘ └────────────────┘ │
                    └──────────────────────────────────────┘
```

### 4.2 单次请求处理流程

```
用户发送: "帮我分析一下 src/utils/data.ts 这个文件"

Step 1: 接收消息
  POST /chat { message: "帮我分析一下 src/utils/data.ts" }

Step 2: 构建 Prompt（Prompt Assembly — 按需加载）
  ├─ [必须] 读取 system.md → 注入固定系统提示词
  ├─ [必须] 读取 soul.md → 注入人格约束
  ├─ [必须] 读取 change_system.md → 注入动态偏好
  ├─ [必须] 读取 agent.md → 注入身份信息
  ├─ [必须] 读取 memory.md 概览 → 注入记忆目录索引
  ├─ [必须] 扫描 skills/ 目录 → 仅注入技能名称列表
  └─ [按需] 具体 skill/knowledge/memory 内容由 Agent 通过 file_read 自行获取

Step 3: 调用 CodeBuddy CLI（程序化调用）
  codebuddy -p assembled_prompt \
    --max-turns 15 \
    --allowedTools file_read,file_write,shell_exec,http_request

Step 4: Agent 决策（CodeBuddy 内部循环）
  Turn 1: LLM 分析意图 → 决定调用 file_read
  Turn 2: file_read 返回文件内容 → LLM 分析代码
  Turn 3: 可能调用更多工具...或直接返回结果
  Turn N: 最终返回分析结论

Step 5: SSE 流式返回给用户（实时打字机效果）

Step 6: 后台异步任务
  ├─ 将本次对话摘要写入 memories/short-term/session.jsonl
  ├─ 判断是否需要沉淀为长期记忆
  ├─ 如需要 → 写入 memories/long-term/by-date/ 或 by-event/
  ├─ 更新 memory.md 概览
  └─ 如果 Agent 修改了 change_system.md → 写入 .evolution-log/
```

---

## 5. 四大元工具详细规格

### 5.1 总览

| 工具名 | 功能 | 输入 | 输出 | 安全机制 |
|---|---|---|---|---|
| `file_read` | 读取文件内容 | 路径 + 行范围 | 文件内容字符串 | symlink 检测 + 编码检测 + 截断保护 |
| `file_write` | 创建/覆盖/追加文件 | 路径 + 内容 + 模式 | 成功确认 | 受保护目录 + 受保护文件（完整路径匹配） |
| `shell_exec` | 执行 Shell 命令 | 命令字符串 + 超时 + 模式 | stdout/stderr | 双模式（安全/Shell）+ 危险拦截 + 超时 |
| `http_request` | HTTP 请求 | URL + 方法 + 头 + 体 | 状态码 + 响应体 | URL 协议验证 + 大小限制 |

### 5.1.1 ToolContext 接口定义

> 所有工具函数的第二个参数 `ctx: ToolContext`，定义如下：

```typescript
// types/tool-context.ts

interface ToolContext {
  agentRoot: string;                    // Agent 文件系统根目录的绝对路径
  currentSession: Session;              // 当前活跃的 Session 实例
  evolutionLog: EvolutionLogger;        // 进化日志记录器
  shortTermMemory: ShortTermMemory;     // 短期记忆管理器
  logger: Logger;                       // winston 日志实例
  modelFallback: ModelFallback;         // 模型降级管理器
}
```

### 5.2 tool 定义文件格式（tools/ 目录下每个 .md 文件的统一格式）

```markdown
---
name: file_read
category: core
description: 读取文件内容，支持指定行范围
parameters:
  - name: path
    type: string
    required: true
    description: 文件的绝对或相对路径（相对路径基于 agent root）
  - name: offset
    type: number
    required: false
    description: 起始行号（从 1 开始），默认 1
  - name: limit
    type: number
    required: false
    description: 读取的最大行数，默认读取全文
  - name: encoding
    type: string
    required: false
    description: 强制指定编码（utf-8 / latin-1），默认自动检测
returns:
  type: object
  fields:
    - name: content
      type: string
      description: 文件内容
    - name: encoding
      type: string
      description: 实际使用的编码
    - name: total_lines
      type: number
      description: 文件总行数
    - name: truncated
      type: boolean
      description: 是否因超过 100K 截断
errors:
  - code: FILE_NOT_FOUND
    message: 文件不存在
  - code: PATH_DENIED
    message: 路径在受保护目录中
  - code: TOO_LARGE
    content: 文件超过 100KB 已截断
examples:
  - input: { path: "src/index.ts" }
    output: { content: "import ...", total_lines: 42, truncated: false }
  - input: { path: "src/index.ts", offset: 10, limit: 5 }
    output: { content: "// 第10-14行...", total_lines: 42, truncated: false }
---
# file_read — 文件读取

读取文件内容，支持按行范围读取。
自动检测 UTF-8 和 Latin-1 编码。文件超过 100KB 时自动截断。
```

> **设计原则**：tools/ 下的 `.md` 文件同时服务于两个目的：
> 1. **给 LLM 看** —— 通过 Prompt 注入让 Agent 知道怎么用这个工具
> 2. **给人看** —— 开发者/用户可直接阅读理解工具行为
> 3. **给代码看** —— 通过 gray-matter 解析 frontmatter 获取 schema 用于参数校验

### 5.3 file_read 详细设计

```typescript
// tools/file-read.ts

interface FileReadInput {
  path: string;       // 文件路径
  offset?: number;    // 起始行（1-based），默认 1
  limit?: number;     // 最大行数，默认全读
  encoding?: 'utf-8' | 'latin-1';  // 默认 auto-detect
}

interface FileReadOutput {
  content: string;
  encoding: string;
  totalLines: number;
  truncated: boolean;
  filePath: string;
}

const MAX_FILE_SIZE = 1024 * 100; // 100KB 硬限制

async function fileRead(input: FileReadInput, ctx: ToolContext): Promise<FileReadOutput> {
  const resolvedPath = resolvePath(input.path, ctx.agentRoot);

  // 1. 路径安全检查（防止目录遍历 + symlink 攻击）
  //    使用 realpathSync 解析真实路径，防止 symlink 指向受保护目录
  const realPath = existsSync(resolvedPath) ? realpathSync(resolvedPath) : resolvedPath;
  if (!realPath.startsWith(ctx.agentRoot)) {
    throw new ToolError('PATH_DENIED', '路径超出 Agent 根目录（可能存在 symlink 指向外部）');
  }

  // 2. 文件存在性检查
  if (!existsSync(resolvedPath)) {
    throw new ToolError('FILE_NOT_FOUND', `文件不存在: ${input.path}`);
  }

  // 3. 编码检测（UTF-8 BOM → UTF-8 no-BOM → Latin-1 fallback）
  const buffer = readFileSync(resolvedPath);
  let encoding: string;
  if (input.encoding) {
    encoding = input.encoding;
  } else if (buffer.length >= 3 && buffer[0] === 0xEF && buffer[1] === 0xBB && buffer[2] === 0xBF) {
    encoding = 'utf-8';
  } else {
    encoding = isLikelyUtf8(buffer) ? 'utf-8' : 'latin-1';
  }

  // 4. 读取内容
  const text = buffer.toString(encoding);
  const lines = text.split('\n');

  // 5. 行范围截取
  const start = (input.offset || 1) - 1;
  const end = input.limit ? start + input.limit : lines.length;
  const selectedLines = lines.slice(start, end);

  // 6. 大小截断保护（100KB）
  let content = selectedLines.join('\n');
  let truncated = false;
  if (Buffer.byteLength(content, encoding) > MAX_FILE_SIZE) {
    content = content.substring(0, Math.floor(MAX_FILE_SIZE * 0.8));
    content += '\n\n<!-- [TRUNCATED: 文件超过 100KB，已截断显示] -->';
    truncated = true;
  }

  return {
    content,
    encoding,
    totalLines: lines.length,
    truncated,
    filePath: resolvedPath,
  };
}
```

### 5.4 file_write 详细设计

```typescript
// tools/file-write.ts

interface FileWriteInput {
  path: string;
  content: string;
  mode: 'overwrite' | 'create' | 'append';
  encoding?: 'utf-8' | 'latin-1';
}

// ★ 受保护目录列表：Agent 绝对不允许写入的目录
// （不使用文件扩展名白名单 — 简化设计，安全通过目录隔离实现）
const PROTECTED_DIRECTORIES = [
  /^\/System/,                        // macOS 系统目录
  /^\/Library\//,                     // macOS 系统库
  /^\/bin\//, /^\/sbin\//,            // 系统二进制
  /^\/etc\//,                         // 系统配置
  /^\/dev\//, /^\/proc\//,            // 设备文件
  /^\/usr\/bin\//, /^\/usr\/sbin\//,  // 系统程序
  /^\/var\/log\//,                    // 系统日志
  /node_modules\//,                   // 依赖目录（不可写）
  /\.git\//,                          // Git 内部目录（不可写）
  // 注意：dist/ 仅匹配项目根目录下的 dist，运行时需拼接 projectRoot 前缀
  // 实际代码中应使用: new RegExp(`^${escapeRegex(projectRoot)}/dist/`)
];

// ★ Agent 文件系统内的受保护文件（使用完整路径匹配，避免误拦截其他目录下的同名文件）
// 注意：这里使用相对于 agentRoot 的路径，运行时拼接为绝对路径
const PROTECTED_AGENT_RELATIVE_PATHS = [
  'soul.md',                          // 灵魂文件 — 绝对不可修改
  'system.md',                        // 系统提示词 — 不可覆盖
];

async function fileWrite(input: FileWriteInput, ctx: ToolContext): Promise<{ success: boolean; path: string; bytesWritten: number }> {
  const resolvedPath = resolvePath(input.path, ctx.agentRoot);

  // 1. 受保护目录检查
  for (const pattern of PROTECTED_DIRECTORIES) {
    if (pattern.test(resolvedPath)) {
      throw new ToolError('PATH_DENIED', `禁止写入受保护的目录: ${resolvedPath}`);
    }
  }

  // 2. 受保护文件检查（完整路径匹配，仅保护 agent 根目录下的特定文件）
  for (const relPath of PROTECTED_AGENT_RELATIVE_PATHS) {
    const protectedAbsPath = join(ctx.agentRoot, relPath);
    if (resolvedPath === protectedAbsPath) {
      throw new ToolError('WRITE_PROTECTED', `${relPath} 是受保护文件，不可修改`);
    }
  }

  // 3. mode 检查
  if (input.mode === 'create' && existsSync(resolvedPath)) {
    throw new ToolError('FILE_EXISTS', `文件已存在，无法以 create 模式创建: ${input.path}`);
  }

  // 4. 执行写入
  const dir = dirname(resolvedPath);
  if (!existsSync(dir)) {
    mkdirSync(dir, { recursive: true });
  }

  const encoding = input.encoding || 'utf-8';
  let bytesWritten: number;

  switch (input.mode) {
    case 'overwrite':
      writeFileSync(resolvedPath, input.content, { encoding });
      break;
    case 'append':
      appendFileSync(resolvedPath, '\n' + input.content, { encoding });
      break;
    case 'create':
      writeFileSync(resolvedPath, input.content, { flag: 'wx', encoding });
      break;
  }

  bytesWritten = Buffer.byteLength(input.content, encoding);

  // 5. 如果修改的是进化相关文件，自动记录
  const evolutionaryFiles = ['change_system.md', 'memory.md'];
  if (evolutionaryFiles.includes(fileName)) {
    await ctx.evolutionLog.record({
      action: 'write',
      target: resolvedPath,
      mode: input.mode,
      timestamp: new Date().toISOString(),
      summary: `${input.mode}: ${fileName} (${bytesWritten} bytes)`,
    });
  }

  return { success: true, path: resolvedPath, bytesWritten };
}
```

### 5.5 shell_exec 详细设计

```typescript
// tools/shell-exec.ts
// ★ 关键安全改进：使用 execFile 而非 exec，避免 shell 注入

import { execFile as execFileCallback } from 'child_process';
import { promisify } from 'util';

const execFile = promisify(execFileCallback);

interface ShellExecInput {
  command: string;
  timeout?: number;   // 超时毫秒数，默认 30000（30秒）
  cwd?: string;       // 工作目录，默认 agent root
  mode?: 'safe' | 'shell';  // 执行模式，默认 'safe'
  // safe:  使用 execFile（不经过 shell，不支持管道/重定向，防注入）
  // shell: 使用 exec（支持管道/重定向，但加强黑名单检查 + 审计日志）
}

// ★ 危险命令黑名单（正则匹配）
const DANGEROUS_COMMAND_PATTERNS = [
  /\brm\s+-rf\s+[\/~]/,              // rm -rf / 或 rm -rf ~
  /\brm\s+-rf\s+\.\./,              // rm -rf ..
  /\bmkfs\b/,                        // 格式化磁盘
  /\bdd\s+if=.*of=\/dev\//,          // dd 破坏性写入设备
  />\s*\/dev\/sd[a-z]/,              // 直接写硬盘
  /\bchmod\s+(777|a\+rwx)/,          // 危险权限
  /\bcurl.*\|\s*(bash|sh|python)/,   // 远程脚本注入
  /\bwget.*\|\s*(bash|sh)/,          // 同上
  /\bshutdown\b/,                    // 关机
  /\breboot\b/,                      // 重启
  /:()\{\s*:\|:&\s*\};:/,            // Fork 炸弹
  /\bgit\s+push\s+--force/,          // 强制推送
];

const DEFAULT_TIMEOUT_MS = 30_000;
const MAX_TIMEOUT_MS = 300_000;

/**
 * ★ 命令 tokenize 解析
 * 将命令字符串拆分为可执行文件和参数数组，
 * 用于 execFile 调用（不经过 shell 解释器，防止注入）
 */
function tokenizeCommand(command: string): { file: string; args: string[] } {
  // 简单的 shell-like tokenizer，处理引号和转义
  const tokens: string[] = [];
  let current = '';
  let inSingle = false;
  let inDouble = false;
  let escaped = false;

  for (const char of command) {
    if (escaped) {
      current += char;
      escaped = false;
      continue;
    }
    if (char === '\\') { escaped = true; continue; }
    if (char === "'" && !inDouble) { inSingle = !inSingle; continue; }
    if (char === '"' && !inSingle) { inDouble = !inDouble; continue; }
    if (char === ' ' && !inSingle && !inDouble) {
      if (current) { tokens.push(current); current = ''; }
      continue;
    }
    current += char;
  }
  if (current) tokens.push(current);

  return { file: tokens[0] || '', args: tokens.slice(1) };
}

async function shellExec(input: ShellExecInput, ctx: ToolContext): Promise<{
  exitCode: number;
  stdout: string;
  stderr: string;
  durationMs: number;
}> {
  const command = input.command.trim();

  // 1. 危险命令阻断
  for (const pattern of DANGEROUS_COMMAND_PATTERNS) {
    if (pattern.test(command)) {
      throw new ToolError(
        'COMMAND_BLOCKED',
        `危险命令已被拦截: ${command}\n匹配规则: ${pattern.source}`
      );
    }
  }

  // 2. 超时限制
  const timeout = Math.min(input.timeout ?? DEFAULT_TIMEOUT_MS, MAX_TIMEOUT_MS);

  // 3. 根据模式选择执行方式
  const mode = input.mode ?? 'safe';
  const startTime = Date.now();

  try {
    let stdout: string;
    let stderr: string;

    if (mode === 'safe') {
      // 安全模式：使用 execFile（不经过 shell，防注入）
      // ⚠️ 注意：此模式不支持管道(|)、重定向(>)、链式(&&)等 shell 特性
      const { file, args } = tokenizeCommand(command);
      const result = await execFile(file, args, {
        cwd: input.cwd ?? ctx.agentRoot,
        timeout,
        maxBuffer: 1024 * 1024,
        env: { ...process.env },
      });
      stdout = result.stdout;
      stderr = result.stderr;
    } else {
      // Shell 模式：使用 exec（支持管道/重定向，但需额外审计）
      // 记录审计日志
      ctx.logger.warn(`[shell_exec] Shell 模式执行: ${command}`);
      const result = await execPromise(command, {
        cwd: input.cwd ?? ctx.agentRoot,
        timeout,
        maxBuffer: 1024 * 1024,
        env: { ...process.env },
      });
      stdout = result.stdout;
      stderr = result.stderr;
    }

    return {
      exitCode: 0,
      stdout: stdout.trim(),
      stderr: stderr.trim(),
      durationMs: Date.now() - startTime,
    };

  } catch (error: any) {
    if (error.killed && error.signal === 'SIGTERM') {
      throw new ToolError('TIMEOUT', `命令执行超时 (${timeout}ms): ${command}`);
    }

    return {
      exitCode: error.code || 1,
      stdout: error.stdout?.trim() || '',
      stderr: error.stderr?.trim() || error.message,
      durationMs: Date.now() - startTime,
    };
  }
}
```

### 5.6 http_request 详细设计

```typescript
// tools/http-request.ts

interface HttpRequestInput {
  url: string;
  method: 'GET' | 'POST' | 'PUT' | 'DELETE' | 'PATCH';
  headers?: Record<string, string>;
  body?: string;
  timeout?: number;
  followRedirects?: boolean;
  maxResponseSize?: number;
}

const ALLOWED_PROTOCOLS = ['https:', 'http:'];
const DEFAULT_TIMEOUT_MS = 15_000;
const MAX_RESPONSE_SIZE = 5 * 1024 * 1024; // 5MB

async function httpRequest(input: HttpRequestInput, _ctx: ToolContext): Promise<{
  status: number;
  statusText: string;
  headers: Record<string, string>;
  body: string;
  contentType: string;
  sizeBytes: number;
  durationMs: number;
}> {
  // 1. URL 协议验证
  let parsed: URL;
  try {
    parsed = new URL(input.url);
  } catch {
    throw new ToolError('INVALID_URL', `无效的 URL: ${input.url}`);
  }

  if (!ALLOWED_PROTOCOLS.includes(parsed.protocol)) {
    throw new ToolError(
      'BLOCKED_PROTOCOL',
      `不允许的协议: ${parsed.protocol}（仅允许 http/https）`
    );
  }

  // 2. 发起请求
  const timeout = Math.min(input.timeout ?? DEFAULT_TIMEOUT_MS, 60_000);
  const maxSize = input.maxResponseSize ?? MAX_RESPONSE_SIZE;
  const startTime = Date.now();

  try {
    const response = await fetch(input.url, {
      method: input.method,
      headers: {
        'User-Agent': 'AIive-Agent/0.1',
        'Accept': 'application/json, text/plain, */*',
        ...(input.headers || {}),
      },
      body: ['POST', 'PUT', 'PATCH'].includes(input.method) ? input.body : undefined,
      signal: AbortSignal.timeout(timeout),
      redirect: input.followRedirects !== false ? 'follow' : 'manual',
    });

    // 3. 读取响应体（带大小限制，流式读取）
    const reader = response.body!.getReader();
    const chunks: Uint8Array[] = [];
    let totalRead = 0;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      totalRead += value.length;
      if (totalRead > maxSize) {
        reader.cancel();
        break;
      }
      chunks.push(value);
    }

    const buffer = new Uint8Array(chunks.reduce((sum, c) => sum + c.length, 0));
    let offset = 0;
    for (const chunk of chunks) {
      buffer.set(chunk, offset);
      offset += chunk.length;
    }

    // 4. 解码
    let body: string;
    try {
      body = new TextDecoder('utf-8', { fatal: false }).decode(buffer);
    } catch {
      body = `[BINARY_DATA: ${buffer.length} bytes, 无法文本解码]`;
    }

    if (totalRead > maxSize) {
      body = body.substring(0, Math.floor(maxSize * 0.9));
      body += `\n\n<!-- [RESPONSE_TRUNCATED: 超过 ${maxSize} 字节] -->`;
    }

    return {
      status: response.status,
      statusText: response.statusText,
      headers: Object.fromEntries(response.headers.entries()),
      body,
      contentType: response.headers.get('content-type') || 'unknown',
      sizeBytes: totalRead,
      durationMs: Date.now() - startTime,
    };

  } catch (error: any) {
    if (error.name === 'TimeoutError') {
      throw new ToolError('REQUEST_TIMEOUT', `请求超时 (${timeout}ms): ${input.url}`);
    }
    throw new ToolError('REQUEST_FAILED', `HTTP 请求失败: ${error.message}`);
  }
}
```

### 5.7 四大元工具在 CodeBuddy CLI 中的注册方式

```typescript
// tools/register.ts — 将四大元工具注册为 CodeBuddy CLI 的自定义工具
// 注意：CodeBuddy CLI 的程序化调用方式，具体 API 需在 PoC 阶段验证

import { z } from 'zod';

// 工具定义（Schema + 执行函数）
export const coreTools = {
  file_read: {
    description: '读取文件内容，支持指定行范围。自动检测 UTF-8/Latin-1 编码，超过 100KB 自动截断。',
    parameters: z.object({
      path: z.string().describe('文件路径（相对或绝对）'),
      offset: z.number().optional().describe('起始行号（从 1 开始）'),
      limit: z.number().optional().describe('读取的最大行数'),
      encoding: z.enum(['utf-8', 'latin-1']).optional().describe('强制指定编码'),
    }),
    execute: fileRead,
  },
  file_write: {
    description: '创建/覆盖/追加文件内容。受保护目录和文件不可写入。',
    parameters: z.object({
      path: z.string().describe('文件路径'),
      content: z.string().describe('要写入的内容'),
      mode: z.enum(['overwrite', 'create', 'append']).describe('写入模式'),
      encoding: z.enum(['utf-8', 'latin-1']).optional(),
    }),
    execute: fileWrite,
  },
  shell_exec: {
    description: '执行 Shell 命令。支持双模式：safe（execFile 防注入）和 shell（支持管道/重定向）。内置危险命令拦截和超时保护。',
    parameters: z.object({
      command: z.string().describe('要执行的命令'),
      timeout: z.number().optional().describe('超时毫秒数，默认 30000'),
      cwd: z.string().optional().describe('工作目录'),
      mode: z.enum(['safe', 'shell']).optional().describe('执行模式，默认 safe'),
    }),
    execute: shellExec,
  },
  http_request: {
    description: '发起 HTTP 请求。仅允许 http/https 协议，响应体限制 5MB。',
    parameters: z.object({
      url: z.string().describe('请求 URL'),
      method: z.enum(['GET', 'POST', 'PUT', 'DELETE', 'PATCH']),
      headers: z.record(z.string()).optional(),
      body: z.string().optional(),
      timeout: z.number().optional(),
    }),
    execute: httpRequest,
  },
};
```

### 5.8 Token 估算策略

> **使用方案 B**：通过 `@anthropic-ai/tokenizer` 精确计算 token 数，确保上下文管理可靠。

```typescript
// utils/tokens.ts

import { countTokens } from '@anthropic-ai/tokenizer';

/**
 * 精确计算文本的 token 数
 * 使用 Anthropic 官方 tokenizer，与 Claude 模型的实际 token 计算一致
 */
export function estimateTokens(text: string): number {
  return countTokens(text);
}

/**
 * 批量计算多段文本的 token 数
 */
export function estimateTokensBatch(texts: string[]): number {
  return texts.reduce((sum, text) => sum + countTokens(text), 0);
}

/**
 * 检查是否超过 token 预算
 */
export function isOverBudget(
  texts: string[],
  budgetTokens: number,
): { over: boolean; total: number; remaining: number } {
  const total = estimateTokensBatch(texts);
  return {
    over: total > budgetTokens,
    total,
    remaining: budgetTokens - total,
  };
}
```

### 5.9 file_patch 工具设计（Phase 2 实现）

> **背景**：当前 `file_write` 只支持 `overwrite`/`append`/`create` 三种模式，但 Agent 经常需要**修改文件中的特定部分**。全量覆盖会丢失上下文，append 只能追加。`file_patch` 支持 search-and-replace 模式，是 Claude Code 和 Cursor 的核心编辑模式。

```typescript
// tools/file-patch.ts — Phase 2 实现

interface FilePatchInput {
  path: string;
  old_string: string;   // 要替换的内容（必须精确匹配）
  new_string: string;   // 替换后的内容
  encoding?: 'utf-8' | 'latin-1';
}

interface FilePatchOutput {
  success: boolean;
  path: string;
  matchCount: number;     // old_string 在文件中出现的次数
  replaced: boolean;      // 是否成功替换
}

async function filePatch(input: FilePatchInput, ctx: ToolContext): Promise<FilePatchOutput> {
  const resolvedPath = resolvePath(input.path, ctx.agentRoot);

  // 1. 安全检查（复用 file_write 的检查逻辑）
  validateWriteAccess(resolvedPath, ctx);

  // 2. 读取文件内容
  const encoding = input.encoding || 'utf-8';
  const content = readFileSync(resolvedPath, { encoding });

  // 3. 查找匹配
  const matchCount = content.split(input.old_string).length - 1;

  if (matchCount === 0) {
    return { success: false, path: resolvedPath, matchCount: 0, replaced: false };
  }

  if (matchCount > 1) {
    // 多处匹配时不自动替换，返回匹配数让 Agent 决策
    return { success: false, path: resolvedPath, matchCount, replaced: false };
  }

  // 4. 精确替换（仅匹配 1 处时自动执行）
  const newContent = content.replace(input.old_string, input.new_string);
  writeFileSync(resolvedPath, newContent, { encoding });

  return { success: true, path: resolvedPath, matchCount: 1, replaced: true };
}
```

---

## 6. Session 与上下文管理

> **本章节学习 Claude Code 的 Session 管理和 Compact 机制设计。**

### 6.1 Session 生命周期

```
┌─────────────────────────────────────────────────────────┐
│                    Session 生命周期                       │
│                                                          │
│  创建 ──→ 活跃 ──→ 压缩 ──→ 继续/结束                    │
│                     ↑                                    │
│                     └── 上下文接近阈值时触发               │
└─────────────────────────────────────────────────────────┘

Session 状态:
  • ACTIVE    — 正常对话中
  • COMPACTING — 正在执行记忆压缩
  • IDLE      — 空闲等待（超时后自动清理）
  • CLOSED    — 已结束
```

### 6.2 Session 数据结构

```typescript
// core/session.ts

interface Session {
  id: string;                    // UUID
  createdAt: string;             // ISO 8601
  lastActiveAt: string;
  status: 'active' | 'compacting' | 'idle' | 'closed';

  // 消息历史（内存中维护）
  messages: Message[];
  
  // Token 统计
  tokenUsage: {
    systemPrompt: number;        // 固定系统提示词占用
    conversationHistory: number; // 对话历史占用
    toolResults: number;         // 工具返回结果占用
    total: number;               // 当前总占用
    limit: number;               // 上下文窗口上限
  };

  // 压缩历史
  compactionCount: number;       // 已压缩次数
  lastCompactedAt?: string;
}

interface Message {
  role: 'user' | 'assistant' | 'system' | 'tool';
  content: string;
  timestamp: string;
  tokenCount: number;            // 该消息的 token 数
  toolCalls?: ToolCallRecord[];  // 如果是 assistant 消息，记录工具调用
}
```

### 6.3 上下文压缩机制（学习 Claude Code /compact）

> **核心思路**：当对话历史的 token 数接近上下文窗口阈值时，提醒 LLM 调用 `memory_compress` 工具，将历史对话压缩成结构化记忆，写入 memory 文档。

```
上下文预算分配:
┌──────────────────────────────────────────────────┐
│  总上下文窗口（如 200K tokens）                     │
│                                                    │
│  ┌──────────────┐  固定区域（~10%）                │
│  │ system.md    │  系统提示词 + soul + agent 等     │
│  │ soul.md      │  不可压缩                        │
│  │ agent.md     │                                  │
│  │ memory.md    │                                  │
│  │ skills 列表   │                                  │
│  └──────────────┘                                  │
│                                                    │
│  ┌──────────────┐  弹性区域（~90%）                │
│  │ 对话历史      │  可被压缩                        │
│  │ 工具返回      │  可被压缩                        │
│  │ 按需加载内容   │  可被释放                        │
│  └──────────────┘                                  │
└──────────────────────────────────────────────────┘

压缩触发条件（学习 Claude Code 的三级预警）:
─────────────────────────────────────────────
  🟢 正常:     总 token < 上下文窗口 × 70%
  🟡 黄色预警:  总 token > 上下文窗口 × 80%
     → 在 system prompt 中注入提醒:
       「上下文即将满载，建议调用 memory_compress 压缩历史」
  🚨 自动压缩:  总 token > 上下文窗口 × 93%
     → 自动触发压缩流程（不等 LLM 主动调用）
     → 熔断机制：连续 3 次压缩失败后停止自动重试，避免浪费 API 调用
  🔴 硬限制:    总 token > 上下文窗口 × 97%
     → 强制截断最早的消息
```

### 6.4 memory_compress 工具设计

```typescript
// tools/memory-compress.ts — 作为第 5 个内置工具（非元工具，而是系统工具）

interface MemoryCompressInput {
  scope: 'all' | 'before_last_n';  // 压缩全部历史 or 保留最近 N 条
  keepLastN?: number;               // 保留最近 N 条消息，默认 5
  summary?: string;                 // LLM 可以提供自己的摘要（可选）
}

async function memoryCompress(input: MemoryCompressInput, ctx: ToolContext): Promise<{
  compressedMessages: number;
  savedTokens: number;
  memoryWrittenTo: string;
}> {
  const session = ctx.currentSession;
  const keepN = input.keepLastN ?? 5;

  // 1. 确定要压缩的消息范围
  const toCompress = input.scope === 'all'
    ? session.messages.slice(0, -1)           // 压缩全部，仅保留最后 1 条（当前用户消息）
    : session.messages.slice(0, -keepN);       // 保留最近 N 条，压缩其余

  // 2. 生成压缩摘要（如果 LLM 没有提供，则自动生成）
  let summary = input.summary;
  if (!summary) {
    // 调用 LLM 生成摘要（使用快速模型降低成本）
    summary = await generateSummary(toCompress, ctx);
  }

  // 3. 写入短期记忆 JSONL
  const memoryEntry = {
    id: crypto.randomUUID(),
    timestamp: new Date().toISOString(),
    sessionId: session.id,
    type: 'session_compact',
    summary,
    messageCount: toCompress.length,
    tokensSaved: toCompress.reduce((sum, m) => sum + m.tokenCount, 0),
    tags: extractTags(summary),
  };
  
  await appendJsonl(
    `${ctx.agentRoot}/memories/short-term/session.jsonl`,
    memoryEntry
  );

  // 4. 从 session 中移除已压缩的消息
  session.messages = session.messages.slice(-keepN);

  // 5. 在保留的消息前插入压缩摘要
  session.messages.unshift({
    role: 'system',
    content: `[历史对话已压缩] 以下是之前对话的摘要:\n${summary}`,
    timestamp: new Date().toISOString(),
    tokenCount: estimateTokens(summary),
  });

  // 6. 更新 token 统计
  session.tokenUsage.conversationHistory = session.messages.reduce(
    (sum, m) => sum + m.tokenCount, 0
  );
  session.compactionCount++;
  session.lastCompactedAt = new Date().toISOString();

  return {
    compressedMessages: toCompress.length,
    savedTokens: memoryEntry.tokensSaved,
    memoryWrittenTo: 'memories/short-term/session.jsonl',
  };
}
```

### 6.5 Session 持久化

```
Session 持久化策略:
├── 内存中维护活跃 Session（单用户，同时只有一个活跃 Session）
├── 定期保存 Session 快照到 memories/short-term/active.json（每 5 条消息自动保存）
├── Session 结束时，将摘要写入 memories/short-term/session.jsonl
├── 服务重启后，优先从 active.json 恢复完整 Session 状态
├── 如果 active.json 损坏，fallback 到 session.jsonl 的最近摘要
└── 支持 /resume 命令恢复上一个 Session

文件存储:
  memories/short-term/session.jsonl  — 所有 Session 的压缩记忆
  memories/short-term/active.json    — 当前活跃 Session 的完整快照（包含消息历史）
```

---

## 7. 记忆系统设计

### 7.1 双层架构：短期记忆 + 长期记忆

```
┌─────────────────────────────────────────────────────────┐
│                     记忆系统                              │
│                                                          │
│  ┌─────────────────────────────────────────────────┐    │
│  │  短期记忆 (Short-term Memory)                    │    │
│  │  存储: JSONL 格式                                │    │
│  │  位置: memories/short-term/session.jsonl          │    │
│  │  特点:                                           │    │
│  │    • 结构化 JSON，每行一条记忆                     │    │
│  │    • 支持快速检索（内存 Map 索引 + 流式扫描）      │    │
│  │    • 自动过期（7 天后迁移到长期记忆或丢弃）        │    │
│  │    • 主要存储：会话压缩摘要、临时事实、短期任务     │    │
│  └─────────────────────────────────────────────────┘    │
│                          │                               │
│                    沉淀 / 迁移                            │
│                          ▼                               │
│  ┌─────────────────────────────────────────────────┐    │
│  │  长期记忆 (Long-term Memory)                     │    │
│  │  存储: Markdown 文档                              │    │
│  │  分为两类:                                        │    │
│  │                                                   │    │
│  │  📅 编年体 (memories/long-term/by-date/)           │    │
│  │     按日期归档，每天一个文件                        │    │
│  │     记录「那天发生了什么」                          │    │
│  │     例: 2026-04-16.md                             │    │
│  │                                                   │    │
│  │  📖 纪传体 (memories/long-term/by-event/)          │    │
│  │     按事件/主题归档，跨时间线                       │    │
│  │     记录「关于某件事的所有信息」                     │    │
│  │     例: project-aiive.md, user-preferences.md     │    │
│  └─────────────────────────────────────────────────┘    │
│                                                          │
│  ┌─────────────────────────────────────────────────┐    │
│  │  记忆索引 (memories/index.jsonl)                   │    │
│  │  用于快速检索所有记忆的元数据                       │    │
│  │  启动时加载到内存 Map 中                           │    │
│  └─────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────┘
```

### 7.2 短期记忆 — JSONL 格式与快速检索

> **为什么用 JSONL？**
> - 每行一个 JSON 对象，追加写入极快（O(1)）
> - 流式读取，不需要一次性加载整个文件
> - 天然支持时间序列（新记忆追加在末尾）
> - Node.js readline 模块可以高效逐行扫描

```jsonl
{"id":"mem-001","ts":"2026-04-16T10:30:00Z","type":"session_compact","tags":["react","bug"],"summary":"用户遇到 React useEffect 闪烁问题，通过 Zustand 状态管理解决","ttl":7}
{"id":"mem-002","ts":"2026-04-16T14:00:00Z","type":"fact","tags":["user"],"summary":"用户偏好 TypeScript 严格模式","ttl":30}
{"id":"mem-003","ts":"2026-04-16T15:20:00Z","type":"task","tags":["aiive","phase1"],"summary":"Phase 1 的 file_read 工具已完成实现","ttl":14}
```

**快速检索方案**：

```typescript
// memory/short-term.ts

/**
 * 短期记忆管理器
 * 
 * 检索策略（按效率从高到低）:
 * 1. 内存 Map 索引 — 启动时从 JSONL 构建，O(1) 按 tag/id 查找
 * 2. 流式扫描 — 用 readline 逐行扫描 JSONL，适合复杂条件查询
 * 3. 全文搜索 — grep 命令快速搜索关键词（利用 shell_exec）
 */
class ShortTermMemory {
  // 内存索引：tag → MemoryEntry[]
  private tagIndex = new Map<string, MemoryEntry[]>();
  // 内存索引：id → MemoryEntry
  private idIndex = new Map<string, MemoryEntry>();
  
  private jsonlPath: string;

  constructor(agentRoot: string) {
    this.jsonlPath = `${agentRoot}/memories/short-term/session.jsonl`;
  }

  /**
   * 启动时从 JSONL 文件构建内存索引
   */
  async buildIndex(): Promise<void> {
    const rl = readline.createInterface({
      input: createReadStream(this.jsonlPath),
      crlfDelay: Infinity,
    });

    for await (const line of rl) {
      if (!line.trim()) continue;
      const entry: MemoryEntry = JSON.parse(line);
      
      // 跳过已过期的记忆
      if (this.isExpired(entry)) continue;
      
      this.idIndex.set(entry.id, entry);
      for (const tag of entry.tags) {
        if (!this.tagIndex.has(tag)) this.tagIndex.set(tag, []);
        this.tagIndex.get(tag)!.push(entry);
      }
    }
  }

  /**
   * 按标签检索（O(1) 查找 + 过滤）
   */
  findByTag(tag: string): MemoryEntry[] {
    return this.tagIndex.get(tag)?.filter(e => !this.isExpired(e)) || [];
  }

  /**
   * 按关键词搜索（遍历内存索引的 summary 字段）
   */
  search(keyword: string): MemoryEntry[] {
    const results: MemoryEntry[] = [];
    for (const entry of this.idIndex.values()) {
      if (this.isExpired(entry)) continue;
      if (entry.summary.includes(keyword) || entry.tags.some(t => t.includes(keyword))) {
        results.push(entry);
      }
    }
    return results.sort((a, b) => b.ts.localeCompare(a.ts)); // 最新的在前
  }

  /**
   * 追加新记忆
   */
  async append(entry: MemoryEntry): Promise<void> {
    await appendFile(this.jsonlPath, JSON.stringify(entry) + '\n');
    this.idIndex.set(entry.id, entry);
    for (const tag of entry.tags) {
      if (!this.tagIndex.has(tag)) this.tagIndex.set(tag, []);
      this.tagIndex.get(tag)!.push(entry);
    }
  }

  /**
   * 定期清理过期记忆（原子写入，防止崩溃丢数据）
   */
  async gc(): Promise<number> {
    const alive: MemoryEntry[] = [];
    for (const entry of this.idIndex.values()) {
      if (!this.isExpired(entry)) alive.push(entry);
    }
    // ★ 使用 write-then-rename 原子写入模式，防止写入过程中崩溃导致数据丢失
    const tmpPath = this.jsonlPath + '.tmp';
    await writeFile(tmpPath, alive.map(e => JSON.stringify(e)).join('\n') + '\n');
    await rename(tmpPath, this.jsonlPath);  // rename 是原子操作
    // 重建索引
    this.tagIndex.clear();
    this.idIndex.clear();
    for (const entry of alive) {
      this.idIndex.set(entry.id, entry);
      for (const tag of entry.tags) {
        if (!this.tagIndex.has(tag)) this.tagIndex.set(tag, []);
        this.tagIndex.get(tag)!.push(entry);
      }
    }
    return alive.length;
  }

  private isExpired(entry: MemoryEntry): boolean {
    if (!entry.ttl) return false;
    const expireAt = new Date(entry.ts).getTime() + entry.ttl * 24 * 60 * 60 * 1000;
    return Date.now() > expireAt;
  }
}
```

### 7.3 长期记忆 — 编年体与纪传体

#### 编年体（by-date）— 按日期归档

```markdown
<!-- memories/long-term/by-date/2026-04-16.md -->
# 2026-04-16 记忆日志

## 上午
- 10:30 开始 AIive 项目的 Phase 1 开发
- 11:00 完成 file_read 工具的基础实现
- 11:45 讨论了记忆系统的 JSONL 方案

## 下午
- 14:00 用户提出将记忆分为编年体和纪传体 → 详见 [AIive 项目](../by-event/project-aiive.md)
- 15:30 完成 Session 压缩机制的设计
- 16:00 修复了 shell_exec 的安全漏洞（exec → execFile）

## 关键决策
- 决定使用 JSONL 作为短期记忆格式
- 决定不做多用户，保持单用户自用模式
```

#### 纪传体（by-event）— 按事件/主题归档

```markdown
<!-- memories/long-term/by-event/project-aiive.md -->
# AIive 项目记忆

## 项目概况
- 定位：自进化 AI Agent 平台
- 核心理念：Agent 即文件系统
- 技术栈：TypeScript + CodeBuddy CLI + Hono + React

## 开发进展
### Phase 1（进行中）
- [x] 项目初始化
- [x] 四大元工具设计
- [ ] CodeBuddy CLI 集成
- [ ] Session 管理

## 关键技术决策
- 2026-04-16: 选择 JSONL 作为短期记忆格式（理由：追加快、流式读取）
- 2026-04-16: shell_exec 改用 execFile（理由：防止 shell 注入）

## 时间线引用
- 2026-04-16: 项目启动 → 详见 [当日日志](../by-date/2026-04-16.md)
```

### 7.4 memory.md 概览格式

> **注意**：memory.md 是注入 Prompt 的概览文件，必须保持精简（< 200 行），只包含索引信息。

```markdown
# 记忆概览

> 此文件由 Agent 和人类共同维护。
> Agent 需要详细记忆时，通过 file_read 读取对应文件。

## 短期记忆（最近 7 天）
- 短期记忆存储在 memories/short-term/session.jsonl
- 当前共 12 条活跃记忆
- 最近话题：AIive Phase 1 开发、记忆系统设计

## 长期记忆索引

### 编年体（按日期）
| 日期 | 文件 | 关键事件 |
|---|---|---|
| 2026-04-16 | memories/long-term/by-date/2026-04-16.md | AIive 项目启动、技术方案确定 |

### 纪传体（按主题）
| 主题 | 文件 | 摘要 | 重要度 |
|---|---|---|---|
| AIive 项目 | memories/long-term/by-event/project-aiive.md | 项目开发进展和技术决策 | 🔴 高 |
| 用户偏好 | memories/long-term/by-event/user-preferences.md | 技术栈、输出风格偏好 | 🔴 高 |

## 快速引用（常用事实）
- 用户偏好中文输出 + 简洁 Markdown
- 当前项目：AIive 自进化 Agent
- 开发环境：Mac mini M4 32GB
- 技术栈：React / TypeScript / Zustand / Tailwind
```

### 7.5 记忆的生命周期

```
事件发生（对话、工具调用、进化操作）
   │
   ▼
写入短期记忆 (memories/short-term/session.jsonl)
   │
   ▼
定期评估（每次 Session 结束时 or 每日定时）
   ├─ 不值得保留 → TTL 过期后自动清理
   │
   └─ 值得长期保存 →
      ├─ 写入编年体 (memories/long-term/by-date/YYYY-MM-DD.md)
      └─ 写入纪传体 (memories/long-term/by-event/xxx.md)
         │
         ▼
      更新 memory.md 概览索引
         │
         ▼
      更新 memories/index.jsonl 检索索引
```

---

## 8. 流式输出设计

### 8.1 架构选择：SSE（Server-Sent Events）

```
为什么不选 WebSocket?
├── WS 是双向的，但我们只需要 Server → Client 单向流
├── WS 需要管理连接状态、心跳、重连逻辑
├── SSE 内置自动重连（浏览器原生 EventSource）
├── SSE 可以被 HTTP 缓存/CDN 加速
└── SSE 的 API 更简单

什么时候需要升级到 WebSocket?
├── 需要客户端随时中断正在执行的 Agent
├── 需要实时上传文件/图片给 Agent
└── 需要多路复用多个并发对话
→ 后续版本再考虑升级
```

### 8.2 SSE 事件协议定义

```typescript
// types/sse.ts

type SSEEvent =
  | { event: 'thinking'; data: string }          // 正在思考
  | { event: 'message_start'; data: MessageMeta } // 消息开始
  | { event: 'content_delta'; data: string }      // 文本增量（打字机效果）
  | { event: 'tool_use'; data: ToolUseInfo }      // 工具调用开始
  | { event: 'tool_result'; data: ToolResultInfo } // 工具调用结果
  | { event: 'context_warning'; data: ContextWarning } // 上下文预警
  | { event: 'message_done'; data: MessageStats } // 消息完成
  | { event: 'error'; data: ErrorInfo }           // 错误
  | { event: 'done'; data: null };                // 整个请求结束

interface MessageMeta {
  id: string;
  role: 'assistant';
  model: string;
  createdAt: string;
}

interface ToolUseInfo {
  id: string;
  name: string;
  input: Record<string, unknown>;
}

interface ToolResultInfo {
  toolUseId: string;
  output: string;
  isOk: boolean;
}

interface ContextWarning {
  level: 'yellow' | 'red';
  usagePercent: number;
  message: string;
}

interface MessageStats {
  totalTokens: number;
  durationMs: number;
  toolCalls: number;
  evolutions: number;
  contextUsagePercent: number;
}
```

### 8.3 前端消费示例（React + Zustand）

```typescript
// web/src/stores/chatStore.ts
import { create } from 'zustand';

interface ChatStore {
  messages: ChatMessage[];
  isStreaming: boolean;
  contextUsage: number;
  sendMessage: (content: string) => void;
}

export const useChatStore = create<ChatStore>((set, get) => ({
  messages: [],
  isStreaming: false,
  contextUsage: 0,

  sendMessage: async (content: string) => {
    // 添加用户消息
    set(state => ({
      messages: [...state.messages, { role: 'user', content, id: crypto.randomUUID() }],
      isStreaming: true,
    }));

    // ★ 改进：先 POST 发起对话，获取 sessionId，再用 GET + sessionId 建立 SSE 连接
    // 避免 GET query string 的长度限制和日志泄露问题
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: content }),
    });
    const { sessionId } = await res.json();

    const es = new EventSource(`/api/chat/stream/${sessionId}`);
    let assistantId = '';

    es.addEventListener('message_start', (e) => {
      const meta = JSON.parse(e.data);
      assistantId = meta.id;
      set(state => ({
        messages: [...state.messages, { role: 'assistant', content: '', id: assistantId }],
      }));
    });

    es.addEventListener('content_delta', (e) => {
      const text = JSON.parse(e.data);  // ★ 服务端 JSON.stringify 了，客户端需要 parse
      set(state => ({
        messages: state.messages.map(m =>
          m.id === assistantId ? { ...m, content: m.content + text } : m
        ),
      }));
    });

    es.addEventListener('tool_use', (e) => {
      const info = JSON.parse(e.data);
      // 展示工具调用状态
      set(state => ({
        messages: state.messages.map(m =>
          m.id === assistantId
            ? { ...m, toolCalls: [...(m.toolCalls || []), { ...info, status: 'running' }] }
            : m
        ),
      }));
    });

    es.addEventListener('context_warning', (e) => {
      const warning = JSON.parse(e.data);
      set({ contextUsage: warning.usagePercent });
    });

    es.addEventListener('done', () => {
      es.close();
      set({ isStreaming: false });
    });

    es.addEventListener('error', () => {
      es.close();
      set({ isStreaming: false });
    });
  },
}));
```

### 8.4 CodeBuddy CLI 流式对接

```typescript
// api/chat.ts — SSE 端点实现

import { Hono } from 'hono';
import { spawn } from 'child_process';

const app = new Hono();

app.post('/api/chat', async (c) => {
  const { message } = await c.req.json();
  const sessionId = crypto.randomUUID();

  // 将消息和 sessionId 存入待处理队列（内存 Map）
  pendingChats.set(sessionId, { message, createdAt: Date.now() });

  return c.json({ sessionId });
});

app.get('/api/chat/stream/:sessionId', async (c) => {
  const sessionId = c.req.param('sessionId');
  const pending = pendingChats.get(sessionId);
  if (!pending) {
    return c.json({ error: 'Session not found' }, 404);
  }
  pendingChats.delete(sessionId);
  const message = pending.message;

  const stream = new ReadableStream({
    async start(controller) {
      const encoder = new TextEncoder();

      const send = (event: string, data: unknown) => {
        controller.enqueue(encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`));
      };

      send('message_start', {
        id: crypto.randomUUID(),
        role: 'assistant',
        model: 'codebuddy-internal',
        createdAt: new Date().toISOString(),
      });

      try {
        // CodeBuddy CLI 程序化调用
        // 具体 API 在 PoC 阶段验证后确定
        const prompt = await assemblePrompt(message);
        const result = await callCodeBuddy(prompt, {
          maxTurns: parseInt(process.env.MAX_TURNS || '15'),
          onContentDelta: (text: string) => send('content_delta', text),
          onToolUse: (info: ToolUseInfo) => send('tool_use', info),
          onToolResult: (result: ToolResultInfo) => send('tool_result', result),
        });

        send('message_done', {
          durationMs: result.durationMs,
          totalTokens: result.totalTokens,
          toolCalls: result.toolCallCount,
          evolutions: result.evolutionCount,
          contextUsagePercent: result.contextUsagePercent,
        });

        send('done', null);
        controller.close();

      } catch (error) {
        send('error', { message: (error as Error).message });
        controller.close();
      }
    },
  });

  return new Response(stream, {
    headers: {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
      Connection: 'keep-alive',
    },
  });
});
```

---

## 9. 自进化机制

### 9.1 什么是自进化？

> **自进化 = Agent 利用四大元工具修改自己的文件系统，从而改变自身的行为、能力或知识。**

这不是科幻里的「AI 自我意识」，而是一个**工程上可控的闭环**：

```
感知（读自己的文件）
  → 推理（LLM 决策：我应该改进什么）
  → 行动（用 file_write 修改 change_system.md / 新建 skill）
  → 验证（下次执行时观察效果）
  → 反思（如果效果不好就回滚）
```

### 9.2 自进化的三种模式

#### Mode A: 微调行为（修改 change_system.md）

```
触发条件示例:
  「我注意到用户经常要我输出表格格式的内容」

进化动作:
  1. file_read(change_system.md) — 读取当前的动态提示词
  2. 在其中追加一行: 「默认情况下，尽量用 Markdown 表格呈现结构化数据」
  3. 记录到 .evolution-log/

效果: 从此以后 Agent 会更倾向输出表格格式
```

#### Mode B: 获取新技能（新建 skill 文件）

```
触发条件示例:
  「我已经连续 3 次手动执行『读取 package.json → 分析依赖 → 搜索最新版本』这个流程」

进化动作:
  1. file_write('skills/dep-analyzer/SKILL.md', ...) — 创建技能描述
  2. file_write('skills/dep-analyzer/impl.md', ...) — 创建使用指南
  3. file_append(memory.md, ...) — 注册新技能到索引
  4. 记录到 .evolution-log/（作为一个事务）

效果: 下次用户说「检查依赖」时，Agent 直接调用 dep-analyzer 技能
```

#### Mode C: 知识沉淀（写入 knowledges 或 memories）

```
触发条件示例:
  「用户告诉我 React 19 的 useOptimistic API 有个 breaking change」

进化动作:
  1. file_write('knowledges/react/use-optimistic-v19.md', ...)
  2. file_append('memories/long-term/by-event/react-knowledge.md', ...)
  3. 更新 memory.md 概览
  4. .evolution-log/ 记录

效果: 未来所有关于 React 的对话都能利用这个知识
```

### 9.3 进化安全约束（防失控）

```
自进化必须遵守的安全规则（写入 soul.md 并由代码强制执行）:

1. 【备份原则】
   修改任何文件前，必须先复制一份 .bak 到 .evolution-log/backups/

2. 【事务原子性】★ 改进：一次进化允许多文件操作，但作为原子事务
   一次进化事务可以包含多个文件操作（如创建 skill + 更新 memory.md），
   但必须作为一个原子单元记录和回滚。
   事务中任何一步失败，整个事务回滚。

3. 【回滚窗口】
   每次修改后的 24 小时内，如果用户说「撤销上次进化」，
   Agent 必须能用 .bak 恢复原状（整个事务回滚）

4. 【审计追踪】
   每次自进化事务必须写入 .evolution-log/ 包含:
   - 事务 ID
   - 时间戳
   - 操作列表（每个文件的 modify / create / delete）
   - 目标文件路径列表
   - 变更原因（Agent 自己写的理由）
   - 变更前后的 diff 摘要
   - 影响评估

5. 【禁区】（代码层面硬阻止）
   - 不能修改 soul.md
   - 不能修改 system.md
   - 不能删除 tools/ 下的四个元工具文件
   - 不能修改 .evolution-log/ 中已有的历史记录
   - 不能写入受保护目录（node_modules, .git, dist 等）

6. 【审批制】（Phase 2 实现）
   涉及以下操作的进化需要用户确认:
   - 修改 memory.md 的索引结构
   - 删除已有的 skill 或 knowledge
   - 修改 change_system.md 超过 30% 的内容
   - 创建涉及网络请求的新 skill

7. 【并发控制】
   进化事务执行前必须获取文件锁，防止并发进化操作导致文件冲突：
   锁文件: .evolution-log/.lock
   获取锁 → 执行事务 → 释放锁
   如果锁已被占用，等待最多 10 秒后报错
```

### 9.4 进化日志格式

```markdown
---
id: evo-20260416-001
timestamp: 2026-04-16T14:30:00Z
transaction:
  - action: modify
    target: change_system.md
    backup: .evolution-log/backups/change_system.md.bak.20260416T143000
  - action: modify
    target: memory.md
    backup: .evolution-log/backups/memory.md.bak.20260416T143000
reason: 观察到用户偏好表格输出，调整默认输出格式并更新记忆索引
confidence: high
approved: auto
rollback_available: true
---

## 变更详情

### change_system.md 变更
**变更前（片段）**
```
## 输出偏好
- 使用简洁的 Markdown 格式
```

**变更后（片段）**
```
## 输出偏好
- 使用简洁的 Markdown 格式
- 结构化数据优先使用表格呈现（NEW）
```

### 影响评估
- 影响范围: 所有未来的文本输出
- 风险等级: 低
- 验证方法: 下次输出结构化数据时观察是否使用表格
```

---

## 10. 错误处理与重试策略

### 10.1 RetryPolicy 设计

```typescript
// core/retry.ts

interface RetryPolicy {
  maxRetries: number;          // 最大重试次数
  baseDelayMs: number;         // 基础延迟（毫秒）
  maxDelayMs: number;          // 最大延迟
  backoffMultiplier: number;   // 退避倍数
  retryableErrors: string[];   // 可重试的错误类型
}

const DEFAULT_RETRY_POLICY: RetryPolicy = {
  maxRetries: 3,
  baseDelayMs: 1000,
  maxDelayMs: 30000,
  backoffMultiplier: 2,
  retryableErrors: [
    'NETWORK_ERROR',
    'TIMEOUT',
    'RATE_LIMITED',
    'SERVICE_UNAVAILABLE',
    'INTERNAL_SERVER_ERROR',
  ],
};

/**
 * 带指数退避的重试执行器
 */
async function withRetry<T>(
  fn: () => Promise<T>,
  policy: RetryPolicy = DEFAULT_RETRY_POLICY,
  onRetry?: (attempt: number, error: Error, delayMs: number) => void,
): Promise<T> {
  let lastError: Error;

  for (let attempt = 0; attempt <= policy.maxRetries; attempt++) {
    try {
      return await fn();
    } catch (error: any) {
      lastError = error;

      // 不可重试的错误直接抛出
      if (!policy.retryableErrors.some(e => error.code === e || error.message?.includes(e))) {
        throw error;
      }

      // 已达最大重试次数
      if (attempt === policy.maxRetries) break;

      // 计算退避延迟（指数退避 + 随机抖动）
      const delay = Math.min(
        policy.baseDelayMs * Math.pow(policy.backoffMultiplier, attempt) + Math.random() * 1000,
        policy.maxDelayMs,
      );

      onRetry?.(attempt + 1, error, delay);
      await sleep(delay);
    }
  }

  throw lastError!;
}
```

### 10.2 模型降级策略

```typescript
// core/model-fallback.ts

/**
 * 模型降级链
 * 从 .env 中读取多个 API Key 和模型，按顺序尝试
 * 
 * .env 示例:
 * CODEBUDDY_API_KEYS=sk-primary,sk-backup1,sk-backup2
 * CODEBUDDY_MODELS=model-a,model-b,model-c
 * CODEBUDDY_FALLBACK_ORDER=0,1,2
 */
class ModelFallback {
  private models: { apiKey: string; model: string }[];
  private currentIndex = 0;
  private consecutiveFailures = 0;

  constructor() {
    const keys = (process.env.CODEBUDDY_API_KEYS || '').split(',');
    const models = (process.env.CODEBUDDY_MODELS || '').split(',');
    const order = (process.env.CODEBUDDY_FALLBACK_ORDER || '0').split(',').map(Number);

    this.models = order.map(i => ({
      apiKey: keys[i]?.trim(),
      model: models[i]?.trim(),
    })).filter(m => m.apiKey && m.model);
  }

  /**
   * 获取当前应使用的模型配置
   */
  getCurrent(): { apiKey: string; model: string } {
    return this.models[this.currentIndex] || this.models[0];
  }

  /**
   * 报告失败，触发降级
   */
  reportFailure(): boolean {
    this.consecutiveFailures++;
    if (this.consecutiveFailures >= 2 && this.currentIndex < this.models.length - 1) {
      this.currentIndex++;
      this.consecutiveFailures = 0;
      return true; // 已降级
    }
    return false; // 无法继续降级
  }

  /**
   * 报告成功，重置失败计数
   */
  reportSuccess(): void {
    this.consecutiveFailures = 0;
  }

  /**
   * 定时探测主模型是否恢复，如果恢复则自动升级回主模型
   * 每 5 分钟尝试用主模型发一个轻量请求
   */
  private probeTimer: ReturnType<typeof setInterval> | null = null;

  startProbing(): void {
    if (this.currentIndex === 0) return; // 已经在主模型，无需探测
    this.probeTimer = setInterval(async () => {
      try {
        // 尝试用主模型发一个轻量请求
        await this.probePrimaryModel();
        // 成功，切回主模型
        this.currentIndex = 0;
        this.consecutiveFailures = 0;
        if (this.probeTimer) clearInterval(this.probeTimer);
        this.probeTimer = null;
      } catch {
        // 主模型仍不可用，继续使用当前降级模型
      }
    }, 5 * 60 * 1000); // 每 5 分钟探测一次
  }

  stopProbing(): void {
    if (this.probeTimer) {
      clearInterval(this.probeTimer);
      this.probeTimer = null;
    }
  }

  private async probePrimaryModel(): Promise<void> {
    // 实现：用主模型发一个最简单的请求（如 "ping"）
    // 如果成功返回则表示主模型已恢复
    throw new Error('Not implemented');
  }
}
```

### 10.3 SSE 连接恢复

```
SSE 断线恢复策略:
├── 浏览器 EventSource 内置自动重连（默认 3 秒）
├── 服务端为每个 SSE 事件附带递增 ID
├── 重连时客户端发送 Last-Event-ID 头
├── 服务端从断点继续推送（如果 Session 仍活跃）
└── 如果 Session 已结束，返回完整结果的缓存
```

---

## 11. 可插拔架构

### 11.1 Agent 作为可插拔单元

由于 Agent 就是文件系统，插拔变得极其简单：

```bash
# 安装一个新 Agent（从市场/他人处获取）
cp -r ./downloaded-agent ./agents/my-new-agent

# 共享技能（软链接）
ln -s ../../shared-skills/web-search ./my-new-agent/skills/web-search

# 共享知识库
ln -s ../../shared-knowledges/typescript ./my-new-agent/knowledges/typescript

# Fork 一个 Agent 来实验
cp -r ./agents/main-agent ./agents/experiment-agent
```

### 11.2 Skills 的加载机制

```typescript
// skills/loader.ts

interface SkillDefinition {
  name: string;
  path: string;
  description: string;
  enabled: boolean;
  loadedAt: Date;
  metadata: {
    author?: string;
    version?: string;
    tags?: string[];
    dependencies?: string[];
  };
}

class SkillLoader {
  private skills = new Map<string, SkillDefinition>();

  /**
   * 扫描 skills/ 目录，加载所有技能的定义（不执行代码）
   * 只解析 SKILL.md 的 frontmatter，构建技能索引
   */
  async scanSkillsDirectory(agentRoot: string): Promise<SkillDefinition[]> {
    const skillDirs = await glob(`${agentRoot}/skills/*/SKILL.md`);
    const definitions: SkillDefinition[] = [];

    for (const skillMdPath of skillDirs) {
      const skillDir = dirname(skillMdPath);
      const name = basename(skillDir);
      const raw = readFileSync(skillMdPath, 'utf-8');
      const { data: meta } = matter(raw);

      definitions.push({
        name,
        path: skillDir,
        description: meta.description || '',
        enabled: meta.enabled !== false,
        loadedAt: new Date(),
        metadata: {
          author: meta.author,
          version: meta.version,
          tags: meta.tags || [],
          dependencies: meta.dependencies || [],
        },
      });
    }

    this.skills.clear();
    for (const def of definitions) {
      this.skills.set(def.name, def);
    }

    return definitions;
  }

  /**
   * 生成注入给 LLM 的 Skills 目录列表（仅名称+描述，不含全文）
   */
  generateSkillsDirectory(): string {
    const entries = Array.from(this.skills.values());
    if (entries.length === 0) return '';

    const lines = entries
      .filter(s => s.enabled)
      .map(s => `- **${s.name}**: ${s.description}${s.metadata.tags ? ` [${s.metadata.tags.join(', ')}]` : ''}`);

    return `## 可用技能目录\n${lines.join('\n')}\n\n> 使用技能前请先通过 file_read 读取对应技能的 SKILL.md 了解详细用法。`;
  }
}
```

### 11.3 Knowledges 的加载机制（类似 Skills）

Knowledge 与 Skill 的区别：

| | Skill（技能） | Knowledge（知识） |
|---|---|---|
| 本质 | **怎么做**（方法论） | **是什么**（事实/参考） |
| 触发方式 | Agent 主动调用执行 | Agent 被动检索查阅 |
| 示例 | 「如何分析依赖冲突」 | 「React 19 的 useOptimistic API 文档」 |
| 生命周期 | 可被 Agent 创建和修改 | 主要由人类或 Agent 积累写入 |
| Prompt 注入 | 仅注入目录列表 | 仅注入目录列表 |
| 详细内容 | Agent 通过 file_read 按需读取 | Agent 通过 file_read 按需读取 |

---

## 12. 实施路线（分阶段）

### Phase 0: PoC 验证（开始编码前必须完成）— 预计 0.5-1 天

> **⚠️ 最高优先级**：在正式编码前，必须先验证 CodeBuddy CLI 的核心能力。

```
目标: 验证 CodeBuddy CLI 程序化调用的可行性

✅ 必须验证:
├── [ ] CodeBuddy CLI 安装与 API Key 配置
├── [ ] 程序化调用（非交互模式）：codebuddy -p "prompt" 能否正常返回
├── [ ] 自定义工具注册：能否注册自定义的 file_read 等工具
├── [ ] 流式输出：能否获取流式的 token 输出
├── [ ] Hook 系统：PreToolUse / PostToolUse 是否可用
├── [ ] 模型选择：CodeBuddy 内部模型切换是否正常
└── [ ] Docker 环境：CodeBuddy CLI 能否在 Docker 容器中运行

验证方式: 写一个最小的 TypeScript 脚本
─────────────────────────────────────
  1. 安装 @tencent-ai/codebuddy-code
  2. 程序化调用，发送 "读取当前目录下的 README.md"
  3. 注册一个自定义 file_read 工具
  4. 观察 CodeBuddy 是否调用了自定义工具
  5. 验证流式输出是否可用

如果 PoC 失败的 Fallback 方案:
─────────────────────────────────
  方案 A: 直接调用 LLM API（Anthropic / OpenAI）+ 自建工具调用循环
  方案 B: 使用 Vercel AI SDK (@ai-sdk/anthropic) 作为 Agent 引擎
  方案 C: 使用 Claude Agent SDK (@anthropic-ai/claude-agent-sdk)
```

### Phase 1: 核心引擎（MVP）— 预计 3-5 天

```
目标: 能在 Web 中与 Agent 对话，Agent 能读写文件、执行命令、发请求
部署: Docker 化，使用 CodeBuddy CLI + API

✅ 必须完成:
├── [ ] 项目初始化 (pnpm + TS + ESLint + Prettier)
├── [ ] Docker + Docker Compose 配置
├── [ ] CodeBuddy CLI 集成（基于 PoC 验证结果）
├── [ ] 四大元工具实现（file_read/write, shell_exec, http_request）
├── [ ] Agent 文件系统初始化（agent/ 子目录下的所有核心文件）
├── [ ] Prompt 组装引擎（必须注入 + 按需加载策略）
├── [ ] Session 管理（创建、消息历史、token 统计）
├── [ ] Hono HTTP 服务 + /api/chat SSE 端点
├── [ ] React 前端（聊天界面 + 流式输出 + 工具调用状态展示）
├── [ ] 安全控制（受保护目录、危险命令拦截、超时保护）
├── [ ] 日志系统（winston）
├── [ ] .env 配置（多 API Key + 模型 + 降级顺序）
└── [ ] memory.md 概览 + memories/short-term JSONL 基础记忆写入

❌ Phase 1 不做:
├── ❌ IM 渠道接入（Telegram/Discord）
├── ❌ 长期记忆（编年体/纪传体）
├── ❌ 自进化功能
├── ❌ Skills 动态加载
├── ❌ 子 Agent / MCP
└── ❌ 上下文压缩（memory_compress）
```

### Phase 2: 记忆 + 自进化 — 预计 3-5 天

```
目标: Agent 能记住事情，并能自我改进

✅ 必须完成:
├── [ ] 完整记忆系统（短期 JSONL + 长期编年体/纪传体）
├── [ ] 记忆索引（memories/index.jsonl + 内存 Map）
├── [ ] memory_compress 工具（上下文压缩）
├── [ ] 上下文预警机制（黄色预警 + 自动压缩 + 熔断机制）
├── [ ] 自进化核心（修改 change_system.md + 创建 skill + 知识沉淀）
├── [ ] 进化事务系统（原子性多文件操作 + 回滚 + 并发锁）
├── [ ] .evolution-log/ 进化审计日志
├── [ ] Skills 扫描与加载（SKILL.md 解析）
├── [ ] Knowledges 扫描与加载
├── [ ] 错误重试 + 模型降级策略（含升级探测）
├── [ ] 进化回滚命令（「撤销上次进化」）
└── [ ] file_patch 工具（支持 search-and-replace 模式的文件编辑）
```

### Phase 3: 连接世界 — 预计 3-5 天

```
目标: Agent 能与外部系统和人交互

✅ 可选完成:
├── [ ] Telegram Bot 适配器
├── [ ] Discord Bot 适配器
├── [ ] MCP 服务器集成
├── [ ] 子 Agent 系统（研究 Agent + 写作 Agent 分工）
├── [ ] 定时调度（node-cron，24/7 主动交互原型）
└── [ ] Web 前端完善（记忆浏览器、进化历史查看器、技能管理面板）

🔮 未来:
├── [ ] MaaFramework 集成（GUI 视觉 + 操作）
├── [ ] VLM Fallback（当 OCR 不够用时调用 Qwen2.5-VL）
├── [ ] 多 Agent 协作网络
└── [ ] Skill 市场 / Agent 分发平台
```

### Phase 里程碑验收标准

| 里程碑 | 验收标准 |
|---|---|
| **P0 完成** | CodeBuddy CLI 程序化调用成功，自定义工具注册可用，流式输出正常 |
| **P1 完成** | 发送「读取 agent.md」→ Agent 调用 file_read → 返回内容，全程 SSE 流式输出可见；Docker 容器正常运行 |
| **P2 完成** | 说「记住我喜欢 TypeScript」→ 下次对话中 Agent 自觉提及这一偏好；说「把输出风格改为幽默」→ Agent 修改 change_system.md → 后续回复确实变幽默；上下文满时自动压缩 |
| **P3 完成** | 在 Telegram @你的Bot → 收到回复；Bot 能定时主动推送消息 |

---

## 13. 关键注意点与风险控制

### 13.1 技术风险

```
⚠️ 风险 1: CodeBuddy CLI 程序化调用能力不确定
解决: Phase 0 PoC 验证，准备三个 Fallback 方案
     核心的 Agent 循环封装一层适配接口（SDK Adapter）
     如果将来换框架，只需替换这层适配
     四大元工具和文件系统层不受影响

⚠️ 风险 2: 自进化可能导致 Agent 行为漂移
解决: 
     - soul.md 的硬约束（代码层面阻止修改）
     - .evolution-log/ 完整审计轨迹
     - 进化事务原子性 + 24小时回滚窗口
     - 定期人工 review 进化日志
     
⚠️ 风险 3: 文件系统作为「数据库」的性能瓶颈
解决: 
     - memory.md 控制在 200 行以内（仅概览）
     - 短期记忆用 JSONL + 内存 Map 索引（O(1) 检索）
     - 频繁读取的文件缓存在内存中（启动时加载）
     - chokidar 监听文件变更，热更新缓存

⚠️ 风险 4: Shell 注入
解决: 
     - 使用 execFile 而非 exec（不经过 shell 解释器）
     - 命令 tokenize 解析（防止管道/重定向注入）
     - 黑名单过滤危险命令
     - 受保护目录不可写入
     - 命令日志完整记录

⚠️ 风险 5: Token 成本失控
解决: 
     - maxTurns 上限（默认 15 次）
     - Prompt 按需加载（不全量注入 skills/knowledges）
     - 上下文压缩机制（三级预警 + 自动 compact）
     - 工具返回截断（100KB / 5MB）
     - 多模型降级（贵模型 → 便宜模型）
     - 监控每次对话的 token 消耗

ℹ️ 风险 6: LLM 调用失败（网络/限流/服务不可用）
解决:
     - RetryPolicy 指数退避重试（最多 3 次）
     - 多 API Key 负载均衡
     - 模型降级链（主模型 → 备用模型）+ 定时探测升级回主模型
     - SSE 断线自动重连

ℹ️ 风险 7: Docker 容器停止时数据丢失
解决:
     - Graceful Shutdown 机制：监听 SIGTERM 信号
     - 保存当前 Session 快照到 active.json
     - 完成正在进行的文件写入
     - 关闭 SSE 连接
     - 实现示例:
       process.on('SIGTERM', async () => {
         await sessionManager.saveSnapshot();
         await memoryManager.flush();
         server.close();
       });```

### 13.2 设计决策备忘（DD）

| 决策 | 选择 | 理由 | 备选方案 |
|---|---|---|---|
| Agent 存储 | 纯文件系统 | 可读、可版本控制、可拷贝 | SQLite / MongoDB |
| 文件格式 | Markdown + Frontmatter | LLM 友好、人类可编辑、结构化元数据 | JSON / YAML / TOML |
| 短期记忆 | JSONL + 内存 Map | 追加快、流式读取、O(1) 检索 | SQLite / Redis |
| 长期记忆 | 编年体 + 纪传体 Markdown | 人类可读、Git 友好、双维度覆盖 | 纯按日期 / 纯按主题 |
| Prompt 策略 | 按需加载 | 节省 token、Agent 自主决策 | 全量注入（浪费 token） |
| 进化粒度 | 事务级别（多文件原子操作） | 实际场景需要多文件联动 | 单文件级别（过于严格） |
| 进化审批 | 自动 + 可回滚（P1/P2） | 先跑通再加强制审批 | 全员审批（拖慢速度） |
| 流式协议 | SSE | 单向够用，简单可靠 | WebSocket（过度工程） |
| 安全模型 | 受保护目录 + 命令黑名单 + execFile | 多层纵深防御，不依赖扩展名白名单 | 单一黑名单不够 |
| 前端 | React + Zustand + Tailwind | 你的技术栈、轻量、快速出 UI | 纯 HTML（不可维护） |
| 部署 | Docker | 环境一致性、便于迁移 | 本地直接运行 |
| 用户模式 | 单用户自用 | 简化架构、类似 OpenClaw 自部署 | 多用户 SaaS |
| Token 估算 | @anthropic-ai/tokenizer | 精确计算，上下文管理可靠 | 字符数估算（不精确） |

---

## 14. 附录：目录树完整示例

```
aiive/
├── .env                              # API Keys + 模型配置（用户自行填写）
├── .env.example                      # .env 模板
├── package.json
├── tsconfig.json
├── pnpm-lock.yaml
├── Dockerfile
├── docker-compose.yml
│
├── src/                              # 运行时代码
│   ├── index.ts                      # 入口：启动 Hono 服务
│   ├── core/
│   │   ├── agent.ts                  # CodeBuddy CLI 适配层（SDK Adapter）
│   │   ├── prompt.ts                 # Prompt 组装引擎（按需加载策略）
│   │   ├── session.ts                # Session 管理 + 上下文压缩
│   │   ├── retry.ts                  # RetryPolicy + 指数退避
│   │   └── model-fallback.ts         # 模型降级链
│   │
│   ├── tools/
│   │   ├── register.ts               # 四大元工具 + memory_compress 注册
│   │   ├── file-read.ts
│   │   ├── file-write.ts
│   │   ├── shell-exec.ts
│   │   ├── http-request.ts
│   │   ├── memory-compress.ts        # 上下文压缩工具
│   │   ├── file-patch.ts             # 文件局部编辑工具（Phase 2）
│   │   └── safety.ts                 # 统一安全检查器（受保护目录）
│   │
│   ├── memory/
│   │   ├── short-term.ts             # JSONL 短期记忆管理 + 内存 Map 索引
│   │   ├── long-term.ts              # 长期记忆管理（编年体 + 纪传体）
│   │   ├── overview.ts               # memory.md 概览生成
│   │   └── migrate.ts                # 短期 → 长期记忆迁移
│   │
│   ├── evolution/
│   │   ├── tracker.ts                # 进化追踪器
│   │   ├── transaction.ts            # 进化事务管理（原子性 + 并发锁）
│   │   ├── rollback.ts               # 事务回滚执行器
│   │   └── logger.ts                 # 进化日志写入
│   │
│   ├── skills/
│   │   └── loader.ts                 # Skills 扫描加载（仅目录列表）
│   │
│   ├── knowledges/
│   │   └── loader.ts                 # Knowledges 扫描加载
│   │
│   ├── api/
│   │   ├── chat.ts                   # /api/chat SSE 端点
│   │   ├── files.ts                  # /api/files CRUD
│   │   ├── evolution.ts              # /api/evolution 进化历史
│   │   └── status.ts                 # /api/health 健康检查
│   │
│   └── utils/
│       ├── logger.ts                 # winston 日志
│       ├── path.ts                   # 路径解析（防穿越 + symlink 检测）
│       ├── tokens.ts                 # Token 精确估算（@anthropic-ai/tokenizer）
│       └── config.ts                 # 配置加载（.env 解析）
│
├── agent/                            # ★ Agent 文件系统（这就是 Agent 本身）
│   ├── agent.md
│   ├── soul.md
│   ├── memory.md
│   ├── system.md
│   ├── change_system.md
│   │
│   ├── tools/
│   │   ├── file_read.md
│   │   ├── file_write.md
│   │   ├── shell_exec.md
│   │   └── http_request.md
│   │
│   ├── skills/
│   │   └── _template/
│   │       └── SKILL.md
│   │
│   ├── knowledges/
│   │   └── .gitkeep
│   │
│   ├── memories/
│   │   ├── short-term/
│   │   │   ├── session.jsonl
│   │   │   └── active.json
│   │   ├── long-term/
│   │   │   ├── by-date/
│   │   │   │   └── .gitkeep
│   │   │   └── by-event/
│   │   │       └── .gitkeep
│   │   └── index.jsonl
│   │
│   └── .evolution-log/
│       ├── backups/
│       │   └── .gitkeep
│       └── .gitkeep
│
└── web/                              # 前端（React + Zustand + Tailwind）
    ├── package.json
    ├── vite.config.ts
    ├── tailwind.config.ts
    ├── index.html
    └── src/
        ├── main.tsx
        ├── App.tsx
        ├── stores/
        │   └── chatStore.ts          # Zustand 状态管理
        ├── components/
        │   ├── ChatPanel.tsx          # 聊天面板
        │   ├── MessageBubble.tsx      # 消息气泡
        │   ├── ToolCallCard.tsx       # 工具调用展示卡片
        │   ├── ContextMeter.tsx       # 上下文使用量指示器
        │   └── Sidebar.tsx            # 侧边栏（记忆/技能/进化历史）
        └── utils/
            └── sse.ts                 # SSE 客户端封装
```