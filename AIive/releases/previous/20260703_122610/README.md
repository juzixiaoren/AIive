# AIive - 自进化个人Agent自举内核

一个带LLM大脑、能读写自己项目、能生成并应用patch、能测试回滚、能通过交互继续开发自己的最小自举Agent。

## 项目简介

AIive是一个**Self-Evolving Personal Agent Kernel**，它不是普通聊天机器人，也不是固定功能助手，而是一个可以通过"交互即开发"持续成长的Agent。

### 核心特性

- **LLM驱动决策**：使用大语言模型理解用户意图、规划修改、生成patch
- **流式输出**：支持实时流式输出，提升交互体验
- **会话记忆**：维护当前任务的工作状态（thread_state.json + event_log.jsonl），支持任务连续性
- **自我修改能力**：能修改自己的代码、文档、记忆和能力注册表
- **候选版本机制**：在candidate工作区修改，测试通过后promote，失败则rollback
- **记忆系统**：记录用户偏好、事件、反馈，支持hit机制统计记忆重要性
- **能力系统**：支持skill生成、MCP接入、工作流管理
- **自我修复**：测试失败时自动尝试修复，最多重试2次

### 架构概览

```
AIive/
├── core/                    # 核心模块
│   ├── agent_loop.py       # Agent主循环
│   ├── llm_client.py       # LLM客户端
│   ├── decision_engine.py  # 决策引擎
│   ├── context_builder.py  # 上下文构建器
│   ├── conversation_session_manager.py # 会话记忆管理器
│   ├── project_reader.py   # 项目读取器
│   ├── update_executor.py  # 更新执行器
│   ├── memory_router.py    # 记忆路由器
│   ├── capability_router.py # 能力路由器
│   ├── memory_hit_manager.py # 记忆hit管理器
│   └── self_repair_loop.py # 自我修复循环
├── kernel/                  # 内核模块
│   ├── version_manager.py  # 版本管理器
│   ├── health_check.py     # 健康检查
│   └── test_runner.py      # 测试运行器
├── mind/                    # Agent心智
│   ├── persona.md          # 人格设定
│   ├── user_model.md       # 用户模型
│   ├── listen_policy.md    # 交互规则
│   ├── self_model.md       # 自我模型
│   └── goals.md            # 目标设定
├── memory/                  # 记忆系统（长期记忆）
│   ├── preferences/        # 用户偏好
│   ├── events/             # 事件记忆
│   ├── feedback/           # 反馈记忆
│   └── index/              # 记忆索引（hit统计）
├── runtime/                 # 运行时数据（短期记忆）
│   ├── threads/            # 线程状态（thread_state.json）
│   │   └── archived/       # 已归档线程
│   └── events/             # 事件日志（event_log.jsonl）
├── capabilities/            # 能力系统
│   ├── registry.md         # 能力注册表
│   ├── local_tools/        # 本地工具
│   ├── mcp/                # MCP能力
│   └── workflows/          # 工作流
├── self_development/        # 自我开发
│   ├── issues.md           # 开发任务
│   ├── ideas.md            # 想法记录
│   └── changelog.md        # 更新日志
├── tests/                   # 测试文件
├── docs/                    # 项目文档
├── main.py                  # CLI入口
└── .env                     # 环境变量配置
```

## 快速开始

### 1. 环境要求

- Python 3.10+
- 依赖包：requests, python-dotenv

### 2. 安装依赖

```bash
cd AIive
pip install -r requirements.txt
```

### 3. 配置LLM API

复制 `.env.example` 为 `.env`，填入你的API配置：

```bash
cp .env.example .env
```

编辑 `.env` 文件：

```env
# LLM API配置
LLM_BASE_URL=https://api.xiaomimimo.com/v1/chat/completions
LLM_API_KEY=your_api_key_here
LLM_MODEL=mimo-v2.5-pro
```

### 4. 运行健康检查

```bash
python main.py --health-check
```

### 5. 启动交互模式

```bash
python main.py
```

## 使用指南

### 基本交互

启动后，你可以直接与Agent对话：

```
User: 你好
Agent: 你好！我是AIive，你的个人Agent。有什么可以帮你的？

User: 我不爱喝瑞幸，我爱喝星巴克。
Agent: 已记录到咖啡偏好中。

User: 以后我叫你大李的时候你才应，和别人聊天时别插嘴。
Agent: 已更新交互规则：只有叫"大李"时回应，多人对话不插嘴。
```

### 流式输出

Agent支持实时流式输出，LLM的回复会逐字显示，提升交互体验：

```
User: 你好
Agent: 你好！我是AIive，你的个人Agent。（逐字显示）
```

### 自我修改请求

Agent可以修改自己的代码和功能：

```
User: 你给自己的记忆加一个 hit 机制，读得越多说明越重要。
Agent: [创建candidate → 修改代码 → 运行测试 → promote/rollback]

User: 你给自己加一个星巴克菜单查询工具，先做 skeleton。
Agent: [创建能力目录 → 生成README和骨架 → 更新注册表]
```

### 命令行选项

```bash
# 运行健康检查
python main.py --health-check

# 显示状态
python main.py --self-status

# 运行测试
python main.py --run-tests
```

## 测试

### 运行所有测试

```bash
python -m pytest tests/ -v
```

### 运行特定测试

```bash
# Skill生成测试
python -m pytest tests/test_skill_generation/ -v

# Memory写入测试
python -m pytest tests/test_memory_writing/ -v

# Memory Hit测试
python -m pytest tests/test_memory_hit/ -v

# MCP生成测试
python -m pytest tests/test_mcp_generation/ -v
```

## 配置说明

### 环境变量

| 变量名 | 说明 | 示例 |
|--------|------|------|
| `LLM_BASE_URL` | LLM API地址 | `https://api.xiaomimimo.com/v1/chat/completions` |
| `LLM_API_KEY` | API密钥 | `sk-xxxxxxxx` |
| `LLM_MODEL` | 模型名称 | `mimo-v2.5-pro` |

### 目录结构说明

- `mind/`：Agent的心智文件，包括人格、用户模型、交互规则等
- `memory/`：长期记忆系统，存储用户偏好、事件、反馈
- `runtime/`：短期记忆（会话记忆），存储当前任务的工作状态
  - `threads/`：线程状态文件（thread_state.json）
  - `events/`：事件日志（event_log.jsonl），按日期存储
- `capabilities/`：能力系统，管理工具、MCP、工作流
- `self_development/`：自我开发任务和想法记录
- `releases/`：版本管理（candidate/previous/current）

## 开发指南

### 会话记忆系统

AIive使用会话记忆系统来维护当前任务的工作状态，实现任务连续性。

#### 核心概念

会话记忆不是聊天历史，而是当前任务的**工作内存**，包含：
- 任务目标和状态
- 已读取和修改的文件
- 工具执行结果
- 测试结果
- 待处理动作
- 工作摘要

#### 使用方式

```python
from core.conversation_session_manager import ConversationSessionManager

# 创建会话管理器
session_manager = ConversationSessionManager(project_root)

# 创建新线程
thread = session_manager.create_thread("用户请求", "task_type")

# 追加消息
session_manager.append_message(thread_id, "user", "用户输入")
session_manager.append_message(thread_id, "assistant", "助手回复")

# 记录工具结果
session_manager.add_tool_result(thread_id, "read_file", "path", "结果摘要")

# 获取上下文
context = session_manager.get_context_for_llm(thread_id)
formatted = session_manager.format_thread_context_for_llm(thread_id)
```

#### 线程生命周期

```
created → running → waiting_user → testing → completed / failed / archived
```

任务完成后，会话记忆会归档到 `runtime/threads/archived/` 目录。

### 添加新能力

1. **本地工具**：
   ```python
   from core.capability_router import CapabilityRouter
   
   router = CapabilityRouter(file_store, project_root)
   result = router.generate_skill("用户输入", "tool_name", "local_tool")
   ```

2. **MCP能力**：
   ```python
   result = router.add_mcp("用户输入", "mcp_name")
   ```

### 记录记忆hit

```python
from core.memory_hit_manager import MemoryHitManager

hit_manager = MemoryHitManager(project_root)
hit_manager.record_hit("memory/preferences/coffee.md")
```

### 运行测试

```bash
# 运行完整测试套件
python -m pytest tests/ -v

# 运行特定测试文件
python -m pytest tests/test_skill_generation/test_skill_generation.py -v
```

## 文档

- [产品愿景与V1架构](docs/01_product_vision_and_v1_architecture.md)
- [宪法文档](docs/02_constitution.md)
- [自举内核提示词](docs/03_bootstrap_kernel_prompt_for_claude_code.md)
- [交互记忆能力文档](docs/04_interaction_memory_capability_self_improvement_docs.md)
- [自修改能力报告](docs/05_self_modification_capability_report.md)

## 已知限制

1. **MCP支持**：当前只有skeleton实现，需要后续完善
2. **语音系统**：未实现
3. **手机端**：未实现
4. **向量记忆**：未实现，使用简单文件存储
5. **完全自主重构**：需要人工监督

## 路线图

### 近期计划

- [ ] 完善MCP实际功能
- [ ] 实现记忆sleep/wake机制
- [ ] 实现能力生命周期管理
- [ ] 优化名称提取算法

### 中期计划

- [ ] 本地工具自动生成
- [ ] 手机端轻壳
- [ ] 端侧语音小脑

### 长期计划

- [ ] 用户声纹识别
- [ ] 环境语音缓存
- [ ] 主动场景感知
- [ ] 动态UI生成

## 贡献指南

1. Fork项目
2. 创建功能分支：`git checkout -b feature/your-feature`
3. 提交更改：`git commit -m 'Add your feature'`
4. 推送分支：`git push origin feature/your-feature`
5. 创建Pull Request

## 许可证

本项目采用 MIT 许可证。

## 联系方式

- 项目地址：[GitHub仓库地址]
- 问题反馈：[Issues页面]

---

**AIive** - 让你的Agent越用越聪明