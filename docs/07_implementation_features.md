# AIive 实现功能文档

## 1. 核心功能概述

AIive 第一版实现了自进化个人 Agent 的最小自举内核，包含以下核心功能：

- 用户交互处理
- LLM 决策引擎
- 文件操作执行
- 候选版本管理
- 健康检查
- 会话管理
- 流式输出

## 2. 用户交互处理

### 2.1 交互模式

支持两种交互模式：
- **CLI 交互模式**：命令行界面，支持流式输出
- **API 调用模式**：通过 `AgentLoop.process_input()` 接口调用

### 2.2 输入处理流程

1. 接收用户输入
2. 创建或加载会话线程
3. 构建决策上下文
4. 调用 LLM 决策
5. 执行操作
6. 返回响应

### 2.3 支持的交互类型

- 普通聊天
- 偏好更新
- 人格调整
- 交互规则更新
- 记忆更新
- 新能力请求
- 运行机制修改
- 自我改进

## 3. LLM 决策引擎

### 3.1 决策上下文构建

上下文包括：
- 项目文档
- 人格文件
- 用户模型
- 能力注册表
- 自我模型
- 会话历史

### 3.2 决策输出

LLM 返回 JSON 格式决策结果：
```json
{
  "request_type": "preference_update",
  "operations": [
    {
      "type": "create_file",
      "path": "memory/preferences/coffee.md",
      "content": "# 咖啡偏好\n\n- 不喜欢：瑞幸\n- 喜欢：星巴克"
    }
  ],
  "user_response_draft": "已记住你的咖啡偏好！",
  "summary": "更新咖啡偏好",
  "needs_code_change": false,
  "read_more_files": []
}
```

### 3.3 决策类型处理

| 决策类型 | 处理方式 |
|---------|---------|
| `chat` | 直接返回响应 |
| `preference_update` | 写入 `memory/preferences/` |
| `persona_update` | 更新 `mind/persona.md` |
| `listen_policy_update` | 更新 `mind/listen_policy.md` |
| `memory_update` | 写入 `memory/` |
| `new_capability_request` | 创建 issue，更新注册表 |
| `runtime_change_request` | 创建候选版本，执行修改 |
| `self_improvement` | 创建 issue，记录改进计划 |

## 4. 文件操作执行

### 4.1 支持的操作

| 操作类型 | 说明 | 安全性 |
|---------|------|--------|
| `create_file` | 创建新文件 | 安全 |
| `write_file` | 写入文件（覆盖） | 安全 |
| `append_file` | 追加内容 | 安全 |
| `patch_file` | 应用补丁 | 安全 |
| `create_directory` | 创建目录 | 安全 |
| `create_issue` | 创建问题 | 安全 |
| `update_registry` | 更新注册表 | 安全 |
| `update_self_model` | 更新自我模型 | 安全 |
| `update_changelog` | 更新变更日志 | 安全 |
| `delete_file` | 删除文件 | **禁止** |

### 4.2 补丁格式

补丁使用 `old_content -> new_content` 格式：
```
原始内容 -> 替换内容
```

仅替换第一个匹配处，避免误替换。

### 4.3 备份机制

- 文件操作前自动备份
- 备份保存在 `releases/backups/`
- 支持从备份恢复

## 5. 候选版本管理

### 5.1 版本目录结构

```
releases/
├── candidate/      # 候选版本工作区
├── previous/       # 历史版本备份
│   ├── 20260703_120000/
│   └── 20260702_150000/
└── logs/           # 版本管理日志
    └── version_manager.log
```

### 5.2 候选版本工作流

#### 创建候选版本
1. 清空 `releases/candidate/`
2. 从项目根目录复制核心目录和文件
3. 记录创建日志

#### 应用操作
1. 在候选版本中执行文件操作
2. 记录操作结果

#### 运行测试
1. 在候选版本目录中运行测试命令
2. 支持自定义测试命令和超时时间
3. 实时输出测试结果

#### 提升候选版本
1. 备份当前版本到 `releases/previous/`
2. 将候选版本复制到项目根目录
3. 清理候选版本
4. 如果中途失败，从备份恢复

#### 回滚候选版本
1. 删除候选版本目录
2. 记录回滚日志

### 5.3 历史版本恢复

- 列出所有历史版本
- 选择指定版本恢复
- 恢复前自动备份当前版本

## 6. 健康检查

### 6.1 检查项目

| 检查项 | 说明 |
|-------|------|
| 必要目录 | `docs/`, `core/`, `mind/`, `memory/`, `capabilities/`, `self_development/`, `kernel/`, `tests/` |
| 必要文件 | `main.py`, `README.md`, `mind/self_model.md`, `capabilities/registry.md` |
| 记忆目录 | `memory/preferences/`, `memory/feedback/`, `memory/events/`, `memory/summaries/`, `memory/archive/`, `memory/index/` |
| 自我开发文件 | `self_development/issues.md`, `self_development/changelog.md` |
| 日志目录 | `releases/logs/` |

### 6.2 检查结果

返回结构：
```python
{
    "pass": True,  # 是否通过
    "reasons": [],  # 失败原因列表
    "timestamp": "2026-07-03 12:00:00"
}
```

## 7. 会话管理

### 7.1 会话线程

- 每个用户输入创建或加载会话线程
- 线程包含：ID、目标、状态、消息历史
- 支持多会话并行

### 7.2 会话上下文

- 记录读取的文件
- 记录修改的文件
- 记录工具调用结果
- 记录错误信息

### 7.3 会话持久化

- 会话数据保存在 `memory/sessions/`
- 支持加载历史会话
- 自动清理过期会话

## 8. 流式输出

### 8.1 流式聊天

- 使用 LLM 流式 API
- 实时输出响应内容
- 支持中断和继续

### 8.2 流式操作

- 操作执行过程实时输出
- 测试运行实时输出
- 错误信息实时输出

## 9. 自我模型更新

### 9.1 更新时机

- 完成重要操作后
- 新增能力后
- 修改人格后
- 更新交互规则后
- 创建或完成 issue 后
- 版本提升或回滚后

### 9.2 更新内容

- 当前能力列表
- 当前限制
- 最近新增能力
- 最近用户反馈
- 活跃能力
- 休眠能力
- 待开发任务
- 最近失败
- 未来计划

## 10. 变更日志

### 10.1 记录格式

```markdown
## 2026-07-03 12:00:00

类型: preference_update
描述: 更新咖啡偏好
操作数: 1
```

### 10.2 记录内容

- 变更时间
- 变更类型
- 变更描述
- 操作数量
- 相关文件

## 11. 错误处理

### 11.1 错误类型

- LLM 调用失败
- 文件操作失败
- 测试运行失败
- 版本管理失败
- 健康检查失败

### 11.2 错误恢复

- LLM 失败：降级处理
- 文件操作失败：回滚操作
- 测试失败：保留候选版本
- 版本管理失败：从备份恢复
- 健康检查失败：记录问题

## 12. 配置管理

### 12.1 环境变量

| 变量名 | 说明 | 必需 |
|-------|------|------|
| `LLM_API_KEY` | LLM API 密钥 | 是 |
| `LLM_BASE_URL` | LLM API 基础 URL | 否 |
| `LLM_MODEL` | LLM 模型名称 | 否 |

### 12.2 配置文件

- `.env`：环境变量配置
- `config.json`：应用配置（可选）

## 13. 测试覆盖

### 13.1 测试文件

| 测试文件 | 测试内容 |
|---------|---------|
| `test_capability_registry.py` | 能力注册表功能 |
| `test_cli_interaction.py` | CLI 交互功能 |
| `test_health_check.py` | 健康检查功能 |
| `test_integration.py` | 集成测试 |
| `test_skill_generation.py` | 技能生成功能 |
| `test_streaming_output.py` | 流式输出功能 |

### 13.2 测试结果

- 总测试数：83
- 通过：78
- 失败：5（LLM 响应变化导致）
- 子测试失败：4（LLM 响应变化导致）

## 14. 性能特征

### 14.1 响应时间

- 普通聊天：2-5 秒
- 偏好更新：3-8 秒
- 代码修改：10-30 秒
- 测试运行：30-120 秒

### 14.2 资源占用

- 内存：50-200 MB
- 磁盘：100 MB - 1 GB（取决于历史版本数量）
- CPU：主要消耗在 LLM 调用和文件操作

## 15. 安全特性

### 15.1 操作限制

- 禁止删除操作
- 文件操作前备份
- 候选版本隔离
- 超时保护

### 15.2 数据保护

- 历史版本保留
- 变更日志记录
- 错误日志记录
- 会话历史保存

## 16. 扩展能力

### 16.1 已实现扩展点

- 新增操作类型
- 新增决策类型
- 新增健康检查项
- 新增测试用例

### 16.2 计划扩展点

- 记忆 hit/sleep/wake 机制
- 能力生命周期管理
- 自动能力生成
- MCP 自动接入
- 多用户支持
- Web UI
