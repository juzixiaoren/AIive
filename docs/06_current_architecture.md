# AIive 当前架构文档

## 1. 项目概述

AIive 是一个自进化个人 Agent 自举内核，目标是构建一个能够通过日常交互自我成长的个人管家型系统。当前实现为第一版最小自举内核，包含核心引擎、记忆管理、版本管理和健康检查等基础模块。

## 2. 目录结构

```
AIive/
├── main.py                    # 程序入口
├── README.md                  # 项目说明
├── docs/                      # 项目文档
│   ├── 01_product_vision_and_v1_architecture.md
│   ├── 02_constitution.md
│   ├── 03_bootstrap_kernel_prompt_for_claude_code.md
│   ├── 04_interaction_memory_capability_self_improvement_docs.md
│   └── 05_self_modification_capability_report.md
├── core/                      # 核心引擎模块
│   ├── agent_loop.py          # Agent 主循环
│   ├── context_builder.py     # 上下文构造器
│   ├── decision_engine.py     # LLM 决策引擎
│   ├── llm_client.py          # LLM 客户端
│   ├── file_store.py          # 文件存储操作
│   ├── project_reader.py      # 项目文件读取器
│   ├── update_executor.py     # 更新执行器
│   ├── self_repair_loop.py    # 自修复循环
│   ├── conversation_session_manager.py  # 会话管理
│   └── utils.py               # 公共工具（超时装饰器等）
├── mind/                      # Agent 人格与认知
│   ├── persona.md             # 人格定义
│   ├── user_model.md          # 用户模型
│   ├── listen_policy.md       # 交互规则
│   ├── goals.md               # 目标
│   ├── self_model.md          # 自我模型
│   └── daily_context.md       # 日常上下文
├── memory/                    # 记忆存储
│   ├── preferences/           # 用户偏好
│   ├── feedback/              # 用户反馈
│   ├── events/                # 事件记录
│   ├── summaries/             # 总结
│   ├── archive/               # 归档
│   └── index/                 # 索引
├── capabilities/              # 能力管理
│   ├── registry.md            # 能力注册表
│   ├── local_tools/           # 本地工具
│   ├── workflows/             # 工作流
│   ├── mcp/                   # MCP 接口
│   ├── ui_components/         # UI 组件
│   ├── dormant/               # 休眠能力
│   └── deprecated/            # 废弃能力
├── self_development/          # 自我开发
│   ├── issues.md              # 问题列表
│   ├── ideas.md               # 想法列表
│   ├── changelog.md           # 变更日志
│   ├── experiments/           # 实验
│   └── patches/               # 补丁
├── kernel/                    # 内核模块
│   ├── version_manager.py     # 版本管理器
│   ├── health_check.py        # 健康检查
│   └── test_runner.py         # 测试运行器
├── releases/                  # 版本发布目录
│   ├── candidate/             # 候选版本
│   ├── previous/              # 历史版本
│   └── logs/                  # 版本日志
└── tests/                     # 测试文件
```

## 3. 核心模块架构

### 3.1 Agent 主循环 (`core/agent_loop.py`)

Agent 主循环是系统的核心入口，负责：
- 接收用户输入
- 调用 LLM 决策引擎
- 执行文件操作
- 管理会话状态
- 处理流式输出

主要流程：
1. 用户输入 → 会话管理 → LLM 决策
2. 决策结果 → 操作执行 → 结果反馈
3. 更新自我模型和变更日志

### 3.2 LLM 决策引擎 (`core/decision_engine.py`)

决策引擎负责：
- 构建决策上下文
- 调用 LLM API
- 解析 LLM 响应
- 生成操作指令

支持的决策类型：
- `chat`：普通聊天
- `preference_update`：偏好更新
- `persona_update`：人格更新
- `listen_policy_update`：交互规则更新
- `memory_update`：记忆更新
- `new_capability_request`：新能力请求
- `runtime_change_request`：运行机制修改
- `self_improvement`：自我改进

### 3.3 更新执行器 (`core/update_executor.py`)

更新执行器负责：
- 执行文件操作（创建、写入、追加、补丁）
- 安全检查（禁止删除操作）
- 备份和恢复文件
- 记录操作结果

支持的操作类型：
- `create_file`：创建文件
- `write_file`：写入文件（覆盖）
- `append_file`：追加文件
- `patch_file`：应用补丁
- `create_directory`：创建目录
- `create_issue`：创建问题
- `update_registry`：更新注册表
- `update_self_model`：更新自我模型
- `update_changelog`：更新变更日志

### 3.4 版本管理器 (`kernel/version_manager.py`)

版本管理器负责：
- 创建候选版本
- 应用操作到候选版本
- 运行测试
- 提升候选版本（带回退保护）
- 回滚候选版本
- 恢复历史版本

目录结构：
- `releases/candidate/`：候选版本工作区
- `releases/previous/`：历史版本备份
- `releases/logs/`：版本管理日志

### 3.5 健康检查 (`kernel/health_check.py`)

健康检查负责：
- 检查必要目录是否存在
- 检查必要文件是否存在
- 检查配置文件格式
- 验证系统可启动性

### 3.6 项目读取器 (`core/project_reader.py`)

项目读取器负责：
- 列出项目文件
- 读取文件内容
- 批量读取文件
- 生成项目树摘要
- 搜索文本内容

## 4. 数据流架构

### 4.1 用户输入处理流程

```
用户输入
    ↓
AgentLoop.process_input()
    ↓
ConversationSessionManager.load_or_create_thread()
    ↓
DecisionEngine.make_decision()
    ↓
[如果需要更多文件] → ProjectReader.read_files()
    ↓
DecisionEngine.make_decision_with_more_files()
    ↓
[如果需要代码修改] → SelfRepairLoop.run_repair_cycle()
    ↓
[否则] → UpdateExecutor.execute_operations()
    ↓
更新 self_model.md 和 changelog.md
    ↓
返回用户响应
```

### 4.2 候选版本工作流

```
创建候选版本
    ↓
VersionManager.create_candidate()
    ↓
在候选版本中应用操作
    ↓
VersionManager.apply_operations_to_candidate()
    ↓
运行测试
    ↓
VersionManager.run_candidate_tests()
    ↓
[测试通过] → VersionManager.promote_candidate()
    ↓
[测试失败] → VersionManager.rollback_candidate()
```

## 5. 关键设计决策

### 5.1 候选版本机制

- 所有代码修改必须在候选版本中进行
- 候选版本通过测试后才能提升
- 提升失败时自动回滚到备份
- 历史版本保存在 `releases/previous/`

### 5.2 超时保护

- 所有关键操作都有超时保护
- 超时装饰器统一在 `core/utils.py` 中定义
- 避免嵌套超时导致的问题

### 5.3 安全边界

- 禁止删除操作
- 文件操作前自动备份
- 候选版本与生产版本隔离
- 健康检查验证系统完整性

### 5.4 会话管理

- 支持多会话线程
- 记录会话历史
- 支持流式输出
- 自动保存会话状态

## 6. 配置与依赖

### 6.1 环境变量

- `LLM_API_KEY`：LLM API 密钥
- `LLM_BASE_URL`：LLM API 基础 URL
- `LLM_MODEL`：LLM 模型名称

### 6.2 依赖包

- Python 3.10+
- requests：HTTP 客户端
- pytest：测试框架

## 7. 测试体系

### 7.1 测试类型

- 单元测试：测试单个模块功能
- 集成测试：测试模块间协作
- 健康检查测试：验证系统完整性

### 7.2 测试覆盖

- 能力注册表测试
- CLI 交互测试
- 健康检查测试
- 集成测试
- 技能生成测试
- 流式输出测试

## 8. 日志与监控

### 8.1 日志记录

- 版本管理日志：`releases/logs/version_manager.log`
- 操作执行日志：控制台输出
- 错误日志：控制台输出

### 8.2 健康检查

- 启动时自动检查
- 支持手动检查
- 检查结果记录到日志

## 9. 扩展点

### 9.1 新增操作类型

在 `UpdateExecutor._execute_single_operation()` 中添加新的操作类型处理。

### 9.2 新增决策类型

在 `DecisionEngine` 中添加新的决策类型处理逻辑。

### 9.3 新增健康检查项

在 `HealthCheck` 中添加新的检查规则。

### 9.4 新增测试

在 `tests/` 目录下添加新的测试文件。

## 10. 已知限制

1. LLM 依赖：需要配置有效的 LLM API 密钥
2. 单用户设计：当前仅支持单用户使用
3. 本地存储：所有数据存储在本地文件系统
4. 无并发控制：未实现多进程/线程安全保护
5. 简单测试：测试覆盖有限，主要依赖集成测试
